"""Migration candidates never grant authority; recovery failures preserve evidence."""

import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from market_signal.data.store import NotAuthoritative, Store
from market_signal.ops import profiles
from market_signal.ops import research_namespace as ns


@pytest.fixture
def candidate(tmp_path):
    db = tmp_path / "import" / "research.duckdb"
    db.parent.mkdir()
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE schema_version(version INT); INSERT INTO schema_version VALUES(30)"
        )
        con.execute(
            "CREATE TABLE runtime_events(event_id VARCHAR, event_type VARCHAR, runtime_id VARCHAR,recorded_at TIMESTAMPTZ)"
        )
        con.execute(
            "INSERT INTO runtime_events VALUES('claim','authority_claimed','railway-prism-runtime','2026-10-04T17:46:19Z')"
        )
        con.execute("CREATE TABLE runtime_cycles(finished_at TIMESTAMPTZ)")
        con.execute("INSERT INTO runtime_cycles VALUES('2026-10-15T11:31:40Z')")
        con.execute(
            "CREATE TABLE scientific_evidence(id INT, value DOUBLE); INSERT INTO scientific_evidence VALUES(1,42)"
        )
    return db


def test_read_only_import_and_wrong_owner_fail_closed(candidate, monkeypatch):
    before = ns.sha256(candidate)
    with ns.connect(candidate) as con, pytest.raises(duckdb.InvalidInputException):
        con.execute("INSERT INTO scientific_evidence VALUES(2,0)")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "reader")
    with pytest.raises(NotAuthoritative):
        Store(candidate)
    for identity in ("vps-prism-research", "vps-prism-autotrader-lab-rehearsal"):
        monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
        monkeypatch.setenv("PRISM_RUNTIME_ID", identity)
        with pytest.raises(NotAuthoritative):
            Store(candidate)
    assert ns.sha256(candidate) == before


def test_disabled_environment_cannot_schedule_collect_or_act():
    text = Path("deploy/consolidated/research.env.example").read_text()
    env = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )
    result = profiles.resolve(env)
    assert not result.processes
    assert not result.scheduled_jobs()
    assert not result.external_actions_allowed
    assert not result.execution_allowed
    assert result.role == "reader"


def test_backup_roundtrip_and_one_generation(candidate, tmp_path):
    source = {"sha256": ns.sha256(candidate), "source_runtime": "railway-prism-runtime"}
    root = tmp_path / "backups"
    first = ns.backup_sealed(candidate, root, source)
    second = ns.backup_sealed(candidate, root, source)
    generations = list(root.glob("generation-*"))
    assert len(generations) == 1
    assert first["sha256"] == second["sha256"]
    assert not second["off_host_verified"]
    proof = ns.restore(generations[0] / "research.duckdb.gz", second, tmp_path / "restore.duckdb")
    assert proof["verified"]
    assert ns.inspect(candidate)["tables"] == proof["inspection"]["tables"]


def test_corruption_existing_restore_and_failed_rotation_preserve_previous(candidate, tmp_path):
    root = tmp_path / "backups"
    source = {"sha256": ns.sha256(candidate)}
    manifest = ns.backup_sealed(candidate, root, source)
    gen = next(root.glob("generation-*"))
    existing = tmp_path / "existing.duckdb"
    existing.write_bytes(b"preserve")
    with pytest.raises(ValueError):
        ns.restore(gen / "research.duckdb.gz", manifest, existing)
    assert existing.read_bytes() == b"preserve"
    pointer = (root / "latest.json").read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        ns.backup_sealed(candidate, root, {"sha256": "bad"})
    assert (root / "latest.json").read_bytes() == pointer
    assert gen.exists()
    corrupt = tmp_path / "corrupt.gz"
    corrupt.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        ns.restore(corrupt, manifest, tmp_path / "new.duckdb")


def test_restore_size_limit_and_partial_preservation(candidate, tmp_path):
    root = tmp_path / "backups"
    manifest = ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    archive = next(root.glob("generation-*")) / "research.duckdb.gz"
    destination = tmp_path / "new.duckdb"
    with pytest.raises(ValueError, match="exceeds"):
        ns.restore(archive, {**manifest, "bytes": 1}, destination)
    assert not destination.exists()
    partial = destination.with_suffix(".duckdb.part")
    partial.write_bytes(b"existing")
    with pytest.raises(ValueError, match="existing partial"):
        ns.restore(archive, manifest, destination)
    assert partial.read_bytes() == b"existing"


def test_no_offhost_without_explicit_independent_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: pytest.fail("no external operation"))
    config = json.loads(Path("deploy/consolidated/research-backup.json.example").read_text())
    with pytest.raises(ValueError, match="approval"):
        ns.offhost(config, tmp_path, tmp_path / "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "/local/not-independent")
    with pytest.raises(ValueError, match="remote repository"):
        ns.offhost(
            {**config, "approved_independent_destination": True, "dedicated_repository": True},
            tmp_path,
            tmp_path / "restore",
        )


def test_offhost_upload_recovery_retention_and_redacted_failure(candidate, tmp_path, monkeypatch):
    root = tmp_path / "backups"
    ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    generation = next(root.glob("generation-*"))
    calls = []
    scratch = tmp_path / "offhost-restore"
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:https://independent.invalid/research")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/protected/password")

    def fake_restic(args, **kwargs):
        calls.append(args)
        assert kwargs["capture_output"]
        if "restore" in args:
            shutil.copytree(generation, scratch)
        output = '{"message_type":"summary","snapshot_id":"abc123"}\n' if "backup" in args else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(ns.subprocess, "run", fake_restic)
    config = {
        "approved_independent_destination": True,
        "dedicated_repository": True,
        "destination_id": "test-only",
    }
    receipt = ns.offhost(config, generation, scratch)
    assert receipt["off_host_verified"] and receipt["restore_verified"]
    assert [c[2] for c in calls] == ["backup", "check", "restore", "forget"]
    assert "--read-data" in calls[1] and "--prune" in calls[-1]
    assert "--group-by" in calls[-1]

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "DO_NOT_PRINT_SECRET")

    monkeypatch.setattr(ns.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="status 1") as exc:
        ns.offhost(config, generation, tmp_path / "different-restore")
    assert "DO_NOT_PRINT_SECRET" not in str(exc.value)


def test_paths_and_runtime_state_do_not_transfer(candidate, tmp_path):
    from scripts.ovh_research_validate import ownership

    with pytest.raises(ValueError, match="traversal"):
        ns.safe(tmp_path / ".." / "elsewhere")
    result = ownership(
        {"tables": {"runtime_events": {"disposition": "RETAIN_WITH_EXPLICIT_HANDOFF"}}},
        ns.inspect(candidate),
    )
    state = result["tables"]["runtime_events"]
    assert not state["may_resume_authority"]
    assert state["import_role"] == "read-only imported history"
