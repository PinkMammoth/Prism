"""Phase 3: backtest timing, no-look-ahead proofs, costs, robustness tooling."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_signal.backtest.engine import AssetInput, ExitRules, Sizing, simulate
from market_signal.backtest.events import (
    AssetEvents,
    Costs,
    baseline_bars,
    decluster,
    forward_returns,
    run_event_study,
)
from market_signal.backtest.robustness import (
    make_folds,
    plateau_verdict,
    walk_forward,
    window_excess,
)
from tests.conftest import make_daily_crypto

NO_COST = Costs(0.0, 0.0)
H = {"1d": 1, "1w": 7, "1m": 30}


def feat_from(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    f["close_time"] = f["ts"] + pd.Timedelta(days=1)
    for c in ("open", "high", "low", "close"):
        f[f"tr_{c}"] = f[c]
    return f


def manual_bars(opens, highs, lows, closes) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    return feat_from(
        pd.DataFrame(
            {"ts": ts, "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}
        )
    )


# --------------------------------------------------------------------------- forward returns


def test_forward_return_enters_next_open_exits_close():
    f = manual_bars([10, 11, 12, 13], [10, 12, 13, 14], [10, 10, 11, 12], [10, 11.5, 12.5, 13.5])
    fwd = forward_returns(f, {"1d": 1, "2d": 2}, Costs(10, 0))
    c = 10 / 1e4
    assert fwd["ret_1d"].iloc[0] == pytest.approx(11.5 * (1 - c) / (11 * (1 + c)) - 1)
    assert fwd["ret_2d"].iloc[0] == pytest.approx(12.5 * (1 - c) / (11 * (1 + c)) - 1)
    assert np.isnan(fwd["ret_2d"].iloc[2])  # t+2 does not exist: NaN, not truncated
    assert fwd["mae_2d"].iloc[0] == pytest.approx(10 / (11 * (1 + c)) - 1)


def test_same_close_fill_is_impossible():
    """A series that gaps up every night: a same-close-fill engine would book the gap;
    next-open execution must not."""
    n = 60
    closes = np.full(n, 100.0)
    opens = np.full(n, 110.0)  # every bar opens 10% above the prior close... then falls back
    opens[0] = 100
    f = manual_bars(opens, np.maximum(opens, closes), np.minimum(opens, closes), closes)
    fwd = forward_returns(f, {"1d": 1}, NO_COST)
    assert fwd["ret_1d"].dropna().max() < 0  # buying at the open loses; the gap is not captured


# --------------------------------------------------------------------------- event study honesty


def _assets(n_assets=6, n=1500, planted=False, seed=0):
    out = []
    for k in range(n_assets):
        df = make_daily_crypto(n, seed=seed + k)
        f = feat_from(df)
        fwd = forward_returns(f, H, NO_COST)
        rng = np.random.default_rng(100 + k)
        sig = pd.Series(rng.random(n) < 0.03)
        if planted:  # peeks one bar ahead -> must show a large 1d edge
            sig = (f["close"].shift(-1) > f["open"].shift(-1)) & (rng.random(n) < 0.3)
        out.append(AssetEvents(f"A{k}", "crypto", f, fwd, sig, pd.Series(True, index=f.index), H))
    return out


def test_random_signals_show_no_edge():
    res = run_event_study(_assets(), primary="1m", n_boot=500, seed=1)
    row = res.summary.set_index("horizon").loc["1m"]
    assert abs(row["excess_mean_indep"]) < 0.03
    assert row["p_value_random_entry"] > 0.01


def test_planted_edge_is_detected_at_correct_horizon():
    res = run_event_study(_assets(planted=True), primary="1d", n_boot=500, seed=1)
    s = res.summary.set_index("horizon")
    assert s.loc["1d", "excess_mean_indep"] > 0.01
    assert s.loc["1d", "p_value_random_entry"] < 0.01


def test_decluster_non_overlapping():
    assert decluster(np.array([0, 2, 5, 6, 12]), 5).tolist() == [0, 5, 12]


# --------------------------------------------------------------------------- trade simulator


def _single(f, signal_bars, stop, rules=None, sizing=None, costs=NO_COST):
    sig = pd.Series(False, index=f.index)
    sig.iloc[signal_bars] = True
    stops = pd.Series(stop, index=f.index, dtype=float)
    a = AssetInput("X", "crypto", f, sig, stops, costs)
    return simulate(
        [a],
        rules or ExitRules(max_hold_bars=100),
        sizing or Sizing(method="fixed", fixed_fraction=0.5),
    )


def test_entry_at_next_open_with_slippage():
    f = manual_bars(
        [100, 105, 106, 107], [101, 106, 107, 108], [99, 104, 105, 106], [100, 105.5, 106.5, 107.5]
    )
    res = _single(f, [0], stop=50.0, costs=Costs(0, 10))
    t = res.trades.iloc[0]
    assert t["entry_price"] == pytest.approx(105 * 1.001)
    assert t["entry_time"] == f["ts"].iloc[1]


def test_skip_entry_when_gapping_through_stop():
    f = manual_bars([100, 90, 91], [101, 92, 92], [99, 89, 90], [100, 91, 91])
    res = _single(f, [0], stop=95.0)
    assert res.trades.empty
    assert res.skipped.iloc[0]["reason"] == "gapped_through_stop"


def test_gap_through_stop_fills_at_open():
    f = manual_bars([100, 100, 80, 81], [101, 101, 82, 82], [99, 99, 79, 80], [100, 100, 81, 81])
    res = _single(f, [0], stop=95.0)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 80  # not the 95 stop


def test_stop_first_when_stop_and_target_same_bar():
    f = manual_bars([100, 100, 100], [101, 101, 130], [99, 99, 90], [100, 100, 100])
    res = _single(f, [0], stop=95.0, rules=ExitRules(max_hold_bars=50, target_r=2.0))
    assert res.trades.iloc[0]["exit_reason"] == "stop"


def test_time_exit_at_next_open():
    closes = np.linspace(100, 110, 12)
    f = manual_bars(closes - 0.1, closes + 1, closes - 1, closes)
    res = _single(f, [0], stop=50.0, rules=ExitRules(max_hold_bars=3))
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "time"
    assert t["exit_time"] == f["ts"].iloc[4]  # entered bar 1, held bars 1-3, exit bar 4 open
    assert t["exit_price"] == pytest.approx(f["open"].iloc[4])


def test_risk_sizing_and_no_leverage():
    n = 300
    assets = []
    for k in range(10):
        f = feat_from(make_daily_crypto(n, seed=k))
        sig = pd.Series(False, index=f.index)
        sig.iloc[[50, 51, 52]] = True
        stop = f["close"] * 0.99  # tight stop -> risk sizing would want huge positions
        assets.append(AssetInput(f"A{k}", "crypto", f, sig, stop, NO_COST))
    res = simulate(
        assets, ExitRules(max_hold_bars=20), Sizing(risk_per_trade=0.01, max_position_fraction=0.25)
    )
    assert res.exposure.max() <= 1.0 + 1e-9
    assert (res.trades["alloc"] <= 0.25 * 100_000 * 1.2).all()
    assert "no_capital" in res.metrics["skipped"]


def test_dividend_is_credited_via_total_return_basis():
    f = manual_bars(
        [100, 100, 100, 100], [100, 100, 100, 100], [100, 100, 100, 100], [100, 100, 100, 100]
    )
    f["tr_close"] = [98.0, 98.0, 100.0, 100.0]  # 2% dividend ex on bar 2
    res = _single(f, [0], stop=50.0, rules=ExitRules(max_hold_bars=2))
    assert res.trades.iloc[0]["ret"] == pytest.approx(100 / 98 - 1)


def test_simulation_truncation_invariance():
    """Trades that closed before bar k are identical whether or not later data exists."""
    f = feat_from(make_daily_crypto(400, seed=9))
    rng = np.random.default_rng(3)
    sig = pd.Series(rng.random(400) < 0.05)
    stop = f["close"] * 0.9
    full = simulate(
        [AssetInput("X", "crypto", f, sig, stop, NO_COST)], ExitRules(max_hold_bars=10), Sizing()
    )
    k = 250
    part = simulate(
        [AssetInput("X", "crypto", f.iloc[:k], sig.iloc[:k], stop.iloc[:k], NO_COST)],
        ExitRules(max_hold_bars=10),
        Sizing(),
    )
    cutoff = f["ts"].iloc[k - 1]
    a = full.trades[(full.trades.exit_time < cutoff) & (full.trades.exit_reason != "end_of_data")]
    b = part.trades[(part.trades.exit_time < cutoff) & (part.trades.exit_reason != "end_of_data")]
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


# --------------------------------------------------------------------------- robustness


def test_make_folds_anchored():
    folds = make_folds(
        pd.Timestamp("2014-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC"), 4, 2
    )
    assert len(folds) == 3
    assert folds[0].train_start == folds[2].train_start  # anchored
    assert folds[1].test_start == folds[0].test_end


def test_walk_forward_limits_params_and_purges():
    assets = _assets(n_assets=3, n=2500)
    base = baseline_bars(assets)

    def events_for(params):
        res = run_event_study(assets, primary="1m", n_boot=10)
        return res.events

    default = {"a": 1, "b": 1, "c": 1}
    with pytest.raises(ValueError):
        walk_forward(
            events_for,
            base,
            [{"a": 2, "b": 2, "c": 2}],
            default,
            [],
            "1m",
            30,
            pd.Timedelta(days=45),
        )
    start, end = base.signal_time.min(), base.signal_time.max()
    folds = make_folds(start, end, 2, 1)
    table, summary = walk_forward(
        events_for,
        base,
        [default],
        default,
        folds,
        "1m",
        30,
        pd.Timedelta(days=45),
        min_train_events=5,
    )
    assert len(table) == len(folds) and summary["folds"] == len(folds)
    # window excess uses only that window's baseline
    w = window_excess(events_for(default), base, "1m", start, end, 30)
    assert abs(w["w_excess"].mean()) < 0.05


def test_plateau_and_fragile_verdicts():
    axes = {"x": [1, 2, 3, 4, 5]}
    plateau = pd.DataFrame(
        {
            "x": [1, 2, 3, 4, 5],
            "n_indep": [80] * 5,
            "excess_mean": [0.010, 0.012, 0.013, 0.011, 0.009],
        }
    )
    fragile = pd.DataFrame(
        {
            "x": [1, 2, 3, 4, 5],
            "n_indep": [80] * 5,
            "excess_mean": [-0.01, -0.02, 0.05, -0.01, -0.02],
        }
    )
    assert plateau_verdict(plateau, {"x": 3}, axes, 30)["verdict"] == "PLATEAU"
    assert plateau_verdict(fragile, {"x": 3}, axes, 30)["verdict"] == "FRAGILE"
    few = plateau.assign(n_indep=10)
    assert plateau_verdict(few, {"x": 3}, axes, 30)["verdict"] == "INSUFFICIENT"


def test_equity_curve_timestamps_unit_agnostic():
    """Regression: microsecond-unit timestamps (as loaded from DuckDB) keep real dates."""
    f = feat_from(make_daily_crypto(100, start="2021-03-01", seed=2))
    for c in ("ts", "close_time"):
        f[c] = f[c].astype("datetime64[us, UTC]")
    res = _single(f, [10], stop=1.0)
    assert res.equity.index[0].year == 2021
    assert res.metrics["days"] > 90
