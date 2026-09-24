"""Phase 2: indicators, point-in-time macro, regimes."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_signal.data.pit import asof_values, daily_history_asof
from market_signal.data.providers.macro import EiaProvider, FredProvider
from market_signal.indicators.technical import (
    atr,
    compute_features,
    rolling_percentile,
    rsi,
    sma,
)
from market_signal.models.domain import AssetClass, Regime
from market_signal.regimes.engine import (
    classify_crypto,
    classify_macro,
    derived_macro_rows,
    regime_asof,
)
from tests.conftest import make_daily_crypto

# StockCharts' published Wilder RSI example
SC_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
             46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35,
             44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13]  # fmt: skip
SC_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99,
          41.46, 41.87, 45.46, 37.30, 33.08, 37.77]  # fmt: skip


def test_rsi_matches_published_reference():
    r = rsi(pd.Series(SC_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    # exact first value: simple averages of the first 14 gains/losses
    d = np.diff(SC_CLOSES[:15])
    rs = d.clip(min=0).mean() / (-d).clip(min=0).mean()
    assert r.iloc[14] == pytest.approx(100 - 100 / (1 + rs), abs=1e-9)
    # StockCharts rounds intermediate averages to 2dp, hence the looser tolerance
    np.testing.assert_allclose(r.iloc[14:].to_numpy(), SC_RSI, atol=0.1)


def test_sma_requires_full_window():
    s = sma(pd.Series([1.0, 2, 3, 4]), 3)
    assert np.isnan(s.iloc[1]) and s.iloc[2] == 2 and s.iloc[3] == 3


def test_atr_constant_range():
    n = 40
    close = pd.Series(np.full(n, 100.0))
    high, low = close + 1, close - 1
    a = atr(high, low, close, 14)
    assert a.iloc[:14].isna().all()
    assert a.iloc[14:].to_numpy() == pytest.approx(2.0)


def test_missing_value_breaks_wilder_recursion_not_filled():
    c = pd.Series(np.linspace(100, 120, 40))
    c.iloc[25] = np.nan
    r = rsi(c, 14)
    assert r.iloc[25:27].isna().all()  # gap -> NaN, not a fabricated value


def test_rolling_percentile():
    x = pd.Series([1.0, 2, 3, 4, 5])
    p = rolling_percentile(x, window=5, min_periods=3)
    assert np.isnan(p.iloc[1]) and p.iloc[4] == 1.0 and p.iloc[2] == 1.0
    assert rolling_percentile(pd.Series([5.0, 4, 3, 2, 1]), 5, 3).iloc[4] == 0.0


@pytest.mark.parametrize("cut", [260, 400, 555, 699])
def test_features_are_truncation_invariant(cut):
    """No look-ahead: features at bar t computed on data[:t+1] equal those on full data."""
    df = make_daily_crypto(700, seed=3)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    full = compute_features(df, AssetClass.CRYPTO)
    part = compute_features(df.iloc[:cut], AssetClass.CRYPTO)
    cols = [c for c in full.columns if c not in ("ts", "close_time")]
    pd.testing.assert_series_equal(
        full.loc[cut - 1, cols], part.loc[cut - 1, cols], check_names=False
    )


def test_future_shock_does_not_change_past_features():
    df = make_daily_crypto(500, seed=4)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    base = compute_features(df, AssetClass.CRYPTO)
    shocked = df.copy()
    shocked.loc[400:, ["open", "high", "low", "close"]] *= 5
    after = compute_features(shocked, AssetClass.CRYPTO)
    cols = [c for c in base.columns if c not in ("ts", "close_time")]
    pd.testing.assert_frame_equal(base.loc[:399, cols], after.loc[:399, cols])


# --------------------------------------------------------------------------- PIT


def _vintage_rows():
    return pd.DataFrame(
        {
            "obs_date": [date(2024, 1, 1), date(2024, 1, 1), date(2024, 2, 1)],
            "value": [1.0, 1.2, 2.0],
            "available_at": pd.to_datetime(["2024-02-15", "2024-03-15", "2024-03-15"], utc=True),
        }
    )


def test_asof_uses_only_published_vintages():
    rows = _vintage_rows()
    times = pd.to_datetime(["2024-02-01", "2024-02-20", "2024-03-01", "2024-03-16"], utc=True)
    out = asof_values(rows, times)
    assert np.isnan(out["value"].iloc[0])  # nothing published yet
    assert out["value"].iloc[1] == 1.0 and out["value"].iloc[2] == 1.0
    assert out["value"].iloc[3] == 2.0 and out["obs_date"].iloc[3] == date(2024, 2, 1)
    hist = daily_history_asof(rows, pd.Timestamp("2024-03-16", tz="UTC"))
    assert hist.loc["2024-01-01"] == 1.2  # revised value, known only after 2024-03-15
    hist_early = daily_history_asof(rows, pd.Timestamp("2024-03-01", tz="UTC"))
    assert hist_early.loc["2024-01-01"] == 1.0


def test_asof_handles_unsorted_times():
    rows = _vintage_rows()
    times = pd.to_datetime(["2024-03-16", "2024-02-20"], utc=True)
    out = asof_values(rows, times)
    assert list(out["value"]) == [2.0, 1.0]


def test_fred_parse_missing_and_policies():
    obs = [
        {
            "date": "2024-01-02",
            "value": "4.1",
            "realtime_start": "2024-01-03",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2024-01-03",
            "value": ".",
            "realtime_start": "2024-01-04",
            "realtime_end": "9999-12-31",
        },
    ]
    mk = FredProvider.parse("DGS10", obs, "market_close", lag_days=2)
    assert np.isnan(mk["value"].iloc[1])  # '.' stays missing, never 0
    assert mk["available_at"].iloc[0] == pd.Timestamp("2024-01-04", tz="UTC")
    assert (mk["pit_method"] == "market_close").all()
    vt = FredProvider.parse("CPIAUCSL", obs, "vintage")
    assert vt["available_at"].iloc[0] == pd.Timestamp("2024-01-04", tz="UTC")  # realtime_start + 1d
    disp = FredProvider.parse("BAMLH0A0HYM2", obs, "display_only")
    assert (disp["pit_method"] == "reconstructed").all()


def test_eia_release_rule():
    payload = {"response": {"data": [{"period": "2024-01-05", "value": 420.0}]}}
    df = EiaProvider.parse("WCESTUS1", payload, lag_days=6)
    assert df["available_at"].iloc[0] == pd.Timestamp("2024-01-11", tz="UTC")
    assert df["pit_method"].iloc[0] == "release_rule"


def test_research_loader_rejects_reconstructed(store):
    from market_signal.data.pit import PitViolation, load_macro
    from market_signal.data.updaters import upsert_macro

    obs = [
        {
            "date": "2024-01-02",
            "value": "4.1",
            "realtime_start": "2024-01-03",
            "realtime_end": "9999-12-31",
        }
    ]
    upsert_macro(store, FredProvider.parse("BAMLH0A0HYM2", obs, "display_only"), "fred", "r1")
    with pytest.raises(PitViolation):
        load_macro(store, "BAMLH0A0HYM2", research=True)
    assert len(load_macro(store, "BAMLH0A0HYM2", research=False)) == 1


def test_derived_rows_refuse_vintage_series():
    with pytest.raises(ValueError):
        derived_macro_rows(_vintage_rows(), "diff", 1)


# --------------------------------------------------------------------------- regimes

CRYPTO_CFG = {
    "factors": {
        "above_sma200": {"weight": 1.0},
        "sma50_above_sma200": {"weight": 1.0},
        "sma200_rising": {"weight": 1.0},
        "drawdown": {"weight": 1.0, "ok_above": -0.2, "bad_below": -0.4},
        "vol_extreme": {"weight": 1.0, "pct_above": 0.9},
        "breadth": {"weight": 1.0, "good_above": 0.6, "bad_below": 0.3, "min_assets": 3},
    },
    "thresholds": {"risk_on": 0.4, "risk_off": -0.3},
    "min_coverage": 0.6,
}


def _trend_features(drift: float, n: int = 700, seed: int = 1) -> pd.DataFrame:
    df = make_daily_crypto(n, seed=seed, drift=drift)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    return compute_features(df, AssetClass.CRYPTO)


def test_crypto_regime_trend_up_down_unknown():
    up = _trend_features(0.004)
    down = _trend_features(-0.004)
    r_up = classify_crypto(
        up, {s: _trend_features(0.004, seed=i) for i, s in enumerate("ABC")}, CRYPTO_CFG
    )
    r_dn = classify_crypto(
        down, {s: _trend_features(-0.004, seed=i) for i, s in enumerate("ABC")}, CRYPTO_CFG
    )
    assert (r_up["regime"].iloc[-300:] == Regime.RISK_ON).mean() > 0.6
    assert (r_dn["regime"].iloc[-300:] == Regime.RISK_OFF).mean() > 0.6
    assert (r_up["regime"].iloc[-300:] == Regime.RISK_OFF).mean() < 0.05
    assert r_up["regime"].iloc[50] == Regime.UNKNOWN  # before 200 bars: insufficient data


def test_regime_asof_is_backward_only():
    idx = pd.DatetimeIndex(pd.to_datetime(["2024-01-02 21:00", "2024-01-03 21:00"], utc=True))
    reg = pd.DataFrame({"regime": ["RISK_ON", "RISK_OFF"], "score": [0.5, -0.5]}, index=idx)
    q = pd.to_datetime(
        ["2024-01-03 00:00", "2024-01-03 20:59", "2024-01-03 21:00", "2024-01-01 00:00"], utc=True
    )
    out = regime_asof(reg, q)
    assert list(out["regime"]) == ["RISK_ON", "RISK_ON", "RISK_OFF", "UNKNOWN"]


def test_macro_regime_votes_from_pit_series():
    spy = _trend_features(0.003)
    cfg = {
        "factors": {
            "spy_above_sma200": {"weight": 1.0},
            "spy_sma50_above_sma200": {"weight": 1.0},
            "qqq_above_sma200": {"weight": 1.0},
            "vix": {"weight": 1.0, "good_below": 20, "bad_above": 30, "series": "VIXCLS"},
            "credit_widening": {"weight": 1.0, "lookback_obs": 3, "bad_above": 0.5, "good_below": -0.25, "series": "BAA10Y"},
            "curve_inverted": {"weight": 0.5, "series": "T10Y2Y"},
            "usd_surge": {"weight": 0.5, "lookback_obs": 3, "bad_above": 0.04, "series": "DTWEXBGS"},
        },
        "thresholds": {"risk_on": 0.4, "risk_off": -0.3},
        "min_coverage": 0.5,
    }  # fmt: skip
    t_last = spy["close_time"].iloc[-1]
    vix_rows = pd.DataFrame(
        {"obs_date": [date(2020, 1, 1), t_last.date()], "value": [15.0, 45.0],
         "available_at": [pd.Timestamp("2020-01-03", tz="UTC"), t_last + pd.Timedelta(days=2)]}
    )  # fmt: skip
    out = classify_macro(spy, None, {"VIXCLS": vix_rows}, cfg)
    # the VIX spike is published AFTER the last close -> not visible at that close
    assert out["vix"].iloc[-1] == 15.0
    assert out["vote_vix"].iloc[-1] == 1.0
    assert np.isnan(out["vote_qqq_above_sma200"].iloc[-1])  # missing, not zero
    assert out["coverage"].iloc[-1] < 1.0
