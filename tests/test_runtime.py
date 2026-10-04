"""Phase 14 always-on runtime: authority guard, single writer, crash/restart recovery, disk
health, backups/restore, continuity fingerprint, schedule and policy immutability."""

# ruff: noqa: F811  (helpers take the imported fixtures' values)

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

from market_signal.copilot.policy import get_policy
from market_signal.data.store import MIGRATIONS, NotAuthoritative, Store, authority_claim
from market_signal.ops import runtime as rt
from market_signal.paper import engine
from market_signal.paper import policy as pol
from market_signal.research.lab.provenance import capture_software
from tests.test_lab_forward import env  # noqa: F401  (fixture)
from tests.test_paper import _create, _live, pap  # noqa: F401

RID = "railway-prism-runtime"

# The released identities the live experiment runs under (Phases 10 and 12). A host move must
# never change them; a code change that does is a semantic change, not infrastructure.
LIVE_POLICY_IDS = {
    "copilot_v1": "copolicy_e235b9dedbfe8cdb77404d15afe7c3069ca947b65e875f85c5d9c6196c745a27",
    "promotion": "appolicy_1f0b69cff1c4c5d0584a62f124f0a5902d37a7b9f0a3d05f388db80e27ce0277",
    "risk": "riskpolicy_e755fb81dd3154b593f75385a9916177bee00e6240f68f1cfd8799750db67181",
    "exit": "exitpolicy_4ef7a1b26f9d6d4c24d32e942ce5257372827464e956490bcc23dcc2c8103812",
    "maturity": "papermaturity_0b19dee3e95d87645f9eb62d9362600a3fd24764a303583224e144d27dbde620",
}


@pytest.fixture
def clean_role(monkeypatch):
    monkeypatch.delenv("PRISM_RUNTIME_ROLE", raising=False)
    monkeypatch.delenv("PRISM_RUNTIME_ID", raising=False)
    monkeypatch.delenv("PRISM_HEARTBEAT_URL", raising=False)
    monkeypatch.setenv("PRISM_LOCK_TIMEOUT", "5")


def _as_runtime(monkeypatch, rid=RID):
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", rid)


@pytest.fixture
def live(project, clean_role, monkeypatch):
    """A claimed 'live' database at the configured path."""
    db = project / "data" / "prism.duckdb"
    Store(db).close()
    rt.claim_authority(db, RID, "test cutover")
    monkeypatch.setattr(rt, "infra_alert", lambda text: None)
    return db


def _fake_steps(calls, fail=()):
    def run(job, name, args):
        calls.append((job, name, args))
        return {"step": name, "exit": 1 if name in fail else 0, "seconds": 0.0}

    return run


# --------------------------------------------------------------------------- authority guard


def test_unclaimed_databases_behave_exactly_as_before(tmp_path, clean_role):
    s = Store(tmp_path / "dev.duckdb")
    assert authority_claim(s.con) is None
    s.close()


def test_claimed_database_refuses_writes_from_a_non_authoritative_process(live, monkeypatch):
    with pytest.raises(NotAuthoritative, match="may only read it"):
        Store(live)
    Store(live, read_only=True).close()  # inspection is always allowed
    _as_runtime(monkeypatch, "home-pc")  # authoritative, but not the runtime named in the claim
    with pytest.raises(NotAuthoritative):
        Store(live)
    _as_runtime(monkeypatch)
    Store(live).close()
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "scratch")  # an explicit, deliberate copy
    Store(live).close()


def test_copied_live_db_is_non_authoritative_by_default(live, tmp_path):
    copy = tmp_path / "laptop" / "prism.duckdb"
    copy.parent.mkdir()
    shutil.copy(live, copy)
    with pytest.raises(NotAuthoritative):
        Store(copy)


def test_authoritative_runtime_never_creates_or_writes_an_unclaimed_db(
    tmp_path, clean_role, monkeypatch
):
    _as_runtime(monkeypatch)
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(NotAuthoritative):
        Store(missing)
    assert not missing.exists()
    monkeypatch.delenv("PRISM_RUNTIME_ROLE")
    Store(tmp_path / "unclaimed.duckdb").close()
    _as_runtime(monkeypatch)
    with pytest.raises(NotAuthoritative, match="no authority claim"):
        Store(tmp_path / "unclaimed.duckdb")


