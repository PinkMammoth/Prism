"""Cohort identity, decision causality, bounded queries and immutable snapshot contracts."""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json

import pytest

from market_signal.paper.dashboard.decisions import Decisions
from market_signal.paper.dashboard.report import Report, runs
from market_signal.paper.nimble import engine, events
from market_signal.paper.v2 import engine as baseline
from tests.test_event_reaction import enroll, evaluate, monitor, source  # noqa: F401
from tests.test_nimble_paper import T0, admit, closed_tp, observation, time
from tests.test_paper_dashboard import evaluate as evaluate_v2
from tests.test_paper_dashboard import hypothesis, obs, quote, render_page


@pytest.fixture
def event_runs(store):
    baseline.register(store, now=T0)
    v2 = baseline.create(store, now=T0)["run_id"]
    engine.register(store, now=T0)
    original = engine.create(store, now=T0)["run_id"]
    engine.register(store, now=T0, events=True)
    cohorts = {
        v: engine.create(store, now=T0, events=True, variant=v)["run_id"]
        for v in ("immediate", "confirmed")
    }
    return {"v2": v2, "nimble": original, **cohorts}


def report(store, rid, start=None, end=None):
    run = next(r for r in runs(store.con) if r["run_id"] == rid)
    return Report(store.con, run, start=start, end=end or time(300))


def test_frozen_cohort_metadata_hypothesis_ids_and_retired_baseline(store, event_runs):
    baseline.event(
        store, event_runs["v2"], "run_state", "fixture-paused", {"state": "PAUSED"}, time(4)
    )
    metadata = {r["cohort"]: r for r in runs(store.con)}
    assert set(metadata) == {"v2", "nimble", "event_immediate", "event_confirmed"}
    assert len({r["label"] for r in metadata.values()}) == 4
    assert metadata["v2"]["status"] == "PAUSED"
    assert len(metadata["nimble"]["hypotheses"]) == 28
    for variant in ("immediate", "confirmed"):
        r = metadata["event_" + variant]
        assert r["event_variant"] == variant
        assert r["starting_equity"] == 100
        assert r["baseline_run_id"] == event_runs["v2"]
        assert r["definition"]["baseline_nimble_runs"] == [event_runs["nimble"]]
        authoritative = {h.hypothesis_id for h in engine.hypotheses(store, r["run_id"])}
        assert set(r["hypotheses"]) == authoritative and len(authoritative) == 31
        assert {h["variant"] for h in r["hypotheses"].values() if h.get("source_phase") == 30} == {
            variant
        }


def test_event_decisions_waiting_admission_entry_and_performance(
    store, event_runs, source, monkeypatch
):
    trigger, eid = enroll(store)
    immediate, confirmed = event_runs["immediate"], event_runs["confirmed"]
    evaluate(store, immediate, source, [trigger])
    evaluate(store, confirmed, source, [trigger])
    waiting = Decisions(store.con, report(store, confirmed)).event_overview()
    assert waiting["counts"]["logical_events"] == 1
    assert waiting["counts"]["pending_current"] == 1
    assert waiting["counts"]["confirmation_wait_observations"] == 1
    assert waiting["counts"]["rejected_event_decisions"] == 0
    assert all(h["net_pnl"] is None and h["total_trades"] == 0 for h in waiting["hypotheses"])
    monkeypatch.setattr(
        events,
        "confirmation",
        lambda *args: {
            "observed_at": time(2).isoformat(),
            "available_at": time(2).isoformat(),
            "signed_return": 0.001,
            "signed_taker_fraction": 0.2,
        },
    )
    evaluate(store, confirmed, source, minute=2, price=99.8)
    monitor(store, immediate)
    monitor(store, confirmed, minute=2, price=99.8)
    for rid, variant in ((immediate, "immediate"), (confirmed, "confirmed")):
        r = report(store, rid)
        position = r.open_positions()[0]
        assert position["event_id"] == eid and position["entry_variant"] == variant
        assert position["source_provider"] == "coinbase_status_v1"
        assert position["execution_component"] == "event"
        assert position["hypothesis_name"] and position["family"].startswith("event:")
        d = Decisions(store.con, r)
        entries, _ = d.page(source="opportunity", event_id=eid)
        assert len(entries) == 1 and entries[0]["outcome"] == "ADMITTED"
        detail = d.detail("opportunity", entries[0]["id"])
        assert (
            detail["frozen_entry_thesis"]["context_snapshot"]["event_entry"]["state"]["event_id"]
            == eid
        )
        assert detail["latest_trade_state"]["state"] == "OPEN"
        decisions, _ = d.page(source="event evidence", event_id=eid)
        assert any(p["kind"] == "decision" and p["outcome"] == "ADMITTED" for p in decisions)
    assert report(store, confirmed).trades[0]["confirmation_delay_seconds"] == 60
    assert report(store, confirmed).trades[0]["move_missed_before_fill"] == pytest.approx(0.002)
    target_price = report(store, immediate).open_positions()[0]["target"] * 0.999
    monitor(store, immediate, minute=3, second=0, price=target_price)
    monitor(store, immediate, minute=3, second=2, price=target_price)
    r = report(store, immediate)
    authoritative = engine.positions(store, immediate, terminal=True)[0]
    assert r.stats["closed_trades"] == 1 and r.stats["net_pnl"] == authoritative["net_pnl"]
    assert r.trades[0]["exit_reason"] == "TAKE_PROFIT"
    # The successor's original hypotheses are a separate component of the same account.
    closed_tp(store, immediate, minute=6)
    components = {
        x["execution_component"]: x
        for x in report(store, immediate).groups(("execution_component",))
    }
    assert components["event"]["closed_trades"] == components["original"]["closed_trades"] == 1
    assert sum(x["net_pnl"] for x in components.values()) == pytest.approx(
        report(store, immediate).stats["net_pnl"]
    )
    assert report(store, confirmed).stats["closed_trades"] == 0


