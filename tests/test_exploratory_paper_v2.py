"""Phase 25A mechanics, causality, immutable retirement and paper-only boundaries."""

# ruff: noqa: F811

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from typer.testing import CliRunner

from market_signal.cli.main import app
from market_signal.ops import runtime
from market_signal.paper import engine as old_engine
from market_signal.paper import observe as old_observe
from market_signal.paper import retirement
from market_signal.paper.v2 import engine, report, spec
from market_signal.paper.v2.data import Funding, Observation, Quote, Sources
from tests.test_lab_forward import SW, day, env  # noqa: F401
from tests.test_paper import _create, pap  # noqa: F401

START = pd.Timestamp("2026-10-09T00:00:00Z")


def minute(n):
    return START + pd.Timedelta(minutes=n)


def hypothesis(side="long", source=24, key="continuation_core"):
    return next(
        h
        for h in spec.bootstrap()
        if h.side == side and h.source_phase == source and (source != 24 or h.source_key == key)
    )


def obs(h=None, *, asset="BTC", at=15, **kw):
    h = h or hypothesis()
    return Observation(
        h.hypothesis_id,
        asset,
        minute(at).isoformat(),
        minute(at).isoformat(),
        kw.pop("fired", True),
        intended_side=h.side,
        context={"snapshot_id": "known-first-seen", "market": {}},
        **kw,
    )


def quote(n, price=100, asset="BTC", **kw):
    return Quote(
        asset,
        minute(n).isoformat(),
        kw.pop("available_at", minute(n).isoformat()),
        price,
        price,
        price,
        **kw,
    )


class Inputs:
    dependencies = {}

    def __init__(self, signals=(), prices=(), rates=None):
        self.signals, self.prices = list(signals), list(prices)
        self.rates = (
            rates
            if rates is not None
            else [Funding(a, minute(0).isoformat(), minute(0).isoformat(), 0) for a in spec.COINS]
        )

    def quotes(self, since):
        return self.prices

    def funding(self, since):
        return self.rates

    def observations(self, hypotheses, activated_at):
        return self.signals


@pytest.fixture
def run(store):
    engine.register(store, now=START)
    return engine.create(store, now=START)["run_id"]


def evaluate(store, run, *, at=16, signals=(), prices=(), rates=None):
    return engine.evaluate(store, run, now=minute(at), sources=Inputs(signals, prices, rates))


def test_frozen_bootstrap_exact_seven_pairs_and_separate_ids(store, run):
    hs = spec.bootstrap()
    assert len(hs) == 28
    assert tuple(h.name for h in hs if h.source_phase == 22) == spec.ALL_TECHNICAL_KEYS
    assert all(
        h.scientific_verdict == ("INTERESTING" if h.side == "long" else "REJECTED")
        for h in hs
        if h.source_phase == 22
    )
    assert len({h.hypothesis_id for h in hs}) == 28
    assert set(spec.HORIZONS) == {15, 30, 60, 120, 240, 480}
    assert run.startswith("paperrun_v2_") and run != retirement.KNOWN_RUN
    assert report.hypotheses(store, run)["long"] == 14
    assert report.hypotheses(store, run)["short"] == 14
    for phase in (22, 23, 24):
        assert sum(h.side == "long" and h.source_phase == phase for h in hs) == sum(
            h.side == "short" and h.source_phase == phase for h in hs
        )
    assert engine.state(store, run)["cash"] == 100
    assert len(store.con.execute("SELECT * FROM paper_v2_policies").fetchall()) == 3


@pytest.mark.parametrize("long_key", spec.TECHNICAL_KEYS)
def test_technical_control_exact_mirror_and_preserved_science(long_key):
    from market_signal.research.discovery.catalogue import IntradayStrategy

    by_key = {h.source_key: h for h in spec.bootstrap()}
    long = by_key[long_key]
    short = by_key[long_key.replace(":long", ":short")]
    assert short.family == long.family.replace(
        "EXPLORATORY_TECHNICAL:", "EXPLORATORY_TECHNICAL_CONTROL:"
    )
    assert short.scientific_verdict == "REJECTED"
    assert short.parameters["scientific_verdict_reasons"] == [
        "contemporary net effect credibly negative"
    ]
    assert short.parameters["baseline_digest"] == long.parameters["baseline_digest"]
    assert short.parameters["warmup_rule"] == long.parameters["warmup_rule"]
    for field in ("horizon_minutes", "cadence_minutes", "feature_version", "definition_version"):
        assert getattr(short, field) == getattr(long, field)
    assert short.cooldown_minutes == long.cooldown_minutes
    ls = IntradayStrategy.model_validate(long.parameters["strategy"])
    ss = IntradayStrategy.model_validate(short.parameters["strategy"])
    assert ls.symmetric and ss.symmetric
    expected = ls.model_dump(mode="json")
    expected["side"] = "short"
    expected["hypothesis"] = "[mirror] " + expected["hypothesis"]
    if expected["baseline"]:
        expected["baseline"] = expected["baseline"].replace(":long", ":short")
    assert ss.model_dump(mode="json") == expected
    assert short.parameters["strategy_id"] == ss.strategy_id


