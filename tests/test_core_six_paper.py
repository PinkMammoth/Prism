"""Static core-six successor: identity, allowlist, venue guard, fill-first worker and funnel.

All trades here are synthetic scratch fixtures; none can reach prospective evidence.
"""

from __future__ import annotations

from contextlib import closing

import pandas as pd
import pytest

from market_signal.data.store import Store
from market_signal.ops.runtime import runtime_lock
from market_signal.paper.nimble import core_spec, engine, events, report, spec
from market_signal.paper.nimble.model import ExecutableQuote
from market_signal.paper.v2 import spec as baseline
from market_signal.paper.v2.data import Funding
from tests.test_event_reaction import enroll, venue
from tests.test_nimble_paper import T0, Source, admit, h, monitor, quote, time

PERSISTENT = ("continuation_persistent", "long")  # allowlisted Phase 24B identity


@pytest.fixture
def core(store):
    engine.register(store, now=T0, core=True)
    return engine.create(store, now=T0, core=True)["run_id"]


def opened(store, rid, minute=1, hh=None, rate=0):
    assert admit(store, rid, hh or h(*PERSISTENT), minute, rate=rate) == {"ADMITTED": 1}
    monitor(store, rid, minute, 2, rate=rate)
    return next(iter(engine.state(store, rid)["open"].values()))


def test_successor_identity_inherits_execution_and_partitions_universe():
    d = core_spec.definition()
    parent = spec.definition()
    assert core_spec.policy_id() != spec.policy_id()
    assert d["parent_policy_id"] == spec.policy_id()
    for key in ("execution", "risk", "geometry", "research_horizons", "holding_regimes"):
        assert d[key] == parent[key]
    assert d["execution"]["economic_cost_multiple"] == 2.0
    assert d["execution"]["entry_wait_seconds"] == 60
    assert d["execution"]["quote_max_age_seconds"] == 3
    assert d["assets"] == list(baseline.COINS)
    assert d["classification"] == "PROSPECTIVE_EXPLORATORY_VALIDATION_COHORT"
    ids = {h.hypothesis_id for h in core_spec.bootstrap()}
    excluded = {e["hypothesis_id"] for e in d["excluded"]}
    assert len(ids) == 16 and len(excluded) == 12 and not ids & excluded
    assert ids | excluded == {h.hypothesis_id for h in baseline.bootstrap()}
    assert all(e["evidence_status"] == "REJECTED" for e in d["excluded"])
    assert all(e["evidence_status"] != "REJECTED" for e in d["allowlist"])
    assert set(d["mappings"]) == ids
    assert all(e["execution_mapping"] == parent["mappings"][e["hypothesis_id"]]
               for e in d["allowlist"])  # fmt: skip


def test_core_run_is_static_single_account_and_frozen(store, core):
    run = engine.load(store, core)
    assert run["policy_id"] == core_spec.policy_id()
    assert len(engine.hypotheses(store, core)) == 16
    with pytest.raises(ValueError, match="already active"):
        engine.create(store, now=time(1), core=True)
    store.con.execute("UPDATE paper_nimble_policies SET definition='{}'")
    with pytest.raises(ValueError, match="identity/content"):
        engine.load(store, core)


def test_core_refuses_while_another_nimble_account_is_active(store):
    engine.register(store, now=T0)
    engine.create(store, now=T0)
    engine.register(store, now=T0, core=True)
    with pytest.raises(ValueError, match="already active"):
        engine.create(store, now=time(1), core=True)


def test_excluded_rejected_identity_is_never_admitted(store, core):
    assert admit(store, core, h("continuation_core"), 1) == {"REJECTED": 1}
    row = store.con.execute(
        "SELECT rejection_reason FROM paper_nimble_opportunities WHERE run_id=?", [core]
    ).fetchone()
    assert row == ("HYPOTHESIS_NOT_REGISTERED",)


def test_core_thesis_records_successor_policy(store, core):
    p = opened(store, core)
    assert p["thesis"]["execution_policy_id"] == core_spec.policy_id()


