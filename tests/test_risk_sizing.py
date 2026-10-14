"""Risk and position-sizing research v1: pure sizer, replay, bootstrap, audit, boundaries."""

from __future__ import annotations

import ast
import json
import math
from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from market_signal.sizing import (
    audit,
    bootstrap,
    engine,
    ledger,
    metrics,
    policy,
    prospective,
    replay,
    scenario,
    stress,
)
from market_signal.sizing.model import (
    OUTCOME_FIELDS,
    AccountSnapshot,
    FamilyEvidence,
    OpenExposure,
    SizingRequest,
    TradeRecord,
)

T0 = pd.Timestamp("2026-03-01T00:00:00Z")
FF1 = policy.fixed_fractional(0.01)
HYB = policy.invalidation_vol(0.01)
DDT = policy.drawdown_throttled(0.01)
NOCL = policy.invalidation_vol(0.01, cluster=False)


def at(minutes=0.0):
    return (T0 + pd.Timedelta(minutes=minutes)).isoformat()


def req(**kw) -> SizingRequest:
    base = dict(
        asset="ETH",
        side="long",
        cohort="CORE",
        hypothesis_id="h1",
        family="f",
        decision_at=at(),
        bid=99.99,
        ask=100.01,
        invalidation_price=98.0,
        target_price=103.0,
        emergency_price=90.0,
        causal_volatility=0.02,
        slippage_bps=2.0,
        funding_rate_hourly=0.0,
        max_hold_minutes=60,
        size_decimals=4,
        venue_max_leverage=25.0,
    )
    base.update(kw)
    return SizingRequest(**base)


def acct(equity=200.0, peak=None, open_=(), **kw):
    return AccountSnapshot(equity=equity, peak_equity=peak or equity, open=tuple(open_), **kw)


def trade(i=0, minutes=0.0, hold=30.0, exit_mid=101.0, asset="ETH", side="long", **kw):
    s = 1 if side == "long" else -1
    d = dict(
        source="test",
        run_id="run_test",
        trade_id=f"t{i:04d}",
        hypothesis_id=kw.pop("hid", "h1"),
        family="f",
        asset=asset,
        side=side,
        cohort="CORE",
        signal_at=at(minutes),
        decision_at=at(minutes),
        priority=f"p{i:04d}",
        entry_bid=99.99,
        entry_ask=100.01,
        invalidation_price=100 - s * 2.0,
        target_price=100 + s * 3.0,
        emergency_price=100 - s * 10,
        causal_volatility=0.02,
        slippage_bps=2.0,
        fee_bps=4.5,
        funding_rate_hourly=0.0,
        max_hold_minutes=60,
        size_decimals=4,
        venue_max_leverage=25.0,
        depth_usd=None,
        regime_ratio=None,
        invalidation_class="EXPLICIT_INVALIDATION",
        entry_at=at(minutes),
        exit_at=at(minutes + hold),
        exit_bid=exit_mid - 0.01,
        exit_ask=exit_mid + 0.01,
        exit_reason="TAKE_PROFIT" if s * (exit_mid - 100) > 0 else "INVALIDATED",
        exit_detail=None,
        mfe=max(0.0, s * (exit_mid / 100 - 1)),
        mae=min(0.0, s * (exit_mid / 100 - 1)),
        funding_paid_fraction=0.0,
        hold_minutes=hold,
    )
    d.update(kw)
    return TradeRecord(**d)


# --- pure sizer -------------------------------------------------------------------------


def test_deterministic_sizing_and_worked_explanation():
    a = engine.size(req(), acct(), HYB)
    b = engine.size(req(), acct(), HYB)
    assert a == b and a.accepted
    e = a.explain()
    for key in (
        "equity",
        "drawdown_multiplier",
        "base_risk_budget",
        "effective_risk_budget",
        "invalidation_distance",
        "volatility_distance",
        "sizing_distance",
        "raw_notional",
        "leverage",
        "margin",
        "final_intended_risk",
    ):
        assert key in e
    # 1% of $200 = $2; effective distance = max(inv ~2.01%, 0.5*2% = 1%) + costs.
    assert e["base_risk_budget"] == pytest.approx(2.0)
    assert a.sizing_distance == pytest.approx(a.invalidation_distance)
    assert a.intended_risk <= 2.0 + 1e-9
    assert a.notional == pytest.approx(2.0 / a.loss_fraction, rel=0.01)