@pytest.mark.parametrize("key", spec.ALL_TECHNICAL_KEYS)
def test_technical_pairs_identical_admission_sizing_costs_horizon_cooldown(store, run, key):
    h = next(h for h in spec.bootstrap() if h.source_key == key)
    science = Sources(store, minute(16)).science(h)
    observation = obs(h, scientific_verdict=science["scientific_verdict"])
    evaluate(store, run, signals=[observation], prices=[quote(15)])
    admitted = report.opportunities(store, run)[0]
    assert admitted["admission"] == "ADMITTED"  # REJECTED science is not an admission gate.
    assert admitted["scientific_verdict"] == h.scientific_verdict
    assert admitted["hypothesis"]["parameters"] == h.parameters
    pending = next(iter(engine.state(store, run)["pending"].values()))
    assert pending["target_notional"] == 20 and pending["leverage"] == 2
    evaluate(store, run, at=17, prices=[quote(17)])
    opened = next(iter(engine.state(store, run)["open"].values()))
    assert opened["exit_due_at"] == minute(17 + h.horizon_minutes).isoformat()
    evaluate(store, run, at=31, signals=[obs(h, at=30)], prices=[quote(30)])
    assert report.opportunities(store, run)[-1]["rejection_reason"] == "COOLDOWN"
    due = 17 + h.horizon_minutes
    price = 110 if h.side == "long" else 90
    rates = [
        Funding("BTC", minute(n).isoformat(), minute(n).isoformat(), 0)
        for n in range(0, due + 1, 60)
    ]
    evaluate(store, run, at=due + 1, prices=[quote(due, price)], rates=rates)
    closed = engine.state(store, run)["closed"][0]
    sign = 1 if h.side == "long" else -1
    entry = 100 * (1 + sign * 0.0002)
    exit = price * (1 - sign * 0.0002)
    units = engine.size(100, entry, "BTC")[0]
    fees = units * (entry + exit) * 0.00045
    assert closed["fees"] == pytest.approx(fees)
    assert closed["net_pnl"] == pytest.approx(sign * units * (exit - entry) - fees)
    assert closed["exit_reason"] == "FIXED_HORIZON"


def test_control_source_metadata_preserved_even_during_feature_warmup(store, run):
    src = Sources(store, minute(67))
    src.run_id = run
    observations = src.observations(spec.bootstrap(), START)
    by_id = {h.hypothesis_id: h for h in spec.bootstrap()}
    controls = [
        o for o in observations if by_id[o.hypothesis_id].source_key in spec.TECHNICAL_CONTROL_KEYS
    ]
    assert len(controls) == 7 * len(spec.COINS)
    for o in controls:
        assert o.scientific_verdict == "REJECTED"
        assert o.evidence["scientific_verdict_reasons"] == [
            "contemporary net effect credibly negative"
        ]


def test_universe_extension_preserves_previous_identities_and_policies(store, monkeypatch):
    previous = json.loads(json.dumps(spec.definition()))
    previous["version"] = "paper_exploratory_v2"
    previous["hypotheses"] = [
        h
        for h in previous["hypotheses"]
        if not h["family"].startswith("EXPLORATORY_TECHNICAL_CONTROL:")
    ]
    with monkeypatch.context() as m:
        m.setattr(spec, "definition", lambda: previous)
        old = engine.register(store, now=START)
    assert old["universe_id"] == (
        "paperuniverse_v2_dd3c5b954cce4ef19f3a416db755f22e39661b8738284a4c866d77e89edd6a09"
    )
    new = engine.register(store, now=minute(1))
    assert old["universe_id"] != new["universe_id"]
    stored = store.con.execute(
        "SELECT definition FROM paper_v2_universes WHERE universe_id=?", [old["universe_id"]]
    ).fetchone()[0]
    assert json.loads(stored) == previous
    assert len(store.con.execute("SELECT * FROM paper_v2_policies").fetchall()) == 3
    assert spec.AdmissionPolicy().policy_id == (
        "paperadmission_v2_f756f122a75360aeb58bcac27b38f5e72ee911011edf1944138f160d0c28d39e"
    )
    assert spec.RiskPolicy().policy_id == (
        "paperrisk_v2_475b7c1e87fc2934eba4bb288f07cc818ed7dadb9fc8b9ab8d24a594a772e8b9"
    )
    assert spec.ExecutionPolicy().policy_id == (
        "paperexecution_v2_1d468dd57dd04acf3389bf67cb48c5353eb03e0d919fe744993d2ce21742f08d"
    )