def test_claims_are_explicit_and_superseding_needs_intent(live):
    again = rt.claim_authority(live, RID, "repeat")
    assert again["recorded"] is False
    with pytest.raises(rt.RuntimeRefused, match="--supersede"):
        rt.claim_authority(live, "home-pc", "rollback")
    out = rt.claim_authority(live, "home-pc", "planned rollback", supersede=True)
    assert out["previous"] == RID
    s = Store(live, read_only=True)
    assert authority_claim(s.con)[0] == "home-pc"
    s.close()
    with pytest.raises(rt.RuntimeRefused):
        rt.claim_authority(live.with_name("nope.duckdb"), RID, "x")


def test_cli_write_commands_are_refused_on_a_claimed_copy(live, project):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRISM_RUNTIME")}
    env.update(PRISM_HOME=str(project), PRISM_LOCK_TIMEOUT="5")
    r = subprocess.run([sys.executable, "-m", "market_signal.cli.main", "lab", "paper", "run"],
                       capture_output=True, text=True, env=env, cwd=project)  # fmt: skip
    assert r.returncode == 2 and "not the authoritative runtime" in r.stdout
    r = subprocess.run([sys.executable, "-m", "market_signal.cli.main", "ops", "cycle", "prospective"],
                       capture_output=True, text=True, env=env, cwd=project)  # fmt: skip
    assert r.returncode == rt.EXIT_REFUSED


# --------------------------------------------------------------------------- jobs


def test_job_refused_off_the_authoritative_runtime(live, monkeypatch):
    calls = []
    with pytest.raises(rt.RuntimeRefused):
        rt.run_job("prospective", trigger="manual", wait=0, step_runner=_fake_steps(calls))
    assert calls == []


def test_job_refused_when_not_provisioned(project, clean_role, monkeypatch):
    _as_runtime(monkeypatch)
    with pytest.raises(rt.RuntimeRefused, match="not provisioned"):
        rt.run_job("prospective", trigger="boot", wait=0, step_runner=_fake_steps([]))
    assert not (project / "data" / "prism.duckdb").exists()


def test_prospective_sequence_runs_in_order_and_is_recorded(live, monkeypatch):
    _as_runtime(monkeypatch)
    calls = []
    out = rt.run_job("prospective", trigger="schedule", wait=0, step_runner=_fake_steps(calls))
    assert out["exit"] == 0 and out["status"] == "ok"
    assert [c[1] for c in calls] == ["forward", "copilot", "paper", "brief"]
    assert [c[2] for c in calls] == [["lab", "forward", "run"],
                                     ["lab", "copilot", "run", "--no-update"],
                                     ["lab", "paper", "run"],
                                     ["lab", "paper", "brief", "--send"]]  # fmt: skip
    s = Store(live, read_only=True)
    row = s.con.execute("SELECT job, trigger, status, runtime_id FROM runtime_cycles").fetchall()
    s.close()
    assert row == [("prospective", "schedule", "ok", RID)]


def test_a_failed_step_does_not_skip_later_steps_and_alerts(live, monkeypatch):
    _as_runtime(monkeypatch)
    alerts, beats = [], []
    monkeypatch.setattr(rt, "infra_alert", alerts.append)
    monkeypatch.setattr(rt, "heartbeat", beats.append)
    calls = []
    out = rt.run_job("prospective", trigger="schedule", wait=0,
                     step_runner=_fake_steps(calls, fail={"copilot"}))  # fmt: skip
    assert out["exit"] == rt.EXIT_FAILED and len(calls) == 4
    assert beats == [False] and "copilot=1" in alerts[0]


def test_real_step_runner_runs_the_cli_and_captures_exit(live, monkeypatch):
    _as_runtime(monkeypatch)
    out = rt._run_step("t", "status", ["status", "--json"])
    assert out["exit"] == 0 and not out["timed_out"]
    monkeypatch.setattr(rt, "STEP_TIMEOUT", 0.01)
    out = rt._run_step("t", "status", ["status"])
    assert out["timed_out"] and out["exit"] != 0


# --------------------------------------------------------------------------- single writer