@pytest.mark.parametrize("eq", [0.0, -5.0, float("nan"), float("inf")])
def test_zero_negative_nonfinite_equity_rejected(eq):
    for p in (policy.baseline(), FF1, HYB):
        d = engine.size(req(), acct(eq, peak=100.0), p)
        assert not d.accepted and d.reason == "NO_EQUITY" and d.notional == 0


def test_missing_invalidation_rejected_or_conservative_fallback():
    r = req(invalidation_price=None)
    assert engine.size(r, acct(), FF1).reason == "MISSING_INVALIDATION"
    assert engine.size(r, acct(), HYB).reason == "MISSING_INVALIDATION"
    vol = engine.size(r, acct(), policy.volatility_normalized(0.01))
    assert vol.accepted and vol.risk_basis == "VOLATILITY"
    base = engine.size(r, acct(), policy.baseline())
    assert base.accepted and base.risk_basis == "EMERGENCY_GUARD"
    assert base.notional == pytest.approx(40.0, rel=0.01)


def test_ultra_tight_invalidation_uses_volatility_floor_and_caps():
    r = req(invalidation_price=99.99)  # ~2 bps below the fill
    ff = engine.size(r, acct(), FF1)
    hy = engine.size(r, acct(), HYB)
    assert ff.accepted and "MAX_NOTIONAL" in ff.bound_by  # would be ~70x equity otherwise
    assert ff.notional <= 200.0 + 1e-9
    assert hy.sizing_distance == pytest.approx(0.01)  # 0.5 x 2% volatility floor
    assert hy.notional < ff.notional


def test_huge_invalidation_small_size_and_min_notional():
    d = engine.size(req(invalidation_price=60.0), acct(100.0), policy.fixed_fractional(0.0025))
    assert not d.accepted and d.reason == "MIN_NOTIONAL"  # $0.25 / 40% -> $0.62 notional
    d = engine.size(req(invalidation_price=85.0), acct(200.0), FF1)
    assert d.accepted and d.notional < 14 and d.intended_risk <= 2.0 + 1e-9


def test_high_volatility_shrinks_hybrid_size_monotonically():
    sizes = [
        engine.size(req(causal_volatility=v), acct(), HYB).notional for v in (0.01, 0.05, 0.10, 0.2)
    ]
    assert sizes == sorted(sizes, reverse=True)


def test_illiquid_asset_depth_cap_and_impact():
    r = req(cohort="SATELLITE", depth_usd=500.0, slippage_bps=20.0, invalidation_price=99.5)
    d = engine.size(r, acct(1000.0), HYB)
    assert d.accepted and "LIQUIDITY_CAP" in d.bound_by and d.notional <= 50.0 + 1e-9
    big = engine.size(req(invalidation_price=99.5), acct(1000.0), HYB)
    assert big.slippage_bps > 2.0  # sqrt impact above the $100 reference notional
    none = HYB.model_copy(
        update={"cost_model": HYB.cost_model.model_copy(update={"impact": "none"})}
    )
    assert engine.size(req(invalidation_price=99.5), acct(1000.0), none).slippage_bps == 2.0


def test_extreme_costs_reduce_then_reject_never_upsize():
    cheap = engine.size(req(invalidation_price=99.0), acct(), HYB)
    mid = engine.size(req(invalidation_price=99.0, slippage_bps=30.0), acct(), HYB)  # ~0.3
    bad = engine.size(req(invalidation_price=99.0, slippage_bps=100.0), acct(), HYB)  # ~0.55
    assert mid.accepted and mid.risk_budget == pytest.approx(cheap.risk_budget / 2)
    assert mid.notional < cheap.notional
    assert not bad.accepted and bad.reason == "UNECONOMIC_COST_TO_RISK"