@pytest.mark.parametrize("side", ["long", "short"])
def test_symmetric_admission_and_horizon_cost_pnl(store, run, side):
    h = hypothesis(side)
    evaluate(store, run, signals=[obs(h)], prices=[quote(15)])
    assert len(engine.state(store, run)["pending"]) == 1
    price = 110 if side == "long" else 90
    evaluate(store, run, at=17, prices=[quote(15), quote(17)])
    st = engine.state(store, run)
    p = next(iter(st["open"].values()))
    assert p["entry_at"] == minute(17).isoformat()
    assert p["exit_due_at"] == minute(32).isoformat()
    evaluate(store, run, at=33, prices=[quote(17), quote(31, price), quote(32, price)])
    t = engine.state(store, run)["closed"][0]
    ef = 100 * (1.0002 if side == "long" else 0.9998)
    xf = price * (0.9998 if side == "long" else 1.0002)
    units = engine.size(100, ef, "BTC")[0]
    sign = 1 if side == "long" else -1
    expected = sign * units * (xf - ef) - units * (ef + xf) * 0.00045
    assert t["net_pnl"] == pytest.approx(expected)
    assert engine.state(store, run)["equity"] == pytest.approx(100 + expected)
    assert t["trade_return"] == pytest.approx(expected / t["notional"])
    assert t["account_impact"] == pytest.approx(expected / 100)
    assert t["exit_reason"] == "FIXED_HORIZON"
    assert t["fees"] > 0 and t["slippage_cost"] > 0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"healthy": False}, "DATA_STALE"),
        ({"warm": False}, "FEATURE_WARMUP"),
        ({"causal": False}, "INVALID_TIMING"),
        ({"context_available": False}, "CONTEXT_UNAVAILABLE"),
        ({"fired": False}, "NO_SIGNAL"),
        ({"available_at": minute(30).isoformat()}, "INVALID_TIMING"),
        ({"hypothesis_id": "unregistered"}, "HYPOTHESIS_NOT_REGISTERED"),
    ],
)
def test_explicit_rejections_recorded(store, run, change, reason):
    evaluate(store, run, signals=[replace(obs(), **change)], prices=[quote(15)])
    rows = report.opportunities(store, run)
    assert len(rows) == 1 and rows[0]["rejection_reason"] == reason
    assert not engine.state(store, run)["pending"]


def test_ledger_records_admitted_rejected_and_no_signal(store, run):
    h = hypothesis()
    result = evaluate(
        store,
        run,
        signals=[obs(h), obs(h, asset="ETH", healthy=False), obs(h, asset="SOL", fired=False)],
        prices=[quote(15)],
    )
    assert result["ADMITTED"] == result["REJECTED"] == result["NO_SIGNAL"] == 1
    assert len(report.opportunities(store, run)) == 3


def test_signal_and_cycle_idempotency(store, run):
    inputs = Inputs([obs()], [quote(15)])
    a = engine.evaluate(store, run, now=minute(16), sources=inputs)
    assert engine.evaluate(store, run, now=minute(16), sources=inputs) == a
    evaluate(store, run, at=17, signals=[obs()], prices=[quote(15)])
    assert len(report.opportunities(store, run)) == 1
    assert len(engine.state(store, run)["pending"]) == 1


def test_cutover_no_old_signals_no_backdated_account(store, run):
    evaluate(store, run, signals=[obs(at=-1), obs(at=0)], prices=[quote(15)])
    assert report.opportunities(store, run) == []
    assert engine.state(store, run)["pending"] == {}
    with pytest.raises(ValueError, match="precedes activation"):
        evaluate(store, run, at=-1)
    with pytest.raises(duckdb.ConstraintException):
        store.con.execute(
            "INSERT INTO paper_v2_opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "bad",
                run,
                "x",
                "BTC",
                "long",
                START,
                START,
                minute(16),
                START,
                True,
                "REJECTED",
                "INVALID_TIMING",
                None,
                "{}",
            ],
        )