def test_duplicate_delivery_and_stale_quote_never_fill(store, core):
    assert admit(store, core, h(*PERSISTENT), 1) == {"ADMITTED": 1}
    assert admit(store, core, h(*PERSISTENT), 1) == {}
    stale = quote(1, 0)  # observed before the intent: never a future fill
    monitor(store, core, 1, 2, qs=[stale])
    assert engine.positions(store, core)[0]["state"] == "PENDING"
    monitor(store, core, 2, 1, qs=[quote(1, 50)])  # 11 s old at evaluation: invalid
    p = engine.positions(store, core, terminal=True)[0]
    assert p["state"] == "CANCELLED" and p["reason"] == "EXECUTION_EXPIRED"
    assert "entry_at" not in p


def test_venue_outage_blocks_admission_entry_and_exit_fills(store, core):
    assert admit(store, core, h(*PERSISTENT), 1) == {"ADMITTED": 1}
    enroll(store, venue(entity="hyperliquid"), time(1, 1))
    monitor(store, core, 1, 2)
    assert engine.positions(store, core, terminal=True)[0]["reason"] == "VENUE_UNAVAILABLE"
    assert admit(store, core, h(*PERSISTENT), 2) == {"REJECTED": 1}
    reasons = store.con.execute(
        "SELECT rejection_reason FROM paper_nimble_opportunities WHERE run_id=? ORDER BY 1",
        [core],
    ).fetchall()
    assert ("VENUE_UNAVAILABLE",) in reasons
    enroll(store, venue(entity="hyperliquid", status="resolved"), time(20))
    assert events.unavailable(store, time(20)) is None


def test_open_position_exit_waits_for_venue_recovery(store, core):
    opened(store, core)
    monitor(store, core, 2, 0, price=102)  # TP observed -> exit intent
    assert next(iter(engine.state(store, core)["open"].values()))["state"] == "EXIT_PENDING"
    enroll(store, venue(entity="hyperliquid", at=time(2, 1)), time(2, 1))
    monitor(store, core, 2, 2, price=102)
    assert next(iter(engine.state(store, core)["open"].values()))["state"] == "EXIT_PENDING"
    enroll(store, venue(entity="hyperliquid", status="resolved", at=time(3)), time(3))
    monitor(store, core, 3, 1, price=102)
    assert engine.state(store, core)["closed"][0]["exit_reason"] == "TAKE_PROFIT"


def test_fills_only_executes_intents_but_decides_nothing(store, core):
    assert admit(store, core, h(*PERSISTENT), 1) == {"ADMITTED": 1}
    with store.transaction():
        engine.monitor(store, core, [quote(1, 2)], (), (), {"BTC": {"funding_rate": 0}},
                       now=time(1, 2), fills_only=True)  # fmt: skip
    p = next(iter(engine.state(store, core)["open"].values()))
    assert p["state"] == "OPEN"
    marks = store.con.execute(
        "SELECT count(*) FROM paper_nimble_events WHERE run_id=? AND event_type='account_mark' "
        "AND recorded_at=?",
        [core, time(1, 2).to_pydatetime()],
    ).fetchone()[0]
    assert marks == 0
    with store.transaction():
        engine.monitor(store, core, [quote(2, 0, 102)], (), (), {"BTC": {"funding_rate": 0}},
                       now=time(2), fills_only=True)  # fmt: skip
    assert next(iter(engine.state(store, core)["open"].values()))["state"] == "OPEN"
    monitor(store, core, 2, 1, price=102)
    assert next(iter(engine.state(store, core)["open"].values()))["state"] == "EXIT_PENDING"