def test_adverse_funding_reduces_size_favourable_does_not_increase():
    zero = engine.size(req(), acct(), HYB).notional
    adverse = engine.size(req(funding_rate_hourly=1e-3), acct(), HYB).notional
    favourable = engine.size(req(funding_rate_hourly=-1e-3), acct(), HYB).notional
    assert adverse < zero and favourable == zero


def test_venue_rounding_floors_never_rounds_up():
    d = engine.size(
        req(
            size_decimals=0,
            bid=33.33,
            ask=33.34,
            invalidation_price=32.0,
            target_price=35.0,
            emergency_price=30.0,
        ),
        acct(),
        FF1,
    )
    assert d.units == math.floor(d.units) and d.notional <= d.explain()["raw_notional"] + 1e-9
    assert engine.floor_units(10.0, 3.0, 2) == 3.33


def test_baseline_exactly_reproduces_engine_size():
    from market_signal.paper.v2 import engine as v2

    r = req()
    fill = r.ask * (1 + 2 / 10000)
    units, ntl = v2.size(200.0, fill, "ETH")
    d = engine.size(r, acct(200.0), policy.baseline())
    assert d.units == units and d.notional == pytest.approx(ntl) and d.leverage == 2.0


def test_max_leverage_policy_and_venue_caps():
    r = req(invalidation_price=99.9, venue_max_leverage=2.0)
    d = engine.size(r, acct(), FF1)
    assert d.leverage <= 2.0
    d = engine.size(
        req(invalidation_price=99.9), acct(), policy.drawdown_throttled(0.01, leverage_cap=1.0)
    )
    assert d.leverage == 1.0 and d.margin <= 0.34 * 200 + 1e-9


def test_liquidation_before_invalidation_rejected():
    p = policy.leverage_stress(10.0)
    # HYPE-like venue max 10x: 10x leverage liquidates ~5% away, inside a 4% stop * 2 buffer.
    r = req(venue_max_leverage=10.0, invalidation_price=96.0)
    p = p.model_copy(update={"leverage_mode": "fixed"})
    d = engine.size(r, acct(), p)
    assert not d.accepted and d.reason == "LIQUIDATION_BEFORE_INVALIDATION"
    ok = engine.size(r, acct(), FF1)
    assert ok.accepted and ok.liquidation_distance >= 2 * ok.loss_fraction


def _exp(asset="BTC", side="long", cohort="CORE", risk=2.0, notional=100.0, hid="h9"):
    return OpenExposure("x" + asset, asset, side, cohort, hid, notional, notional / 2, risk)


def test_concurrency_asset_open_and_max_positions():
    assert engine.size(req(), acct(open_=[_exp("ETH")]), HYB).reason == "ASSET_ALREADY_OPEN"
    full = [_exp("BTC"), _exp("SOL"), _exp("LINK")]
    assert engine.size(req(), acct(open_=full), HYB).reason == "MAX_POSITIONS"
    sats = [_exp("SATA", cohort="SATELLITE"), _exp("SATB", cohort="SATELLITE")]
    assert (
        engine.size(req(cohort="SATELLITE"), acct(open_=sats), HYB).reason
        == "SATELLITE_CONCENTRATION"
    )


def test_gross_limit_skip_for_control_reduce_for_risk():
    open_ = [_exp("BTC", notional=40.0, risk=0.1), _exp("SOL", notional=40.0, risk=0.1)]
    base = engine.size(req(), acct(open_=open_), policy.baseline())
    assert base.accepted  # 80 + 40 = 120 = 60% of 200
    open_ = [_exp("BTC", notional=60.0, risk=0.1), _exp("SOL", notional=40.0, risk=0.1)]
    assert engine.size(req(), acct(open_=open_), policy.baseline()).reason == "EXPOSURE_CAP"
    big = [_exp("BTC", notional=250.0, risk=0.1)]
    d = engine.size(req(invalidation_price=99.5), acct(open_=big), HYB)
    assert d.accepted and "EXPOSURE_CAP" in d.bound_by
    assert d.notional <= 1.5 * 200 - 250 + 1e-6


