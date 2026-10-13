"""SYNTHETIC scenario trade streams for sizing-policy behaviour. Not market data, not evidence.

Prism has too few prospective trades to estimate sizing performance. These scenarios answer a
narrower question: how does each sizing rule transform the *same* trade stream when the
underlying signals have no edge, a weak edge, a negative edge, or an edge that fails in
correlated clusters. Signals are planted (the Prism engine-validation convention), never fitted.

Prism geometry is reused, not imitated: hypotheses are the frozen ``paper_nimble_v1`` registry,
theses come from ``build_thesis`` with the frozen 15-minute prior-structure geometry, economics
from ``economics`` (2x cost filter), and exits from a vectorized replica of ``exit_condition``
(emergency > invalidation > take-profit > expiry > max hold) that the tests check against the
real function. Not modelled: state-rule invalidations (flow/OI/absorption), cost-decay funding
exits, data failures, ambiguous wick ordering (quotes carry no high/low).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.research.lab.common import content_id

from .model import TradeRecord

TICK = 15  # seconds; production polls quotes every 2 s, so stop fills here are conservative
TPM = 60 // TICK
START = pd.Timestamp("2026-01-01T00:00:00Z")  # synthetic clock, deliberately not real dates


@dataclass(frozen=True)
class Asset:
    name: str
    price: float
    annual_vol: float
    spread_bps: float
    slippage_bps: float
    size_decimals: int
    max_leverage: float
    cohort: str = "CORE"
    depth_usd: float | None = None


ASSETS = (
    Asset("BTC", 100_000, 0.50, 0.5, 2, 5, 40),
    Asset("ETH", 4_000, 0.65, 1.0, 2, 4, 25),
    Asset("SOL", 200, 0.85, 2.0, 4, 2, 20),
    Asset("HYPE", 40, 1.10, 3.0, 6, 2, 10),
    Asset("LINK", 20, 0.85, 3.0, 6, 1, 10),
    Asset("AAVE", 300, 0.90, 4.0, 8, 2, 10),
    Asset("SATA", 2.5, 1.40, 6.0, 15, 1, 5, "SATELLITE", 60_000),
    Asset("SATB", 0.8, 1.60, 8.0, 20, 0, 5, "SATELLITE", 40_000),
    Asset("SATC", 15.0, 1.30, 6.0, 15, 2, 5, "SATELLITE", 80_000),
    Asset("SATD", 0.3, 1.80, 10.0, 25, 0, 3, "SATELLITE", 30_000),
)


@dataclass(frozen=True)
class Scenario:
    name: str
    edge_calm: float  # planted signal-to-noise over the reaction window, calm regime
    edge_stressed: float  # ... stressed regime
    description: str
    days: int = 150
    warmup_days: int = 30
    signal_rate_per_day: float = 0.12  # per (hypothesis, asset) candidate evaluation stream
    correlation: float = 0.6
    seed: int = 20261013

    @property
    def scenario_id(self) -> str:
        return content_id("sizingscenario_", self.__dict__)


# Planted edge = expected drift toward the frozen target, as a multiple of the target distance,
# spread over the hypothesis' reaction window. Values were set so the *control* policy's
# expectancy lands in a stated band; they are scenario definitions, never sizing parameters.
SCENARIOS = {
    "NULL_EDGE": Scenario("NULL_EDGE", 0.0, 0.0, "No planted edge; costs alone (control ~ -0.5R)."),
    "NEGATIVE_EDGE": Scenario("NEGATIVE_EDGE", -0.5, -0.5, "Signals wrong (control ~ -0.65R)."),
    "WEAK_EDGE": Scenario("WEAK_EDGE", 1.5, 1.5, "Weak edge after costs (control ~ +0.1R)."),
    "MODERATE_EDGE": Scenario("MODERATE_EDGE", 2.2, 2.2, "Optimistic edge (control ~ +0.3R)."),
    "REGIME_FLIP": Scenario(
        "REGIME_FLIP",
        2.2,
        -1.0,
        "Edge in calm regimes that reverses in stressed regimes: clustered correlated losses.",
    ),
}


def _student(rng, n, df=4.0):
    return rng.standard_t(df, n) / math.sqrt(df / (df - 2))


def _regimes(rng, hours):
    """Two-state hourly Markov volatility regime: calm 10d mean, stressed 1.5d mean."""
    state = np.zeros(hours, dtype=np.int8)
    s = 0
    for i in range(hours):
        if s == 0 and rng.random() < 1 / 240:
            s = 1
        elif s == 1 and rng.random() < 1 / 36:
            s = 0
        state[i] = s
    noise = np.exp(rng.normal(0, 0.25, hours))
    return state, np.where(state == 1, 2.2, 0.8) * noise


def _hypotheses():
    from market_signal.paper.nimble import spec

    return list(spec.baseline.bootstrap())


def _portable(h) -> bool:
    return h.source_phase in (22, 24)  # universe portability: phase 23 is core-only/unvalidated


def generate(scenario: Scenario) -> list[TradeRecord]:
    from market_signal.paper.nimble.model import ExecutableQuote, build_thesis, economics
    from market_signal.paper.v2.data import Observation
    from market_signal.universe.execution import SatelliteQuote

    rng = np.random.default_rng(scenario.seed)
    n_hours = scenario.days * 24
    n = n_hours * 3600 // TICK
    state, vmult = _regimes(rng, n_hours)
    tick_vmult = np.repeat(vmult, 3600 // TICK)
    tick_state = np.repeat(state, 3600 // TICK)
    zm = _student(rng, n)
    rho = scenario.correlation
    sig = {a.name: a.annual_vol / math.sqrt(365 * 24 * 3600 / TICK) for a in ASSETS}
    logret = {}
    # Three synthetic market-wide crash windows (30 minutes, -4% .. -8% on the market factor).
    crash = np.zeros(n)
    for c in rng.choice(np.arange(scenario.warmup_days * 24, n_hours - 2), 3, replace=False):
        i0 = int(c) * (3600 // TICK)
        crash[i0 : i0 + 30 * TPM] = rng.uniform(-0.08, -0.04) / (30 * TPM)
    for a in ASSETS:
        zi = _student(rng, n)
        beta = 1.5 if a.cohort == "SATELLITE" else 1.0
        logret[a.name] = sig[a.name] * tick_vmult * (
            math.sqrt(rho) * zm + math.sqrt(1 - rho) * zi
        ) + beta * crash * (a.annual_vol / 0.5)
    # Hourly funding: small positive carry, noisier in stressed regimes.
    funding = {
        a.name: np.clip(
            1e-5 + rng.normal(0, 1e-5, n_hours) * np.where(state == 1, 3, 1), -2e-4, 2e-4
        )
        for a in ASSETS
    }
    hyps = _hypotheses()
    # Candidate signal stream: independent of the future path by construction.
    candidates = []
    for h in hyps:
        cadence = h.cadence_minutes
        evals_per_day = 1440 // cadence
        p = scenario.signal_rate_per_day / evals_per_day
        for a in ASSETS:
            if a.cohort == "SATELLITE" and not _portable(h):
                continue
            k = scenario.warmup_days * evals_per_day
            fire = np.nonzero(rng.random(scenario.days * evals_per_day - k - 1) < p)[0] + k
            for j in fire:
                candidates.append((int(j) * cadence * TPM, h, a))
    candidates.sort(key=lambda c: (c[0], c[2].name, c[1].hypothesis_id))
    logp = {a.name: math.log(a.price) + np.cumsum(logret[a.name]) for a in ASSETS}
    spreads = {a.name: a.spread_bps / 10000 * np.where(tick_state == 1, 2.0, 1.0) for a in ASSETS}

    def at(i):
        return (START + pd.Timedelta(seconds=int(i) * TICK)).isoformat()

    def quote(a, i):
        m = math.exp(logp[a.name][i])
        sp = spreads[a.name][i]
        kw = dict(
            asset=a.name, at=at(i), available_at=at(i), bid=m * (1 - sp / 2), ask=m * (1 + sp / 2)
        )
        if a.cohort == "SATELLITE":
            return SatelliteQuote(**kw, depth_usd=a.depth_usd, slippage_bps=a.slippage_bps)
        return ExecutableQuote(**kw)

    def geometry(a, t0, h):
        """Frozen Nimble geometry from the 15 complete minutes before the signal (causal)."""
        minute = t0 // TPM
        x = np.exp(logp[a.name][(minute - 16) * TPM : minute * TPM]).reshape(16, TPM)
        hi, lo, cl = x.max(axis=1)[1:], x.min(axis=1)[1:], x[:, -1]
        prev = cl[:-1]
        tr = np.maximum.reduce([hi - lo, np.abs(hi - prev), np.abs(lo - prev)])
        vol = 15**0.5 * tr.mean() / cl[-1]
        evidence = {"source": "synthetic"}
        if h.source_phase == 22:
            # Wilder ATR(14) of hourly log true range, prior complete hours (200h window).
            hours = min(minute // 60, 200)
            if hours < 15:
                return None
            y = logp[a.name][(minute // 60 - hours) * 3600 // TICK : minute // 60 * 3600 // TICK]
            y = y.reshape(hours, 3600 // TICK)
            lh, ll, lc = y.max(axis=1), y.min(axis=1), y[:, -1]
            pc = np.r_[lc[0], lc[:-1]]
            trh = np.maximum.reduce([lh - ll, np.abs(lh - pc), np.abs(ll - pc)])
            atr = trh[:14].mean()
            for v in trh[14:]:
                atr = (atr * 13 + v) / 14
            evidence["atr"] = float(atr)
            vol = math.expm1(atr)
        return {
            "local_low": float(lo.min()),
            "local_high": float(hi.max()),
            "volatility_move": float(vol),
            "microstructure": {},
        }, evidence

    from market_signal.paper.nimble import spec

    # Pass 1 (chronological): freeze each thesis from the path known at its signal, then plant
    # that signal's drift strictly after its entry fill. Later drifts only touch later ticks, so
    # every earlier-thesis input stays exactly what was causally known.
    frozen = []
    for t0, h, a in candidates:
        if t0 // TPM < 16 or t0 + 2 >= n:
            continue
        g = geometry(a, t0, h)
        if g is None:
            continue
        geo, evidence = g
        q0 = quote(a, t0)
        rate = float(funding[a.name][max(0, t0 // (3600 // TICK) - 1)])
        obs = Observation(
            h.hypothesis_id,
            a.name,
            at(t0),
            at(t0),
            True,
            evidence=evidence,
            context={"as_of": at(t0)},
            intended_side=h.side,
        )
        try:
            th = build_thesis(h, obs, q0, geo, at(t0))
        except ValueError:
            continue
        s = 1 if h.side == "long" else -1
        if not economics(th, q0, rate)["economic"]:
            continue
        if s * (th.invalidation["price"] - th.invalidation["emergency_price"]) <= 0:
            continue
        frozen.append((t0, h, a, th, geo["volatility_move"], rate))
        edge = scenario.edge_stressed if tick_state[t0] else scenario.edge_calm
        if edge:
            window = spec.mapping(h)["expiry_minutes"] * TPM
            total = s * edge * abs(math.log(th.objective["price"] / th.entry_reference_price))
            lo, hi = t0 + 2, min(n, t0 + 2 + window)
            logp[a.name][lo:hi] += total * np.arange(1, hi - lo + 1) / window
            logp[a.name][hi:] += total * min(1.0, (hi - lo) / window)
    mids = {k: np.exp(v) for k, v in logp.items()}
    # Causal regime ratio: trailing 24h core-basket RV / trailing 30d daily mean, hourly.
    m1 = {k: v[TPM - 1 :: TPM] for k, v in mids.items()}
    core = [a.name for a in ASSETS if a.cohort == "CORE"]
    rv_min = np.mean([np.r_[0, np.diff(np.log(m1[c]))] ** 2 for c in core], axis=0)
    rv_hour = rv_min.reshape(n_hours, 60).sum(axis=1)
    c24 = np.convolve(rv_hour, np.ones(24), "full")[:n_hours]
    c720 = np.convolve(rv_hour, np.ones(720), "full")[:n_hours] / 30
    ratio = np.where(np.arange(n_hours) >= 720, c24 / np.maximum(c720, 1e-18), np.nan)

    out = []
    for t0, h, a, th, vol, rate in frozen:
        s = 1 if h.side == "long" else -1
        i_fill = t0 + 1  # first quote strictly after the intent
        q1 = quote(a, i_fill)
        if not economics(th, q1, rate)["economic"]:
            continue
        if s * (q1.mid - th.invalidation["price"]) <= 0:
            continue
        exit_i, reason = first_exit(
            mids[a.name], i_fill, s, th, spread=spreads[a.name], t_trigger=t0
        )
        fill_i = min(exit_i + 1, n - 1)  # exit fills at the next quote after the decision
        q2 = quote(a, fill_i)
        path = mids[a.name][i_fill : fill_i + 1] / q1.mid - 1
        hours = range(i_fill // (3600 // TICK) + 1, fill_i // (3600 // TICK) + 1)
        paid = s * float(sum(funding[a.name][k] for k in hours if k < n_hours))
        hr = t0 // (3600 // TICK) - 1
        out.append(
            TradeRecord(
                source="synthetic:" + scenario.name,
                run_id=scenario.scenario_id,
                trade_id=content_id(
                    "sizingsyntrade_", [scenario.name, a.name, h.hypothesis_id, t0]
                ),
                hypothesis_id=h.hypothesis_id,
                family=h.family,
                asset=a.name,
                side=h.side,
                cohort=a.cohort,
                signal_at=at(t0),
                decision_at=at(t0),
                priority=content_id("", [a.name, h.hypothesis_id, at(t0)]),
                entry_bid=q1.bid,
                entry_ask=q1.ask,
                invalidation_price=float(th.invalidation["price"]),
                target_price=float(th.objective["price"]),
                emergency_price=float(th.invalidation["emergency_price"]),
                causal_volatility=float(vol),
                slippage_bps=float(a.slippage_bps),
                fee_bps=4.5,
                funding_rate_hourly=rate,
                max_hold_minutes=int(th.max_hold_minutes),
                size_decimals=a.size_decimals,
                venue_max_leverage=float(a.max_leverage),
                depth_usd=a.depth_usd,
                regime_ratio=float(ratio[hr]) if hr >= 0 and math.isfinite(ratio[hr]) else None,
                invalidation_class="EXPLICIT_INVALIDATION"
                if h.source_phase == 24
                else "DERIVABLE_INVALIDATION",
                entry_at=at(i_fill),
                exit_at=at(fill_i),
                exit_bid=q2.bid,
                exit_ask=q2.ask,
                exit_reason=reason,
                exit_detail=None,
                mfe=float(max(0.0, (s * path).max())),
                mae=float(min(0.0, (s * path).min())),
                funding_paid_fraction=paid,
                hold_minutes=(fill_i - i_fill) * TICK / 60,
                synthetic=True,
            )
        )
    return out


def first_exit(mid, i_entry, s, th, *, spread, t_trigger):
    """Vectorized ``exit_condition`` for price/time rules on executable bid/ask ticks.

    Returns (decision tick, exit_reason). Precedence on one tick follows the engine.
    """
    entry_ref = mid[i_entry]
    expiry = t_trigger + th.expiry["reaction_window_minutes"] * TPM
    max_hold = i_entry + th.max_hold_minutes * TPM
    end = min(len(mid) - 2, max_hold)
    idx = np.arange(i_entry + 1, end + 1)
    if not len(idx):
        return i_entry, "MAX_HOLD"
    m = mid[idx]
    sp = spread[idx]
    px = m * (1 - s * sp / 2)  # long exits on the bid, short on the ask
    target = s * (th.objective["price"] - entry_ref)
    progress = s * (px - entry_ref)
    emergency = s * (px - th.invalidation["emergency_price"]) <= 0
    invalid = s * (px - th.invalidation["price"]) <= 0
    tp = s * (px - th.objective["price"]) >= 0
    expired = (idx >= expiry) & (progress < target * th.expiry["minimum_progress_target_fraction"])
    held = idx >= max_hold
    any_ = emergency | invalid | tp | expired | held
    hit = np.nonzero(any_)[0]
    if not len(hit):
        return end, "MAX_HOLD"
    k = hit[0]
    for mask, reason in (
        (emergency, "CATASTROPHE_GUARD"),
        (invalid, "INVALIDATED"),
        (tp, "TAKE_PROFIT"),
        (expired, "THESIS_EXPIRED"),
        (held, "MAX_HOLD"),
    ):
        if mask[k]:
            return int(idx[k]), reason
    raise AssertionError("unreachable")