def test_causal_entry_never_fills_earlier_available_quote(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(16), quote(17, available_at=minute(19).isoformat())])
    assert engine.state(store, run)["open"] == {}
    evaluate(store, run, at=19, prices=[quote(17, available_at=minute(19).isoformat())])
    assert len(engine.state(store, run)["open"]) == 1


def test_incomplete_microstructure_quote_cannot_fill(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17, complete=False)])
    assert not engine.state(store, run)["open"]


def test_same_asset_multi_family_one_trade_and_support(store, run):
    a, b = hypothesis(), hypothesis(key="continuation_persistent")
    evaluate(store, run, signals=[obs(a), obs(b)], prices=[quote(15)])
    st = engine.state(store, run)
    assert len(st["pending"]) == 1
    p = next(iter(st["pending"].values()))
    assert set(p["supporting_hypothesis_ids"]) == {a.hypothesis_id, b.hypothesis_id}
    assert p["support_count"] == 2
    assert {o["admission"] for o in report.opportunities(store, run)} == {"ADMITTED", "SUPPORTED"}


def test_conflicting_sides_cancel_both(store, run):
    evaluate(
        store, run, signals=[obs(hypothesis("long")), obs(hypothesis("short"))], prices=[quote(15)]
    )
    assert not engine.state(store, run)["pending"]
    assert all(
        o["rejection_reason"] == "CONFLICT" and len(o["conflict"]) == 2
        for o in report.opportunities(store, run)
    )


def test_degraded_hypothesis_cannot_cancel_enabled_opposite_signal(store, run):
    disabled = hypothesis("long")
    engine.event(
        store,
        run,
        "hypothesis_state",
        "degrade-test",
        {"hypothesis_id": disabled.hypothesis_id, "state": "DEGRADED", "reason": "test"},
        minute(1),
    )
    evaluate(store, run, signals=[obs(disabled), obs(hypothesis("short"))], prices=[quote(15)])
    decisions = {o["side"]: o for o in report.opportunities(store, run)}
    assert decisions["long"]["rejection_reason"] == "HYPOTHESIS_DISABLED"
    assert decisions["short"]["admission"] == "ADMITTED"


def test_cooldown_not_every_minute(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=20, signals=[obs(at=19)], prices=[quote(19)])
    assert report.opportunities(store, run)[-1]["rejection_reason"] == "COOLDOWN"


def test_duplicate_episode_not_unlimited_pyramiding(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(
        store,
        run,
        at=33,
        signals=[obs(hypothesis(key="continuation_persistent"), at=30)],
        prices=[quote(17), quote(31)],
    )
    assert report.opportunities(store, run)[-1]["rejection_reason"] == "DUPLICATE_EPISODE"
    assert len(engine.state(store, run)["open"]) == 1


def test_max_positions_across_assets(store, run):
    evaluate(
        store,
        run,
        signals=[obs(asset=a) for a in ("BTC", "ETH", "SOL", "HYPE")],
        prices=[quote(15, asset=a) for a in spec.COINS],
    )
    assert len(engine.state(store, run)["pending"]) == 3
    assert any(o["rejection_reason"] == "MAX_POSITIONS" for o in report.opportunities(store, run))


def test_small_account_min_notional_and_no_round_up(store, run):
    engine.event(
        store,
        run,
        "mark",
        "test_small",
        {"equity": 49, "peak": 100, "at": minute(1).isoformat(), "drawdown": 0.51},
        minute(1),
    )
    # manage re-marks actual cash; exercise pure sizing, and entry check on honest ledger loss.
    units, notional = engine.size(49, 100000, "BTC")
    assert notional <= 9.8 and notional < 10 and units > 0
    assert engine.size(100, 100000, "BTC")[1] <= 20
    engine.event(
        store,
        run,
        "pending",
        "pending:test",
        {
            "trade_id": "test",
            "asset": "BTC",
            "side": "long",
            "decision_at": minute(15).isoformat(),
            "equity_at_decision": 49,
            "horizon_minutes": 15,
        },
        minute(15),
    )
    evaluate(store, run, at=18, prices=[quote(17, 100000)])
    evs = engine.rows(store, "SELECT payload FROM paper_v2_events WHERE event_type='expired'")
    assert json.loads(evs[0]["payload"])["reason"] == "MIN_NOTIONAL"


def test_missing_funding_not_zero_filled(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    p = next(iter(engine.state(store, run)["open"].values()))
    # Inject a longer registered primary horizon in a synthetic event to test settlement arithmetic.
    assert engine.funding_cost("BTC", "long", 20, minute(17), minute(77), [])[1] == [
        minute(60).isoformat()
    ]
    assert p["horizon_minutes"] == 15


@pytest.mark.parametrize("side,expected", [("long", 0.002), ("short", -0.002)])
def test_hourly_funding_hand_calculated(side, expected):
    rates = [Funding("BTC", minute(60).isoformat(), minute(61).isoformat(), 0.0001)]
    paid, missing = engine.funding_cost("BTC", side, 20, minute(17), minute(77), rates)
    assert paid == pytest.approx(expected) and missing == []


def test_no_funding_dependency_rejects(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)], rates=[])
    assert report.opportunities(store, run)[0]["rejection_reason"] == "FUNDING_UNAVAILABLE"