def test_correlated_cluster_cap_limits_same_direction_risk():
    longs = [_exp("BTC", risk=2.0), _exp("SOL", risk=1.5)]
    d = engine.size(req(), acct(open_=longs), HYB)
    # Cluster cap 2 x $2 budget = $4; $3.5 used -> at most $0.5 more same-direction risk.
    assert d.accepted and "CORRELATED_CLUSTER_CAP" in d.bound_by
    assert d.intended_risk <= 0.5 + 1e-6
    short = engine.size(
        req(side="short", invalidation_price=102.0, target_price=97.0, emergency_price=110.0),
        acct(open_=longs),
        HYB,
    )
    assert "CORRELATED_CLUSTER_CAP" not in short.bound_by
    sat_long = [_exp("SATA", cohort="SATELLITE", risk=1.5)]
    d2 = engine.size(req(cohort="SATELLITE"), acct(open_=sat_long), HYB)
    assert d2.intended_risk * 1.5 + 1.5 * 1.5 <= 4.0 + 1e-6


def test_drawdown_throttle_and_halt_never_increase_after_loss():
    budgets = []
    for eq in (200.0, 189.0, 179.0, 159.0, 131.0):
        d = engine.size(req(), acct(eq, peak=200.0), DDT)
        budgets.append(d.risk_budget / eq if d.accepted else 0.0)
    assert budgets[0] == pytest.approx(0.01)
    assert budgets[1] == pytest.approx(0.0075)
    assert budgets[2] == pytest.approx(0.005)
    assert budgets[3] == pytest.approx(0.0025)
    assert budgets[4] == pytest.approx(0.0025)  # 34.5% drawdown: still trading, minimum
    assert engine.size(req(), acct(130.0, peak=200.0), DDT).reason == "DRAWDOWN_HALT"
    assert all(s.multiplier <= 1 for s in DDT.drawdown_steps)
    assert budgets == sorted(budgets, reverse=True)


def test_account_recovery_restores_full_risk():
    hurt = engine.size(req(), acct(170.0, peak=200.0), DDT)
    back = engine.size(req(), acct(201.0, peak=201.0), DDT)
    assert hurt.risk_budget / 170 == pytest.approx(0.005)
    assert back.risk_budget / 201 == pytest.approx(0.01)


def test_regime_throttle_reduces_only():
    p = policy.drawdown_throttled(0.01, regime=True)
    calm = engine.size(req(regime_ratio=1.0), acct(), p)
    hot = engine.size(req(regime_ratio=3.0), acct(), p)
    unknown = engine.size(req(regime_ratio=None), acct(), p)
    assert hot.risk_budget == pytest.approx(calm.risk_budget / 2)
    assert unknown.risk_budget == calm.risk_budget


def test_kelly_fallback_cap_and_non_positive():
    p = policy.fractional_kelly(0.25)
    none = engine.size(req(), acct(), p)
    assert none.accepted and none.risk_budget == pytest.approx(200 * 0.005)
    weak = FamilyEvidence(50, 0.5, 0.3, 1.0)
    assert engine.size(req(), acct(evidence={"h1": weak}), p).risk_budget == pytest.approx(1.0)
    strong = FamilyEvidence(500, 0.5, 0.4, 1.0)  # 0.25 * 0.4 = 10% -> capped at 1.5%
    assert engine.size(req(), acct(evidence={"h1": strong}), p).risk_budget == pytest.approx(3.0)
    neg = FamilyEvidence(500, 0.05, -0.02, 1.0)
    assert engine.size(req(), acct(evidence={"h1": neg}), p).reason == "KELLY_NON_POSITIVE"
    pooled = policy.fractional_kelly(0.125, scope="pooled")
    assert engine.size(req(), acct(evidence={"*": strong}), pooled).risk_budget == pytest.approx(
        3.0
    )


