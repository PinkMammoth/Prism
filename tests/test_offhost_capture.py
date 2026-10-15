"""A failed off-host stream must never publish a recoverable archive or receipt."""

import base64
import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "offhost_capture", Path(__file__).parents[1] / "scripts/railway_snapshot_offhost.py"
)
capture_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture_module)


@pytest.mark.parametrize(
    "mode", ["incomplete", "bad_base64", "checksum_mismatch", "duplicate_manifest"]
)
def test_failed_stream_never_publishes(tmp_path, mode):
    data = base64.b64encode(gzip.compress(b"untrusted bytes")).decode()
    manifest = json.dumps({"sha256": "0" * 64})
    lines = ["PRISM_CHUNK " + data]
    if mode == "bad_base64":
        lines = ["PRISM_CHUNK !!!"]
    if mode in ("checksum_mismatch", "duplicate_manifest"):
        lines.append("PRISM_MANIFEST " + manifest)
    if mode == "duplicate_manifest":
        lines.append("PRISM_MANIFEST " + manifest)
    code = "\n".join("print(" + repr(line) + ")" for line in lines)
    target = tmp_path / "recovery.duckdb.gz"
    with pytest.raises(ValueError):
        capture_module.capture([sys.executable, "-c", code], target)
    assert not target.exists()
    assert not capture_module.b.sidecar(target).exists()
    assert list(tmp_path.iterdir()) == []


def test_main_refuses_production_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setattr(sys, "argv", ["capture", str(tmp_path / "backup.gz")])
    with pytest.raises(ValueError, match="off-host destination"):
        capture_module.main()
    assert list(tmp_path.iterdir()) == []
