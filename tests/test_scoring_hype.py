"""Phase 5–6: HYPE valuation, scoring components, risk sizing, end-to-end scan."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_signal.fundamentals.hype import HypeInputs, _v, aqa_revenue, band, run_rates, value_hype
from market_signal.fundamentals.modules import ModuleResult, ramp
from market_signal.scoring.engine import (
    Component,
    SetupState,
    catalyst_component,
    entry_component,
    macro_component,
    module_components,
)
from market_signal.scoring.risk import suggest_size

HYPE_CFG = {
    "revenue": {"normalisation": "blend", "blend_weights": {"30d": 0.25, "90d": 0.5, "365d": 0.25},
                "min_window_coverage": 0.95, "haircut": 0.0, "af_share_of_revenue": 1.0},
    "aqa_v2": {"active_since": "2026-08-26", "yield_share": 0.9, "cost_adjustment": 0.0035, "eligible_fraction": 1.0,
               "reserve_yield_series": "DTB3", "reserve_yield_override": None},
    "dilution": {"contributor_unlock_per_month": 10_000_000, "contributor_unlock_until": "2027-11-30",
                 "staking_emissions_per_year": None, "contributor_sell_fraction": 0.5},
    "bands": [{"min": 0.06, "label": "STRONGLY_UNDERVALUED"}, {"min": 0.05, "label": "UNDERVALUED"},
              {"min": 0.04, "label": "FAIR"}, {"min": 0.03, "label": "OVERVALUED"}, {"min": 0.0, "label": "STRONGLY_OVERVALUED"}],
    "inverse_table": {"price_grid_pct": [-20, 0, 20], "target_yield": 0.045},
    "sensitivity": {"revenue_pct": [-10, 0, 10], "usdc_pct": [0], "reserve_yield_pp": [0.0]},
}  # fmt: skip

AS_OF = date(2026, 9, 20)


def _inputs(price=40.0, circ=300e6, usdc=5e9, ry=0.04, daily=2e6, days=400):
    idx = pd.date_range(end=pd.Timestamp(AS_OF), periods=days, freq="D").date
    return HypeInputs(
        price=_v("price", price, "observed", "USD", "test"),
        circulating_supply=_v("circulating_supply", circ, "observed", "HYPE", "test"),
        usdc_on_hyperliquid=_v("usdc_on_hyperliquid", usdc, "observed", "USD", "test"),
        reserve_yield=_v("reserve_yield", ry, "observed", "p.a.", "test"),
        daily_revenue=pd.Series(daily, index=idx),
    )


def test_run_rates_require_coverage():
    s = pd.Series(1.0, index=pd.date_range(end=pd.Timestamp(AS_OF), periods=60, freq="D").date)
    r = run_rates(s, AS_OF, {"30d": 30, "90d": 90}, 0.95)
    assert r["30d"] == pytest.approx(365.0)
    assert r["90d"] is None  # only 60 of 90 days: not scaled up


def test_structural_bid_and_yield():
    v = value_hype(_inputs(), HYPE_CFG, AS_OF)
    core = 2e6 * 365
    aqa = 5e9 * 1.0 * 0.9 * (0.04 - 0.0035)
    assert v.get("normalised_revenue") == pytest.approx(core)
    assert v.get("aqa_revenue") == pytest.approx(aqa)
    assert v.get("structural_bid") == pytest.approx(core + aqa)
    y = (core + aqa) / (40.0 * 300e6)
    assert v.get("buyback_yield") == pytest.approx(y)
    assert v.signal == band(y, HYPE_CFG["bands"])


def test_observed_assumed_derived_are_separated():
    v = value_hype(_inputs(), HYPE_CFG, AS_OF)
    assert all(x.kind == "observed" for x in v.observed)
    assert all(x.kind == "assumed" for x in v.assumed)
    assert all(x.kind == "derived" for x in v.derived.values())
    assert {"aqa_yield_share", "aqa_cost_adjustment"} <= {x.name for x in v.assumed}


def test_inverse_round_trip():
    v = value_hype(_inputs(), HYPE_CFG, AS_OF)
    for price in (20.0, 40.0, 80.0):
        r = v.required_for_price(price, 0.045)
        # plugging the required revenue back in yields exactly the target yield
        bid = r["required_core_revenue"] + v.get("aqa_revenue")
        assert bid / (price * 300e6) == pytest.approx(0.045)
        # plugging the required USDC back in (holding revenue) also yields the target
        bid2 = v.get("core_bid") + aqa_revenue(r["required_usdc"], 0.04, HYPE_CFG["aqa_v2"])
        assert bid2 / (price * 300e6) == pytest.approx(0.045) or r["required_usdc"] == 0.0
    # zones: price at which yield hits each band edge
    z = v.zones()
    assert v.price_for_yield(0.05) == pytest.approx(z["fair"][0])
    assert z["accumulate"][0] < z["accumulate"][1] < z["fair"][1]


def test_missing_inputs_propagate_not_zero():
    inp = _inputs()
    inp = HypeInputs(inp.price, _v("circulating_supply", None, "observed", "HYPE", "missing"), inp.usdc_on_hyperliquid,
                     inp.reserve_yield, inp.daily_revenue)  # fmt: skip
    v = value_hype(inp, HYPE_CFG, AS_OF)
    assert v.get("buyback_yield") is None and v.signal == "UNKNOWN"
    assert any("circulating_supply missing" in w for w in v.warnings)
    short = value_hype(_inputs(days=100), HYPE_CFG, AS_OF)  # no 365d window -> blend undefined
    assert short.get("normalised_revenue") is None


def test_aqa_inactive_before_activation():
    v = value_hype(_inputs(), HYPE_CFG, date(2026, 8, 1))
    assert v.get("aqa_revenue") == 0.0


# --------------------------------------------------------------------------- risk


RISK = {"trade_risk": {"actionable": 0.005, "strong": 0.0075, "exceptional": 0.01},
        "investment_fraction": {"actionable": 0.05, "strong": 0.075, "exceptional": 0.1},
        "max_position_fraction": 0.25, "max_gross_exposure": 1.0}  # fmt: skip


def test_trade_sizing_formula_and_caps():
    s = suggest_size("TRADE", "strong", 0.75, entry=100, stop=90, cfg=RISK)
    assert s.portfolio_risk == pytest.approx(0.0075 * 0.75)
    assert s.position_fraction == pytest.approx(0.0075 * 0.75 / 0.10)
    tight = suggest_size("TRADE", "exceptional", 1.0, entry=100, stop=99.5, cfg=RISK)
    assert tight.position_fraction == 0.25 and tight.capped  # no leverage via tight stops
    full = suggest_size(
        "TRADE", "actionable", 1.0, entry=100, stop=90, cfg=RISK, current_gross_exposure=0.98
    )
    assert full.position_fraction == pytest.approx(0.02)
    assert (
        suggest_size("TRADE", "strong", 1.0, entry=100, stop=None, cfg=RISK).position_fraction
        == 0.0
    )
    assert suggest_size("TRADE", "watch", 1.0, entry=100, stop=90, cfg=RISK) is None


def test_investment_sizing_has_no_stop():
    s = suggest_size("INVESTMENT", "actionable", 0.5, entry=100, stop=None, cfg=RISK)
    assert s.position_fraction == pytest.approx(0.025) and s.stop_distance is None


# --------------------------------------------------------------------------- components


def test_missing_components_are_na_not_zero():
    mod = ModuleResult("etf", None, None)
    q, v = module_components(mod, {"fundamental": 25, "valuation": 20})
    assert q.points is None and v.points is None
    assert macro_component("UNKNOWN", 10, "macro").points is None
    assert macro_component("RISK_OFF", 10, "macro").points == pytest.approx(2.0)


def test_score_denominator_excludes_na():
    comps = [Component("a", 10, 20), Component("b", None, 25), Component("c", 15, 15)]
    avail = [c for c in comps if c.available]
    score = 100 * sum(c.points for c in avail) / sum(c.max_points for c in avail)
    assert score == pytest.approx(100 * 25 / 35)


def test_catalyst_window_and_netting():
    cats = [{"symbol": "HYPE", "description": "good", "date": "2026-08-26", "expires": "2026-12-31", "impact": "positive", "strength": 6},
            {"symbol": "HYPE", "description": "bad", "date": "2026-01-01", "expires": "2027-12-31", "impact": "negative", "strength": 3}]  # fmt: skip
    c = catalyst_component("HYPE", cats, pd.Timestamp("2026-09-24", tz="UTC"), 10)
    assert c.points == 3
    assert catalyst_component("HYPE", cats, pd.Timestamp("2027-06-01", tz="UTC"), 10).points == 0
    assert catalyst_component("BTC", cats, pd.Timestamp("2026-09-24", tz="UTC"), 10).points is None


def test_entry_valuation_vs_technical_are_separate():
    """Cheap (in value zone) but 4h-extended: entry is penalised while valuation is not."""
    feat = pd.DataFrame(
        {"close": [60.0], "atr_14": [3.0], "dist_sma_50_atr": [1.0], "rsi_14": [55.0]}
    )
    h4 = pd.DataFrame({"close": np.r_[np.full(40, 50.0), 60.0], "ema_21": 50.0, "atr_14": 1.0})
    st = SetupState(
        "rerating",
        "Re-rating",
        "INVESTMENT",
        "ACTIVE",
        "",
        entry_zone=(0.0, 62.0),
        ideal_entry=62.0,
    )
    cfg = {"setup_active_points": 5, "in_zone_points": 6, "near_zone_atr": 1.0, "near_zone_points": 3,
           "not_extended_max_sma50_atr": 2.5, "not_extended_points": 2, "rsi_overbought": 70, "rsi_points": 2,
           "h4_overextension_atr": 2.0, "h4_penalty": 4}  # fmt: skip
    comp, in_zone = entry_component(feat, h4, st, cfg, 15)
    assert in_zone and comp.points == 15 - 4
    assert any("4h short-term extended" in r for r in comp.reasons)


def test_ramp():
    assert ramp(None, 0, 1) is None and ramp(0.5, 0, 1) == 0.5 and ramp(2, 0, 1) == 1.0


# --------------------------------------------------------------------------- end-to-end on synthetic DB


def test_scan_end_to_end_on_synthetic_db(settings, store, monkeypatch):
    from market_signal.demo import build_demo_db
    from market_signal.scoring.engine import run_scan

    monkeypatch.setenv("PRISM_SOURCE_OVERRIDE", "synthetic")
    build_demo_db(settings, store, years=4, seed=3)
    res = run_scan(store, settings)
    assert len(res.assessments) == len(settings.active_assets())
    for a in res.assessments:
        avail = [c for c in a.components if c.available]
        assert a.score == pytest.approx(
            100 * sum(c.points for c in avail) / sum(c.max_points for c in avail)
        )
        assert 0 <= a.score <= 100
        if a.status in ("ACTIONABLE", "STRONG", "EXCEPTIONAL"):
            assert a.setup.state == "ACTIVE" and a.coverage >= 0.6
    assert res.headline
    saved = store.query("SELECT count(*) n FROM scan_results WHERE scan_id=?", [res.scan_id]).iloc[
        0
    ]["n"]
    assert saved == len(res.assessments)