def test_no_confidence_inputs_and_policy_hashing():
    names = {f.name for f in fields(SizingRequest)}
    assert not names & {"confidence", "score", "conviction", "llm", "leverage", "notional"}
    assert not names & OUTCOME_FIELDS
    with pytest.raises(TypeError):
        req(confidence=0.99)
    a = policy.invalidation_vol(0.01)
    b = a.model_copy(update={"volatility_k": 0.6})
    assert a.policy_id == policy.invalidation_vol(0.01).policy_id
    assert a.policy_id != b.policy_id
    ids = [p.policy_id for p in policy.catalogue()]
    assert len(ids) == len(set(ids))
    with pytest.raises(ValueError):
        policy.DrawdownStep(drawdown=0.1, multiplier=1.5)  # no martingale steps


def test_causal_outcome_fields_cannot_change_sizing():
    t = trade()
    alt = replace(
        t,
        exit_bid=150.0,
        exit_ask=150.1,
        mfe=0.5,
        mae=-0.4,
        exit_reason="X",
        exit_at=at(999),
        funding_paid_fraction=0.3,
        hold_minutes=999.0,
    )
    for p in policy.catalogue():
        assert engine.size(t.request(), acct(), p) == engine.size(alt.request(), acct(), p)


# --- replay -----------------------------------------------------------------------------


def test_independent_replay_r_geometry():
    win, loss = trade(0, exit_mid=103.0), trade(1, exit_mid=97.9)
    outs = replay.independent([win, loss], FF1, 200.0)
    assert outs[0].r > 1.0 and -1.3 < outs[1].r < -0.95
    assert outs[1].net_pnl == pytest.approx(outs[1].r * outs[1].intended_risk)
    s = metrics.independent_summary(outs, 200.0)
    assert s["taken"] == 2 and s["win_rate"] == 0.5 and s["payoff_ratio"] > 1


def test_sequential_replay_capital_release_ordering_and_skips():
    # Three overlapping trades fill the slots; the fourth arrives after one exits.
    ts = [trade(i, minutes=i, hold=30, asset=a) for i, a in enumerate(("BTC", "SOL", "LINK"))]
    ts.append(trade(3, minutes=2.5, hold=10, asset="AAVE"))  # all three still open
    ts.append(trade(4, minutes=30.0, hold=10, asset="HYPE"))  # BTC exits exactly at 30
    res = replay.sequential(ts, NOCL, 200.0)
    by = {o.trade_id: o for o in res.outcomes}
    assert by["t0003"].reason == "MAX_POSITIONS"
    assert by["t0004"].accepted  # exits at or before a decision release capital first
    assert res.curve[-1][1] == pytest.approx(200 + sum(o.net_pnl for o in res.outcomes))


def test_deterministic_tie_ordering_uses_priority_not_row_order():
    a = trade(0, asset="BTC", priority="b")
    b = trade(1, asset="SOL", priority="a")
    c = trade(2, asset="LINK", priority="c")
    d = trade(3, asset="AAVE", priority="0")
    one = replay.sequential([a, b, c, d], NOCL, 200.0)
    two = replay.sequential([d, c, b, a], NOCL, 200.0)
    assert [o.trade_id for o in one.outcomes] == [o.trade_id for o in two.outcomes]
    assert one.outcomes[0].trade_id == "t0003"
    assert one.outcomes[-1].reason == "MAX_POSITIONS"


def test_liquidation_counted_when_mae_reaches_isolated_liquidation():
    t = trade(exit_mid=99.0, mae=-0.30)
    p = policy.leverage_stress(5.0, safety=False).model_copy(update={"leverage_mode": "fixed"})
    o = replay.independent([t], p, 200.0)[0]
    assert o.liquidated and o.exit_reason == "LIQUIDATED"
    assert o.net_pnl == pytest.approx(-o.margin - o.entry_fees)
    safe = replay.independent([t.with_(mae=-0.12)], p, 200.0)[0]
    assert not safe.liquidated and safe.near_miss


def test_causal_evidence_is_size_independent_and_strictly_prior():
    ts = [trade(i, minutes=i * 60, exit_mid=103.0 if i % 2 else 97.9) for i in range(10)]
    src = replay.source_r(ts)
    assert len(src) == 10
    control = replay.independent(ts, policy.baseline(), 100.0)
    assert [r for *_, r in src] == [o.r for o in control]  # R as executed, no impact
    ev = replay._evidence(src, replay._ts(ts[3].exit_at), {})
    assert ev["h1"].samples == 3  # exits strictly before the decision only


