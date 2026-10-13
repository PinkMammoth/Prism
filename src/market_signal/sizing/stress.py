"""Deterministic stresses. Sizing assumptions stay at the frozen cost model; the realized
world gets worse. A policy must not need perfect execution to survive."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from .replay import _ts, ordered


@dataclass(frozen=True)
class Realized:
    """Realized-cost multipliers applied after sizing (unknown to the sizer)."""

    fee_mult: float = 1.0
    slippage_mult: float = 1.0
    extra_adverse_funding_hourly: float = 0.0


def _rank(t, salt: str) -> str:
    return hashlib.sha256((salt + t.trade_id).encode()).hexdigest()


def _loss_at_invalidation(t, gap: float = 1.0):
    """Exit through the frozen invalidation (``gap`` x its distance from the entry mid)."""
    if not t.invalidation_price:
        return t
    mid0 = (t.entry_bid + t.entry_ask) / 2
    d = t.sign * (mid0 - t.invalidation_price) / mid0
    px = mid0 * (1 - t.sign * gap * d)
    half = (t.exit_ask - t.exit_bid) / 2 if t.exit_ask > t.exit_bid else 0.0
    return replace(
        t,
        exit_bid=px - half,
        exit_ask=px + half,
        exit_reason="STRESS_INVALIDATED",
        mae=min(t.mae, -gap * d),
        mfe=min(t.mfe, max(0.0, t.mfe)),
    )


def winners(trades):
    return [t for t in trades if t.exit_reason == "TAKE_PROFIT"]


def reduce_win_rate(trades, fraction=0.10):
    w = sorted(winners(trades), key=lambda t: _rank(t, "winrate"))
    flip = {t.trade_id for t in w[: round(len(w) * fraction)]}
    return [_loss_at_invalidation(t) if t.trade_id in flip else t for t in trades]


def degrade_payoff(trades, keep=0.75):
    out = []
    for t in trades:
        mid0 = (t.entry_bid + t.entry_ask) / 2
        xm = (t.exit_bid + t.exit_ask) / 2
        move = t.sign * (xm / mid0 - 1)
        if move > 0:
            new = mid0 * (1 + t.sign * keep * move)
            t = replace(t, exit_bid=t.exit_bid + new - xm, exit_ask=t.exit_ask + new - xm)
        out.append(t)
    return out


def missed_fills(trades, fraction=0.10):
    """Adverse selection: the fills that are missed are winners."""
    w = sorted(winners(trades), key=lambda t: _rank(t, "missed"))
    drop = {t.trade_id for t in w[: round(len(w) * fraction)]}
    return [t for t in trades if t.trade_id not in drop]


def losing_streak(trades, length=10):
    o = ordered(trades)
    i = len(o) // 2
    hit = {t.trade_id for t in o[i : i + length]}
    return [_loss_at_invalidation(t) if t.trade_id in hit else t for t in trades]


def clustered_losses(trades, days=2):
    """Every trade in the busiest ``days``-day window loses at its invalidation."""
    o = ordered(trades)
    if not o:
        return trades
    times = [_ts(t.decision_at) for t in o]
    best, start = -1, 0
    j = 0
    for i, t0 in enumerate(times):
        while times[j] < t0 - days * 86400:
            j += 1
        if i - j > best:
            best, start = i - j, j
    end_t = times[start] + days * 86400
    hit = {t.trade_id for t, x in zip(o, times, strict=True) if times[start] <= x <= end_t}
    return [_loss_at_invalidation(t) if t.trade_id in hit else t for t in trades]


def tail_trades(trades, count=1):
    """Gap through the stop to the 10% emergency guard on the tightest-stop trades: the case
    where risk-normalized size is largest relative to the realized loss."""

    def dist(t):
        mid0 = (t.entry_bid + t.entry_ask) / 2
        return t.sign * (mid0 - t.invalidation_price) / mid0

    cands = sorted((t for t in trades if t.invalidation_price), key=lambda t: (dist(t), t.trade_id))
    hit = {t.trade_id: t for t in cands[:count]}
    out = []
    for t in trades:
        if t.trade_id in hit:
            g = 0.10 / max(dist(t), 1e-9)
            t = _loss_at_invalidation(t, gap=g)
        out.append(t)
    return out


def scenarios(trades):
    """(name, trades, realized) for every stress in the protocol."""
    return [
        ("base", trades, Realized()),
        ("fees_x1.25", trades, Realized(fee_mult=1.25)),
        ("fees_x1.5", trades, Realized(fee_mult=1.5)),
        ("slippage_x1.5", trades, Realized(slippage_mult=1.5)),
        ("slippage_x2", trades, Realized(slippage_mult=2.0)),
        ("win_rate_minus_10pct_of_winners", reduce_win_rate(trades), Realized()),
        ("payoff_degraded_25pct", degrade_payoff(trades), Realized()),
        ("losing_streak_10", losing_streak(trades), Realized()),
        ("clustered_correlated_losses_2d", clustered_losses(trades), Realized()),
        ("missed_fills_10pct_winners", missed_fills(trades), Realized()),
        ("adverse_funding_0.005pct_h", trades, Realized(extra_adverse_funding_hourly=5e-5)),
        ("tail_gap_to_emergency_x1", tail_trades(trades, 1), Realized()),
        ("tail_gap_to_emergency_x2", tail_trades(trades, 2), Realized()),
        (
            "combined_fees1.5_slip2_payoff",
            degrade_payoff(trades),
            Realized(fee_mult=1.5, slippage_mult=2.0),
        ),
    ]
