"""A bridge source may be removed only after an exact, readable remote restore."""

import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import duckdb
import pytest

SPEC = importlib.util.spec_from_file_location(
    "bridge", Path(__file__).parents[1] / "deploy/consolidated/bridge_archive.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


@pytest.fixture
def packet(tmp_path):
    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as con:
        con.execute(
            "CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES (30)"
        )
        con.execute(
            "CREATE TABLE runtime_events(event_id VARCHAR,event_type VARCHAR,runtime_id VARCHAR,recorded_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO runtime_events VALUES ('claim','authority_claimed','railway-prism-runtime','2026-10-15 00:00:00')"
        )
        con.execute(
            "CREATE TABLE runtime_cycles(finished_at TIMESTAMP); INSERT INTO runtime_cycles VALUES ('2026-10-15 01:00:00')"
        )
        con.execute(
            "CREATE TABLE evidence(id INTEGER, payload VARCHAR); INSERT INTO evidence VALUES (1,'preserve me')"
        )
    raw = source.read_bytes()
    header = {
        "name": "prism-20261015T013000Z.duckdb",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_size": len(raw),
        "schema_version": 30,
        "source_manifest": {
            "tables_read": {
                "schema_version": 1,
                "runtime_events": 1,
                "runtime_cycles": 1,
                "evidence": 1,
            }
        },
    }
    return header, gzip.compress(raw, mtime=0)


def stream(header, compressed):
    return io.BytesIO((json.dumps(header) + "\n").encode() + compressed)


def test_complete_archive_requires_all_table_read_and_checksums(tmp_path, packet):
    header, compressed = packet
    receipt = bridge.receive(stream(header, compressed), tmp_path / "archive", disk_floor=0)
    assert receipt["off_host_verified"]
    assert receipt["sha256"] == header["sha256"]
    assert receipt["artifact_sha256"] == hashlib.sha256(compressed).hexdigest()
    assert receipt["restore_test"]["all_tables"]["evidence"]["rows"] == 1
    assert Path(receipt["archive_path"]).read_bytes() == compressed
    assert list((tmp_path / "archive").glob("incoming-*")) == []


@pytest.mark.parametrize("failure", ["checksum", "table_inventory", "path", "truncated", "size"])
def test_bad_stream_never_publishes(tmp_path, packet, failure):
    header, compressed = packet
    if failure == "checksum":
        header["sha256"] = "0" * 64
    elif failure == "table_inventory":
        header["source_manifest"]["tables_read"]["evidence"] = 2
    elif failure == "path":
        header["name"] = "../evidence.duckdb"
    elif failure == "truncated":
        compressed = compressed[:-20]
    else:
        header["source_size"] -= 1
    root = tmp_path / "archive"
    with pytest.raises((ValueError, EOFError, OSError)):
        bridge.receive(stream(header, compressed), root, disk_floor=0)
    assert list(root.iterdir()) == []


def test_existing_archive_conflict_is_preserved(tmp_path, packet):
    header, compressed = packet
    root = tmp_path / "archive"
    receipt = bridge.receive(stream(header, compressed), root, disk_floor=0)
    path = Path(receipt["archive_path"])
    path.write_bytes(b"forensic artifact")
    with pytest.raises(ValueError, match="conflict"):
        bridge.receive(stream(header, compressed), root, disk_floor=0)
    assert path.read_bytes() == b"forensic artifact"


@pytest.mark.parametrize("mode", ["dry_run", "apply", "bad_receipt", "early_reject"])
def test_sender_deletes_only_after_verified_receipt(tmp_path, packet, monkeypatch, mode):
    from contextlib import nullcontext

    from market_signal.ops import backups, runtime

    header, compressed = packet
    root = tmp_path / "backups"
    path = root / "daily" / header["name"]
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.decompress(compressed))
    manifest = {
        **header["source_manifest"],
        "verification_result": "verified",
        "sha256": header["sha256"],
        "schema_version": 30,
    }
    sidecar = backups.sidecar(path)
    sidecar.write_text(json.dumps(manifest))
    monkeypatch.setattr(runtime, "runtime_lock", lambda *a: nullcontext())
    monkeypatch.setattr(runtime, "require_authoritative", lambda *a: None)

    class Pipe(io.BytesIO):
        def write(self, value):
            if mode == "early_reject":
                raise BrokenPipeError()
            return super().write(value)

        def close(self):
            pass

    class Process:
        def __init__(self, *a, **kw):
            self.stdin = Pipe()
            self.saved = self.stdin
            self.returncode = None

        def communicate(self, **kw):
            self.returncode = 1 if mode == "early_reject" else 0
            if self.returncode:
                return b"", b"receiver safety floor"
            body = self.saved.getvalue().split(b"\n", 1)[1]
            receipt = {
                "off_host_verified": True,
                "sha256": header["sha256"],
                "artifact_sha256": hashlib.sha256(body).hexdigest(),
                "restore_test": {"sha256": header["sha256"]},
            }
            if mode == "bad_receipt":
                receipt["restore_test"]["sha256"] = "0" * 64
            return json.dumps(receipt).encode(), b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(bridge.subprocess, "Popen", Process)
    config = {"target": "archive", "identity_file": "private", "known_hosts": "pinned"}
    if mode in {"bad_receipt", "early_reject"}:
        with pytest.raises((ValueError, RuntimeError)):
            bridge.send(config, apply=True, root=root, db=tmp_path / "live.duckdb")
    else:
        result = bridge.send(config, apply=mode == "apply", root=root, db=tmp_path / "live.duckdb")
        assert result[0]["applied"] == (mode == "apply")
    assert path.exists() == (mode != "apply")
    assert sidecar.exists()
    if mode == "apply":
        rows = [
            json.loads(line) for line in (root / "bridge-archive.jsonl").read_text().splitlines()
        ]
        assert [r["action"] for r in rows] == ["delete_intent", "deleted"]
        assert (root / (path.name + ".bridge-receipt.json")).exists()


@pytest.mark.parametrize(
    "mode,backup_exit,archive_fails,expected",
    [
        ("nightly", 0, False, 0),
        ("nightly", 1, False, 1),
        ("retry", None, False, 0),
        ("retry", None, True, 1),
    ],
)
def test_retired_bridge_entrypoint_never_runs_historical_scheduler(
    tmp_path, monkeypatch, mode, backup_exit, archive_fails, expected
):
    import logging
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "bridge_archive", bridge)
    spec = importlib.util.spec_from_file_location(
        "bridge_job", SPEC.origin.replace("bridge_archive.py", "railway_bridge_job.py")
    )
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)
    calls = []
    monkeypatch.setattr(job.sys, "argv", ["bridge_job", mode])
    monkeypatch.setattr(job, "RotatingFileHandler", lambda *a, **kw: logging.NullHandler())
    monkeypatch.setattr(job.signal, "alarm", lambda *a: None)
    monkeypatch.setattr(job, "headroom", lambda: {"safe": True})
    monkeypatch.setattr(job.Path, "read_text", lambda *a: "{}")

    def run(command, **kw):
        calls.append(command)
        return SimpleNamespace(returncode=backup_exit)

    def send(config, **kw):
        assert kw == {"apply": True}
        calls.append("archive")
        if archive_fails:
            raise ValueError("no restore receipt")
        return []

    monkeypatch.setattr(job.subprocess, "run", run)
    monkeypatch.setattr(job, "send", send)
    assert job.main() == 0
    assert calls == []