def test_bootstrap_reproducible_and_seed_sensitive():
    ts = [
        trade(
            i,
            minutes=i * 600,
            hold=30,
            exit_mid=101.5 if i % 3 else 97.9,
            asset=("BTC", "SOL", "ETH")[i % 3],
        )
        for i in range(60)
    ]
    a = bootstrap.run(ts, HYB, 200.0, n=20, seed=3)
    b = bootstrap.run(ts, HYB, 200.0, n=20, seed=3)
    c = bootstrap.run(ts, HYB, 200.0, n=20, seed=4)
    assert a == b and a != c
    s = bootstrap.resample(ts, np.random.default_rng(1))
    assert len({t.trade_id for t in s}) == len(s)


def test_stresses_only_worsen():
    ts = [
        trade(i, minutes=i * 120, exit_mid=103.0 if i % 2 else 97.9, asset=("BTC", "SOL")[i % 2])
        for i in range(40)
    ]
    base = metrics.sequential_summary(replay.sequential(ts, HYB, 200.0))["terminal_equity"]
    for name, st, realized in stress.scenarios(ts):
        term = metrics.sequential_summary(replay.sequential(st, HYB, 200.0, realized=realized))[
            "terminal_equity"
        ]
        assert term <= base + 1e-9, name


def test_drawdown_metrics_known_curve():
    curve = [(0, 100.0), (86400, 110.0), (2 * 86400, 99.0), (3 * 86400, 111.0), (4 * 86400, 105.0)]
    d = metrics.drawdowns(curve)
    assert d["max_drawdown"] == pytest.approx(0.1)
    assert d["drawdown_episodes"] == 2
    assert d["max_drawdown_recovery_days"] == pytest.approx(1.0)


# --- synthetic scenarios ----------------------------------------------------------------


@pytest.fixture(scope="module")
def small_stream():
    sc = replace(scenario.SCENARIOS["WEAK_EDGE"], days=40, warmup_days=31, seed=11)
    return scenario.generate(sc)


def test_scenario_deterministic_causal_and_synthetic(small_stream):
    sc = replace(scenario.SCENARIOS["WEAK_EDGE"], days=40, warmup_days=31, seed=11)
    again = scenario.generate(sc)
    assert [t.to_dict() for t in small_stream] == [t.to_dict() for t in again]
    assert small_stream and all(
        t.synthetic and t.source.startswith("synthetic:") for t in small_stream
    )
    for t in small_stream:
        assert t.decision_at < t.entry_at <= t.exit_at
        s = t.sign
        mid = (t.entry_bid + t.entry_ask) / 2
        assert s * (mid - t.invalidation_price) > 0 and s * (t.target_price - mid) > 0


