"""Counterfactual sizing replay. Signals, timestamps, prices, exits and funding stay frozen.

Two analyses that must not be conflated:

* ``independent`` sizes every trade from the same fresh account: payoff transformation only.
* ``sequential`` replays trades chronologically through one account: compounding, capacity,
  concurrency, skipped trades, drawdown throttles and margin.

Sequential replay can only *skip* or *shrink* source trades. A trade the source run never
executed has no frozen outcome, so a policy that would have had spare capacity cannot add one.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .engine import SizingDecision, size
from .model import AccountSnapshot, FamilyEvidence, OpenExposure, TradeRecord
from .policy import SizingPolicy


@dataclass(frozen=True)
class Outcome:
    trade_id: str
    accepted: bool
    reason: str | None
    exit_reason: str | None
    net_pnl: float
    r: float | None
    equity_before: float
    notional: float
    margin: float
    leverage: float
    intended_risk: float
    risk_fraction: float  # intended risk / equity at decision
    entry_fees: float
    fees: float
    funding: float
    slippage_bps: float
    liquidated: bool
    near_miss: bool
    liquidation_distance: float | None
    invalidation_distance: float | None
    target_distance: float | None
    nominal_reward_risk: float | None
    mfe: float
    mae: float
    bound_by: tuple[str, ...]
    decision_at: str
    exit_at: str
    hypothesis_id: str
    asset: str
    cohort: str


def evaluate(
    trade: TradeRecord, dec: SizingDecision, policy: SizingPolicy, equity: float, realized=None
):
    """Frozen-path PnL at the counterfactual size (engine accounting, isolated loss cap).

    ``realized`` (stress.Realized) worsens costs after sizing; the sizer never sees it.
    """
    s = trade.sign
    tgt = (
        s * (trade.target_price - dec.entry_fill) / dec.entry_fill
        if dec.accepted and trade.target_price
        else None
    )
    if not dec.accepted:
        return Outcome(
            trade.trade_id,
            False,
            dec.reason,
            None,
            0.0,
            None,
            equity,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            False,
            False,
            None,
            None,
            None,
            None,
            trade.mfe,
            trade.mae,
            dec.bound_by,
            trade.decision_at,
            trade.exit_at,
            trade.hypothesis_id,
            trade.asset,
            trade.cohort,
        )
    fee_mult = realized.fee_mult if realized else 1.0
    slip_mult = realized.slippage_mult if realized else 1.0
    fee = policy.cost_model.fee_bps * fee_mult / 10000
    units, margin = dec.units, dec.margin
    entry_fill = dec.entry_fill
    if slip_mult != 1.0:
        side_px = trade.entry_ask if s > 0 else trade.entry_bid
        entry_fill = side_px * (1 + s * dec.slippage_bps * slip_mult / 10000)
    ntl = units * entry_fill
    entry_fee = ntl * fee
    mid0 = (trade.entry_bid + trade.entry_ask) / 2
    # Adverse move from the entry mid at which the isolated position is liquidated.
    liq_move = s * (mid0 - dec.liquidation_price) / mid0
    liquidated = -trade.mae >= liq_move
    near = (not liquidated) and -trade.mae >= 0.5 * liq_move
    funding = trade.funding_paid_fraction * ntl
    if realized and realized.extra_adverse_funding_hourly:
        funding += realized.extra_adverse_funding_hourly * trade.hold_minutes / 60 * ntl
    if liquidated:
        net, fees, exit_reason = -margin - entry_fee, entry_fee, "LIQUIDATED"
        funding = 0.0
    else:
        exit_px = trade.exit_bid if s > 0 else trade.exit_ask
        xf = exit_px * (1 - s * dec.slippage_bps * slip_mult / 10000)
        gross = s * units * (xf - entry_fill)
        fees = entry_fee + units * xf * fee
        net = gross - fees - funding
        net = max(net, -margin - entry_fee)  # engine liquidation_adjustment loss cap
        exit_reason = trade.exit_reason
    rr = tgt / dec.loss_fraction if tgt is not None and dec.loss_fraction else None
    return Outcome(
        trade.trade_id,
        True,
        None,
        exit_reason,
        net,
        net / dec.intended_risk if dec.intended_risk > 0 else None,
        equity,
        ntl,
        margin,
        dec.leverage,
        dec.intended_risk,
        dec.intended_risk / equity,
        entry_fee,
        fees,
        funding,
        dec.slippage_bps,
        liquidated,
        near,
        dec.liquidation_distance,
        dec.invalidation_distance,
        tgt,
        rr,
        trade.mfe,
        trade.mae,
        dec.bound_by,
        trade.decision_at,
        trade.exit_at,
        trade.hypothesis_id,
        trade.asset,
        trade.cohort,
    )


@lru_cache(maxsize=1 << 20)
def _ts(x: str) -> float:
    from market_signal.paper.v2.data import ts

    return ts(x).timestamp()


def ordered(trades):
    """Deterministic chronology: decision time, then the frozen admission priority key."""
    return sorted(trades, key=lambda t: (_ts(t.decision_at), t.priority, t.trade_id))


def independent(trades, policy: SizingPolicy, equity: float, realized=None) -> list[Outcome]:
    acct = AccountSnapshot(equity=equity, peak_equity=equity)
    return [
        evaluate(t, size(t.request(), acct, policy), policy, equity, realized)
        for t in ordered(trades)
    ]


def _evidence(
    closed: list[tuple[float, str, float]], at: float, cache: dict, pooled: bool = False
) -> dict:
    """Causal R summary: only source trades whose exit strictly preceded ``at``."""
    groups: dict[str, list[float]] = defaultdict(list)
    for exit_t, hid, r in closed:
        if exit_t < at:
            groups["*" if pooled else hid].append(r)
    out = {}
    for hid, rs in groups.items():
        key = (hid, len(rs))
        if key not in cache:
            a = np.asarray(rs, dtype=float)
            n = len(a)
            mean = float(a.mean())
            sd = float(a.std(ddof=1)) if n > 1 else float("inf")
            # One-sided 95% normal bound; Kelly and evidence weighting use only this.
            cache[key] = FamilyEvidence(
                n, mean, mean - 1.645 * sd / math.sqrt(n), float((a**2).mean())
            )
        out[hid] = cache[key]
    return out


def source_r(trades) -> list[tuple[float, str, float]]:
    """Source-stream outcomes in R at a standard unit risk (for causal evidence only)."""
    from .policy import fixed_fractional

    unit = fixed_fractional(0.01)
    out = []
    for t in trades:
        o = evaluate(t, size(t.request(), AccountSnapshot(1000.0, 1000.0), unit), unit, 1000.0)
        if o.accepted and o.r is not None:
            out.append((_ts(t.exit_at), t.hypothesis_id, o.r))
    return out


@dataclass
class SequentialResult:
    policy: SizingPolicy
    starting_equity: float
    outcomes: list[Outcome]
    curve: list[tuple[float, float]]  # (epoch seconds, realized equity) after every close
    exposure: list[tuple[float, float, float, float]]  # (t, gross/eq, margin/eq, risk/eq)
    halted_at: float | None


def sequential(
    trades, policy: SizingPolicy, equity: float, *, evidence=None, realized=None
) -> SequentialResult:
    """Realized-equity account replay. Exits at or before a decision release capital first."""
    needs_evidence = policy.family in ("fractional_kelly", "evidence_weighted")
    closed_src = evidence if evidence is not None else source_r(trades) if needs_evidence else []
    ev_cache: dict = {}
    cash = peak = equity
    open_: dict[str, tuple[OpenExposure, Outcome]] = {}
    exits: list[tuple[float, str]] = []
    outcomes: list[Outcome] = []
    chronology = ordered(trades)
    curve = [(_ts(chronology[0].decision_at) if chronology else 0.0, equity)]
    exposure = []
    halted_at = None
    halt = policy.halt_drawdown

    def release(until: float):
        nonlocal cash, peak, halted_at
        while exits and exits[0][0] <= until:
            t, tid = heapq.heappop(exits)
            _, out = open_.pop(tid)
            cash += out.net_pnl
            peak = max(peak, cash)
            curve.append((t, cash))
            if halt is not None and halted_at is None and cash <= peak * (1 - halt):
                halted_at = t

    for tr in chronology:
        at = _ts(tr.decision_at)
        release(at)
        acct = AccountSnapshot(
            equity=cash,
            peak_equity=peak,
            open=tuple(o for o, _ in open_.values()),
            halted=halted_at is not None,
            evidence=_evidence(closed_src, at, ev_cache, policy.evidence_scope == "pooled")
            if needs_evidence
            else {},
        )
        dec = size(tr.request(), acct, policy)
        out = evaluate(tr, dec, policy, cash, realized)
        outcomes.append(out)
        if out.accepted:
            exp = OpenExposure(
                tr.trade_id,
                tr.asset,
                tr.side,
                tr.cohort,
                tr.hypothesis_id,
                out.notional,
                out.margin,
                out.intended_risk,
            )
            open_[tr.trade_id] = (exp, out)
            heapq.heappush(exits, (max(_ts(tr.exit_at), at), tr.trade_id))
            gross = sum(o.notional for o, _ in open_.values())
            margin = sum(o.margin for o, _ in open_.values())
            risk = sum(o.risk for o, _ in open_.values())
            exposure.append((at, gross / cash, margin / cash, risk / cash))
    release(math.inf)
    return SequentialResult(policy, equity, outcomes, curve, exposure, halted_at)
