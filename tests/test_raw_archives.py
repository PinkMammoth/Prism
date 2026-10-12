from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from market_signal.ops import raw_archives as raw


@pytest.fixture
def provider(tmp_path):
    root = tmp_path / "raw"
    path = root / "hyperliquid" / "20261001" / "run_example_0000.json.gz"
    path.parent.mkdir(parents=True)
    body = b'[{"value":42}]'
    digest = hashlib.sha256(body).hexdigest()
    envelope = {
        "provider": "hyperliquid",
        "method": "POST",
        "url": "https://example.test/info",
        "params": {},
        "status": 200,
        "fetched_at": "2026-10-01T00:00:00+00:00",
        "sha256": digest,
    }
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(envelope).encode() + b"\n" + body)
    context = {
        "verified_backup_sha256": "verified-db",
        "snapshot_timestamp": "2026-10-02T00:00:00+00:00",
        "active_file_scan_complete": True,
        "dependency_review": "reviewed existing DB-only consumers",
        "references": {
            str(path): [
                {
                    "sha256": digest,
                    "run_id": "run_example",
                    "status": "ok",
                    "finished_at": "2026-10-01T00:01:00+00:00",
                    "rows_rejected": 0,
                }
            ]
        },
    }
    return root, path, context


def exported(provider, tmp_path):
    root, path, context = provider
    return raw.export_file(
        path,
        tmp_path / "off-host" / path.name,
        raw.make_manifest(root, path, "provider-raw", context),
    )


def test_export_and_verified_delete_dry_run_exact_audit(provider, tmp_path):
    root, path, context = provider
    receipt = exported(provider, tmp_path)
    assert receipt["artifact_sha256"] == raw.sha256(path)
    assert receipt["restore_read_test"] == receipt["read_test"]
    assert receipt["read_test"]["record_count"] == 1 and receipt["eligibility"]["class"] == "B"
    log = tmp_path / "audit" / "deletions.jsonl"
    assert raw.delete_verified(root, path, receipt, context, log)["dry_run"]
    assert path.exists() and not log.exists()
    raw.delete_verified(root, path, receipt, context, log, apply=True)
    assert not path.exists()
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["action"] for r in rows] == ["delete_intent", "deleted"]
    assert all(r["path"] == str(path) and r["sha256"] == receipt["sha256"] for r in rows)


@pytest.mark.parametrize(
    "protection", ["latest", "active", "pending", "research", "failed", "rejected", "hash", "scan"]
)
def test_dependency_ingest_and_current_source_protection(provider, protection):
    root, path, context = provider
    parsed = raw.read_test(path, "provider-raw")
    if protection in {"active", "pending", "research"}:
        key = {"active": "open_paths", "pending": "pending_paths", "research": "dependency_paths"}[
            protection
        ]
        context[key] = [str(path)]
    elif protection == "latest":
        context["latest_run_ids"] = ["run_example"]
    elif protection == "failed":
        context["references"][str(path)][0]["status"] = "failed"
    elif protection == "rejected":
        context["references"][str(path)][0]["rows_rejected"] = 1
    elif protection == "scan":
        context["active_file_scan_complete"] = False
    else:
        context["references"][str(path)][0]["sha256"] = "mismatch"
    assert not raw.provider_eligibility(path, root, context, parsed)["export_eligible"]


def test_failed_verification_and_changed_identity_prevent_delete(provider, tmp_path):
    root, path, context = provider
    receipt = exported(provider, tmp_path)
    log = tmp_path / "audit.jsonl"
    with pytest.raises(ValueError, match="receipt"):
        raw.delete_verified(
            root, path, {**receipt, "off_host_verified": False}, context, log, apply=True
        )
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="identity"):
        raw.delete_verified(root, path, receipt, context, log, apply=True)
    assert path.exists() and not log.exists()


def test_path_safety_and_current_spool_protection(provider, tmp_path):
    root, path, context = provider
    with pytest.raises(ValueError, match="traversal"):
        raw.safe_source(root, path.parent / ".." / path.name, "provider-raw")
    link = path.with_name("run_link.json.gz")
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        raw.safe_source(root, link, "provider-raw")
    with pytest.raises(ValueError, match="protected"):
        raw.delete_verified(
            root,
            path,
            {"archive_type": "microstructure-spool"},
            context,
            tmp_path / "log",
            apply=True,
        )
    with pytest.raises(ValueError, match="approved"):
        raw.safe_source(root, path.with_name(path.name + ".part"), "provider-raw")


