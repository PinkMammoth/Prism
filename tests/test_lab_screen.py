"""Fast screen: entry/exit timing, costs, funding, baselines, warmup, triage and governance."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.models.domain import AssetClass
from market_signal.research.lab import screen as screen_mod
from market_signal.research.lab.compiler import Snapshot, compile_strategy
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset, snapshot_rows
from market_signal.research.lab.ledger import DuplicateExperiment, Ledger, LedgerError
from market_signal.research.lab.policy import EvaluationPlan, ScreenPlan, parse_plan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.screen import (
    ScreenWorkspace,
    run_screen,
    screen,
    triage,
)
from market_signal.research.lab.spec import Hypothesis
from tests.test_lab_compiler import _bars, _definition, _funding, _insert_perp

START = datetime(2023, 1, 1, tzinfo=UTC)
N = 420
WARM = 100
P0 = START + timedelta(days=200)
P1 = START + timedelta(days=N + 1)  # exclusive; the last close (START + N days) is inside
COINS = ("BTC", "ETH", "SOL")
SW = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)


def _plan(**over) -> ScreenPlan:
    base = {
        "name": "screen_test",
        "version": 1,
        "market": "perp",
        "source": "hyperliquid",
        "warmup_days": WARM,
        "periods": [{"role": "discovery", "start": P0, "end": P1}],
        "costs": [{"symbol": s, "fee_bps": 4.5, "slippage_bps": 2.0} for s in COINS],
        "horizons": [
            {"label": "1d", "bars": 1},
            {"label": "5d", "bars": 5},
            {"label": "10d", "bars": 10},
        ],
        "primary_horizon": "5d",
        "return_model": "perp_notional_v1",
        "funding": {},
        "statistics": {"min_independent_events": 5, "random_entry_samples": 200},
        "gates": {"min_assets_with_events": 2},
    }
    return ScreenPlan.model_validate({**base, **over})


def _selections(plan, coins=COINS, start=None):
    period = plan.period("discovery")
    return tuple(
        SeriesSelection(
            kind=kind,
            symbol=coin,
            source="hyperliquid",
            timeframe="1d" if kind == "perp_bars" else None,
            start=start or plan.data_start("discovery"),
            end=period.end,
        )
        for coin in coins
        for kind in ("perp_bars", "perp_funding")
    )


def _snapshot(store, selections) -> Snapshot:
    capture = capture_dataset(store, selections)
    blobs = dict(capture.blobs)
    rows = tuple(snapshot_rows(s, blobs[s.sha256]) for s in capture.manifest.series)
    return Snapshot(capture.manifest.dataset_id, capture.manifest, rows)


def _seed(store, coins=COINS):
    for i, coin in enumerate(coins):
        bars = _bars(N, seed=i)
        _insert_perp(store, bars, _funding(bars, seed=10 + i), coin=coin)


DEFAULT_RULE = [("close", "crosses_above", "ema_20"), ("rsi_14", "gt", 50)]


@pytest.fixture
def market(store):
    _seed(store)
    return store


def _hypothesis(definition, name="screen_rule"):
    return Hypothesis.model_validate(
        {
            "name": name,
            "hypothesis": "fixture",
            "source": "test",
            "created_at": START,
            "definition": definition.model_dump(mode="json"),
        }
    )


@pytest.fixture
def env(market):
    ledger = Ledger(market)
    definition = _definition(DEFAULT_RULE)
    receipt = ledger.submit(_hypothesis(definition).model_dump_json(), family_id="f", origin="t")
    assert receipt.accepted, receipt.error
    plan = _plan()
    plan_id = ledger.register_plan(plan)
    dataset_id = ledger.register_dataset(capture_dataset(market, _selections(plan)))
    return SimpleNamespace(
        store=market,
        ledger=ledger,
        definition=definition,
        submission_id=receipt.submission_id,
        plan=plan,
        plan_id=plan_id,
        dataset_id=dataset_id,
    )


def _prereg(env, **kw):
    return env.ledger.preregister(
        kw.pop("submission_id", env.submission_id),
        kw.pop("plan_id", env.plan_id),
        kw.pop("dataset_id", env.dataset_id),
        role="discovery",
        assets=kw.pop("assets", COINS),
        software=SW,
        origin="test",
        **kw,
    )


def _without_identity(metrics: dict) -> dict:
    out = json.loads(json.dumps(metrics, default=str))
    for key in ("experiment_id", "attempt", "logical_id", "software_id"):
        out["provenance"].pop(key, None)
    return out


# --------------------------------------------------------------------------- hand-calculated toy


def _toy(store, every_hours=1, rate=1e-5):
    c = np.array([100.0] * 20 + [110, 112, 115, 120, 120, 120, 120, 120, 100, 110])
    o = np.r_[100.0, c[:-1] + 1]  # open differs from the prior close, so entry is visible
    ts = pd.date_range(START, periods=len(c), freq="1D", tz="UTC")
    bars = pd.DataFrame(
        {
            "ts": ts,
            "open": o,
            "high": np.maximum(o, c) + 1,
            "low": np.minimum(o, c) - 1,
            "close": c,
            "volume": 1000.0,
            "close_time": ts + pd.Timedelta(days=1),
        }
    )
    funding = _funding(bars, every_hours=every_hours)
    funding["funding_rate"] = rate
    _insert_perp(store, bars, funding)
    plan = ScreenPlan.model_validate(
        {
            "name": "toy",
            "version": 1,
            "market": "perp",
            "source": "hyperliquid",
            "warmup_days": 0,
            "periods": [{"role": "discovery", "start": START, "end": START + timedelta(days=31)}],
            "costs": [{"symbol": "BTC", "fee_bps": 5.0, "slippage_bps": 5.0}],
            "horizons": [{"label": "1d", "bars": 1}, {"label": "3d", "bars": 3}],
            "primary_horizon": "1d",
            "return_model": "perp_notional_v1",
            "funding": {},
            "statistics": {"min_independent_events": 1, "random_entry_samples": 50},
            "gates": {"min_assets_with_events": 1},
        }
    )
    selections = tuple(
        SeriesSelection(
            kind=k,
            symbol="BTC",
            source="hyperliquid",
            timeframe="1d" if k == "perp_bars" else None,
            start=START,
            end=START + timedelta(days=31),
        )
        for k in ("perp_bars", "perp_funding")
    )
    return c, o, plan, _snapshot(store, selections)


def _row(result, horizon, symbol="BTC"):
    return next(
        r for r in result.metrics["per_asset"] if r["horizon"] == horizon and r["symbol"] == symbol
    )


def test_hand_calculated_long_returns_enter_next_open_and_pay_funding(store):
    c, o, plan, snapshot = _toy(store)
    result = screen(_definition([("close", "crosses_above", 105.0)]), plan, snapshot,
                    "discovery", ("BTC",))  # fmt: skip
    fd = 24 * 1e-5  # hourly settlements of 1e-5 -> funding_day
    rt = 2 * (5 + 5) / 1e4
    entry = o[21]  # signal at bar 20 (close 110 crosses 105); entry at bar 21's OPEN
    assert entry == 111.0 and entry != c[20]
    gross_1, gross_3 = c[21] / entry - 1, c[23] / entry - 1
    net_1 = gross_1 - rt - fd * c[21] / entry
    net_3 = gross_3 - rt - fd * (c[21] + c[22] + c[23]) / entry
    one, three = _row(result, "1d"), _row(result, "3d")
    # bar 29 also crosses 105 but is the final bar: no next open, so not evaluable
    assert one["raw_signals"] == 2 and one["evaluable_events"] == 1
    assert one["gross_mean"] == pytest.approx(gross_1, rel=1e-12)
    assert one["net_mean"] == pytest.approx(net_1, rel=1e-12)
    assert three["gross_mean"] == pytest.approx(gross_3, rel=1e-12)
    assert three["net_mean"] == pytest.approx(net_3, rel=1e-12)
    events = result.events
    assert set(events["bar"]) == {20}
    # baseline: eligible in-window bars with an outcome. Warmup = atr_sma_14 (14) + 1 for
    # the crossover's T-1, so bars 14..28 (1d) and 14..26 (3d).
    assert one["baseline_n"] == 15 and three["baseline_n"] == 13


def test_hand_calculated_short_returns_receive_positive_funding(store):
    c, o, plan, snapshot = _toy(store)
    result = screen(_definition([("close", "crosses_below", 105.0)], side="short"), plan,
                    snapshot, "discovery", ("BTC",))  # fmt: skip
    entry = o[29]  # signal at bar 28 (close 100 crosses below 105)
    gross = -(c[29] / entry - 1)
    net = gross - 2 * 10 / 1e4 + 24e-5 * c[29] / entry  # shorts receive positive funding
    one, three = _row(result, "1d"), _row(result, "3d")
    assert one["gross_mean"] == pytest.approx(gross, rel=1e-12)
    assert one["net_mean"] == pytest.approx(net, rel=1e-12)
    assert three["evaluable_events"] == 0  # bar 31 does not exist


def test_missing_funding_makes_outcomes_unevaluable_not_free(store):
    _, _, plan, snapshot = _toy(store)
    store.con.execute(
        "DELETE FROM perp_funding WHERE time > ? AND time <= ?",
        [START + timedelta(days=22), START + timedelta(days=23)],
    )
    selections = tuple(s.selection for s in snapshot.manifest.series)  # same selection, new content
    snapshot = _snapshot(store, selections)
    result = screen(_definition([("close", "crosses_above", 105.0)]), plan, snapshot,
                    "discovery", ("BTC",))  # fmt: skip
    assert _row(result, "1d")["evaluable_events"] == 1  # bar 21 funding still known
    assert _row(result, "3d")["evaluable_events"] == 0  # bar 22's funding day is missing


def test_costs_lower_net_returns_exactly(store):
    _, _, plan, snapshot = _toy(store)
    rule = _definition([("close", "crosses_above", 105.0)])
    cheap = screen(rule, plan, snapshot, "discovery", ("BTC",))
    dear_plan = plan.model_copy(
        update={"costs": (plan.costs[0].model_copy(update={"fee_bps": 25.0}),)}
    )
    dear = screen(rule, ScreenPlan.model_validate(dear_plan.model_dump()), snapshot,
                  "discovery", ("BTC",))  # fmt: skip
    a, b = _row(cheap, "1d"), _row(dear, "1d")
    assert a["gross_mean"] == b["gross_mean"]
    assert a["net_mean"] - b["net_mean"] == pytest.approx(2 * 20 / 1e4, rel=1e-9)


# --------------------------------------------------------------------------- window & warmup


def test_events_and_baselines_are_confined_to_the_role_window(market):
    plan = _plan()
    snapshot = _snapshot(market, _selections(plan))
    rule = _definition(DEFAULT_RULE)
    result = screen(rule, plan, snapshot, "discovery", COINS)
    ev = result.events
    assert len(ev) and (ev["signal_time"] >= P0).all() and (ev["signal_time"] < P1).all()
    for coin in COINS:
        compiled = compile_strategy(rule, snapshot, coin)
        close = compiled.inputs["close_time"]
        window = (close >= P0) & (close < P1)
        assert compiled.signal[~window].sum() > 0  # warmup signals exist but are not scored
        meta = result.metrics["assets"][coin]
        assert meta["prewindow_bars"] == (~window).sum() == WARM
        assert meta["raw_signals"] == int((compiled.signal & window).sum())
        assert meta["eligible_bars"] == int((compiled.eligible & window).sum())
        assert meta["warmup_shortfall_bars"] == 0
        # random-entry/baseline pool: eligible in-window bars with a complete outcome
        ret = screen_mod._returns(ScreenWorkspace(snapshot), plan, coin, "long", compiled)[0][
            "ret_5d"
        ].to_numpy()
        pool = (compiled.eligible & window).to_numpy() & np.isfinite(ret)
        assert _row(result, "5d", coin)["baseline_n"] == pool.sum()
        # the last 5 bars of the window can signal but can never complete a 5d outcome
        assert not np.isfinite(ret[-5:]).any()


def test_warmup_region_feeds_features_but_without_it_eligibility_starts_late(market):
    rule = _definition([("close", "gt", "ema_50")])
    with_warmup = _plan()
    snap = _snapshot(market, _selections(with_warmup))
    a = screen(rule, with_warmup, snap, "discovery", COINS)
    no_warmup = _plan(warmup_days=0)
    snap0 = _snapshot(market, _selections(no_warmup))
    b = screen(rule, no_warmup, snap0, "discovery", COINS)
    ma, mb = a.metrics["assets"]["BTC"], b.metrics["assets"]["BTC"]
    assert mb["prewindow_bars"] == 0 and mb["warmup_shortfall_bars"] == 50 - 1
    assert ma["eligible_bars"] == ma["window_bars"]  # warmup covered entirely
    assert mb["eligible_bars"] == mb["window_bars"] - 49
    assert a.metrics["provenance"]["data_start"] == P0 - timedelta(days=WARM)


def test_mutations_outside_the_dataset_do_not_matter_inside_warmup_do(market):
    plan = _plan()
    rule = _definition([("close", "crosses_above", "ema_50")])
    snap = _snapshot(market, _selections(plan))
    base = screen(rule, plan, snap, "discovery", COINS)
    # Before the data start and after the window end: not part of the dataset at all.
    market.con.execute("UPDATE perp_bars SET close = close * 9 WHERE close_time < ?",
                       [plan.data_start("discovery")])  # fmt: skip
    market.con.execute(
        "INSERT INTO perp_bars SELECT coin, timeframe, source, ts + INTERVAL 30 DAY, open, "
        "high, low, close * 5, volume, close_time + INTERVAL 30 DAY, ingested_at, "
        "ingest_run_id FROM perp_bars WHERE close_time > ?",
        [P1 - timedelta(days=30)],
    )
    same_snap = _snapshot(market, _selections(plan))
    assert same_snap.dataset_id == snap.dataset_id
    assert screen(rule, plan, same_snap, "discovery", COINS).metrics == base.metrics
    # Inside the warmup region (documented): recursive features such as EMA carry it
    # into the window, so in-window values legitimately change; the dataset ID changes too.
    market.con.execute(
        "UPDATE perp_bars SET close = close * 1.5 WHERE coin='BTC' AND close_time >= ? "
        "AND close_time < ?",
        [plan.data_start("discovery"), plan.data_start("discovery") + timedelta(days=10)],
    )
    warm_snap = _snapshot(market, _selections(plan))
    assert warm_snap.dataset_id != snap.dataset_id
    before = compile_strategy(rule, snap, "BTC").features["ema_50"]
    after = compile_strategy(rule, warm_snap, "BTC").features["ema_50"]
    in_window = before.index >= P0
    assert not np.allclose(before[in_window].iloc[:5], after[in_window].iloc[:5])
    # ...while a feature with no memory beyond its window (sma_20) is unaffected in-window.
    sma = _definition([("close", "gt", "sma_20")])
    a = compile_strategy(sma, snap, "BTC").features["sma_20"][in_window]
    b = compile_strategy(sma, warm_snap, "BTC").features["sma_20"][in_window]
    pd.testing.assert_series_equal(a, b)


def test_mutations_after_a_cutoff_inside_the_window_only_affect_later_outcomes(market):
    plan = _plan()
    rule = _definition(DEFAULT_RULE)
    before = screen(rule, plan, _snapshot(market, _selections(plan)), "discovery", COINS)
    cutoff = P0 + timedelta(days=120)
    market.con.execute("UPDATE perp_bars SET close = close * 2, open = open * 2 WHERE "
                       "close_time > ?", [cutoff])  # fmt: skip
    after = screen(rule, plan, _snapshot(market, _selections(plan)), "discovery", COINS)
    # an event whose 10d exit closes at or before the cutoff is unchanged
    key = ["symbol", "horizon", "bar"]
    a = before.events.set_index(key)
    b = after.events.set_index(key)
    safe = a[a["signal_time"] + pd.Timedelta(days=11) <= cutoff].index
    assert len(safe) > 10
    pd.testing.assert_series_equal(a.loc[safe, "ret"], b.loc[safe, "ret"])


# --------------------------------------------------------------------------- baseline & events


def test_random_baseline_is_deterministic_and_seeded(market):
    plan = _plan()
    snapshot = _snapshot(market, _selections(plan))
    rule = _definition(DEFAULT_RULE)
    a = screen(rule, plan, snapshot, "discovery", COINS)
    b = screen(rule, plan, snapshot, "discovery", COINS)
    assert a.metrics == b.metrics and a.p_values == b.p_values
    other = _plan(statistics={"min_independent_events": 5, "random_entry_samples": 200,
                              "seed": 7})  # fmt: skip
    c = screen(rule, other, snapshot, "discovery", COINS)
    assert c.metrics["aggregate"][0] == a.metrics["aggregate"][0]  # 1d has no p-value
    assert c.metrics["provenance"]["statistics"]["seed"] == 7
    assert a.p_values[0].endpoint == "pooled:long:5d"


def test_independent_events_are_declustered_by_horizon(market):
    plan = _plan()
    snapshot = _snapshot(market, _selections(plan))
    result = screen(_definition([("ret_1", "gt", 0.0)]), plan, snapshot, "discovery", COINS)
    ev = result.events
    for (_, horizon), group in ev.groupby(["symbol", "horizon"]):
        h = {"1d": 1, "5d": 5, "10d": 10}[horizon]
        kept = np.sort(group.loc[group["independent"], "bar"].to_numpy())
        assert (np.diff(kept) >= h).all()
        if h > 1:
            assert len(kept) < len(group)  # persistent signals are thinned
    row = _row(result, "10d")
    assert row["independent_events"] < row["evaluable_events"]


def test_per_asset_and_aggregate_metrics_are_consistent(market):
    plan = _plan()
    result = screen(_definition(DEFAULT_RULE), plan, _snapshot(market, _selections(plan)),
                    "discovery", COINS)  # fmt: skip
    for agg in result.metrics["aggregate"]:
        rows = [r for r in result.metrics["per_asset"] if r["horizon"] == agg["horizon"]]
        assert len(rows) == 3 and agg["assets"] == 3
        assert agg["independent_events"] == sum(r["independent_events"] for r in rows)
        excess = [r["excess_mean"] for r in rows if r["independent_events"]]
        assert agg["assets_with_events"] == len(excess)
        assert agg["assets_positive_excess"] == sum(e > 0 for e in excess)
        assert agg["median_asset_excess"] == pytest.approx(float(np.median(excess)))
        assert agg["max_asset_event_share"] == pytest.approx(
            max(r["independent_events"] for r in rows) / agg["independent_events"]
        )
        for r in rows:
            assert r["worst_net"] <= r["net_median"] <= r["best_net"]


def _primary(**over):
    base = {
        "independent_events": 100,
        "assets_with_events": 4,
        "excess_mean": 0.01,
        "positive_asset_share": 1.0,
        "p_value_random_entry": 0.05,
    }
    return {**base, **over}


def test_triage_rules_respect_cross_asset_direction():
    plan = _plan(gates={"min_assets_with_events": 3, "min_positive_asset_share": 0.75})
    assert triage(_primary(independent_events=0), plan)[0] == "NO_EVENTS"
    assert triage(_primary(independent_events=4), plan)[0] == "INSUFFICIENT_EVENTS"
    assert triage(_primary(assets_with_events=2), plan)[0] == "INSUFFICIENT_EVENTS"
    # every asset mildly positive
    assert triage(_primary(), plan)[0] == "INTERESTING"
    # one big winner, most assets negative: a positive pooled mean is not enough
    assert triage(_primary(positive_asset_share=0.25), plan)[0] == "WEAK"
    assert triage(_primary(excess_mean=-0.001), plan)[0] == "WEAK"
    label, checks = triage(_primary(p_value_random_entry=0.2), plan)  # default gate 0.1
    assert label == "WEAK" and checks["max_p_value"] is False
    lenient = _plan(gates={"min_assets_with_events": 3, "max_p_value": None})
    assert triage(_primary(p_value_random_entry=None), lenient)[0] == "INTERESTING"


def test_no_events_and_insufficient_events(market):
    plan = _plan()
    snapshot = _snapshot(market, _selections(plan))
    none = screen(_definition([("close", "gt", 1e12)]), plan, snapshot, "discovery", COINS)
    assert none.triage == "NO_EVENTS" and none.p_values == ()
    strict = _plan(statistics={"min_independent_events": 10_000, "random_entry_samples": 50})
    few = screen(_definition(DEFAULT_RULE), strict, snapshot, "discovery", COINS)
    assert few.triage == "INSUFFICIENT_EVENTS"


def test_workspace_reuse_gives_identical_results(market):
    plan = _plan()
    snapshot = _snapshot(market, _selections(plan))
    ws = ScreenWorkspace(snapshot)
    rules = [_definition(DEFAULT_RULE), _definition([("close", "gt", "ema_20")])]
    shared = [screen(r, plan, snapshot, "discovery", COINS, workspace=ws) for r in rules]
    fresh = [screen(r, plan, snapshot, "discovery", COINS) for r in rules]
    assert [s.metrics for s in shared] == [f.metrics for f in fresh]
    assert ("BTC", "perp", AssetClass.CRYPTO, "ema_20") in ws.compile_cache.features


# --------------------------------------------------------------------------- plans


def test_v1_plans_keep_their_identity_but_cannot_be_screened(env):
    v1 = EvaluationPlan.model_validate(
        {
            "name": "legacy",
            "version": 1,
            "market": "perp",
            "source": "hyperliquid",
            "periods": [{"role": "discovery", "start": P0, "end": P1}],
            "costs": [{"symbol": s, "fee_bps": 4.5, "slippage_bps": 2} for s in COINS],
            "horizons": [{"label": "5d", "bars": 5}],
            "primary_horizon": "5d",
            "return_model": "perp_notional_v1",
            "funding": {},
        }
    )
    assert v1.funding.aggregation == "prism_daily_full_history_median_v1"
    plan_id = env.ledger.register_plan(v1)
    assert env.ledger.get_plan(plan_id) == v1 and parse_plan(v1.canonical_json()).plan_id == plan_id
    dataset_id = env.ledger.register_dataset(capture_dataset(env.store, _selections(v1, start=P0)))
    attempt = _prereg(env, plan_id=plan_id, dataset_id=dataset_id)
    assert attempt.stage == "event_study"
    with pytest.raises(LedgerError, match="v2 screen plan"):
        run_screen(env.ledger, attempt.experiment_id, software=SW)
    assert env.ledger.inspect_experiment(attempt.experiment_id)["status"] == "preregistered"


def test_v2_plan_freezes_the_causal_funding_method():
    plan = _plan()
    assert plan.funding.aggregation == "lab_causal_trailing_7d_median_v1"
    with pytest.raises(ValidationError):
        _plan(funding={"aggregation": "prism_daily_full_history_median_v1"})
    with pytest.raises(ValidationError):
        _plan(funding={"min_daily_coverage": 0.5})
    with pytest.raises(ValidationError, match="asset class"):
        _plan(market="spot", return_model="spot_total_return_v1", funding=None)


def test_v2_preregistration_requires_the_warmup_region(env):
    short = env.ledger.register_dataset(capture_dataset(env.store, _selections(env.plan, start=P0)))
    with pytest.raises(LedgerError, match="warmup region"):
        _prereg(env, dataset_id=short)
    with pytest.raises(LedgerError, match="minimum assets"):
        _prereg(env, assets=("BTC",))


# --------------------------------------------------------------------------- governance


def test_governed_screen_attaches_one_immutable_result(env):
    attempt = _prereg(env)
    assert attempt.stage == "screen"
    out = run_screen(env.ledger, attempt.experiment_id, software=SW)
    record = env.ledger.inspect_experiment(attempt.experiment_id)
    (result,) = record["results"]
    assert result["verdict"] == out["triage"] in screen_mod.TRIAGE_STATUS
    assert result["status"] == screen_mod.TRIAGE_STATUS[out["triage"]]
    prov = result["metrics"]["provenance"]
    for key, value in {
        "experiment_id": attempt.experiment_id,
        "plan_id": env.plan_id,
        "dataset_id": env.dataset_id,
        "strategy_id": env.definition.strategy_id,
        "software_id": SW.software_id,
        "screen_version": "lab_fast_screen_v1",
        "compiler_version": "lab_daily_compiler_v1",
        "vocabulary_version": "lab_features_v1",
        "warmup_days": WARM,
    }.items():
        assert prov[key] == value, key
    assert prov["statistics"]["seed"] == 12345 and prov["horizons"]["5d"] == 5
    assert result["p_values"][0]["endpoint"] == "pooled:long:5d"
    assert [i["kind"] for i in record["inspections"]] == ["evaluation_started"]
    with pytest.raises(LedgerError, match="already started"):
        run_screen(env.ledger, attempt.experiment_id, software=SW)
    with pytest.raises(LedgerError, match="immutable"):
        env.ledger.record_result(attempt.experiment_id, status="succeeded")
    assert len(env.ledger.inspect_experiment(attempt.experiment_id)["results"]) == 1


def test_errors_after_start_are_recorded(env, monkeypatch):
    attempt = _prereg(env)

    def boom(*args, **kwargs):
        raise RuntimeError("feature store exploded")

    monkeypatch.setattr(screen_mod, "compile_strategy", boom)
    out = run_screen(env.ledger, attempt.experiment_id, software=SW)
    assert out["triage"] == "ERROR"
    (result,) = env.ledger.inspect_experiment(attempt.experiment_id)["results"]
    assert result["status"] == "errored" and result["verdict"] == "ERROR"
    assert result["error"]["kind"] == "RuntimeError"
    assert "feature store exploded" in result["error"]["message"]
    assert result["metrics"]["provenance"]["experiment_id"] == attempt.experiment_id


def test_preconditions_fail_before_any_start(env):
    attempt = _prereg(env)
    other = SoftwareIdentity(label="other", python_version="3.12", source_sha256="b" * 64)
    with pytest.raises(LedgerError, match="software differs"):
        run_screen(env.ledger, attempt.experiment_id, software=other)
    assert env.ledger.inspect_experiment(attempt.experiment_id)["status"] == "preregistered"


def test_reruns_are_explicit_and_reproduce_exactly_despite_live_store_edits(env):
    first = _prereg(env)
    run_screen(env.ledger, first.experiment_id, software=SW)
    with pytest.raises(DuplicateExperiment):
        _prereg(env)
    # The live market tables change after registration; the retained snapshot does not.
    env.store.con.execute("UPDATE perp_bars SET close = close * 3")
    env.store.con.execute("DELETE FROM perp_funding")
    second = _prereg(env, rerun_of=first.experiment_id, rerun_reason="reproducibility check")
    assert second.attempt == 2
    workspaces = {}
    run_screen(env.ledger, second.experiment_id, software=SW, workspaces=workspaces)
    assert env.dataset_id in workspaces
    a = env.ledger.inspect_experiment(first.experiment_id)["results"][0]
    b = env.ledger.inspect_experiment(second.experiment_id)["results"][0]
    assert (a["status"], a["verdict"], a["p_values"]) == (b["status"], b["verdict"], b["p_values"])
    assert _without_identity(a["metrics"]) == _without_identity(b["metrics"])


def test_cli_preregister_screen_and_inspect(env):
    from market_signal.cli.main import app

    path = env.store.path
    env.store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab"]
    pre = runner.invoke(app, [*base, "preregister", env.submission_id, env.plan_id,
                              env.dataset_id, "--role", "discovery", "--asset", "BTC",
                              "--asset", "ETH", "--asset", "SOL"])  # fmt: skip
    assert pre.exit_code == 0, pre.output
    experiment_id = json.loads(pre.output)["experiment_id"]
    out = runner.invoke(app, [*base, "screen", experiment_id])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["triage"] in ("NO_EVENTS", "INSUFFICIENT_EVENTS", "WEAK",
                                                "INTERESTING")  # fmt: skip
    shown = runner.invoke(app, [*base, "experiment", experiment_id])
    assert shown.exit_code == 0 and json.loads(shown.output)["results"][0]["metrics"]
    again = runner.invoke(app, [*base, "screen", experiment_id])
    assert again.exit_code == 1 and "already started" in again.output


def test_spot_screen_uses_total_return_outcomes_across_a_split(store):
    from tests.test_lab_compiler import _insert_spot

    split_on = (P0 + timedelta(days=60)).date()
    raw = _insert_spot(store, _bars(N), split_on)  # raw prices halve on the split date
    plan = _plan(
        market="spot",
        source="tiingo",
        return_model="spot_total_return_v1",
        funding=None,
        costs=[{"symbol": "AAPL", "fee_bps": 5.0, "slippage_bps": 5.0, "asset_class": "equity"}],
        gates={"min_assets_with_events": 1},
    )
    period = plan.period("discovery")
    selections = tuple(
        SeriesSelection(kind=k, symbol="AAPL", source="tiingo",
                        timeframe="1d" if k == "bars" else None,
                        start=plan.data_start("discovery"), end=period.end)
        for k in ("bars", "corporate_actions")
    )  # fmt: skip
    snapshot = _snapshot(store, selections)
    rule = _definition([("ret_1", "gt", 0.0)], market="spot", atr="wilder_14")
    result = screen(rule, plan, snapshot, "discovery", ("AAPL",))
    ev = result.events[result.events["horizon"] == "1d"]
    assert len(ev) > 20 and ev["ret"].min() > -0.3  # no fake -50% across the split
    # A signal at T = k-2 (k = first post-split bar) enters at bar k-1's open (pre-split
    # raw price) and its 5d horizon exits at bar k+3's close (post-split raw price).
    frame = raw[raw["close_time"] >= plan.data_start("discovery")].reset_index(drop=True)
    k = int(np.flatnonzero((frame["ts"].dt.date >= split_on).to_numpy())[0])
    entry, exit_ = frame["open"][k - 1], frame["close"][k + 3] * 2.0  # undo the 2:1 split
    assert frame["close"][k + 3] / entry - 1 < -0.3  # what a raw-price screen would report
    compiled = compile_strategy(rule, snapshot, "AAPL", asset_class=AssetClass.EQUITY)
    net, gross = screen_mod._returns(ScreenWorkspace(snapshot), plan, "AAPL", "long", compiled)
    c = 10 / 1e4
    assert gross["5d"].iloc[k - 2] == pytest.approx(exit_ / entry - 1, rel=1e-12)
    assert net["ret_5d"].iloc[k - 2] == pytest.approx(
        exit_ * (1 - c) / (entry * (1 + c)) - 1, rel=1e-12
    )
