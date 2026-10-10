"""Operational retirement preserves the experiment and its frozen exit."""
# ruff: noqa: F811
import json
from contextlib import closing

import pytest

from market_signal.data.store import Store
from market_signal.ops import runtime
from market_signal.paper.v2 import engine, report, spec
from market_signal.paper.v2.data import Funding
from tests.test_exploratory_paper_v2 import evaluate, minute, obs, quote, run  # noqa: F401


def ledger(store):
    return {t: store.con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            for (t,) in store.con.execute("SHOW TABLES").fetchall() if t.startswith("paper_v2_")}


def test_drain_preserves_frozen_exit_and_history(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    before = ledger(store)
    result = engine.retire(store, run, now=minute(19))
    assert result["state"] == "DRAINING"
    evaluate(store, run, at=20, signals=[obs(asset="ETH", at=20)], prices=[quote(17), quote(20)])
    assert len(engine.state(store, run)["open"]) == 1
    assert runtime.active_steps(store, "paper_v2")
    evaluate(store, run, at=33, prices=[quote(17), quote(32, 101)])
    st = engine.state(store, run)
    assert st["status"] == "RETIRED"
    assert st["retired_at"] == minute(33).isoformat()
    assert st["admission_stopped_at"] == minute(19).isoformat()
    assert st["closed"][0]["exit_reason"] == "FIXED_HORIZON"
    assert st["closed"][0]["exit_at"] == minute(32).isoformat()
    after = ledger(store)
    for table, records in before.items():
        assert all(row in after[table] for row in records)
    assert before["paper_v2_opportunities"] == after["paper_v2_opportunities"]
    frozen = ledger(store)
    assert engine.retire(store, run, now=minute(40))["retired_at"] == minute(33).isoformat()
    assert evaluate(store, run, at=40)["evaluated"] is False
    engine.manage(store, run, [quote(40)], [], minute(40))
    engine.outcomes(store, run, [quote(40)], [], minute(40))
    engine.degrade(store, run, minute(40))
    assert engine.admit(store, run, [obs(at=40)], [], [], minute(40), minute(0)) == {}
    with pytest.raises(ValueError, match="frozen exit"):
        engine.kill(store, run, reason="test")
    sent = []
    assert report.record_daily(store, run, now=minute(1500), sender=sent.append)["recorded"] is False
    assert not sent
    assert ledger(store) == frozen
    for job in ("paper_v2", "paper_v2_brief", "paper_v2_funding"):
        assert runtime.active_steps(store, job) == []
    tab = runtime.crontab(excluded_jobs=("paper_v2", "paper_v2_brief"))
    assert "ops cycle paper_v2 " not in tab
    assert "ops cycle paper_v2_brief " not in tab
    assert "ops cycle paper_nimble_brief " in tab
    assert report.status(store, run)["historical_baseline"]
    assert report.explain(store, run, st["closed"][0]["trade_id"])["context_snapshots"]


def test_flat_and_pending_retire_immediately(store, run):
    evaluate(store, run, signals=[obs()], prices=[quote(15)])
    original = ledger(store)
    result = engine.retire(store, run, now=minute(17))
    assert result["state"] == "RETIRED"
    assert result["closed_trades"] == 0
    assert not engine.state(store, run)["pending"]
    expired = store.con.execute("SELECT payload FROM paper_v2_events WHERE event_type='expired'").fetchone()
    assert json.loads(expired[0])["reason"] == "RETIREMENT"
    for table, records in original.items():
        assert all(row in ledger(store)[table] for row in records)


def test_drain_waits_for_frozen_funding_settlement(store, run):
    h = next(h for h in spec.bootstrap() if h.source_phase == 23 and h.horizon_minutes == 60)
    evaluate(store, run, signals=[obs(h)], prices=[quote(15)])
    evaluate(store, run, at=18, prices=[quote(17)])
    engine.retire(store, run, now=minute(19))
    evaluate(store, run, at=78, prices=[quote(17), quote(77, 101)], rates=[])
    assert engine.state(store, run)["status"] == "DRAINING"
    assert engine.state(store, run)["open"]
    rate = Funding("BTC", minute(60).isoformat(), minute(79).isoformat(), 0.0001)
    evaluate(store, run, at=80, prices=[quote(17), quote(77, 101)], rates=[rate])
    st = engine.state(store, run)
    assert st["status"] == "RETIRED"
    assert st["retired_at"] == minute(80).isoformat()
    assert st["closed"][0]["exit_at"] == minute(77).isoformat()
    assert st["closed"][0]["exit_reason"] == "FIXED_HORIZON"
    assert st["closed"][0]["funding"] != 0


def test_research_horizons_continue_with_v2_runtime_retired(store, run):
    from market_signal.paper.nimble import engine as nimble
    from tests.test_nimble_paper import closed_tp, time
    from tests.test_nimble_paper import quote as nimble_quote

    engine.retire(store, run, now=minute(1))
    frozen = ledger(store)
    nimble.register(store, now=minute(2))
    nimble_run = nimble.create(store, now=minute(2))["run_id"]
    closed = closed_tp(store, nimble_run, minute=3)
    with store.transaction():
        nimble.research(store, nimble_run,
                        [nimble_quote(484, price=102)], now=time(484))
    outcomes = store.con.execute(
        "SELECT horizon_minutes,payload FROM paper_nimble_research ORDER BY horizon_minutes"
    ).fetchall()
    assert [h for h, _ in outcomes] == [15, 30, 60, 120, 240, 480]
    assert all(json.loads(p)["execution_exit_independent"] for _, p in outcomes)
    assert nimble.state(store, nimble_run)["closed"][0] == closed
    assert ledger(store) == frozen
    assert runtime.active_steps(store, "paper_v2") == []
    assert runtime.active_steps(store, "paper_v2_brief") == []
    assert runtime.active_steps(store, "paper_v2_funding") == runtime.JOBS["paper_v2_funding"]
    assert runtime.active_steps(store, "paper_nimble_brief") == runtime.JOBS["paper_nimble_brief"]


def test_retired_scheduled_jobs_write_no_cycles_or_notifications(store, run, monkeypatch):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    engine.retire(store, run, now=minute(1))
    db = store.path
    before = ledger(store)
    cycles = store.con.execute("SELECT * FROM runtime_cycles ORDER BY ALL").fetchall()
    store.close()
    monkeypatch.setattr(runtime, "_db", lambda: db)
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "test")
    calls = []
    for job in ("paper_v2", "paper_v2_brief", "paper_v2_funding"):
        result = runtime.run_job(job, trigger="schedule", wait=0,
                                 step_runner=lambda *args: calls.append(args))
        assert result["writes"] == 0
    result = CliRunner().invoke(app, ["ops", "crontab"])
    assert result.exit_code == 0
    assert "ops cycle paper_v2 " not in result.stdout
    assert "ops cycle paper_v2_brief " not in result.stdout
    assert "ops cycle paper_nimble_brief " in result.stdout
    assert not calls
    with closing(Store(db, read_only=True)) as reader:
        assert reader.con.execute("SELECT * FROM runtime_cycles ORDER BY ALL").fetchall() == cycles
        assert ledger(reader) == before
