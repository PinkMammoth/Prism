"""Phase 4: setup rules, causality, and point-in-time equity fundamentals."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_signal.fundamentals.equity import (
    EdgarProvider,
    fundamentals_timeline,
    quality_score,
    snapshot,
)
from market_signal.indicators.technical import compute_features
from market_signal.models.domain import AssetClass
from market_signal.setups.base import SetupContext, conditions_mask, edge_trigger, get_setup
from tests.conftest import make_daily_crypto


@pytest.fixture(autouse=True)
def _project(project):  # setups read config/setups/*.yaml from the project root
    return project


def _feat(df: pd.DataFrame, cls=AssetClass.CRYPTO) -> pd.DataFrame:
    d = df.copy()
    d["close_time"] = d["ts"] + pd.Timedelta(days=1)
    return compute_features(d, cls)


def _path_bars(closes: np.ndarray, rng_width: float = 0.5, vol: float = 1000.0) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")
    opens = np.r_[closes[0], closes[:-1]]
    high = np.maximum(opens, closes) + rng_width
    low = np.minimum(opens, closes) - rng_width
    return pd.DataFrame(
        {
            "ts": ts,
            "open": opens,
            "high": high,
            "low": low,
            "close": closes,
            "volume": np.full(n, vol),
        }
    )


def _breakout_path(
    retest_close: float = 101.6, retest_low: float = 101.2, fail: bool = False
) -> pd.DataFrame:
    trend = np.linspace(50, 99, 250)
    base = 99.5 + 0.5 * np.sin(np.arange(40))  # tight base, highs <= ~100.5
    post = [103.0, 103.5, 104.0, retest_close, 103.0, 104.0]
    if fail:
        post = [103.0, 103.5, 97.0, 101.0, 101.0, 101.0]
    closes = np.r_[trend, base, post]
    df = _path_bars(closes)
    b = 290  # breakout bar: wide range + volume
    df.loc[b, "high"] = 103.5
    df.loc[b, "low"] = 100.0
    df.loc[b, "volume"] = 3000.0
    if not fail:
        df.loc[b + 3, "low"] = retest_low
    return df


def test_breakout_retest_fires_on_retest_bar():
    f = _feat(_breakout_path())
    setup = get_setup("breakout_retest")
    out = setup.evaluate(f, setup.default_params("crypto"), SetupContext("X", "crypto"))
    fired = np.flatnonzero(out.signal.to_numpy())
    assert fired.tolist() == [293]  # the retest bar, not the breakout bar
    level = f["high"].iloc[250:290].max()
    assert out.stop.iloc[293] == pytest.approx(level - f["atr_14"].iloc[293])
    assert bool(out.diagnostics["breakout_bar"].iloc[290])


def test_breakout_failure_cancels_and_no_chasing():
    setup = get_setup("breakout_retest")
    p = setup.default_params("crypto")
    failed = setup.evaluate(_feat(_breakout_path(fail=True)), p, SetupContext("X", "crypto"))
    assert not failed.signal.any()
    extended = setup.evaluate(
        _feat(_breakout_path(retest_close=106.0, retest_low=105.0)), p, SetupContext("X", "crypto")
    )
    assert not extended.signal.iloc[:294].any()


def test_quality_pullback_conditions_and_trigger():
    setup = get_setup("quality_pullback")
    p = setup.default_params("equity")
    f = pd.DataFrame({
        "close": [100.0, 100, 100], "sma_50": [99.0, 99, 99], "sma_200": [90.0, 90, 90],
        "sma_200_slope": [0.01, 0.01, 0.01], "roc_6m": [0.1, 0.1, 0.1], "pullback_63_atr": [1.0, 3.0, 3.0],
        "dist_sma_50_atr": [0.5, 0.5, 0.5], "rsi_14": [45.0, 45, 45], "rvol_pct": [0.5, 0.5, 0.5],
        "atr_14": [2.0, 2, 2], "swing_low_20": [95.0, 95, 95],
    })  # fmt: skip
    out = setup.evaluate(f, p, SetupContext("X", "equity"))
    assert out.signal.tolist() == [False, True, False]  # edge-triggered once
    assert out.stop.iloc[1] == pytest.approx(95 - 0.5 * 2)
    f.loc[:, "rvol_pct"] = np.nan  # missing input -> not eligible, never a signal
    out2 = setup.evaluate(f, p, SetupContext("X", "equity"))
    assert not out2.signal.any() and not out2.eligible.any()


@pytest.mark.parametrize("name", ["quality_pullback", "breakout_retest"])
def test_setup_signals_truncation_invariant(name):
    setup = get_setup(name)
    df = make_daily_crypto(1200, seed=11, drift=0.001)
    full_f = _feat(df)
    p = setup.default_params("crypto")
    full = setup.evaluate(full_f, p, SetupContext("X", "crypto"))
    assert full.signal.sum() > 0, "test needs at least one signal"
    for k in (700, 950, 1199):
        part = setup.evaluate(_feat(df.iloc[:k]), p, SetupContext("X", "crypto"))
        pd.testing.assert_series_equal(full.signal.iloc[:k], part.signal, check_names=False)
        pd.testing.assert_series_equal(
            full.stop.iloc[:k][full.signal.iloc[:k]], part.stop[part.signal], check_names=False
        )


def test_rerating_crypto_is_never_historically_eligible():
    setup = get_setup("rerating")
    f = _feat(make_daily_crypto(400))
    out = setup.evaluate(f, setup.default_params("crypto"), SetupContext("HYPE", "crypto"))
    assert not out.eligible.any() and not out.signal.any()


def test_rerating_equity_rules():
    setup = get_setup("rerating")
    f = pd.DataFrame({"dist_52w_high": [-0.05, -0.20, -0.20]})
    fund = pd.DataFrame({"rev_growth": [0.1, 0.1, 0.1], "ni_growth": [0.05, 0.05, -0.2], "net_income_ttm": [1.0, 1, 1],
                         "pe_pct_5y": [0.1, 0.1, 0.1]})  # fmt: skip
    out = setup.evaluate(
        f, setup.default_params("equity"), SetupContext("M", "equity", fundamentals=fund)
    )
    assert out.signal.tolist() == [False, True, False]
    assert bool(out.diagnostics["earnings_not_deteriorating"].iloc[2]) is False


def test_conditions_dsl_missing_is_not_eligible():
    f = pd.DataFrame({"close": [1.0, 2.0], "sma_200": [np.nan, 1.0], "rsi_14": [40.0, 40.0]})
    cond, _frame, defined = conditions_mask(
        f, {"price_above_sma_200": True, "rsi_14": {"min": 30, "max": 50}}
    )
    assert cond.tolist() == [False, True] and defined.tolist() == [False, True]
    with pytest.raises(ValueError):
        conditions_mask(f, {"not_a_feature": {"min": 1}})


def test_edge_trigger_cooldown():
    c = pd.Series([True, True, False, True, False, False, True])
    assert edge_trigger(c, 2).tolist() == [True, False, False, True, False, False, True]
    assert edge_trigger(c, 4).tolist() == [True, False, False, False, False, False, True]


# --------------------------------------------------------------------------- EDGAR PIT


def _fact(start, end, val, filed, form="10-Q"):
    return {
        "start": start,
        "end": end,
        "val": val,
        "filed": filed,
        "form": form,
        "accn": f"a{filed}{end}",
        "fy": 2023,
        "fp": "Q",
    }


EDGAR_FIXTURE = {
    "facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                _fact("2022-01-01", "2022-03-31", 95, "2022-04-30"),
                _fact("2022-01-01", "2022-06-30", 195, "2022-07-30"),
                _fact("2022-01-01", "2022-12-31", 400, "2023-02-15", "10-K"),
                _fact("2023-01-01", "2023-03-31", 110, "2023-04-30"),
                _fact("2022-01-01", "2022-03-31", 95, "2023-04-30"),
                _fact("2023-01-01", "2023-06-30", 230, "2023-07-30"),
                _fact("2022-01-01", "2022-06-30", 195, "2023-07-30"),
                _fact("2022-01-01", "2022-12-31", 410, "2024-02-15", "10-K"),  # restatement, filed later
            ]}},
            "NetIncomeLoss": {"units": {"USD": [_fact("2022-01-01", "2022-12-31", 40, "2023-02-15", "10-K")]}},
            "UnknownConcept": {"units": {"USD": [_fact("2022-01-01", "2022-12-31", 1, "2023-02-15")]}},
        }
    }
}  # fmt: skip


def _facts():
    df = EdgarProvider.parse("TEST", EDGAR_FIXTURE)
    metric_order = {"Revenues": 1, "NetIncomeLoss": 0}
    df["priority"] = df["concept"].map(metric_order).fillna(0)
    return df


def test_edgar_parse_availability_is_filed_plus_one_day():
    df = EdgarProvider.parse("TEST", EDGAR_FIXTURE)
    assert set(df["concept"]) == {"Revenues", "NetIncomeLoss"}  # unknown concepts ignored
    row = df[(df["period_end"] == date(2022, 12, 31)) & (df["value"] == 400)].iloc[0]
    assert row["available_at"] == pd.Timestamp("2023-02-16", tz="UTC")
    assert row["pit_method"] == "filing_date"


def test_ttm_from_ytd_identity_and_restatement_is_not_backdated():
    facts = _facts()
    at = lambda s: snapshot(facts, pd.Timestamp(s, tz="UTC"))  # noqa: E731
    assert at("2023-03-01")["revenue_ttm"] == 400  # FY known
    assert at("2023-05-05")["revenue_ttm"] == 110 + 400 - 95
    assert at("2023-08-01")["revenue_ttm"] == 230 + 400 - 195
    assert at("2023-02-16")["revenue_ttm"] == 400  # available from filed + 1 day
    # before the 10-K is available only H1-2022 YTD exists and no prior FY: TTM is undefined
    assert np.isnan(at("2023-02-15 23:59")["revenue_ttm"])
    # the 2024 restatement to 410 must not leak into 2023 snapshots
    assert at("2023-12-31")["revenue_ttm"] == 230 + 400 - 195
    # prior-year TTM unavailable -> growth input missing, not zero
    assert np.isnan(at("2023-08-01")["revenue_ttm_prev"])


def test_timeline_changes_only_on_filings():
    tl = fundamentals_timeline(_facts())
    assert len(tl) == tl["asof"].nunique()
    assert tl["asof"].is_monotonic_increasing


def test_quality_score_requires_minimum_inputs():
    f = pd.DataFrame({"rev_growth": [0.20, np.nan], "ni_growth": [0.25, np.nan], "op_margin": [0.35, 0.1],
                      "fcf_margin": [0.3, np.nan], "roe": [0.25, np.nan]})  # fmt: skip
    q = quality_score(f)
    assert q.iloc[0] == pytest.approx(1.0) and np.isnan(q.iloc[1])
    assert quality_score(f, is_bank=True).iloc[0] == pytest.approx(1.0)