def test_missing_funding_reconciles_to_final_accounting(store, core):
    p = opened(store, core, 59, h("crowding_vulnerability"), rate=0.0001)
    monitor(store, core, 60, 0, 103, rate=0.0001)
    monitor(store, core, 60, 2, 103, rate=0.0001)
    c = engine.state(store, core)["closed"][0]
    assert not c["accounting_final"] and c["funding_reserve"] > 0
    rates = [Funding("BTC", time(60).isoformat(), time(61).isoformat(), 0.0001)]
    with store.transaction():
        engine.reconcile(store, core, rates, time(61))
    c = engine.state(store, core)["closed"][0]
    assert c["accounting_final"] and c["funding"] == pytest.approx(p["notional"] * 0.0001)


def test_admin_drain_suspends_entries_but_manages_open_position(store, core):
    opened(store, core)
    engine.kill(store, core, reason="scratch drain", now=time(1, 5))
    assert admit(store, core, h("crowding_vulnerability"), 2) == {"REJECTED": 1}
    monitor(store, core, 2, 2)
    c = engine.state(store, core)
    assert not c["open"] or next(iter(c["open"].values()))["state"] == "EXIT_PENDING"
    monitor(store, core, 2, 4)
    c = engine.state(store, core)
    assert not c["open"] and c["closed"][0]["exit_reason"] == "ADMIN_STOP"


def test_recover_rebuilds_core_account_and_positions(store, core):
    opened(store, core)
    before = engine.state(store, core)
    store.con.execute("DELETE FROM paper_nimble_positions WHERE run_id=?", [core])
    store.con.execute("DELETE FROM paper_nimble_accounts WHERE run_id=?", [core])
    engine.recover(store, core)
    assert engine.state(store, core) == before


# --------------------------------------------------------------------------- worker timing


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at.to_pydatetime()

    def advance(self, seconds):
        self.at += pd.Timedelta(seconds=seconds)


def live(clock):
    def quotes(store, at):
        t = clock.at.isoformat()
        q = ExecutableQuote("BTC", t, t, 99.99, 100.01)
        return [q], {"BTC": {"funding_rate": 0}}

    return quotes


@pytest.fixture
def pending_worker(store, core, monkeypatch):
    """A real intent at T+1 min; worker inputs replaced only where they touch the network."""
    from market_signal.paper.nimble import worker as module

    assert admit(store, core, h(*PERSISTENT), 1) == {"ADMITTED": 1}
    path = store.path
    store.close()
    clock = Clock(time(1))
    monkeypatch.setattr(module, "utcnow", clock)
    monkeypatch.setattr(module, "require_authoritative", lambda db: None)
    monkeypatch.setattr(module, "quotes_for_store", live(clock))
    monkeypatch.setattr(module, "live_cache", lambda root, at, **kw: live(clock)(None, at))

    class Inputs(Source):
        def __init__(self, s, at):
            super().__init__()

        def observations(self, hs, activation, *, assets):
            return []

        def updates(self, assets):
            return []

        def funding(self, since):
            return []

        def research_quotes(self, assets, since):
            return []

    monkeypatch.setattr(module, "Sources", Inputs)
    # Measured Lab ingest/discovery: seconds of work under the same lock before monitoring.
    monkeypatch.setattr(module.Worker, "_spool_changed", lambda self: (True, {}))
    monkeypatch.setattr(module, "ingest", lambda *a, **k: clock.advance(15) or {"status": "ok"})
    monkeypatch.setattr(module.triggers, "discover", lambda *a, **k: clock.advance(5))
    w = module.Worker(path, run_id=core)

    def state():
        with closing(Store(path)) as s:
            return engine.positions(s, core, terminal=True)[0]

    return w, clock, state, module


def test_reproduced_slow_pre_monitor_work_expired_hourly_intent(pending_worker, monkeypatch):
    """Pre-fix ordering: 20 s of ingest/discovery before monitoring ages a 50 s intent past 60 s."""
    w, clock, state, module = pending_worker
    monkeypatch.setattr(module.Worker, "_fill_intents", lambda self, store, now: None)
    clock.advance(50)
    assert w.tick()["state"] == "OK"
    p = state()
    assert p["state"] == "CANCELLED" and p["reason"] == "EXECUTION_EXPIRED"