def _hold_lock(db: Path, seconds: float) -> subprocess.Popen:
    code = (
        "import sys, time; from pathlib import Path; from market_signal.ops import runtime as rt\n"
        f"with rt.runtime_lock(Path({str(db)!r}), 'holder', 0):\n"
        "    print('held', flush=True); time.sleep(float(sys.argv[1]))\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", code, str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert p.stdout.readline().strip() == "held"
    return p


def test_concurrent_invocations_cannot_both_run(live, monkeypatch):
    _as_runtime(monkeypatch)
    p = _hold_lock(live, 30)
    try:
        calls = []
        t = time.monotonic()
        with pytest.raises(rt.RuntimeBusy, match="holder"):
            rt.run_job("prospective", trigger="manual", wait=0.5, step_runner=_fake_steps(calls))
        assert calls == [] and time.monotonic() - t < 5
    finally:
        p.kill()
        p.wait()


def test_queued_invocation_runs_after_the_holder_finishes(live, monkeypatch):
    _as_runtime(monkeypatch)
    p = _hold_lock(live, 1.0)
    try:
        calls = []
        out = rt.run_job("oi", trigger="schedule", wait=30, step_runner=_fake_steps(calls))
        assert out["exit"] == 0 and calls
    finally:
        p.wait()


def test_crashed_holder_leaves_no_stale_lock(live, monkeypatch):
    _as_runtime(monkeypatch)
    p = _hold_lock(live, 60)
    p.send_signal(signal.SIGKILL)  # a crash mid-job
    p.wait()
    calls = []
    out = rt.run_job("prospective", trigger="manual", wait=0, step_runner=_fake_steps(calls))
    assert out["exit"] == 0


def test_cli_cycle_reports_busy(live, project, monkeypatch):
    _as_runtime(monkeypatch)
    p = _hold_lock(live, 30)
    try:
        env = {**os.environ, "PRISM_HOME": str(project)}
        r = subprocess.run([sys.executable, "-m", "market_signal.cli.main", "ops", "cycle", "oi"],
                           capture_output=True, text=True, env=env, cwd=project)  # fmt: skip
        assert r.returncode == rt.EXIT_BUSY and "skipped" in r.stdout
    finally:
        p.kill()
        p.wait()


def test_crash_mid_cycle_then_retry(live, monkeypatch):
    """A cycle that dies mid-way leaves a 'running' row and no lock; the retry runs normally."""
    _as_runtime(monkeypatch)

    def boom(job, name, args):
        if name == "paper":
            raise KeyboardInterrupt  # stands in for SIGKILL/OOM mid-step
        return {"step": name, "exit": 0, "seconds": 0}

    with pytest.raises(KeyboardInterrupt):
        rt.run_job("prospective", trigger="schedule", wait=0, step_runner=boom)
    out = rt.run_job("prospective", trigger="schedule", wait=0, step_runner=_fake_steps([]))
    assert out["exit"] == 0
    s = Store(live, read_only=True)
    st = [
        r[0]
        for r in s.con.execute("SELECT status FROM runtime_cycles ORDER BY started_at").fetchall()
    ]
    s.close()
    assert st == ["running", "ok"]


# --------------------------------------------------------------------------- disk


def test_disk_thresholds(tmp_path):
    def d(free):
        return rt.DiskState(str(tmp_path), 1000, free)

    assert d(500).level == "OK" and d(149).level == "WARNING" and d(49).level == "CRITICAL"


def test_critical_disk_refuses_the_job_and_deletes_nothing(live, monkeypatch):
    _as_runtime(monkeypatch)
    (live.parent / "backups" / "daily").mkdir(parents=True)
    keep = live.parent / "backups" / "daily" / "prism-20260101T000000Z.duckdb"
    keep.write_bytes(b"x")
    alerts = []
    monkeypatch.setattr(rt, "infra_alert", alerts.append)
    monkeypatch.setattr(rt, "disk_state", lambda p: rt.DiskState(str(p), 1000, 10))
    calls = []
    out = rt.run_job("prospective", trigger="schedule", wait=0, step_runner=_fake_steps(calls))
    assert out["exit"] == rt.EXIT_DISK and calls == [] and "disk critical" in alerts[0]
    assert live.exists() and keep.exists()


def test_warning_disk_still_runs(live, monkeypatch):
    _as_runtime(monkeypatch)
    monkeypatch.setattr(rt, "disk_state", lambda p: rt.DiskState(str(p), 1000, 100))
    assert rt.run_job("oi", trigger="schedule", wait=0, step_runner=_fake_steps([]))["exit"] == 0


# --------------------------------------------------------------------------- backups & continuity


@pytest.fixture
def paper_live(pap, clean_role, monkeypatch):
    """A claimed database holding a real paper run with a few processed days."""
    rid = _create(pap)["run_id"]
    _live(pap, rid, 6)
    db = pap.store.path
    pap.store.close()
    rt.claim_authority(db, RID, "test")
    monkeypatch.setenv("PRISM_DB_PATH", str(db))
    monkeypatch.setattr(rt, "infra_alert", lambda text: None)
    _as_runtime(monkeypatch)
    return db, rid


def test_backup_is_verified_versioned_and_recorded(paper_live):
    db, rid = paper_live
    before = _fp(db)
    out = rt.backup_live(db, kind="daily")
    assert out["paper_runs"][0]["run_id"] == rid and Path(out["path"]).exists()
    assert out["weekly"] and os.path.samefile(
        out["path"], out["weekly"]
    )  # hard link, no extra space
    assert rt.verify_backup(Path(out["path"]))["sha256"] == out["sha256"]
    after = _fp(db)
    assert rt.compare(before, after) == []  # a backup never touches evidence
    s = _ro(db)
    n = s.con.execute(
        "SELECT count(*) FROM runtime_events WHERE event_type='backup_verified'"
    ).fetchone()[0]
    s.close()
    assert n == 1


def _ro(db):
    return Store(db, read_only=True)


def _fp(db):
    s = _ro(db)
    try:
        return rt.fingerprint(s)
    finally:
        s.close()


def test_restore_rehearsal_reproduces_production_on_a_scratch_copy(paper_live, tmp_path):
    db, rid = paper_live
    out = rt.backup_live(db, kind="manual", label="pre-deploy")
    sha = rt._sha256(Path(out["path"]))
    r = rt.restore_test(Path(out["path"]), tmp_path)
    assert r["restored_ok"] and r["paper_runs"] == [rid]
    assert rt._sha256(Path(out["path"])) == sha  # the backup itself is never modified


def test_backup_verification_detects_a_bad_file(tmp_path, clean_role):
    bad = tmp_path / "prism-x.duckdb"
    bad.write_bytes(b"not a database")
    with pytest.raises(duckdb.Error):
        rt.verify_backup(bad)
    empty = tmp_path / "e.duckdb"
    Store(empty).close()
    with pytest.raises(RuntimeError, match="no paper run"):
        rt.verify_backup(empty)


def test_retention_keeps_the_newest_and_never_touches_the_live_db(tmp_path):
    root = tmp_path / "backups"
    for kind, n in (("daily", 10), ("weekly", 7), ("manual", 3)):
        (root / kind).mkdir(parents=True)
        for i in range(n):
            (root / kind / f"prism-202601{i + 10:02d}T000000Z.duckdb").write_bytes(b"x")
    (root / "daily" / "x.duckdb.part").write_bytes(b"x")
    live = tmp_path / "prism.duckdb"
    live.write_bytes(b"live")
    removed = rt.prune_backups(root)
    assert len(list((root / "daily").glob("prism-*"))) == rt.BACKUP_KEEP["daily"]
    assert len(list((root / "weekly").glob("prism-*"))) == rt.BACKUP_KEEP["weekly"]
    assert len(list((root / "manual").glob("prism-*"))) == 3
    assert sorted((root / "daily").glob("prism-*"))[0].name == "prism-20260113T000000Z.duckdb"
    assert live.exists() and all(Path(r).name != "prism.duckdb" for r in removed)


def test_backup_job_via_runtime(paper_live):
    out = rt.run_job("backup", trigger="schedule", wait=0)
    assert out["exit"] == 0 and out["steps"][0]["path"].endswith(".duckdb")


def test_copied_db_preserves_every_id_and_the_ledger(paper_live, tmp_path):
    """The migration itself: a byte copy taken with no writer open has the same fingerprint."""
    db, rid = paper_live
    before = _fp(db)
    dest = tmp_path / "server" / "prism.duckdb"
    rt._copy_atomic(db, dest)
    assert rt._sha256(dest) == rt._sha256(db)
    after = _fp(dest)
    assert rt.compare(before, after) == [] and after == before
    run = after["paper_runs"][0]
    assert (
        run["run_id"] == rid
        and run["event_count"] > 1
        and run["last_event"]["seq"] == run["event_count"]
    )


def test_fingerprint_detects_evidence_changes_and_venue_merges(paper_live):
    db, _ = paper_live
    before = _fp(db)
    s = Store(db)
    # simulate a divergent copy: one ledger row altered
    s.con.execute(
        "UPDATE paper_events SET recorded_at = recorded_at + INTERVAL 1 SECOND WHERE seq=1"
    )
    s.close()
    diffs = rt.compare(before, _fp(db))
    assert any("paper_events" in d for d in diffs)
    a = json.loads(json.dumps(before))
    a["oi"] = {"perp_oi_history/binance+hyperliquid": {"rows": 1, "last": "x"}}
    assert any("oi series changed" in d for d in rt.compare(before, a))


def test_paper_run_and_trackings_survive_a_host_restart(paper_live):
    """Restart = close everything and reopen from disk: same run, same ledger, same state."""
    db, rid = paper_live
    before = _fp(db)
    s = Store(db)  # the restarted process (authoritative) opens and migrates
    s.close()
    after = _fp(db)
    assert rt.compare(before, after) == []
    assert [r["run_id"] for r in after["paper_runs"]] == [rid]


def test_status_reports_runtime_health_separately(paper_live, settings):
    db, _ = paper_live
    rt.run_job("prospective", trigger="schedule", wait=0, step_runner=_fake_steps([]))
    rt.backup_live(db)
    from market_signal.cli.status_cmds import system_status

    s = _ro(db)
    rows = {r["component"]: r for r in system_status(s, settings)}
    s.close()
    assert (
        rows["Runtime (infra)"]["state"] == "OK"
        and "(this process)" in rows["Runtime (infra)"]["detail"]
    )
    assert rows["Backups (infra)"]["state"] == "OK"
    assert rows["Disk (infra)"]["state"] in ("OK", "ATTENTION", "CRITICAL")
    assert "Telegram (infra)" in rows and "Paper trader" in rows


# --------------------------------------------------------------------------- frozen semantics


def test_policy_ids_are_unchanged():
    assert get_policy(1).policy_id == LIVE_POLICY_IDS["copilot_v1"]
    assert pol.promotion_policy(1).policy_id == LIVE_POLICY_IDS["promotion"]
    assert pol.risk_policy(1).policy_id == LIVE_POLICY_IDS["risk"]
    assert pol.exit_policy(1).policy_id == LIVE_POLICY_IDS["exit"]
    assert pol.maturity_policy(1).policy_id == LIVE_POLICY_IDS["maturity"]
    assert engine.ENGINE_VERSION == "paper_engine_v1"


def test_migration_17_is_runtime_only():
    ddl = MIGRATIONS[16]
    assert len(MIGRATIONS) == 17
    assert "runtime_events" in ddl and "runtime_cycles" in ddl
    assert not any(p in ddl for p in ("lab_", "copilot_", "paper_"))


def test_schedule_and_crontab():
    assert rt.SCHEDULE["prospective"][0] == "00:10"
    tab = rt.crontab()
    assert "10 0 * * * market ops cycle prospective --trigger schedule" in tab
    assert all(f"ops cycle {j} " in tab for j in rt.SCHEDULE)
    from datetime import UTC, datetime

    assert rt.next_run("prospective", datetime(2026, 10, 4, 23, 0, tzinfo=UTC)) == datetime(
        2026, 10, 5, 0, 10, tzinfo=UTC
    )
    # longest gap between prospective runs stays within the heartbeat window (≤ 7h10m)
    mins = sorted(int(t[:2]) * 60 + int(t[3:]) for t in rt.SCHEDULE["prospective"])
    gaps = [b - a for a, b in itertools.pairwise(mins)] + [mins[0] + 1440 - mins[-1]]
    assert max(gaps) <= 7 * 60 + 10


def test_deployed_export_records_its_commit_without_git(tmp_path):
    root = tmp_path / "app"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src", root / "src")
    (root / "REVISION").write_text("abc123\n")
    sw = capture_software(root)
    assert sw.git_commit == "abc123" and rt.revision(root) == "abc123"
    (root / "REVISION").unlink()
    assert capture_software(root).git_commit is None
