"""Daily feature compiler: vocabulary, warmup, Prism parity, causality and snapshot isolation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.data.prices import PriceBasis, adjustment_factors, apply_basis
from market_signal.data.store import Store
from market_signal.indicators import technical as ta
from market_signal.models.domain import AssetClass, Calendar
from market_signal.perps.backtest import daily_funding
from market_signal.perps.strategies import funding_percentile, get_strategy, sma_atr
from market_signal.research.lab import features as lab_features
from market_signal.research.lab.compiler import (
    CompileError,
    Snapshot,
    compile_registered,
    compile_strategy,
    edge_signal,
    evaluate_condition,
)
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset, snapshot_rows
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.spec import Hypothesis, StrategyDefinition
from market_signal.research.lab.vocabulary import FAMILIES, catalog, parse_feature
from market_signal.setups.base import edge_trigger
from tests.conftest import make_daily_crypto

START = datetime(2023, 1, 1, tzinfo=UTC)
N = 420
T = 300  # cut-off bar for the causality tests


# --------------------------------------------------------------------------- fixtures


def _bars(n: int = N, seed: int = 0) -> pd.DataFrame:
    df = make_daily_crypto(n=n, start="2023-01-01", seed=seed)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    return df


def _funding(bars: pd.DataFrame, seed: int = 1, every_hours: int = 1) -> pd.DataFrame:
    """Settlements every ``every_hours`` inside (ts, ts + 1d], available when settled."""
    rng = np.random.default_rng(seed)
    times = [
        ts + pd.Timedelta(hours=h) for ts in bars["ts"] for h in range(every_hours, 25, every_hours)
    ]
    rates = rng.normal(1e-5, 2e-5, len(times))
    return pd.DataFrame({"time": times, "funding_rate": rates, "available_at": times})


def _insert_perp(store: Store, bars: pd.DataFrame, funding: pd.DataFrame | None, coin="BTC"):
    b = bars.assign(coin=coin, timeframe="1d", source="hyperliquid", ingest_run_id="fixture")
    b["ingested_at"] = START
    store.con.register("_b", b)
    store.con.execute(
        "INSERT INTO perp_bars SELECT coin, timeframe, source, ts, open, high, low, close, "
        "volume, close_time, ingested_at, ingest_run_id FROM _b"
    )
    store.con.unregister("_b")
    if funding is not None:
        f = funding.assign(coin=coin, source="hyperliquid", premium=None, ingest_run_id="fixture")
        f["pit_method"] = "market_close"
        store.con.register("_f", f)
        store.con.execute(
            "INSERT INTO perp_funding SELECT coin, source, time, funding_rate, "
            "CAST(premium AS DOUBLE), available_at, pit_method, ingest_run_id FROM _f"
        )
        store.con.unregister("_f")


def _selections(kinds=("perp_bars", "perp_funding"), symbol="BTC", source="hyperliquid"):
    return tuple(
        SeriesSelection(
            kind=kind,
            symbol=symbol,
            source=source,
            timeframe="1d" if kind in ("bars", "perp_bars") else None,
            start=START - timedelta(days=5),
            end=START + timedelta(days=N + 5),
        )
        for kind in kinds
    )


def _snapshot(store: Store, selections) -> Snapshot:
    capture = capture_dataset(store, selections)
    blobs = dict(capture.blobs)
    rows = tuple(snapshot_rows(s, blobs[s.sha256]) for s in capture.manifest.series)
    return Snapshot(capture.manifest.dataset_id, capture.manifest, rows)


def _definition(conditions, *, market="perp", side="long", cooldown=0, atr="perp_sma_14"):
    def ref(name):
        return {"name": name, "timeframe": "1d"}

    return StrategyDefinition.model_validate(
        {
            "market": market,
            "side": side,
            "trigger_timeframe": "1d",
            "conditions": [
                {
                    "left": ref(left),
                    "op": op,
                    "right": ref(right) if isinstance(right, str) else right,
                }
                for left, op, right in conditions
            ],
            "cooldown_bars": cooldown,
            "exit": {"atr": atr, "stop_atr": 2.0, "max_hold_bars": 10},
        }
    )


@pytest.fixture
def perp(store):
    bars = _bars()
    _insert_perp(store, bars, _funding(bars))
    return store


def _frame(n=N, seed=0) -> pd.DataFrame:
    """In-memory feature input with clean daily funding."""
    f = _bars(n, seed)
    f["funding_day"] = np.random.default_rng(seed + 7).normal(2e-4, 3e-4, n)
    return f


REPRESENTATIVE = sorted(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ret_1",
        "ret_5",
        "roc_1m",
        "range_pct",
        "sma_20",
        "ema_21",
        "dist_sma_50",
        "dist_ema_20",
        "rsi_14",
        "atr_14",
        "atr_sma_14",
        "atr_pct_14",
        "rvol_20",
        "donchian_high_20",
        "donchian_low_20",
        "close_high_20",
        "close_low_20",
        "dist_donchian_high_20",
        "dist_donchian_low_20",
        "vol_sma_20",
        "rel_volume",
        "rel_volume_10",
        "vol_z_20",
        "funding_day",
        "funding_sum_7",
        "funding_mean_7",
        "funding_pct_7_60",
    }
)


# --------------------------------------------------------------------------- vocabulary


def test_every_family_has_one_implementation_and_is_exercised():
    assert lab_features.IMPLEMENTATIONS.keys() == FAMILIES.keys()
    assert {parse_feature(n).family for n in REPRESENTATIVE} == set(FAMILIES)
    assert [c["family"] for c in catalog()] == list(FAMILIES)


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("rel_volume_20", "not canonical"),
        ("ema_050", "leading zeros"),
        ("ema_1", "must be in"),
        ("sma", "requires parameters"),
        ("sma_20_5", "expects"),
        ("funding_percentile", "unknown"),
        ("oi_change_24h", "unknown"),
        ("future_return_5", "unknown"),
        ("close_1", "expects"),
    ],
)
def test_feature_tokens_have_one_canonical_spelling(name, error):
    with pytest.raises(ValueError, match=error):
        parse_feature(name)


def test_original_v1_names_keep_their_meaning():
    for name in ("close", "sma_20", "sma_50", "sma_200", "ema_21", "rsi_14", "rel_volume"):
        assert parse_feature(name).name == name
    assert parse_feature("rel_volume").params == (20,)
    assert parse_feature("roc_1m").spec.daily_only
    assert parse_feature("funding_day").spec.market == "perp"


def test_spec_accepts_parameterised_features_and_crossovers():
    d = _definition(
        [("close", "crosses_above", "donchian_high_20"), ("funding_pct_7_365", "lt", 0.9)]
    )
    assert d.strategy_id.startswith("strategy_")
    with pytest.raises(ValidationError, match="requires the perp"):
        _definition([("funding_pct_7_365", "lt", 0.9)], market="spot", atr="wilder_14")
    with pytest.raises(ValidationError, match="itself"):
        _definition([("close", "gt", "close")])
    with pytest.raises(ValidationError):
        _definition([("close", "eq", 1.0)])
    raw = d.model_dump(mode="json")
    raw["conditions"][1]["left"]["timeframe"] = "4h"
    raw["trigger_timeframe"] = "4h"
    with pytest.raises(ValidationError, match="only on daily"):
        StrategyDefinition.model_validate(raw)


# --------------------------------------------------------------------------- warmup


@pytest.mark.parametrize("name", REPRESENTATIVE)
def test_declared_warmup_is_the_first_valid_bar(name):
    key = parse_feature(name)
    values = lab_features.compute(key, _frame(), AssetClass.CRYPTO)
    first = int(np.flatnonzero(np.isfinite(values.to_numpy()))[0])
    assert first == lab_features.warmup(key, AssetClass.CRYPTO) - 1


def test_roc_1m_needs_an_asset_class():
    with pytest.raises(ValueError, match="asset class"):
        lab_features.compute(parse_feature("roc_1m"), _frame(), None)
    assert lab_features.warmup(parse_feature("roc_1m"), AssetClass.EQUITY) == 22


# --------------------------------------------------------------------------- hand calculations


def test_small_hand_calculations():
    f = pd.DataFrame(
        {
            "open": [1.0, 2, 3, 4, 5],
            "high": [10.0, 12, 11, 15, 13],
            "low": [8.0, 9, 7, 10, 12],
            "close": [9.0, 11, 10, 14, 12],
            "volume": [100.0, 100, 200, 100, 400],
        }
    )

    def calc(name):
        return lab_features.compute(parse_feature(name), f, None).tolist()

    assert calc("ret_1")[1:] == pytest.approx([11 / 9 - 1, 10 / 11 - 1, 14 / 10 - 1, 12 / 14 - 1])
    # prior 2 highs, today's excluded: bar 3 (high 15) compares with max(12, 11) = 12
    assert calc("donchian_high_2")[2:] == [12.0, 12.0, 15.0]
    assert calc("donchian_low_2")[2:] == [8.0, 7.0, 7.0]
    assert calc("close_high_2")[2:] == [11.0, 11.0, 14.0]
    assert calc("range_pct")[0] == pytest.approx(2 / 9)
    # vol_z over the prior 3 bars: bar 3 vs [100, 100, 200]
    prior = np.array([100.0, 100, 200])
    assert calc("vol_z_3")[3] == pytest.approx((100 - prior.mean()) / prior.std(ddof=1))
    # constant prior volume has no dispersion: undefined rather than infinite
    assert np.isnan(calc("vol_z_2")[2])


# --------------------------------------------------------------------------- Prism parity


def test_parity_with_prism_daily_feature_frame():
    bars = _bars()
    prism = ta.compute_features(bars, AssetClass.CRYPTO)
    f = _frame()

    def lab(name):
        return lab_features.compute(parse_feature(name), f, AssetClass.CRYPTO).to_numpy()

    for name, column in [
        ("sma_20", "sma_20"),
        ("sma_50", "sma_50"),
        ("sma_200", "sma_200"),
        ("ema_21", "ema_21"),
        ("rsi_14", "rsi_14"),
        ("atr_14", "atr_14"),
        ("atr_pct_14", "atr_pct"),
        ("roc_1m", "roc_1m"),
        ("dist_sma_50", "dist_sma_50"),
        ("vol_sma_20", "vol_sma_20"),
        ("rel_volume", "rel_volume"),
    ]:
        # Same functions on the same input: bit-identical, NaN in the same places.
        np.testing.assert_array_equal(lab(name), prism[column].to_numpy(), err_msg=name)
    # Prism annualises with the class year; the Lab deliberately does not.
    np.testing.assert_allclose(
        lab("rvol_20") * np.sqrt(365), prism["rvol_20"].to_numpy(), rtol=1e-12
    )


def test_parity_with_perp_strategy_building_blocks():
    f = _frame()

    def lab(name):
        return lab_features.compute(parse_feature(name), f, AssetClass.CRYPTO).to_numpy()

    np.testing.assert_array_equal(lab("atr_sma_14"), sma_atr(f, 14).to_numpy())
    # trend_ls prior closing high / breakout_ls prior range, as written in perps/strategies.py
    np.testing.assert_array_equal(lab("close_high_20"), f["close"].rolling(20).max().shift(1))
    np.testing.assert_array_equal(lab("close_low_20"), f["close"].rolling(20).min().shift(1))
    np.testing.assert_array_equal(lab("donchian_high_30"), f["high"].rolling(30).max().shift(1))
    np.testing.assert_array_equal(lab("donchian_low_30"), f["low"].rolling(30).min().shift(1))
    np.testing.assert_array_equal(
        lab("funding_pct_7_60"), funding_percentile(f, 7, 60, min_history=60).to_numpy()
    )


def test_funding_day_matches_prism_on_a_constant_cadence():
    bars = _bars(60)
    for every in (1, 8):
        funding = _funding(bars, every_hours=every)
        prism = daily_funding(
            bars, pd.Series(funding["funding_rate"].to_numpy(), index=funding["time"])
        )
        lab = lab_features.lab_funding_day(bars, funding)
        np.testing.assert_array_equal(lab, prism)
        assert np.isfinite(lab).all()


def test_edge_signal_equals_edge_trigger_when_everything_is_eligible():
    rng = np.random.default_rng(3)
    active = pd.Series(rng.random(500) < 0.3)
    # Only difference: edge_trigger fires on an active bar 0, whose previous state is
    # unobservable; the Lab never does. Away from bar 0 the semantics are identical.
    active.iloc[0] = False
    eligible = pd.Series(True, index=active.index)
    for cooldown in (0, 1, 5):
        expected = edge_trigger(active, cooldown)
        pd.testing.assert_series_equal(edge_signal(active, eligible, cooldown), expected)


def test_compiled_trend_conjunction_matches_trend_ls(perp):
    snapshot = _snapshot(perp, _selections(("perp_bars",)))
    p = {**get_strategy("trend_ls").defaults, "slow": 100}
    d = _definition(
        [
            ("close", "gt", "sma_100"),
            ("sma_50", "gt", "sma_100"),
            ("close", "gt", "close_high_20"),
        ]
    )
    out = compile_strategy(d, snapshot, "BTC")
    c = out.inputs["close"]
    slow = c.rolling(p["slow"]).mean()
    up = (c > slow) & (c.rolling(p["fast"]).mean() > slow) & (c > c.rolling(20).max().shift(1))
    pd.testing.assert_series_equal(out.active, (up & out.eligible).rename("active"))
    assert out.metadata.warmup_bars == 100 and out.eligible.sum() == N - 99


# --------------------------------------------------------------------------- causality


@pytest.mark.parametrize("name", REPRESENTATIVE)
def test_feature_values_ignore_mutated_future_rows(name):
    base = _frame()
    mutated = base.copy()
    later = mutated.index > T
    for col in ("open", "high", "low", "close", "volume"):
        mutated.loc[later, col] *= 7.0
    mutated.loc[later, "funding_day"] = 0.5
    key = parse_feature(name)
    a = lab_features.compute(key, base, AssetClass.CRYPTO).iloc[: T + 1]
    b = lab_features.compute(key, mutated, AssetClass.CRYPTO).iloc[: T + 1]
    pd.testing.assert_series_equal(a, b)
    truncated = lab_features.compute(key, base.iloc[: T + 1], AssetClass.CRYPTO)
    pd.testing.assert_series_equal(a, truncated)


def _assert_prefix_equal(a, b, cutoff):
    for field in ("features", "conditions", "condition_defined", "eligible", "active", "signal"):
        x, y = getattr(a, field), getattr(b, field)
        pd.testing.assert_frame_equal(
            pd.DataFrame(x).loc[:cutoff], pd.DataFrame(y).loc[:cutoff], obj=field
        )
    pd.testing.assert_series_equal(a.stop.loc[:cutoff], b.stop.loc[:cutoff])


def test_compiled_masks_ignore_mutated_and_truncated_future_snapshot_rows(perp):
    d = _definition(
        [
            ("close", "crosses_above", "ema_20"),
            ("funding_pct_3_30", "lt", 0.8),
            ("rsi_14", "gt", 40),
        ]
    )
    # 8-hourly funding throughout (Binance-like) before the mutation.
    perp.con.execute("DELETE FROM perp_funding WHERE extract('hour' FROM time) % 8 != 0")
    before = compile_strategy(d, _snapshot(perp, _selections()), "BTC")
    cutoff = before.signal.index[T]
    assert before.signal.loc[:cutoff].sum() > 0
    assert before.feature_valid.loc[:cutoff, "funding_pct_3_30"].sum() > 200
    # Rewrite every price and funding row after the cut-off, and switch to half-hourly
    # funding there. Those settlements now dominate the series, so Prism's full-history
    # cadence estimate (48/day) would void every earlier 3/day bar; the causal one must not.
    perp.con.execute(
        "UPDATE perp_bars SET close=close*3, high=high*4, low=low*0.5 WHERE ts>?",
        [cutoff - timedelta(days=1)],
    )
    perp.con.execute("UPDATE perp_funding SET funding_rate=0.01 WHERE time>?", [cutoff])
    perp.con.execute(
        "INSERT INTO perp_funding SELECT coin, source, time + to_minutes(m), funding_rate, "
        "premium, available_at + to_minutes(m), pit_method, ingest_run_id FROM perp_funding, "
        "range(30, 480, 30) AS r(m) WHERE time>?",
        [cutoff],
    )
    after = compile_strategy(d, _snapshot(perp, _selections()), "BTC")
    assert after.metadata.dataset_id != before.metadata.dataset_id
    _assert_prefix_equal(before, after, cutoff)
    assert not before.features.loc[cutoff:].iloc[1:].equals(after.features.loc[cutoff:].iloc[1:])

    selections = tuple(
        s.model_copy(update={"end": cutoff + timedelta(hours=1)}) for s in _selections()
    )
    truncated = compile_strategy(d, _snapshot(perp, selections), "BTC")
    assert truncated.signal.index[-1] == cutoff
    _assert_prefix_equal(before, truncated, cutoff)


def test_breakout_threshold_excludes_the_current_bar():
    f = _frame(60)
    f.loc[40, "high"] = f["high"].max() * 2  # an extreme new high at bar 40
    f.loc[40, "close"] = f.loc[40, "high"] * 0.99
    level = lab_features.compute(parse_feature("donchian_high_20"), f, None)
    assert level[40] == f["high"].iloc[20:40].max() < f.loc[40, "high"]
    assert level[41] == f.loc[40, "high"]  # it only enters the threshold from the next bar
    dist = lab_features.compute(parse_feature("dist_donchian_high_20"), f, None)
    assert dist[40] > 0  # a close above a threshold built from its own bar is impossible


def test_rolling_percentile_is_not_rescaled_by_later_extremes():
    f = _frame(200)
    shocked = f.copy()
    shocked.loc[150:, "funding_day"] = 1.0  # extreme later funding
    key = parse_feature("funding_pct_3_30")
    a = lab_features.compute(key, f, None)
    b = lab_features.compute(key, shocked, None)
    pd.testing.assert_series_equal(a.iloc[:150], b.iloc[:150])
    assert b.iloc[150] == 1.0  # the shock only affects values whose window includes it


def test_crossover_fires_only_when_the_relation_changes():
    f = pd.DataFrame({"close": [1.0, 2, 3, 5, 6, 4, 6, np.nan, 7, 8]})
    feats = pd.DataFrame({"close": f["close"]})
    d = _definition([("close", "crosses_above", 4.5)])
    truth, defined = evaluate_condition(d.conditions[0], feats)
    assert truth.tolist() == [False, False, False, True, False, False, True, False, False, False]
    # bar 0 has no previous bar; bars 7 and 8 touch the NaN, so they are undefined
    assert defined.tolist() == [False, True, True, True, True, True, True, False, False, True]


def test_missing_inputs_are_ineligible_not_false(perp):
    # Remove a funding day in the middle: funding features become undefined there.
    gap = START + timedelta(days=250)
    perp.con.execute(
        "DELETE FROM perp_funding WHERE time>? AND time<=?", [gap, gap + timedelta(days=1)]
    )
    d = _definition([("funding_day", "gt", -1.0)])  # always true where defined
    out = compile_strategy(d, _snapshot(perp, _selections()), "BTC", asset_class=None)
    bar = out.eligible.index[250]
    assert np.isnan(out.features.loc[bar, "funding_day"])
    assert not out.feature_valid.loc[bar, "funding_day"]
    assert not out.condition_defined.loc[bar].iloc[0] and not out.eligible.loc[bar]
    # a condition already true when data becomes valid again has no observed rising edge
    assert out.active.iloc[251] and not out.signal.iloc[251]
    assert out.metadata.eligible_bars == N - 13 - 1  # atr_sma_14 warmup (13 bars), gap bar
    assert out.metadata.signals == 0


def test_late_published_funding_is_unavailable(perp):
    day = START + timedelta(days=200)
    perp.con.execute(
        "UPDATE perp_funding SET available_at = available_at + INTERVAL 2 DAY WHERE time=?",
        [day + timedelta(hours=5)],
    )
    out = compile_strategy(
        _definition([("funding_day", "gt", -1.0)]), _snapshot(perp, _selections()), "BTC"
    )
    assert np.isnan(out.features["funding_day"].iloc[200])
    assert np.isfinite(out.features["funding_day"].iloc[199])


# --------------------------------------------------------------------------- spot


def _insert_spot(store, bars, split_on: date | None):
    raw = bars.copy()
    raw["ts"] = raw["ts"] + pd.Timedelta(hours=14, minutes=30)
    raw["close_time"] = raw["ts"] + pd.Timedelta(hours=6, minutes=30)
    if split_on is not None:
        after = raw["ts"].dt.date >= split_on
        for col in ("open", "high", "low", "close"):
            raw.loc[after, col] /= 2.0
        raw.loc[after, "volume"] *= 2.0
        store.con.execute(
            "INSERT INTO corporate_actions VALUES ('AAPL','tiingo',?,2.0,0.0,'fixture')",
            [split_on],
        )
    raw = raw.assign(symbol="AAPL", timeframe="1d", source="tiingo", ingest_run_id="fixture")
    raw["ingested_at"] = START
    store.con.register("_s", raw)
    store.con.execute(
        "INSERT INTO bars SELECT symbol, timeframe, source, ts, close_time, open, high, low, "
        "close, volume, ingest_run_id, ingested_at FROM _s"
    )
    store.con.unregister("_s")
    return raw


def test_spot_forward_split_adjustment_is_causal_and_ratio_compatible(project):
    d = _definition(
        [("dist_sma_50", "gt", 0.0), ("ret_1", "gt", -0.2)], market="spot", atr="wilder_14"
    )
    split_on = (START + timedelta(days=T + 30)).date()
    outs = []
    for i, split in enumerate((None, split_on)):
        store = Store(project / "data" / f"spot{i}.duckdb", project / "data" / "raw")
        try:
            raw = _insert_spot(store, _bars(), split)
            snapshot = _snapshot(
                store, _selections(("bars", "corporate_actions"), "AAPL", "tiingo")
            )
            with pytest.raises(CompileError, match="asset class"):
                compile_strategy(d, snapshot, "AAPL")
            outs.append(compile_strategy(d, snapshot, "AAPL", asset_class=AssetClass.EQUITY))
        finally:
            store.close()
    clean, split = outs
    assert split.metadata.price_basis == "split_forward"
    # A split after T cannot change anything at or before T, including price levels.
    cutoff = clean.signal.index[T]
    _assert_prefix_equal(clean, split, cutoff)
    # The adjusted series is continuous across the split, and ratio features match
    # Prism's backward-adjusted research basis.
    assert abs(split.features["ret_1"].iloc[T + 30]) < 0.2
    bars = raw.reset_index(drop=True)
    actions = pd.DataFrame({"date": [split_on], "split_factor": [2.0], "dividend": [0.0]})
    prism = ta.compute_features(
        apply_basis(bars, adjustment_factors(bars, actions, Calendar.NYSE), PriceBasis.SPLIT),
        AssetClass.EQUITY,
    )
    np.testing.assert_allclose(
        split.features["dist_sma_50"].to_numpy(), prism["dist_sma_50"].to_numpy(), rtol=1e-12
    )


# --------------------------------------------------------------------------- inputs & isolation


def test_explicit_failures_instead_of_fallbacks(perp):
    bars_only = _snapshot(perp, _selections(("perp_bars",)))
    with pytest.raises(CompileError, match="perp_funding series"):
        compile_strategy(_definition([("funding_day", "gt", 0.0)]), bars_only, "BTC")
    with pytest.raises(CompileError, match="no perp_bars series for ETH"):
        compile_strategy(_definition([("close", "gt", 1.0)]), bars_only, "ETH")
    with pytest.raises(CompileError, match="crypto only"):
        compile_strategy(
            _definition([("close", "gt", 1.0)]), bars_only, "BTC", asset_class=AssetClass.EQUITY
        )
    raw = _definition([("close", "gt", 1.0)]).model_dump(mode="json")
    raw["trigger_timeframe"] = "4h"
    raw["conditions"][0]["left"]["timeframe"] = "4h"
    with pytest.raises(CompileError, match="daily definitions only"):
        compile_strategy(StrategyDefinition.model_validate(raw), bars_only, "BTC")
    spot = _definition([("close", "gt", 1.0)], market="spot", atr="wilder_14")
    with pytest.raises(CompileError, match="no bars series"):
        compile_strategy(spot, bars_only, "BTC", asset_class=AssetClass.CRYPTO)


def test_compilation_is_deterministic_and_described(perp):
    d = _definition([("ema_20", "crosses_above", "ema_50"), ("rel_volume", "gt", 0.5)], cooldown=5)
    snapshot = _snapshot(perp, _selections())
    a, b = compile_strategy(d, snapshot, "BTC"), compile_strategy(d, snapshot, "BTC")
    assert a.metadata == b.metadata
    m = a.metadata
    assert m.strategy_id == d.strategy_id and m.dataset_id == snapshot.dataset_id
    assert [f.name for f in m.features] == ["atr_sma_14", "ema_20", "ema_50", "rel_volume"]
    assert m.conditions == ("ema_20 crosses_above ema_50", "rel_volume gt 0.5")
    assert m.warmup_bars == 51 and m.rows == N
    assert m.signals == int(a.signal.sum()) > 0 and m.eligible_bars == int(a.eligible.sum())
    assert (a.signal <= a.active).all() and (a.active <= a.eligible).all()
    # A different rule on the same data has a different digest.
    other = compile_strategy(_definition([("close", "gt", 1.0)]), snapshot, "BTC")
    assert other.metadata.digest != m.digest
    # Short stops sit above the close.
    short = compile_strategy(_definition([("close", "gt", 1.0)], side="short"), snapshot, "BTC")
    valid = short.eligible
    assert (short.stop[valid] > short.inputs["close"][valid]).all()


def _register(store, d):
    ledger = Ledger(store)
    hypothesis = Hypothesis.model_validate(
        {
            "name": "compiler_test",
            "hypothesis": "fixture",
            "source": "test",
            "created_at": START,
            "definition": d.model_dump(mode="json"),
        }
    )
    receipt = ledger.submit(hypothesis.model_dump_json(), family_id="compiler", origin="test")
    assert receipt.accepted, receipt.error
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections()))
    return ledger, dataset_id


def test_snapshot_isolation_from_live_store_changes(perp):
    d = _definition([("close", "crosses_above", "sma_20"), ("funding_mean_7", "gt", 0.0)])
    ledger, dataset_id = _register(perp, d)
    (before,) = compile_registered(ledger, d.strategy_id, dataset_id)
    # Edit, delete and extend the live market tables after registration.
    perp.con.execute("UPDATE perp_bars SET close = close * 10")
    perp.con.execute("DELETE FROM perp_funding WHERE time < ?", [START + timedelta(days=100)])
    newer = _bars(30)
    newer[["ts", "close_time"]] += pd.Timedelta(days=N)
    _insert_perp(perp, newer, None)
    (after,) = compile_registered(ledger, d.strategy_id, dataset_id)
    assert after.metadata == before.metadata
    pd.testing.assert_frame_equal(after.features, before.features)
    with pytest.raises(CompileError, match="no perp_bars"):
        compile_registered(ledger, d.strategy_id, dataset_id, symbols=("ETH",))


def test_read_only_compile_cli(perp):
    from market_signal.cli.main import app

    d = _definition([("close", "gt", "sma_50")])
    _, dataset_id = _register(perp, d)
    path = perp.path
    perp.close()
    runner = CliRunner()
    result = runner.invoke(app, ["--db", str(path), "lab", "compile", d.strategy_id, dataset_id])
    assert result.exit_code == 0, result.output
    (summary,) = json.loads(result.output)
    assert summary["symbol"] == "BTC" and summary["rows"] == N
    assert summary["compiler_version"] == "lab_daily_compiler_v1"
    missing = runner.invoke(app, ["--db", str(path), "lab", "compile", "strategy_x", dataset_id])
    assert missing.exit_code == 1 and "does not exist" in missing.output