def test_fill_first_pass_fills_inside_frozen_window(pending_worker):
    w, clock, state, _ = pending_worker
    clock.advance(50)
    r = w.tick()
    p = state()
    assert p["state"] == "OPEN" and p["intent_to_fill_seconds"] == pytest.approx(50)
    assert r["intent_fill_pass"]["at"] == time(1, 50).isoformat()


def test_lock_contention_then_release_fills_without_backdating(pending_worker):
    w, clock, state, _ = pending_worker
    with runtime_lock(w.db, "scheduler-simulation", wait=0):
        for _ in range(4):
            clock.advance(10)
            assert w.tick()["state"] == "BUSY"
    clock.advance(2)
    w.tick()
    p = state()
    assert p["state"] == "OPEN" and p["intent_to_fill_seconds"] == pytest.approx(42)
    assert p["entry_quote_at"] == time(1, 42).isoformat()


def test_contention_beyond_window_expires_without_fabricated_fill(pending_worker):
    w, clock, state, _ = pending_worker
    with runtime_lock(w.db, "scheduler-simulation", wait=0):
        clock.advance(61)
        assert w.tick()["state"] == "BUSY"
    w.tick()
    p = state()
    assert p["state"] == "CANCELLED" and p["reason"] == "EXECUTION_EXPIRED"
    assert "entry_fill" not in p


def test_restarted_worker_fills_pending_intent(pending_worker):
    w, clock, state, module = pending_worker
    clock.advance(30)
    fresh = module.Worker(w.db, run_id=w.rid)  # restart: no cached positions
    fresh.tick()
    assert state()["state"] == "OPEN"


# --------------------------------------------------------------------------- reporting


def test_funnel_attributes_every_stage(store, core):
    opened(store, core)
    monitor(store, core, 2, 0, price=102)
    monitor(store, core, 2, 2, price=102)
    admit(store, core, h("continuation_core"), 3)
    assert admit(store, core, h(*PERSISTENT), 30) == {"ADMITTED": 1}
    monitor(store, core, 32, 0, qs=[])
    f = report.funnel(store, core)
    assert f["admitted_intents"] == 2 and f["fills"] == 1
    assert f["outcomes"]["HYPOTHESIS_NOT_REGISTERED"] == 1
    assert f["intents"]["execution_expired"] == 1
    assert f["closes"] == {"total": 1, "exit_reasons": {"TAKE_PROFIT": 1},
                           "accounting_final": 1, "accounting_provisional": 0}  # fmt: skip
    assert f["latency_seconds"]["intent_to_fill"]["max"] == pytest.approx(2)
    assert f["evaluations"]["deferred_never_ready"] >= 0
    assert set(f["input_frontiers"]) == set(baseline.COINS)
    assert f["pnl"]["fees"] > 0 and f["account"]["status"] == "ACTIVE"


def test_funnel_and_status_are_read_only(project, store, core, monkeypatch):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    opened(store, core)
    before = store.con.execute("SELECT count(*) FROM paper_nimble_events").fetchone()
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(project / "data" / "test.duckdb"))
    result = CliRunner().invoke(app, ["paper", "nimble", "funnel", "--run-id", core])
    assert result.exit_code == 0, result.output
    assert '"fills": 1' in result.output.replace("\n", " ") or '"fills":1' in result.output
    with closing(Store(project / "data" / "test.duckdb", read_only=True)) as s:
        assert s.con.execute("SELECT count(*) FROM paper_nimble_events").fetchone() == before


def test_brief_records_daily_report_without_telegram(project, store, core, monkeypatch):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(project / "data" / "test.duckdb"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    result = CliRunner().invoke(app, ["paper", "nimble", "brief", "--send"])
    assert result.exit_code == 0, result.output
    assert "unconfigured" in result.output
    with closing(Store(project / "data" / "test.duckdb", read_only=True)) as s:
        assert s.con.execute(
            "SELECT count(*) FROM paper_nimble_reports WHERE run_id=?", [core]
        ).fetchone() == (1,)