def test_vectorized_exit_matches_frozen_exit_condition():
    from market_signal.paper.nimble import engine as nimble
    from market_signal.paper.nimble import spec

    rng = np.random.default_rng(5)
    for side in ("long", "short"):
        h = next(
            x
            for x in spec.baseline.bootstrap()
            if x.source_key == "oi_price_stall" and x.side == side
        )
        s = 1 if side == "long" else -1
        for k in range(12):
            n = 400
            mid = 100 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))
            spread = np.full(n, 0.0002)
            ref = mid[0]
            th = type("Th", (), {})()
            th.objective = {"price": ref * (1 + s * 0.004)}
            th.invalidation = {
                "price": ref * (1 - s * 0.004),
                "emergency_price": ref * (1 - s * 0.1),
            }
            th.expiry = {"reaction_window_minutes": 30, "minimum_progress_target_fraction": 0.25}
            th.max_hold_minutes = 60
            i0 = 1
            got = scenario.first_exit(mid, i0, s, th, spread=spread, t_trigger=0)
            p = {
                "state": "OPEN",
                "side": side,
                "entry_ref": mid[i0],
                "entry_at": (scenario.START + pd.Timedelta(seconds=i0 * scenario.TICK)).isoformat(),
                "last_quote_received_at": None,
                "thesis": {
                    "objective": th.objective,
                    "invalidation": {**th.invalidation, "state_rule": "price_structure_only"},
                    "expiry": th.expiry,
                    "max_hold_minutes": 60,
                    "trigger_at": scenario.START.isoformat(),
                    "microstructure_state": {},
                    "context_snapshot": {},
                },
                "hypothesis_id": h.hypothesis_id,
            }
            expected = None
            for i in range(i0 + 1, n - 1):
                now = scenario.START + pd.Timedelta(seconds=i * scenario.TICK)
                m = mid[i]
                q = nimble.ExecutableQuote if hasattr(nimble, "ExecutableQuote") else None
                from market_signal.paper.nimble.model import ExecutableQuote

                q = ExecutableQuote(
                    "ETH", now.isoformat(), now.isoformat(), m * (1 - 0.0001), m * (1 + 0.0001)
                )
                c = nimble.exit_condition(
                    {**p, "last_quote_received_at": now.isoformat()}, q, [], now
                )
                if c:
                    expected = (i, c["exit_reason"])
                    break
            assert expected is not None and got == expected, (side, k, got, expected)


# --- audit, ledger, boundaries ------------------------------------------------------------


def test_invalidation_audit_covers_every_hypothesis():
    from market_signal.paper.nimble import event_spec, spec

    a = audit.audit()
    ids = {r["hypothesis_id"] for r in a["rows"] if r["hypothesis_id"]}
    assert {h.hypothesis_id for h in spec.baseline.bootstrap()} <= ids
    assert {h.hypothesis_id for h in event_spec.bootstrap()} <= ids
    assert set(a["counts"]) <= {
        "EXPLICIT_INVALIDATION",
        "DERIVABLE_INVALIDATION",
        "EXPIRY_ONLY",
        "UNSUITABLE_FOR_RISK_NORMALIZED_SIZING",
    }
    assert a["counts"]["EXPIRY_ONLY"] == 1
    tech = [r for r in a["rows"] if r.get("source_phase") == 22]
    assert tech and all(r["classification"] == "DERIVABLE_INVALIDATION" for r in tech)


def test_nimble_ledger_adapter_reconciles_real_engine_trade(store):
    """End-to-end: a trade closed by the frozen paper engine is reproduced exactly."""
    import tests.test_nimble_paper as nimble_tests
    from market_signal.paper.nimble import engine as nimble

    nimble.register(store, now=nimble_tests.T0)
    rid = nimble.create(store, now=nimble_tests.T0)["run_id"]
    closed = nimble_tests.closed_tp(store, rid)
    assert closed["state"] == "CLOSED"
    path = Path(store.path) if hasattr(store, "path") else None
    store.con.execute("CHECKPOINT")
    db = path or Path(store.con.execute("PRAGMA database_list").fetchall()[0][2])
    store.close()
    trades, meta = ledger.nimble_trades(db, [rid])
    assert meta["opened_read_only"] and len(trades) == 1
    t = trades[0]
    assert t.trade_id == closed["trade_id"] and t.recorded_net_pnl == closed["net_pnl"]
    rec = ledger.reconcile(trades)
    assert rec["all_units_match"] and rec["max_net_pnl_error"] < 1e-9
    # The adapter never wrote: payload unchanged after the read.
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        after = json.loads(
            con.execute(
                "SELECT payload FROM paper_nimble_positions WHERE trade_id=?", [t.trade_id]
            ).fetchone()[0]
        )
    finally:
        con.close()
    assert after["net_pnl"] == closed["net_pnl"]