def test_mfe_mae_no_pre_entry_wicks():
    path = [
        quote(17),
        replace(quote(18, 101), high=103, low=99),
        replace(quote(19, 99), high=102, low=95),
    ]
    m = engine.path_metrics("long", 100, path)
    assert m["mfe"] == pytest.approx(0.03) and m["mae"] == pytest.approx(-0.05)
    assert m["time_to_mfe_minutes"] == 1 and m["time_to_mae_minutes"] == 2
    assert m["favourable_first"] == "AMBIGUOUS"
    s = engine.path_metrics("short", 100, path)
    assert s["mfe"] == pytest.approx(0.05) and s["mae"] == pytest.approx(-0.03)


def test_rejected_signal_forward_outcome_research_only(store, run):
    evaluate(store, run, signals=[obs(healthy=False)], prices=[quote(15)])
    evaluate(store, run, at=40, prices=[replace(quote(17), high=150, low=50), quote(32, 110)])
    o = report.opportunities(store, run)[0]
    assert o["outcome"]["hypothetical_only"] and o["outcome"]["net_return"] > 0
    assert o["outcome"]["mfe"] == pytest.approx(0.1)
    assert o["outcome"]["mae"] == 0
    assert o["outcome"]["time_to_mfe_minutes"] == 15
    assert not engine.state(store, run)["closed"]
    result = report.postmortem(store, run, start=minute(0), end=minute(40))
    assert result["signal_count"] == 1 and result["reasons"]["DATA_STALE"] == 1


