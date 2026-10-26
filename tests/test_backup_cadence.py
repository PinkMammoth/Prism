"""Focused lifecycle failure, retry, retention and exclusion tests."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cadence", Path(__file__).parents[1] / "deploy/backup-cadence/cadence.py"
)
cadence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cadence)


@pytest.mark.parametrize("failed_stage", ["inspect", "export", "ack"])
def test_failure_never_runs_retention(tmp_path, monkeypatch, failed_stage):
    calls = []
    generation = tmp_path / "generation-one"
    generation.mkdir()
    row = {"path": "source", "manifest": {}}

    def invoke(authority, mode, payload=None, **kwargs):
        calls.append(mode)
        if mode == failed_stage:
            raise RuntimeError("injected failure")
        return {
            "inventory": [row],
            "inspect": {"registered": {"sha256": "one"}},
            "export": {
                "sha256": "one",
                "off_host_verified": True,
                "restore_verified": True,
                "compressed_sha256": "compressed",
                "snapshot_id": "snapshot",
                "verified_at": "now",
            },
            "ack": {},
        }[mode]

    monkeypatch.setattr(cadence, "invoke", invoke)
    monkeypatch.setattr(cadence, "stage_export", lambda *a: generation)
    with pytest.raises(RuntimeError):
        cadence.drain("lab")
    assert "retain" not in calls
    if failed_stage in ("inspect", "export"):
        assert "ack" not in calls
    assert generation.exists()


@pytest.mark.parametrize(
    "receipt",
    [
        {"sha256": "wrong", "off_host_verified": True, "restore_verified": True},
        {"sha256": "one", "off_host_verified": False, "restore_verified": True},
        {"sha256": "one", "off_host_verified": True, "restore_verified": False},
    ],
)
def test_bad_receipt_never_acknowledged(monkeypatch, receipt):
    calls = []

    def invoke(authority, mode, *a, **kw):
        calls.append(mode)
        return {
            "inventory": [{"path": "source", "manifest": {}}],
            "inspect": {"registered": {"sha256": "one"}},
            "export": receipt,
        }[mode]

    monkeypatch.setattr(cadence, "invoke", invoke)
    monkeypatch.setattr(cadence, "stage_export", lambda *a: Path("unused"))
    with pytest.raises(ValueError):
        cadence.drain("lab")
    assert calls == ["inventory", "inspect", "export"]


def test_acknowledged_retry_needs_no_owner_lock_or_retention(monkeypatch):
    calls = []

    def invoke(authority, mode, *a, **kw):
        calls.append(mode)
        if mode == "inventory":
            return [
                {
                    "path": "source",
                    "manifest": {
                        "sha256": "one",
                        "export_receipt": {
                            "off_host_verified": True,
                            "sha256": "one",
                            "restore_test": {"sha256": "one"},
                            "artifact_sha256": "artifact",
                            "archive_path": "restic:snapshot",
                            "export_download_timestamp": "now",
                        },
                    },
                }
            ]
        return {"removed": []}

    monkeypatch.setattr(cadence, "invoke", invoke)
    monkeypatch.setattr(cadence, "prune_exports", lambda a: None)
    for _ in range(2):
        assert cadence.drain("lab")["already_reconciled"] is True
    assert calls == ["inventory", "inventory"]


def test_unacknowledged_staging_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(cadence, "BASE", tmp_path)
    generation = tmp_path / "lab/generation-one"
    generation.mkdir(parents=True)
    (generation / "offhost-receipt.json").write_text(
        json.dumps({"off_host_verified": True, "sha256": "one"})
    )
    with pytest.raises(ValueError, match="unacknowledged"):
        cadence.prune_exports("lab")
    assert generation.exists()


def test_overlapping_run_excluded(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setattr(cadence, "BASE", tmp_path)
    root = tmp_path / "lab"
    root.mkdir()
    with (root / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cadence.run("lab")


def test_all_exports_acknowledged_before_governed_retention(tmp_path, monkeypatch):
    calls = []
    generation = tmp_path / "generation-one"
    generation.mkdir()

    def invoke(authority, mode, payload=None, **kwargs):
        calls.append(mode)
        if mode == "inventory":
            return [{"path": "source", "manifest": {}}]
        if mode == "inspect":
            return {"registered": {"sha256": "one"}}
        if mode == "export":
            return {
                "sha256": "one",
                "off_host_verified": True,
                "restore_verified": True,
                "compressed_sha256": "compressed",
                "snapshot_id": "snapshot",
                "verified_at": "now",
            }
        if mode == "ack":
            assert payload["receipt"]["archive_path"] == "restic:snapshot"
            assert generation.exists()
            return {"acknowledged": "source"}
        assert mode == "retain"
        assert json.loads((generation / "owner-ack.json").read_text())["sha256"] == "one"
        return {"removed": ["eligible-old-copy"]}

    monkeypatch.setattr(cadence, "invoke", invoke)
    monkeypatch.setattr(cadence, "stage_export", lambda *a: generation)
    monkeypatch.setattr(cadence, "prune_exports", lambda a: None)
    assert cadence.drain("lab")["removed"] == ["eligible-old-copy"]
    assert calls == ["inventory", "inspect", "export", "ack", "retain"]


@pytest.mark.parametrize("pin_changes", [False, True])
def test_public_retention_preserves_pin_under_owner_lock(tmp_path, monkeypatch, pin_changes):
    from contextlib import nullcontext

    from market_signal.cli import main
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    policy = {"keep": 2, "previous": tmp_path / "rollback.duckdb", "reason": "pending migration"}
    changed = {"keep": 2, "previous": tmp_path / "new-pin.duckdb", "reason": "new rollback"}
    snapshots = iter([policy, changed if pin_changes else policy])
    monkeypatch.setattr(b, "policy", lambda root: next(snapshots))

    def original_lock(*a, **kw):
        return nullcontext()

    monkeypatch.setattr(rt, "runtime_lock", original_lock)
    monkeypatch.setenv("PRISM_BACKUP_DIR", str(tmp_path))
    calls = []

    def app(args, standalone_mode):
        with rt.runtime_lock(tmp_path / "live.duckdb", "backup-retention", 600):
            calls.append(args)

    monkeypatch.setattr(main, "app", app)
    if pin_changes:
        with pytest.raises(ValueError, match="policy changed"):
            cadence.owner("retain", "lab", {})
        assert calls == []
    else:
        cadence.owner("retain", "lab", {})
        assert calls == [
            [
                "ops",
                "backup-retain",
                "--apply",
                "--keep",
                "2",
                "--previous",
                str(policy["previous"]),
                "--reason",
                policy["reason"],
            ]
        ]
    assert rt.runtime_lock is original_lock


@pytest.mark.parametrize(
    "authority,operation,keep_all",
    [
        ("lab", "forget", True),
        ("research", "forget", False),
        ("lab", "backup", False),
    ],
)
def test_lab_archive_history_and_research_retention_unchanged(authority, operation, keep_all):
    calls = []

    def tool(name, args, **kwargs):
        calls.append(args)

    cadence.scoped_offhost_tool(authority, tool, "restic", [operation, "--host", authority])
    assert ("--keep-tag" in calls[0]) is keep_all
    if keep_all:
        assert calls[0][-1] == "prism-lab-backup-v1"
    else:
        assert calls[0] == [operation, "--host", authority]