def test_existing_execution_identities_and_import_boundary():
    from market_signal.paper.nimble import dynamic_spec, event_spec, spec

    assert spec.policy_id() == (
        "paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501"
    )
    assert event_spec.policy_id() == (
        "paperexecution_event_12208f088bec2bcfaa8dfe10af41ccac81975d012cd2e5b0919ec5918712c885"
    )
    assert spec.risk_id() == (
        "paperrisk_v2_475b7c1e87fc2934eba4bb288f07cc818ed7dadb9fc8b9ab8d24a594a772e8b9"
    )
    assert dynamic_spec.policy_id().startswith("paperexecution_dynamic_")
    root = Path(__file__).resolve().parents[1] / "src" / "market_signal"
    for f in [
        *(root / "paper").rglob("*.py"),
        *(root / "universe").rglob("*.py"),
        *(root / "ops").rglob("*.py"),
    ]:
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom | ast.Import):
                mods = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                assert not any(m.startswith("market_signal.sizing") for m in mods), f


def test_prospective_design_disabled_and_runtime_refuses():
    d = prospective.design()
    assert d["enabled"] is False and d["registered"] is False
    assert set(d["cohorts"]) == {"dynamic_lab_fixed_v1", "dynamic_lab_risk_sizing_v1"}
    assert prospective.design_id() == prospective.design_id()
    with pytest.raises(RuntimeError):
        prospective.runtime_size(req(), 100.0, 100.0, ())
    src = Path(prospective.__file__).read_text()
    assert "os.environ" not in src and "getenv" not in src


def test_request_from_thesis_matches_real_thesis():
    import tests.test_nimble_paper as nt
    from market_signal.paper.nimble.model import build_thesis

    hh = nt.h("oi_price_stall", "long")
    th = build_thesis(
        hh,
        nt.observation(hh),
        nt.quote(),
        nt.Source().geometry("BTC", nt.observation(hh)),
        nt.time(1),
    )
    r = prospective.request_from_thesis(
        th.model_dump(mode="json"),
        nt.quote(1, 2),
        cohort="CORE",
        predicted_funding_rate_hourly=0.0,
        size_decimals=5,
        venue_max_leverage=40,
        volatility_move=0.02,
        slippage_bps=2,
    )
    assert r.invalidation_price == th.invalidation["price"]
    assert engine.size(r, acct(100.0), policy.baseline()).accepted


def test_cli_policies_audit_replay_and_live_db_refusal(project, tmp_path):
    from market_signal.cli.main import app

    runner = CliRunner()
    res = runner.invoke(app, ["sizing", "policies", "--json"])
    assert res.exit_code == 0 and "fixed_allocation_baseline_v1" in res.stdout
    assert runner.invoke(app, ["sizing", "audit", "--json"]).exit_code == 0
    path = tmp_path / "t.jsonl"
    ledger.write_jsonl(
        [trade(i, minutes=i * 60, asset=("BTC", "SOL")[i % 2]) for i in range(6)], path
    )
    out = tmp_path / "out.json"
    res = runner.invoke(
        app,
        [
            "sizing",
            "replay",
            "--trades",
            str(path),
            "--policy",
            "fixed_allocation_baseline_v1",
            "--policy",
            HYB.name,
            "--equity",
            "100",
            "--out",
            str(out),
        ],
    )
    assert res.exit_code == 0, res.stdout
    v = json.loads(out.read_text())
    assert v["kind"].startswith("COUNTERFACTUAL") and len(v["results"]) == 2
    assert v["results"][0]["policy"]["policy_id"].startswith("sizingpolicy_")
    live = project / "data" / "prism.duckdb"
    live.write_bytes(b"")
    res = runner.invoke(app, ["sizing", "replay", "--db", str(live)])
    assert res.exit_code == 2 and "offline backup" in res.stdout


def test_catalogue_statuses_research_only_never_candidate():
    by = {p.name: p for p in policy.catalogue()}
    assert by["fixed_allocation_baseline_v1"].status == "CONTROL"
    for p in by.values():
        if p.family in ("fractional_kelly", "evidence_weighted"):
            assert p.status in ("RESEARCH_ONLY", "STRESS_ONLY")
        if p.name.startswith("leverage_stress"):
            assert p.status == "STRESS_ONLY"
    assert prospective.candidate().status == "CANDIDATE"
    assert prospective.candidate().leverage_cap <= 3
