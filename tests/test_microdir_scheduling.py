"""Phase 24B operations: scheduled checkpoints of the frozen study on the existing runtime.

End to end without the network or real data: ``run_job`` (runtime lock, authority, cycle
records, alerts) -> the real ``market lab microstructure checkpoint`` command (in-process) ->
governance (fixed grid, in-order catch-up). Only the study evaluation itself is faked.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

import pandas as pd
import pytest
from typer.testing import CliRunner

from market_signal.data.store import Store
from market_signal.ops import runtime as rt
from market_signal.research.microdir import governance as gv
from market_signal.research.microdir import spec as sp

RID = "railway-prism-runtime"
PINNED_STUDY_ID = "sstudy_16c5f72d6c4ec79e34d7d7bda50050323219d27df17fc2f76189afcc7684c8a9"


# --------------------------------------------------------------------------- schedule


def test_daily_and_weekly_monday_schedule():
    assert rt.JOBS["microdir_daily"] == [
        ("checkpoint", ["lab", "microstructure", "checkpoint", "--cadence", "daily"])
    ]
    assert rt.JOBS["microdir_weekly"] == [
        ("checkpoint", ["lab", "microstructure", "checkpoint", "--cadence", "weekly"])
    ]
    assert rt.SCHEDULE["microdir_daily"] == ["01:10"]
    assert rt.SCHEDULE["microdir_weekly"] == ["Mon 01:20"]
    tab = rt.crontab()
    assert "10 1 * * * market ops cycle microdir_daily --trigger schedule --wait 3600" in tab
    assert "20 1 * * 1 market ops cycle microdir_weekly --trigger schedule --wait 3600" in tab
    # existing entries render exactly as before (weekday support changed nothing else)
    assert "6 * * * * market ops cycle microstructure --trigger schedule --wait 600" in tab
    assert "10 0 * * * market ops cycle prospective --trigger schedule --wait 3600" in tab
    t = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
    assert rt.next_run("microdir_daily", t(2026, 10, 9, 1, 10)) == t(2026, 10, 10, 1, 10)
    assert rt.next_run("microdir_daily", t(2026, 10, 9, 0, 59)) == t(2026, 10, 9, 1, 10)
    assert rt.next_run("microdir_weekly", t(2026, 10, 8, 21, 0)) == t(2026, 10, 12, 1, 20)  # Thu
    assert rt.next_run("microdir_weekly", t(2026, 10, 12, 1, 0)) == t(2026, 10, 12, 1, 20)  # Mon
    assert rt.next_run("microdir_weekly", t(2026, 10, 12, 1, 20)) == t(2026, 10, 19, 1, 20)
    # both run after their 00:00 instant has settled (governance SETTLE = 1 h)
    assert pd.Timedelta(minutes=70) >= gv.SETTLE
    with pytest.raises(ValueError):
        rt._parse("Funday 01:00")


def test_scheduling_changes_no_study_identity():
    defn = sp.MicroStudyDefinition(definition=sp.frozen(), costs=_costs())
    assert defn.study_id == PINNED_STUDY_ID


def _costs():
    from market_signal.cli.common import get_settings
    from market_signal.perps.data import perp_config

    return sp.build_definition(perp_config(get_settings())).costs


# --------------------------------------------------------------------------- end to end


@pytest.fixture
def runtime(project, monkeypatch):
    """A claimed live database, this process as the authoritative runtime, the study
    registered ``days`` ago (set by each test) and a fake, recorded evaluation."""
    monkeypatch.setenv("PRISM_LOCK_TIMEOUT", "5")
    monkeypatch.delenv("PRISM_HEARTBEAT_URL", raising=False)
    db = project / "data" / "prism.duckdb"
    Store(db).close()
    rt.claim_authority(db, RID, "test")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", RID)
    alerts: list[str] = []
    monkeypatch.setattr(rt, "infra_alert", alerts.append)
    evaluated: list[pd.Timestamp] = []
    fail_at: set[pd.Timestamp] = set()

    def evaluator(store, registered_at=None):
        def run(defn, as_of):
            evaluated.append(pd.Timestamp(as_of))
            if pd.Timestamp(as_of) in fail_at:
                fail_at.discard(pd.Timestamp(as_of))  # transient: the retry succeeds
                raise RuntimeError("transient evaluation failure")
            return {"study": sp.STUDY_NAME, "as_of": pd.Timestamp(as_of).isoformat(),
                    "maturity": {"study_level": "WARMUP"}, "members": {},
                    "summary": {"verdicts": {}}}, {}  # fmt: skip

        return run

    monkeypatch.setattr("market_signal.research.microdir.data.evaluator", evaluator)

    class R:
        pass

    r = R()
    r.db, r.alerts, r.evaluated, r.fail_at = db, alerts, evaluated, fail_at

    def register(ago: pd.Timedelta):
        from market_signal.cli.lab_cmds import _software

        with closing(Store(db)) as s:
            defn = sp.MicroStudyDefinition(definition=sp.frozen(), costs=_costs())
            gv.register(s, defn, software=_software(), origin="test", reason="test")
            s.con.execute("UPDATE lab_prospective_studies SET registered_at=?",  # test backdate
                          [(pd.Timestamp.now(tz="UTC") - ago).to_pydatetime()])  # fmt: skip
        return defn.study_id

    r.register = register
    return r


def _cli_steps(seen: list[int]):
    """Step runner that runs the real CLI in-process (so the fake evaluation applies)."""
    from market_signal.cli.main import app

    def run(job, name, args):
        res = CliRunner().invoke(app, args)
        seen.append(res.exit_code)
        return {"step": name, "exit": res.exit_code, "seconds": 0.0, "output": res.output}

    return run


def _checkpoints(db, sid):
    with closing(Store(db, read_only=True)) as s:
        return gv.checkpoints(s, sid)


def _expected(reg_ago: pd.Timedelta, cadence: str) -> list[pd.Timestamp]:
    now = pd.Timestamp.now(tz="UTC")
    t = gv.grid_after(now - reg_ago, cadence)
    out = []
    while t <= now - gv.SETTLE:
        out.append(t)
        t = gv.grid_after(t, cadence)
    return out


def test_scheduled_daily_run_catches_up_in_order_on_the_grid_and_is_idempotent(runtime):
    ago = pd.Timedelta(days=4, hours=3)
    sid = runtime.register(ago)
    want = _expected(ago, "daily")
    assert len(want) >= 3  # downtime: several daily instants owed at once
    seen: list[int] = []
    out = rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    assert out["status"] == "ok" and seen == [0]
    cps = _checkpoints(runtime.db, sid)
    assert [c["as_of"] for c in cps] == want  # in order, every owed instant, none skipped
    assert all(c["as_of"] == c["as_of"].floor("D") for c in cps)  # 00:00 grid, not run time
    assert all(c["cadence"] == "daily" and c["status"] == "COMPLETED" for c in cps)
    assert all(pd.Timestamp(c["started_at"]) > c["as_of"] for c in cps)
    # a duplicate invocation (same day, or a manual run) changes nothing
    out = rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    assert out["status"] == "ok" and len(_checkpoints(runtime.db, sid)) == len(want)
    assert runtime.evaluated == want and runtime.alerts == []


def test_scheduled_weekly_run_evaluates_only_monday_instants(runtime):
    ago = pd.Timedelta(days=16)
    sid = runtime.register(ago)
    want = _expected(ago, "weekly")
    assert len(want) >= 2 and all(t.weekday() == 0 and t == t.floor("D") for t in want)
    seen: list[int] = []
    rt.run_job("microdir_weekly", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    cps = _checkpoints(runtime.db, sid)
    assert [c["as_of"] for c in cps] == want and {c["cadence"] for c in cps} == {"weekly"}
    rt.run_job("microdir_weekly", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    assert len(_checkpoints(runtime.db, sid)) == len(want) and seen == [0, 0]


def test_failure_is_recorded_blocks_later_instants_and_is_retried_next_run(runtime):
    ago = pd.Timedelta(days=4, hours=3)
    sid = runtime.register(ago)
    want = _expected(ago, "daily")
    runtime.fail_at.add(want[1])
    seen: list[int] = []
    out = rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    assert out["status"] == "failed" and seen == [1]
    cps = _checkpoints(runtime.db, sid)
    assert [(c["as_of"], c["status"]) for c in cps] == [(want[0], "COMPLETED"),
                                                        (want[1], "FAILED")]  # fmt: skip
    with closing(Store(runtime.db, read_only=True)) as s:
        st = gv.schedule_state(s, sid)["daily"]
        assert st["next_owed_as_of"] == want[1].isoformat()  # same fixed instant, re-owed
        assert st["latest_failure"]["status"] == "FAILED"
        assert st["latest_failure"]["error"]["kind"] == "RuntimeError"
        assert st["latest_failure"]["resolved"] is False
        assert st["last_success"]["as_of"] == want[0].isoformat()
    assert runtime.alerts == []  # one transient failure: no message
    out = rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=_cli_steps(seen))
    assert out["status"] == "ok"
    cps = _checkpoints(runtime.db, sid)
    assert [c["as_of"] for c in cps] == [want[0], want[1], *want[1:]]  # retry, then the rest
    assert [c["status"] for c in cps].count("FAILED") == 1  # the failure is kept
    with closing(Store(runtime.db, read_only=True)) as s:
        st = gv.schedule_state(s, sid)["daily"]
        assert st["owed"] == [] and st["overdue"] == 0 and st["latest_failure"]["resolved"]
    assert runtime.alerts == []


def test_status_reports_schedule_health(runtime):
    ago = pd.Timedelta(days=2, hours=6)
    sid = runtime.register(ago)
    from market_signal.cli.main import app

    with closing(Store(runtime.db, read_only=True)) as s:
        st = gv.schedule_state(s, sid)
    want = _expected(ago, "daily")
    assert st["daily"]["owed"] == [t.isoformat() for t in want]
    assert st["daily"]["overdue"] == sum(1 for t in want
                                         if t + gv.OVERDUE_AFTER < pd.Timestamp.now(tz="UTC"))  # fmt: skip
    assert st["daily"]["last_success"] is None and st["weekly"]["latest_failure"] is None
    res = CliRunner().invoke(app, ["lab", "microstructure", "status", "--json"])
    assert res.exit_code == 0, res.output
    import json

    sched = json.loads(res.output)["schedule"]
    assert sched["daily"]["runtime_job"]["schedule_utc"] == ["01:10"]
    assert sched["weekly"]["runtime_job"]["schedule_utc"] == ["Mon 01:20"]
    assert sched["overdue_total"] == st["daily"]["overdue"] + st["weekly"]["overdue"]
    for k in ("last_success", "next_owed_as_of", "last_owed_as_of", "next_grid_as_of",
              "overdue", "latest_failure"):  # fmt: skip
        assert k in sched["daily"] and k in sched["weekly"]


# --------------------------------------------------------------------------- runtime safety


def test_daily_and_weekly_jobs_serialize_on_the_runtime_lock(runtime):
    runtime.register(pd.Timedelta(days=1))
    with rt.runtime_lock(runtime.db, "microdir_daily", 0), pytest.raises(rt.RuntimeBusy):
        rt.run_job("microdir_weekly", trigger="schedule", wait=0, step_runner=_cli_steps([]))


def test_jobs_and_checkpoint_command_require_the_authoritative_runtime(runtime, monkeypatch):
    from market_signal.cli.main import app
    from market_signal.data.store import NotAuthoritative

    runtime.register(pd.Timedelta(days=2))
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "")
    with pytest.raises((rt.RuntimeRefused, NotAuthoritative)):
        rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=_cli_steps([]))
    res = CliRunner().invoke(app, ["lab", "microstructure", "checkpoint", "--cadence", "daily"])
    assert res.exit_code == 2  # a claimed database refuses a non-runtime writer
    assert runtime.evaluated == []


def test_alerts_daily_on_second_consecutive_failure_weekly_on_first(runtime):
    def steps(code):
        return lambda job, name, args: {"step": name, "exit": code, "seconds": 0.0}

    for code in (1, 1, 1, 0, 1, 1):
        rt.run_job("microdir_daily", trigger="schedule", wait=0, step_runner=steps(code))
    assert len(runtime.alerts) == 2  # once per streak, at its second failure
    assert all("microdir_daily" in a for a in runtime.alerts)
    runtime.alerts.clear()
    for code in (1, 1, 0, 1):
        rt.run_job("microdir_weekly", trigger="schedule", wait=0, step_runner=steps(code))
    assert len(runtime.alerts) == 2  # first failure of each streak (weekly cadence)
