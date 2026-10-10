"""Snapshot retrieval verifies provenance and uses remote reads only."""

# ruff: noqa: F811

from __future__ import annotations

import base64
import gzip
import hashlib
import json

import duckdb
import pytest
from scripts import pull_paper_snapshot as snapshot

from market_signal.paper.dashboard.report import Report, freshness, runs
from tests.test_paper_dashboard import accounts  # noqa: F401


def test_compact_snapshot_round_trip_read_only_remote_commands(
    monkeypatch, tmp_path, store, accounts
):
    export = {}
    for table in snapshot.PAPER_TABLES:
        columns = store.con.execute(
            "SELECT column_name,data_type FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        data = store.con.execute(f"SELECT * FROM {table}").fetchall()
        export[table] = {"columns": columns, "rows": data}
    raw = json.dumps(export, default=str, separators=(",", ":")).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    remote = "/data/backups/manual/prism-20261010T141331Z-fixture.duckdb"
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if "find" in args:
            return remote + "\n"
        if "sha256sum" in args:
            return "a" * 64 + "  " + remote
        assert "-c" in args and "read_only=True" in args[args.index("-c") + 1]
        return (
            "PRISM_PAPER_EXPORT " + checksum + " " + base64.b64encode(gzip.compress(raw)).decode()
        )

    monkeypatch.setattr(snapshot, "command", command)
    dest = tmp_path / "paper.duckdb"
    result = snapshot.pull(dest)
    assert len(calls) == 3
    assert all("ssh" in c and "upload" not in c for c in calls)
    assert "SELECT * FROM " in snapshot.REMOTE_EXPORT
    assert all(
        word not in snapshot.REMOTE_EXPORT.upper()
        for word in ("INSERT ", "DELETE ", "UPDATE ", "CHECKPOINT", "COPY ")
    )
    assert result["export_sha256"] == checksum
    assert result["snapshot_at"] == "2026-10-10T14:13:31+00:00"
    with duckdb.connect(str(dest), read_only=True) as con:
        metadata = runs(con)
        assert {r["run_id"] for r in metadata} == set(accounts)
        for run in metadata:
            r = Report(con, run)
            r.curve(con)
            assert r.stats["current_equity"] == 100
    fresh = freshness(dest, metadata)
    assert fresh["last_production_sync"] is not None
    assert fresh["production_path"] == remote


def test_bad_checksum_preserves_previous_snapshot_and_manifest(monkeypatch, tmp_path):
    dest = tmp_path / "paper.duckdb"
    dest.write_bytes(b"previous snapshot")
    sidecar = dest.with_suffix(".snapshot.json")
    sidecar.write_text("previous provenance")
    remote = "/data/backups/daily/prism-20261010T013003Z.duckdb"

    def command(args, **kwargs):
        if "find" in args:
            return remote
        if "sha256sum" in args:
            return "a" * 64 + "  " + remote
        return (
            "PRISM_PAPER_EXPORT " + "b" * 64 + " " + base64.b64encode(gzip.compress(b"{}")).decode()
        )

    monkeypatch.setattr(snapshot, "command", command)
    with pytest.raises(ValueError, match="checksum mismatch"):
        snapshot.pull(dest)
    assert dest.read_bytes() == b"previous snapshot"
    assert sidecar.read_text() == "previous provenance"


def test_remote_backup_path_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        snapshot,
        "command",
        lambda *a, **k: (
            "/data/prism.duckdb\n/data/backups/../../prism-20261010T013003Z.duckdb\nmalicious;command"
        ),
    )
    with pytest.raises(ValueError, match="No existing production backups"):
        snapshot.pull(tmp_path / "copy.duckdb")


@pytest.mark.parametrize("authoritative", [False, True])
def test_snapshot_cannot_replace_execution_database(monkeypatch, tmp_path, authoritative):
    execution = tmp_path / "live.duckdb"
    execution.write_bytes(b"execution state")
    monkeypatch.setenv("PRISM_DB_PATH", str(execution))
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative" if authoritative else "read-only")
    monkeypatch.setattr(
        snapshot,
        "command",
        lambda *args, **kwargs: pytest.fail("retrieval ran before safety guard"),
    )
    with pytest.raises(ValueError, match="refuses an authoritative runtime"):
        snapshot.pull(tmp_path / "snapshot.duckdb" if authoritative else execution)
    assert execution.read_bytes() == b"execution state"