def test_fixed_exit_uses_later_bar_fallback_when_minute_stream_stops(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    evaluate(store, run, at=33, prices=[quote(17), quote(18), quote(32, 101, kind="bar_close")])
    assert engine.state(store, run)["closed"][0]["exit_at"] == minute(32).isoformat()
    partial_bar = replace(quote(20, 101, kind="bar_close"), high=200, low=1)
    bounded = engine.price_path([partial_bar], "BTC", minute(17))
    assert bounded[0].high == 101 and bounded[0].low == 101


def test_rejected_forward_outcome_never_enters_before_signal_availability(store, run):
    evaluate(
        store,
        run,
        signals=[replace(obs(), available_at=minute(30).isoformat())],
        prices=[quote(15)],
    )
    evaluate(store, run, at=40, prices=[quote(17), quote(32)])
    signal = report.opportunities(store, run)[0]
    assert signal["rejection_reason"] == "INVALID_TIMING" and signal["outcome"] is None
    evaluate(store, run, at=48, prices=[quote(17), quote(32), quote(47, 101)])
    outcome = report.opportunities(store, run)[0]["outcome"]
    assert outcome["entry_at"] == minute(32).isoformat()
    assert outcome["exit_at"] == minute(47).isoformat()
    assert not engine.state(store, run)["closed"]


def test_global_kill_deterministic_drain(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    engine.kill(store, run, reason="human switch", now=minute(19))
    evaluate(store, run, at=21, signals=[obs(at=20)], prices=[quote(17), quote(20)])
    st = engine.state(store, run)
    assert st["status"] == "KILLED" and not st["open"]
    assert st["closed"][0]["exit_reason"] == "KILL_SWITCH"
    assert len(report.opportunities(store, run)) == 1


def test_catastrophe_guard_not_5_or_10_percent(store, run):
    assert spec.RiskPolicy().catastrophe_drawdown == 0.75
    # Legitimate drawdown from an open marked position remains ACTIVE.
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    evaluate(store, run, at=20, prices=[quote(17), quote(19, 75)])
    assert engine.state(store, run)["status"] == "ACTIVE"
    # Frozen 75% catastrophic marked loss independently triggers global kill.
    engine.event(
        store,
        run,
        "mark",
        "peak",
        {"equity": 100, "peak": 500, "at": minute(20).isoformat(), "drawdown": 0.8},
        minute(20),
    )
    evaluate(store, run, at=21, prices=[quote(17), quote(20, 75)])
    assert engine.state(store, run)["status"] == "KILLED"


def test_retirement_preserves_rows_equity_and_stops_cycles(pap):
    rid = _create(pap)["run_id"]
    before = retirement.digests(pap.store, rid)
    result = retirement.retire(pap.store, rid, now=day(1, 3))
    assert result["state"] == "RETIRED" and result["equity"] == 10000
    assert result["reason"] == retirement.REASON
    assert result["historical_digests"]["paper_runs"] == before["paper_runs"]
    after = retirement.digests(pap.store, rid)
    # One terminal audit event appended; existing sequence and every policy/definition preserved.
    assert after["paper_events"]["rows"] == before["paper_events"]["rows"] + 1
    assert old_engine.account(pap.store, rid).status == "STOPPED"
    old_engine.cycle(pap.ledger, rid, software=SW, now=day(2, 3))
    old_observe.record_brief(pap.store, rid, now=day(2, 3))
    old_engine.record_evidence(pap.store, rid, now=day(2, 3))
    assert retirement.digests(pap.store, rid) == after
    assert retirement.retire(pap.store, rid) == result
    assert [x[0] for x in runtime.active_steps(pap.store, "prospective")] == [
        "forward",
        "copilot",
        "incubation",
    ]
    assert [x[0] for x in runtime.active_steps(pap.store, "intraday")] == ["bars"]


def test_frozen_comparison_detects_tampering(pap):
    rid = _create(pap)["run_id"]
    retirement.retire(pap.store, rid, now=day(1, 3))
    engine.register(pap.store, now=START)
    v2 = engine.create(pap.store, now=START)["run_id"]
    result = report.compare_v1(pap.store, v2, now=minute(30))
    assert result["v1"]["state"] == "RETIRED" and result["v1"]["trades"] == 0
    pap.store.con.execute("UPDATE paper_cycles SET events_written=99 WHERE run_id=?", [rid])
    # No cycles exist, so change a historical event to demonstrate the digest invariant.
    pap.store.con.execute(
        "UPDATE paper_events SET recorded_at=recorded_at+INTERVAL 1 SECOND WHERE run_id=? AND seq=1",
        [rid],
    )
    with pytest.raises(ValueError, match="history changed"):
        report.compare_v1(pap.store, v2)


def test_retirement_with_open_positions_drains_before_freeze(pap):
    from tests.test_paper import _until

    rid = _create(pap)["run_id"]
    k, _ = _until(pap, rid, "position_opened")
    result = retirement.retire(pap.store, rid, now=day(k, 4))
    assert result["state"] == "DRAINING" and retirement.frozen(pap.store, rid) is None
    assert retirement.v1_work_needed(pap.store)
    old = old_engine.account(pap.store, rid).counts.copy()
    for n in range(k + 1, k + 12):
        pap.market.publish(n)
        old_engine.cycle(pap.ledger, rid, software=SW, now=day(n, 3))
    assert old_engine.account(pap.store, rid).counts["intent_created"] == old["intent_created"]
    assert retirement.retire(pap.store, rid, now=day(k + 12, 3))["state"] == "RETIRED"


def test_daily_telegram_once_frequency_and_status(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    evaluate(store, run, at=33, prices=[quote(17), quote(32, 101)])
    sent = []
    result = report.record_daily(store, run, now=minute(1450), sender=sent.append)
    again = report.record_daily(store, run, now=minute(1451), sender=sent.append)
    assert len(sent) == 1 and "PAPER V2" in sent[0] and "trades 1" in sent[0]
    assert result["delivery"] == "sent" and again["delivery"] == "already_attempted"
    stats = report.frequency(store, run, now=minute(33))
    assert stats["trades_per_day"] == 1 and stats["days_ge_share"]["1"] == 1
    status = report.status(store, run, now=minute(34))
    assert status["healthy"] and status["last_24h"]["trades"] == 1
    assert status["db_storage"]["run_payload_bytes"] > 0
    assert result["payload"]["telegram_snapshot"]["equity"] == status["equity"]


def test_daily_fees_follow_entry_and_exit_days_and_brief_shows_current_account(store, run):
    rates = [Funding("BTC", minute(n).isoformat(), minute(n).isoformat(), 0) for n in (1380, 1440)]
    evaluate(store, run, at=1426, signals=[obs(at=1425)], prices=[quote(1425)], rates=rates)
    evaluate(store, run, at=1428, prices=[quote(1427)], rates=rates)
    evaluate(store, run, at=1443, prices=[quote(1427), quote(1442, 101)], rates=rates)
    trade = engine.state(store, run)["closed"][0]
    first = report.daily(store, run, day=minute(0), now=minute(1450))
    second = report.daily(store, run, day=minute(1440), now=minute(1450))
    assert first["fees"] == pytest.approx(trade["entry_fee"])
    assert second["fees"] == pytest.approx(trade["exit_fee"])
    assert first["equity"] != first["telegram_snapshot"]["equity"]
    assert f"Equity {engine.state(store, run)['equity']:.2f}" in first["telegram"]


def test_failures_rollback_and_are_observable(store, run):
    class Broken(Inputs):
        def observations(self, hypotheses, activated_at):
            raise RuntimeError("broken data dependency")

    for n in (16, 17, 18):
        with pytest.raises(RuntimeError):
            engine.evaluate(store, run, now=minute(n), sources=Broken())
    assert report.status(store, run, now=minute(19))["failure_count"] == 3
    assert not report.opportunities(store, run)
    assert runtime.ALERT_STREAK["paper_v2"] == 3


def test_actual_source_empty_micro_warmup_does_not_block_other_phases(store, run):
    result = engine.evaluate(store, run, now=minute(67))
    assert result["status"] == "ok"
    signals = report.opportunities(store, run)
    assert {o["hypothesis"]["source_phase"] for o in signals} == {22, 23, 24}
    assert not engine.state(store, run)["pending"]


def test_context_first_seen_never_earlier_publication(store):
    from tests.test_context import ingest
    from tests.test_context import obs as context_observation

    ingest(
        store,
        context_observation(published_at=minute(1).isoformat()),
        at=minute(20).to_pydatetime(),
    )
    before = Sources(store, minute(19)).context("AAVE")
    after = Sources(store, minute(21)).context("AAVE")
    assert before["active_events"] == [] and before["recent_events"] == []
    assert after["active_events"][0]["first_seen_at"] == minute(20).isoformat()


def test_compact_scientific_checkpoint_count_and_completion_time(store, settings):
    from market_signal.perps.data import perp_config
    from market_signal.research.microdir import governance
    from market_signal.research.microdir import spec as micro_spec

    definition = micro_spec.build_definition(perp_config(settings))
    governance.register(store, definition, software=SW, origin="test", reason="frozen study")
    store.con.execute(
        "INSERT INTO lab_prospective_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
        [
            "checkpoint",
            definition.study_id,
            1,
            "daily",
            minute(1440),
            minute(1500),
            SW.software_id,
            None,
            None,
        ],
    )
    payload = {
        "compact": True,
        "members": {
            "continuation_core:long": {
                "episodes": 42,
                "maturity": "EARLY",
                "verdict": "INTERESTING",
            }
        },
    }
    store.con.execute(
        "INSERT INTO lab_prospective_results VALUES (?,?,?,?,?,?,?)",
        ["result", "checkpoint", minute(1501), "COMPLETED", "digest", json.dumps(payload), "{}"],
    )
    assert Sources(store, minute(1500)).science(hypothesis()) == {}
    evidence = Sources(store, minute(1502)).science(hypothesis())
    assert evidence["maturity"] == "EARLY" and evidence["prospective_sample_count"] == 42
    assert evidence["scientific_verdict"] == "INTERESTING"


def test_structural_paper_only_no_transport_keys_or_order_imports():
    root = Path(__file__).parents[1] / "src/market_signal/paper/v2"
    imports = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports += [node.module or ""]
        text = path.read_text().lower()
        for banned in (
            "private_key",
            "eth_account",
            "sign_transaction",
            "place_order",
            "submit_order",
            "wallet_address",
            "getenv(",
            "environ[",
        ):
            assert banned not in text, (path, banned)
    assert not any(
        i.startswith(("httpx", "requests", "websockets", "hyperliquid", "ccxt", "eth_account"))
        for i in imports
    )
    assert not any("paper.execution" in i or "providers" in i for i in imports)


def test_cli_all_read_commands_json_and_no_manual_trade(project, store, monkeypatch):
    engine.register(store, now=START)
    rid = engine.create(store, now=START)["run_id"]
    path = store.path
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(path))
    runner = CliRunner()
    for command in ("status", "trades", "signals", "rejected", "hypotheses", "frequency", "daily"):
        result = runner.invoke(app, ["paper", "v2", command, "--json"])
        assert result.exit_code == 0, (command, result.stdout, result.exception)
        json.loads(result.stdout)
    result = runner.invoke(app, ["paper", "v2", "trade", "BTC"])
    assert result.exit_code != 0
    assert rid.startswith("paperrun_v2_")


@pytest.mark.parametrize("horizon", spec.HORIZONS)
def test_every_registered_fixed_horizon(store, run, horizon):
    h = next(h for h in spec.bootstrap() if h.horizon_minutes == horizon)
    evaluate(store, run, signals=[obs(h)], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    rates = [
        Funding("BTC", minute(n).isoformat(), minute(n).isoformat(), 0.00001)
        for n in range(0, 600, 60)
    ]
    due = 17 + horizon
    evaluate(store, run, at=due, prices=[quote(17), quote(due - 1, 101)], rates=rates)
    assert len(engine.state(store, run)["open"]) == 1
    evaluate(store, run, at=due + 1, prices=[quote(17), quote(due, 101)], rates=rates)
    trade = engine.state(store, run)["closed"][0]
    assert trade["exit_at"] == minute(due).isoformat()
    assert trade["horizon_minutes"] == horizon
    assert trade["funding"] >= 0


def test_actual_microstructure_ingest_availability_and_warmup(store):
    from market_signal.microstructure import definitions as md
    from market_signal.research.microdir import synthetic as sy
    from tests.test_microstructure_direction import _seed

    frame = sy.market(sy.Scenario("n", coins=("BTC",), days=4, seed=9))["BTC"]
    _seed(store, frame, "BTC")
    store.con.execute(
        "INSERT INTO microstructure_cutover VALUES (?,?,?,?,?,?,?)",
        [
            md.FEATURE_VERSION,
            "test",
            sy.T0,
            "BTC",
            sy.T0 + pd.Timedelta(minutes=1, seconds=5),
            "s",
            sy.T0,
        ],
    )
    cut = sy.T0 + pd.Timedelta(days=4) - pd.Timedelta(minutes=1)
    engine.register(store, now=cut)
    rid = engine.create(store, now=cut)["run_id"]
    known = sy.T0 + pd.Timedelta(days=4, minutes=7)
    src = Sources(store, known)
    src.run_id = rid
    observations = src.observations(spec.bootstrap(), cut)
    micro = [
        o
        for o in observations
        if o.asset == "BTC"
        and next(h for h in spec.bootstrap() if h.hypothesis_id == o.hypothesis_id).source_phase
        == 24
    ]
    assert len(micro) == 8 and all(o.warm for o in micro)
    assert all(pd.Timestamp(o.available_at) <= known for o in micro)
    # A finalized window still in the spool is unavailable to the paper database consumer.
    store.con.execute(
        "UPDATE microstructure_minutes SET ingested_at=? WHERE minute_open >= ?",
        [known + pd.Timedelta(minutes=20), known.floor("15min") - pd.Timedelta(minutes=15)],
    )
    src = Sources(store, known)
    src.run_id = rid
    observations = src.observations(spec.bootstrap(), cut)
    micro = [
        o
        for o in observations
        if o.asset == "BTC"
        and next(h for h in spec.bootstrap() if h.hypothesis_id == o.hypothesis_id).source_phase
        == 24
    ]
    assert all(not o.healthy and not o.fired for o in micro)


def test_hourly_public_funding_refresh_is_bounded_and_preserves_history(store, settings):
    from market_signal.perps.data import update_settled_funding

    class PublicFunding:
        calls = []

        def funding_history(self, coin, start, end, page_size):
            self.calls.append((coin, start, end, page_size))
            return pd.DataFrame(
                {
                    "time": [minute(0), minute(60)],
                    "funding_rate": [0.0001, 0.0002],
                    "premium": [0, 0],
                }
            )

        def drain_raw(self):
            return []

    provider = PublicFunding()
    out = update_settled_funding(settings, store, provider, now=minute(67))
    assert len(out) == len(spec.COINS) and len(provider.calls) == len(spec.COINS)
    before = store.con.execute("SELECT * FROM perp_funding ORDER BY coin,time").fetchall()
    update_settled_funding(settings, store, provider, now=minute(68))
    assert len(provider.calls) == len(spec.COINS)
    assert store.con.execute("SELECT * FROM perp_funding ORDER BY coin,time").fetchall() == before


def test_unactivated_v2_jobs_do_not_start_children(store):
    for job in ("paper_v2", "paper_v2_brief", "paper_v2_funding"):
        assert runtime.active_steps(store, job) == []


def test_policy_and_universe_tampering_cannot_change_risk(store, run):
    p = spec.RiskPolicy()
    store.con.execute(
        "UPDATE paper_v2_policies SET definition='{}' WHERE policy_id=?", [p.policy_id]
    )
    with pytest.raises(ValueError, match="identity/content mismatch"):
        evaluate(store, run, signals=[obs()], prices=[quote(15)])