def test_raw_decompression_existing_replay_and_damaged_member(tmp_path):
    from market_signal.microstructure.spool import Spool

    spool = Spool(tmp_path)
    spool.archive([("S", 1000000, {}), ("K", 1060000)], 1000000)
    path = next((tmp_path / "raw").glob("*/*.gz"))
    result = raw.read_test(path, "microstructure-raw")
    assert result["record_count"] == 2 and "Engine.replay" in result["reader"]
    path.write_bytes(path.read_bytes()[:-5])
    with pytest.raises(EOFError):
        raw.read_test(path, "microstructure-raw")


def test_bundle_format_and_subsecond_causal_boundary(provider):
    root, path, context = provider
    with gzip.open(path, "rb") as f:
        env, body = f.read().split(b"\n", 1)
    header = json.loads(env)
    header["fetched_at"] = "2026-10-02T00:00:00.001+00:00"
    path = path.with_name("run_bundle.jsonl.gz")
    with gzip.open(path, "wb") as f:
        f.write(json.dumps({"envelope": header, "body": body.decode()}).encode() + b"\n")
    context["references"][str(path)] = next(iter(context["references"].values()))
    result = raw.read_test(path, "provider-raw")
    assert result["format"] == "provider-response-bundle-v1"
    assert not raw.provider_eligibility(path, root, context, result)["export_eligible"]


def test_calibration_archive_existing_reader_without_deletion(tmp_path):
    path = tmp_path / "lp" / "2026-10-01.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"BTC": {"bins": {"1": 10}, "minutes": 10}}))
    result = raw.read_test(path, "large-print-calibration")
    assert result["record_count"] == 1 and "LargePrints.threshold" in result["reader"]
    manifest = raw.make_manifest(
        path.parent, path, "large-print-calibration", {"active_file_scan_complete": True}
    )
    assert manifest["eligibility"]["export_eligible"] and not manifest["deletion_eligibility"]


@pytest.mark.parametrize("malicious", [False, True])
def test_batch_transfer_verified_off_host_without_tar_path_extraction(
    provider, tmp_path, monkeypatch, malicious
):
    import base64
    import io
    import tarfile
    from types import SimpleNamespace

    from scripts import raw_archive as helper

    root, path, context = provider
    manifest = raw.make_manifest(root, path, "provider-raw", context)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo("../../escape" if malicious else "0")
        member.size = path.stat().st_size
        tar.addfile(member, io.BytesIO(path.read_bytes()))

    class Process:
        stdout = io.StringIO(
            "PRISM_CHUNK " + base64.b64encode(buffer.getvalue()).decode() + "\nPRISM_DONE\n"
        )

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setitem(helper.ROOTS, "provider-raw", str(root))
    args = SimpleNamespace(project="test-project", service="test-service")
    dest = tmp_path / "off-host"
    dest.mkdir()
    if malicious:
        with pytest.raises(ValueError, match="unexpected tar"):
            helper.export_batch(args, [manifest], dest)
        assert not list(dest.glob("provider-raw/**/*"))
    else:
        helper.export_batch(args, [manifest], dest)
        receipt = helper.verified_receipt(dest, manifest)
        assert receipt["sha256"] == raw.sha256(path) and path.exists()


