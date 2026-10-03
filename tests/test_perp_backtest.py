"""Perp engine validation on synthetic data with KNOWN answers.

The engine must (1) find a planted short-side edge and not invent one from noise, (2) charge
funding with the right sign and size, (3) liquidate before the stop when that is what
would really happen, (4) size from risk with leverage as an output, and (5) never look ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_signal.backtest.events import run_event_study
from market_signal.perps.backtest import (
    PerpCosts,
    PerpInput,
    PerpRisk,
    PerpRules,
    choose_leverage,
    daily_funding,
    liquidation_price,
    maintenance_rate,
    perp_asset_events,
    side_forward_returns,
    simulate_perps,
)

NO_COSTS = PerpCosts(0.0, 0.0, 0.0)
COSTS = PerpCosts(4.5, 2.0, 10.0)


def frame(closes, funding=0.0, wick=0.0, start="2024-01-01"):
    c = np.asarray(closes, float)
    o = np.concatenate([[c[0]], c[:-1]])
    ts = pd.date_range(start, periods=len(c), freq="1D", tz="UTC")
    f = pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o,
                      "high": np.maximum(o, c) * (1 + wick), "low": np.minimum(o, c) * (1 - wick),
                      "close": c, "volume": 1.0})  # fmt: skip
    f["funding_day"] = funding if np.ndim(funding) else float(funding)
    return f


# ----------------------------------------------------------------- funding alignment


def test_daily_funding_window_and_missing_days():
    bars = frame([100, 100, 100, 100])
    t0 = bars["ts"].iloc[0]
    hours = pd.date_range(t0, t0 + pd.Timedelta(days=3), freq="1h", tz="UTC")  # incl. both ends
    f = pd.Series(0.0001, index=hours)
    f = f.drop(
        f.index[
            (f.index > t0 + pd.Timedelta(days=1)) & (f.index <= t0 + pd.Timedelta(days=1, hours=2))
        ]
    )
    f = f.drop(
        f.index[
            (f.index > t0 + pd.Timedelta(days=2)) & (f.index <= t0 + pd.Timedelta(days=2, hours=10))
        ]
    )
    out = daily_funding(bars, f, min_coverage=20 / 24)
    assert out[0] == pytest.approx(
        0.0024
    )  # 24 settlements in (ts, ts+1d]; the one AT ts is excluded
    assert out[1] == pytest.approx(0.0024)  # 22 settlements → scaled to a full day
    assert np.isnan(out[2])  # only 14 settlements → missing, never assumed 0
    assert np.isnan(out[3])  # no data after the last hour


# ----------------------------------------------------------------- forward returns


def test_forward_returns_charge_funding_with_the_right_sign():
    f = frame(np.full(40, 100.0), funding=0.001)  # flat price, longs pay 0.1%/day
    lng = side_forward_returns(f, {"1w": 7}, +1, COSTS)
    sht = side_forward_returns(f, {"1w": 7}, -1, COSTS)
    rt = 2 * COSTS.per_side
    assert lng["ret_1w"].iloc[0] == pytest.approx(-rt - 7 * 0.001)
    assert sht["ret_1w"].iloc[0] == pytest.approx(-rt + 7 * 0.001)
    assert lng["funding_1w"].iloc[0] == pytest.approx(0.007)
    assert sht["funding_1w"].iloc[0] == pytest.approx(-0.007)
    assert lng["ret_1w"].iloc[-7:].isna().all()  # no future bar → missing, never truncated


def test_forward_returns_short_side_price_and_excursions():
    f = frame(100 * 0.99 ** np.arange(30), wick=0.005)  # falls 1%/day
    sht = side_forward_returns(f, {"3d": 3}, -1, NO_COSTS)
    lng = side_forward_returns(f, {"3d": 3}, +1, NO_COSTS)
    assert sht["ret_3d"].iloc[5] == pytest.approx(1 - 0.99**3, rel=1e-9)
    assert lng["ret_3d"].iloc[5] == pytest.approx(0.99**3 - 1, rel=1e-9)
    assert sht["mae_3d"].iloc[5] < 0 < sht["mfe_3d"].iloc[5]  # adverse = up for a short


def test_missing_funding_inside_window_makes_return_missing():
    fd = np.full(30, 0.0005)
    fd[10] = np.nan
    r = side_forward_returns(frame(np.full(30, 100.0), funding=fd), {"1w": 7}, +1, NO_COSTS)[
        "ret_1w"
    ]
    assert r.iloc[3:10].isna().all() and r.iloc[2:3].notna().all() and r.iloc[10:15].notna().all()


# ----------------------------------------------------------------- event study: planted edge


def _market(seed, n=1500, plant=None):
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0005, 0.03, n)
    sig = np.zeros(n, bool)
    if plant is not None:
        idx = rng.choice(np.arange(50, n - 20), size=plant, replace=False)
        sig[idx] = True
        for i in idx:
            r[i + 1 : i + 8] -= 0.008  # the next week drifts down: a real short edge
    return frame(100 * np.exp(np.cumsum(r)), funding=0.0003, wick=0.01), pd.Series(sig)


def test_event_study_finds_planted_short_edge_and_rejects_noise():
    hz = {"1w": 7, "1m": 30}
    assets, noise = [], []
    for k, coin in enumerate(("AAA", "BBB", "CCC")):
        f, sig = _market(10 + k, plant=40)
        assets += perp_asset_events(coin, f, hz, COSTS, short_signal=sig)
        f2, _ = _market(50 + k)
        rnd = pd.Series(np.random.default_rng(90 + k).random(len(f2)) < 0.03)
        noise += perp_asset_events(coin, f2, hz, COSTS, short_signal=rnd)
    assert {a.symbol for a in assets} == {"AAA:short", "BBB:short", "CCC:short"}
    planted = run_event_study(assets, "1w", n_boot=400).summary.set_index("horizon").loc["1w"]
    assert planted["n_independent"] >= 60
    assert planted["excess_mean_indep"] > 0.03 and planted["p_value_random_entry"] < 0.01
    random_ = run_event_study(noise, "1w", n_boot=400).summary.set_index("horizon").loc["1w"]
    assert abs(random_["excess_mean_indep"]) < 0.02 and random_["p_value_random_entry"] > 0.05


# ----------------------------------------------------------------- leverage & liquidation maths


def test_leverage_is_an_output_of_the_stop():
    m = maintenance_rate(40)  # 1.25%
    assert m == pytest.approx(0.0125)
    assert choose_leverage(0.05, m, cap=3, venue_max=40, liq_buffer=2) == 3  # capped
    assert choose_leverage(0.30, 0.05, cap=3, venue_max=10, liq_buffer=2) == pytest.approx(1 / 0.65)
    assert (
        choose_leverage(0.60, 0.05, 3, 10, 2) is None
    )  # even 1x can't keep liq beyond 2x the stop
    assert choose_leverage(0.0, m, 3, 40, 2) is None
    # isolated long, 3x, maint 1.25%: liq = e(1 − 1/3)/(1 − m)
    assert liquidation_price(100, 1, 1.0, 100 / 3, m) == pytest.approx(100 * (2 / 3) / (1 - m))
    assert liquidation_price(100, -1, 1.0, 100 / 3, m) == pytest.approx(100 * (4 / 3) / (1 + m))


# ----------------------------------------------------------------- simulator


def _sim(
    f,
    side=1,
    stop=None,
    signal_bar=0,
    rules=PerpRules(max_hold_bars=10),
    costs=NO_COSTS,
    venue=40.0,
    risk=None,
):
    n = len(f)
    sig = pd.Series(np.arange(n) == signal_bar)
    stops = pd.Series(np.full(n, stop if stop is not None else np.nan))
    a = PerpInput("TST", f, costs, venue, setup="t",
                  long_signal=sig if side > 0 else None, short_signal=sig if side < 0 else None,
                  long_stop=stops if side > 0 else None, short_stop=stops if side < 0 else None)  # fmt: skip
    return simulate_perps([a], rules, risk or PerpRisk())


def test_short_trade_pnl_funding_received_and_sizing():
    closes = [100, 100, 98, 96, 94, 92, 90, 90, 90, 90, 90, 90, 90, 90]
    res = _sim(frame(closes, funding=0.001), side=-1, stop=105.0)
    t = res.trades.iloc[0]
    assert t["side"] == "short" and t["exit_reason"] == "time"
    assert t["notional"] == pytest.approx(0.005 * 100_000 / 0.05)  # risk 0.5% / 5% stop distance
    assert t["leverage"] == pytest.approx(3.0)
    assert t["funding_paid"] < 0  # shorts receive positive funding
    assert t["pnl"] > 0.09 * t["notional"]  # price fell 10%, plus funding received
    assert res.metrics["shorts"] == 1 and res.metrics["liquidations"] == 0


def test_stop_loss_costs_about_one_risk_unit():
    closes = [100, 100, 99, 97, 94, 90, 90]
    res = _sim(frame(closes, wick=0.002), side=1, stop=95.0, costs=COSTS)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert -1.35 < t["r_multiple"] < -1.0  # one R plus fees and stop slippage


def test_gap_through_liquidation_loses_the_margin_not_just_the_stop():
    closes = [
        100,
        100,
        100,
        55,
        55,
    ]  # bar 3 opens at 100 then closes 55; force a gap open below liq
    f = frame(closes)
    f.loc[3, "open"] = 55.0
    f.loc[3, ["high", "low"]] = [56.0, 54.0]
    res = _sim(f, side=1, stop=95.0)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "liquidation"
    assert t["pnl"] == pytest.approx(-t["margin"])  # the whole isolated margin is lost
    assert t["r_multiple"] < -5  # far worse than the planned 1R


def test_funding_erosion_moves_liquidation_inside_the_stop():
    # wide stop (25% away) at the max safe leverage, then heavy funding against the long
    n = 40
    closes = np.full(n, 100.0)
    closes[30:] = 100 * 0.985 ** np.arange(1, n - 29)  # slow slide after margin has eroded
    f = frame(closes, funding=0.01, wick=0.001)
    res = _sim(
        f,
        side=1,
        stop=75.0,
        rules=PerpRules(max_hold_bars=60),
        risk=PerpRisk(leverage_cap=3, liq_buffer=1.2),
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "liquidation"
    assert t["exit_price"] > 75.0  # liquidated ABOVE the stop: funding ate the buffer
    assert t["funding_paid"] > 0


def test_short_entry_gapping_through_stop_is_skipped():
    f = frame([100, 100, 110, 110, 110])
    f.loc[1, ["open", "high"]] = [106.0, 107.0]  # the bar after the signal opens above the stop
    res = _sim(f, side=-1, stop=105.0)
    assert res.trades.empty
    assert res.skipped["reason"].tolist() == ["gapped_through_stop"]


def test_unsafe_leverage_is_skipped():
    res = _sim(frame([100] * 6), side=1, stop=30.0, venue=10.0)  # 70% stop distance
    assert res.trades.empty and res.skipped["reason"].tolist() == ["unsafe_leverage"]


def test_no_lookahead_truncation_invariance():
    rng = np.random.default_rng(3)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, 400)))
    f = frame(closes, funding=rng.normal(0.0003, 0.0005, 400), wick=0.01)
    sig = pd.Series(rng.random(400) < 0.05)
    stop = f["close"] * 0.93

    def run(k):
        g = f.iloc[:k].reset_index(drop=True)
        a = PerpInput("TST", g, COSTS, 20.0, long_signal=sig.iloc[:k], long_stop=stop.iloc[:k])
        return simulate_perps([a], PerpRules(max_hold_bars=7), PerpRisk()).trades

    full, cut = run(400), run(250)
    cutoff = f["ts"].iloc[249]
    done = cut[(cut["exit_reason"] != "end_of_data")]
    ref = full[full["exit_time"] < cutoff].head(len(done))
    pd.testing.assert_frame_equal(done.reset_index(drop=True)[["entry_time", "exit_time", "exit_reason", "pnl"]],
                                  ref.reset_index(drop=True)[["entry_time", "exit_time", "exit_reason", "pnl"]])  # fmt: skip


def test_engine_runs_on_stored_perp_data(tmp_path):
    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db
    from market_signal.perps.backtest import load_perp_input

    s = get_settings()
    st = Store(tmp_path / "d.duckdb")
    try:
        build_demo_db(s, st, years=2, seed=4)
        a = load_perp_input(st, s, "BTC")
        assert a is not None and a.venue_max_leverage == 40.0  # from the stored snapshot
        assert a.frame["funding_day"].notna().mean() > 0.95  # hourly funding aligned to daily bars
        n = len(a.frame)
        a.long_signal = pd.Series(np.arange(n) % 45 == 0)
        a.long_stop = a.frame["close"] * 0.92
        a.short_signal = pd.Series(np.arange(n) % 45 == 20)
        a.short_stop = a.frame["close"] * 1.08
        res = simulate_perps(
            [a], PerpRules(max_hold_bars=14), PerpRisk.from_config(s.yaml("perps.yaml"))
        )
        assert res.metrics["longs"] > 5 and res.metrics["shorts"] > 5
        assert (res.trades["leverage"] <= 3.0 + 1e-9).all()
        assert np.isfinite(res.metrics["max_drawdown"]) and res.equity.iloc[0] > 0
    finally:
        st.close()
