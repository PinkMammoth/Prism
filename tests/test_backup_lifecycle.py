from __future__ import annotations

# ruff: noqa: F811
import json
import os
from pathlib import Path

import duckdb
import pytest

from market_signal.ops import backups as b
from market_signal.ops import runtime as rt
from tests.test_lab_forward import env  # noqa: F401
from tests.test_paper import pap  # noqa: F401
from tests.test_runtime import clean_role, paper_live  # noqa: F401


@pytest.fixture
def copies(paper_live):
    db, _ = paper_live
    root = db.parent / "backups"
    paths = []
    for i in range(3):
        path = root / "daily" / f"prism-2026010{i + 1}T000000Z.duckdb"
        rt._copy_atomic(db, path)
        b.register(root, path, db, origin="test")
        paths.append(path)
    return db, root, paths


def archive(db, root, path, tmp_path):
    manifest = json.loads(b.sidecar(path).read_text())
    receipt = b.export_backup(path, tmp_path / (path.name + ".gz"), manifest)
    b.acknowledge(root, path, db, receipt)
    return receipt


def test_export_checksum_restore_manifest(copies, tmp_path):
    db, root, paths = copies
    receipt = archive(db, root, paths[0], tmp_path)
    assert receipt["off_host_verified"]
    assert receipt["sha256"] == receipt["restore_test"]["sha256"]
    assert receipt["restore_test"]["tables_read"]["paper_events"] > 0
    assert receipt["schema_version"] and receipt["origin_environment"] == "test"
    assert receipt["backup_timestamp"] and receipt["export_download_timestamp"]
    assert rt._sha256(Path(receipt["archive_path"])) == receipt["artifact_sha256"]


def test_one_backup_dry_run_protects_newest_and_live(copies, tmp_path):
    db, root, paths = copies
    for path in paths:
        archive(db, root, path, tmp_path)
    result = b.retain(root, db)
    assert result["keep"] == [str(paths[-1])]
    assert len(result["removed"]) == 2 and all(p.exists() for p in paths)
    result = b.retain(root, db, apply=True)
    assert len(result["removed"]) == 2 and paths[-1].exists() and db.exists()
    assert len((root / "retention.jsonl").read_text().splitlines()) == 4


def test_two_requires_explicit_recovery_benefit(copies, tmp_path):
    db, root, paths = copies
    for path in paths:
        archive(db, root, path, tmp_path)
    with pytest.raises(ValueError):
        b.retain(root, db, keep=2)
    result = b.retain(
        root, db, keep=2, previous=paths[0], reason="pre-migration rollback", apply=True
    )
    assert result["keep"] == [str(paths[0]), str(paths[2])]
    assert paths[0].exists() and not paths[1].exists()


def test_failed_export_unexported_cannot_delete(copies, tmp_path):
    db, root, paths = copies
    manifest = json.loads(b.sidecar(paths[0]).read_text())
    with pytest.raises(ValueError):
        b.export_backup(paths[0], tmp_path / "bad.gz", {**manifest, "sha256": "wrong"})
    assert not (tmp_path / "bad.gz").exists()
    assert not b.retain(root, db, apply=True)["removed"]
    with pytest.raises(ValueError):
        b.acknowledge(root, paths[0], db, {"off_host_verified": True})


def test_corrupt_newest_prevents_cleanup(copies, tmp_path):
    db, root, paths = copies
    archive(db, root, paths[0], tmp_path)
    paths[-1].write_bytes(b"corrupt")
    with pytest.raises(duckdb.Error):
        b.retain(root, db, apply=True)
    assert paths[0].exists()


def test_paths_symlinks_and_active_inode_rejected(copies):
    db, root, paths = copies
    with pytest.raises(ValueError):
        b.safe_path(root, root / "daily" / ".." / paths[0].name, db)
    link = root / "manual" / paths[0].name
    link.parent.mkdir()
    link.symlink_to(paths[0])
    with pytest.raises(ValueError):
        b.safe_path(root, link, db)
    link.unlink()
    os.link(db, link)
    with pytest.raises(ValueError, match="active"):
        b.safe_path(root, link, db)
    with pytest.raises(ValueError):
        b.safe_path(root, db, db)


def test_unverified_history_never_pruned(copies):
    db, root, paths = copies
    b.sidecar(paths[0]).unlink()
    result = b.retain(root, db, apply=True)
    assert not result["removed"] and len(result["blocked"]) == 2


def test_registration_preserves_receipt_and_policy(copies, tmp_path):
    db, root, paths = copies
    b.register(root, paths[0], db, origin="test", source_revision="test-creator-revision")
    receipt = archive(db, root, paths[0], tmp_path)
    b.register(root, paths[0], db, origin="test")
    assert json.loads(b.sidecar(paths[0]).read_text())["export_receipt"] == receipt
    assert json.loads(b.sidecar(paths[0]).read_text())["source_revision"] == "test-creator-revision"
    b.save_policy(root, db, keep=2, previous=paths[0], reason="before migration")
    saved = b.policy(root)
    assert saved["keep"] == 2 and saved["previous"] == paths[0]
    result = b.retain(root, db, **saved)
    assert str(paths[0]) in result["keep"]


def test_third_full_copy_refused(copies):
    db, _root, paths = copies
    with pytest.raises(RuntimeError, match="export and retain"):
        rt.backup_live(db)
    assert all(p.exists() for p in paths)


def test_pin_latest_reserves_predeployment_rollback(copies):
    db, root, paths = copies
    b.save_policy(root, db, keep=2, previous=paths[-1], reason="health validation pending")
    result = b.retain(root, db, **b.policy(root))
    assert result["keep"] == [str(paths[-1])]


def test_schema_missing_dynamic_table_rejected(tmp_path):
    from market_signal.data.store import Store

    path = tmp_path / "missing-table.duckdb"
    Store(path).close()
    with duckdb.connect(str(path)) as con:
        con.execute("DROP TABLE universe_outcomes")
    with pytest.raises(ValueError, match="missing tables"):
        b.inspect_backup(path)


def test_unknown_schema_rejected(tmp_path):
    from market_signal.data.store import Store

    path = tmp_path / "future.duckdb"
    Store(path).close()
    with duckdb.connect(str(path)) as con:
        con.execute("INSERT INTO schema_version VALUES (99999)")
    with pytest.raises(ValueError, match="unsupported schema"):
        b.inspect_backup(path)


def test_empty_symlink_root_and_export_traversal_rejected(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    root = tmp_path / "backups"
    root.symlink_to(empty, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        b.retain(root, tmp_path / "live.duckdb", apply=True)
    source = tmp_path / "source.duckdb"
    source.write_bytes(b"unverified")
    with pytest.raises(ValueError, match="traversal"):
        b.export_backup(source, tmp_path / "allowed" / ".." / "archive.gz", {})
    assert not (tmp_path / "archive.gz").exists()
    with pytest.raises(ValueError, match="symlink"):
        b.write_json(root / "policy.json", {"keep": 1})
    assert not (empty / "policy.json").exists()