def test_context_ledger_recovery_and_restart_without_historical_raw(
    provider, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import duckdb

    from market_signal.context.providers.base import last_state
    from market_signal.ops import backups

    root, path, _ = provider
    path = path.rename(path.with_name("ctxrun_fred_old_0000.json.gz"))
    digest = raw.read_test(path, "provider-raw")["payload_sha256"][0]
    db = tmp_path / "snapshot.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE ingestion_runs(run_id VARCHAR,provider VARCHAR,dataset VARCHAR,entity VARCHAR,status VARCHAR,finished_at TIMESTAMPTZ,rows_rejected INT,raw_paths VARCHAR,raw_sha256 VARCHAR)"
    )
    con.execute("CREATE TABLE microstructure_ingest_offsets(path VARCHAR,bytes BIGINT)")
    con.execute("CREATE TABLE microstructure_minutes(minute_open TIMESTAMPTZ)")
    con.execute(
        "CREATE TABLE context_provider_runs(run_id VARCHAR,provider VARCHAR,status VARCHAR,finished_at TIMESTAMPTZ,rejected INT,payload VARCHAR)"
    )
    for run_id, finished, refs in [
        ("old", "2026-10-01T00:01:00Z", [{"path": str(path), "sha256": digest}]),
        ("latest", "2026-10-01T02:00:00Z", []),
    ]:
        con.execute(
            "INSERT INTO context_provider_runs VALUES (?, 'fred', 'ok', ?, 0, ?)",
            [run_id, finished, json.dumps({"raw": refs, "state": {"cursor": 42}})],
        )
    con.close()
    manifest = {
        "verification_result": True,
        "sha256": raw.sha256(db),
        "backup_timestamp": "2026-10-02T00:00:00+00:00",
    }
    monkeypatch.setattr(backups, "inventory", lambda *a: [{"path": str(db), "manifest": manifest}])
    # Isolate the fixture process scan; host processes may be inaccessible to this user.
    import os
    from pathlib import Path

    original_iterdir = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda p: (
            iter([Path("/proc") / str(os.getpid())]) if p == Path("/proc") else original_iterdir(p)
        ),
    )
    context = raw.recovery_context(db, "DB-only runtime reader inspected")
    assert (
        context["references"][str(path)][0]["reference_table"]
        == "context_provider_runs.payload.raw"
    )
    receipt = raw.export_file(
        path,
        tmp_path / "off-host" / path.name,
        raw.make_manifest(root, path, "provider-raw", context),
    )
    raw.delete_verified(root, path, receipt, context, tmp_path / "audit.jsonl", apply=True)
    with duckdb.connect(str(db), read_only=True) as restored:
        assert last_state(SimpleNamespace(con=restored), "fred") == {"cursor": 42}
    assert raw.read_test(tmp_path / "off-host" / path.name, "provider-raw") == receipt["read_test"]
    assert raw.sha256(db) == manifest["sha256"]


def test_rss_existing_reader_and_ingest_completion_boundary(provider):
    root, path, context = provider
    body = b"<rss><channel><title>Test</title><item><title>Incident</title><link>https://example.test/1</link></item></channel></rss>"
    with gzip.open(path, "rb") as f:
        env = json.loads(f.read().split(b"\n", 1)[0])
    env.update(provider="rss_coinbase_status", sha256=hashlib.sha256(body).hexdigest())
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(env).encode() + b"\n" + body)
    context["references"][str(path)][0]["sha256"] = env["sha256"]
    parsed = raw.read_test(path, "provider-raw")
    assert "RSS" in parsed["reader"]
    assert raw.provider_eligibility(path, root, context, parsed)["export_eligible"]
    context["references"][str(path)][0]["finished_at"] = "2026-10-02T00:00:00.001+00:00"
    assert not raw.provider_eligibility(path, root, context, parsed)["export_eligible"]


def test_closed_hour_without_start_exercises_replay(tmp_path):
    path = tmp_path / "01.jsonl.gz"
    events = [("F", 3600000, "BTC", 3600000, 10, 11, 100, 100), ("K", 3720000)]
    with gzip.open(path, "wb") as f:
        f.write(b"".join(json.dumps(e).encode() + b"\n" for e in events))
    result = raw.read_test(path, "microstructure-raw")
    assert "seed supplied=True" in result["reader"]
    assert "0 emitted records" not in result["reader"]


def test_operator_apply_receipts_stream_over_stdin(monkeypatch):
    from types import SimpleNamespace

    from scripts import raw_archive as cli

    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout='{"dry_run":true,"results":[]}\n', stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = cli.remote(
        SimpleNamespace(project="project", service="service"),
        cli.APPLY_ACTION,
        "review",
        "no",
        stdin="[]",
    )
    assert result["dry_run"]
    assert observed["kwargs"]["input"] == "[]"
    assert observed["command"][-2:] == ["review", "no"]
    assert "json.loads(sys.stdin.read())" in observed["command"][-3]