def test_v2_and_original_nimble_decision_evidence_and_isolation(store, event_runs):
    v2, original = event_runs["v2"], event_runs["nimble"]
    h = hypothesis()
    evaluate_v2(store, v2, signals=[obs(h)], prices=[quote(15)])
    evaluate_v2(store, v2, at=17, prices=[quote(17)])
    closed_tp(store, original, minute=3)
    for rid in (v2, original):
        d = Decisions(store.con, report(store, rid))
        decisions, total = d.page(source="opportunity")
        assert total == 1 and decisions[0]["outcome"] == "ADMITTED"
        detail = d.detail("opportunity", decisions[0]["id"])
        assert detail["recorded_payload"]["hypothesis"]
        assert detail["latest_trade_state"]["trade_id"] == decisions[0]["trade_id"]
        execution, total = d.page(source="execution", trade_id=decisions[0]["trade_id"])
        assert total >= 1 and all(x["trade_id"] == decisions[0]["trade_id"] for x in execution)
        assert d.event_overview() is None
        with pytest.raises(ValueError, match="selected run"):
            d.detail("opportunity", "other-run-or-unknown-id")
    event_d = Decisions(store.con, report(store, event_runs["confirmed"]))
    assert event_d.page()[1] == 0
    with pytest.raises(ValueError):
        event_d.detail("unsafe_table", "anything")


def test_large_event_ledger_pagination_filters_and_window(store, event_runs):
    rid = event_runs["confirmed"]
    records = [
        [
            f"fixture-{i:04}",
            rid,
            f"event-{i % 5}",
            "classification",
            "BTC",
            time(1 + i % 2),
            "classification",
            json.dumps({"reason": "UNSUPPORTED_EVENT_CLASS"}),
        ]
        for i in range(3001)
    ]
    with store.transaction():
        store.con.executemany("INSERT INTO paper_event_evidence VALUES (?,?,?,?,?,?,?,?)", records)
    d = Decisions(store.con, report(store, rid))
    first, total = d.page()
    second, _ = d.page(page=1)
    assert total == 3001 and len(first) == len(second) == 50
    assert not {p["id"] for p in first} & {p["id"] for p in second}
    filtered, count = d.page(reason="UNSUPPORTED_EVENT_CLASS", asset="BTC", event_id="event-0")
    assert count == 601 and all(p["event_id"] == "event-0" for p in filtered)
    assert d.event_overview()["counts"]["logical_events"] == 5
    assert Decisions(store.con, report(store, rid, start=time(2))).page()[1] == 1500
    assert d.page(reason="x' OR TRUE --")[1] == 0
    assert d.reasons()[0]["count"] == 3001
    with pytest.raises(ValueError):
        d.page(page_size=201)


def test_rejection_reasons_preserve_engine_and_event_stages(store, event_runs, source):
    evaluate_v2(store, event_runs["v2"], signals=[obs(warm=False)], prices=[quote(15)])
    admit(store, event_runs["nimble"], obs=[observation(warm=False)])
    for key in ("v2", "nimble"):
        d = Decisions(store.con, report(store, event_runs[key]))
        selected, count = d.page(reason="FEATURE_WARMUP")
        assert count == 1 and selected[0]["outcome"] == "REJECTED"
        assert d.detail("opportunity", selected[0]["id"])["recorded_payload"]["warm"] is False
    trigger, _ = enroll(store)
    rid = event_runs["confirmed"]
    evaluate(store, rid, source, [trigger])
    evaluate(store, rid, source, minute=200)
    d = Decisions(store.con, report(store, rid))
    waiting, _ = d.page(reason="CONFIRMATION_MISSING")
    rejected, _ = d.page(reason="STALE_EVENT")
    assert waiting[0]["outcome"] == "WAITING" and rejected[0]["outcome"] == "REJECTED"
    assert d.event_overview()["counts"]["rejected_event_decisions"] == 1
    assert d.event_overview()["counts"]["pending_current"] == 0


def test_old_snapshot_without_event_tables_degrades_explicitly(store, event_runs):
    store.con.execute("DROP TABLE paper_event_evidence")
    store.con.execute("DROP TABLE paper_event_pending")
    r = report(store, event_runs["immediate"])
    d = Decisions(store.con, r)
    assert d.event_overview() == {"available": False}
    assert d.page() == ([], 0)
    assert len(r.run["hypotheses"]) == 31 and r.stats["current_equity"] == 100


def test_frontend_all_four_runs_decision_inspector_and_read_only(
    monkeypatch, store, event_runs, source
):
    trigger, _ = enroll(store)
    evaluate(store, event_runs["confirmed"], source, [trigger])
    baseline.event(
        store, event_runs["v2"], "run_state", "fixture-paused", {"state": "PAUSED"}, time(4)
    )
    store.close()
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    at = render_page(monkeypatch, store.path)
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    labels = {s.label for s in at.selectbox}
    assert {"Nimble · original", "Nimble events · immediate", "Nimble events · confirmed"} <= labels
    assert any("Retired strategy" in c.value for c in at.caption)
    run_selector = next(s for s in at.selectbox if s.label == "Inspect decisions for")
    run_selector.select_index(3).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    inspector = next(s for s in at.selectbox if s.label == "Inspect a recorded decision")
    inspector.select_index(1).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
    assert at.json
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    assert all("pause" not in b.label.lower() and "trade" not in b.label.lower() for b in at.button)
