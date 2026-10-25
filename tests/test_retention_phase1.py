from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from market_signal.ops import evaluation_triggers as triggers
from market_signal.ops import retention_phase1 as r

CAPTURE = datetime(2026, 10, 25, 1, tzinfo=UTC)
NOW = datetime(2026, 12, 1, tzinfo=UTC)


@pytest.fixture
def configured(store):
    store.con.execute("INSERT INTO runtime_events VALUES ('claim','authority_claimed','test',?,NULL,'{}')", [CAPTURE])
    manifest = r.prepare(store.con, store.path, CAPTURE - timedelta(days=1), execution_off=True)
    manifest["families"][r.FUNDING] = dict(state="active", activated_at=manifest["activated_at"])
    manifest["seal"] = r.digest({k: v for k, v in manifest.items() if k != "seal"})
    r.write_json(r.state_dir(store.path) / "protection.json", manifest)
    return store, manifest


def source(store, *, capture=CAPTURE, extra=None):
    at = capture.replace(minute=0, second=0)
    raw = [{"coin": "BTC", "time": int(at.timestamp() * 1000), "fundingRate": "0.0001", "premium": "0.0002", **(extra or {})}]
    body = json.dumps(raw).encode()
    sha = hashlib.sha256(body).hexdigest()
    header = dict(provider="hyperliquid", method="POST", status=200, fetched_at=capture.isoformat(), sha256=sha,
                  url="https://api.hyperliquid.xyz/info?token=SECRET", params={"_json": {"type": "fundingHistory", "coin": "BTC"}})
    path = store.raw_dir / "hyperliquid" / capture.strftime("%Y%m%d") / "run_funding_0000.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n" + body)
    store.con.execute("INSERT INTO perp_funding VALUES ('BTC','hyperliquid',?,.0001,.0002,?,'market_close','run_funding')", [at, at])
    store.con.execute("INSERT INTO ingestion_runs (run_id,provider,dataset,entity,started_at,finished_at,status,rows_rejected,raw_paths,raw_sha256) VALUES ('run_funding','hyperliquid','perp_funding_hourly_v2','BTC',?,?,'ok',0,?,?)",
                      [capture, capture + timedelta(seconds=1), json.dumps([str(path)]), json.dumps([sha])])
    return path


def inspect(configured, path, now=NOW):
    store, manifest = configured
    refs, _ = r.references(store.con)
    return r.candidate(store.con, store.path, manifest, store.raw_dir, path, now, refs, set())


def save_manifest(store, manifest):
    manifest["seal"] = r.digest({k: v for k, v in manifest.items() if k != "seal"})
    r.write_json(r.state_dir(store.path) / "protection.json", manifest)


def test_hold_overrides_expiry(configured):
    store, manifest = configured
    path = source(store)
    manifest["holds"].append(dict(id="unique", authority="test", representation="provider_raw", family=r.FUNDING,
                                  dataset="*", owner="study", reason="original required", assets=["BTC"]))
    result = inspect(configured, path)
    assert not result["eligible"] and "unique" in result["holds"]


def test_unknown_dependencies_and_policy_tampering_fail_closed(configured):
    store, _manifest = configured
    path = source(store)
    store.con.execute("INSERT INTO lab_datasets VALUES ('new','{}',?)", [CAPTURE])
    assert not inspect(configured, path)["eligible"]
    assert not r.inactive_execution(store, "microstructure")
    p = r.state_dir(store.path) / "protection.json"
    value = json.loads(p.read_text())
    value["execution_off"] = False
    p.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity"):
        r.load_manifest(store.path)


def test_microdir_cutover_future_history_and_dependencies_protected(store):
    store.con.execute("INSERT INTO runtime_events VALUES ('claim','authority_claimed','test',?,NULL,'{}')", [CAPTURE])
    store.con.execute("INSERT INTO lab_software VALUES ('sw','{}',?)", [CAPTURE])
    store.con.execute("INSERT INTO lab_prospective_studies VALUES ('study','microdir','v','PROSPECTIVE_EXPLORATORY','observed',?,'reason','test','sw','{}')", [CAPTURE])
    store.con.execute("INSERT INTO microstructure_cutover VALUES ('v','test',?,'BTC',?,'session',?)", [CAPTURE, CAPTURE, CAPTURE])
    manifest = r.prepare(store.con, store.path, CAPTURE)
    target = dict(authority="test", representation="normalized", dataset="microstructure_minutes", assets=["BTC"], event_at=NOW)
    assert "study:microstructure_minutes" in r.protections(manifest, target)
    target.update(dataset="perp_funding", event_at=CAPTURE - timedelta(days=119))
    assert "study:perp_funding" in r.protections(manifest, target)
    assert all(h["event"]["end"] is None for h in manifest["holds"])


@pytest.mark.parametrize("offset,blocked", [(30, True), (31, False)])
def test_provider_utc_calendar_age_boundary(configured, offset, blocked):
    store, _ = configured
    path = source(store)
    result = inspect(configured, path, CAPTURE.replace(hour=0) + timedelta(days=offset))
    assert ("diagnostic_window" in result["blockers"]) is blocked


def test_unresolved_ingestion_and_unknown_unique_fields_block(configured):
    store, _ = configured
    path = source(store, extra={"new_scientific_field": 7})
    refs, _ = r.references(store.con)
    assert r.funding_coverage(store.con, path, refs[str(path)])[1] == "unique_or_unknown_source_fields"
    store.con.execute("UPDATE ingestion_runs SET status='failed'")
    assert "unresolved_ingestion_disposition" in inspect(configured, path)["blockers"]


def test_normalized_revision_or_missing_availability_blocks(configured):
    store, _ = configured
    path = source(store)
    store.con.execute("UPDATE perp_funding SET funding_rate=.02")
    refs, _ = r.references(store.con)
    assert not r.funding_coverage(store.con, path, refs[str(path)])[0]


def test_prospective_and_family_cutover_separately_protect(configured):
    store, manifest = configured
    path = source(store)
    manifest["activated_at"] = (CAPTURE + timedelta(seconds=1)).isoformat()
    assert "legacy_custody_requires_separate_qualification" in inspect(configured, path)["blockers"]
    manifest["activated_at"] = (CAPTURE - timedelta(days=1)).isoformat()
    manifest["families"][r.FUNDING]["activated_at"] = (CAPTURE + timedelta(seconds=1)).isoformat()
    assert "before_family_cutover" in inspect(configured, path)["blockers"]


def test_missing_recovery_and_unenrolled_family_retain(configured):
    store, manifest = configured
    path = source(store)
    assert not inspect(configured, path)["eligible"]
    del manifest["families"][r.FUNDING]
    assert "family_not_enrolled" in inspect(configured, path)["blockers"]


def test_real_recovery_snapshot_and_artifact_are_required(configured, tmp_path):
    import duckdb

    store, _ = configured
    path = source(store)
    snapshot = tmp_path / "recovery.duckdb"
    with duckdb.connect(str(snapshot)) as backup:
        backup.execute("CREATE TABLE ingestion_runs(run_id VARCHAR,status VARCHAR,raw_sha256 JSON,finished_at TIMESTAMPTZ,rows_rejected INTEGER)")
        backup.execute("INSERT INTO ingestion_runs VALUES (?,?,?,?,?)", list(store.con.execute(
            "SELECT run_id,status,raw_sha256,finished_at,rows_rejected FROM ingestion_runs").fetchone()))
        backup.execute("CREATE TABLE perp_funding(coin VARCHAR,source VARCHAR,time TIMESTAMPTZ,funding_rate DOUBLE,premium DOUBLE,available_at TIMESTAMPTZ,pit_method VARCHAR)")
        backup.execute("INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?)", list(store.con.execute(
            "SELECT coin,source,time,funding_rate,premium,available_at,pit_method FROM perp_funding").fetchone()))
    artifact = tmp_path / "independent.duckdb.gz"
    artifact.write_bytes(gzip.compress(snapshot.read_bytes()))
    receipt = tmp_path / "export.json"
    receipt.write_text(json.dumps(dict(off_host_verified=True, sha256=r.file_sha(snapshot),
                                      archive_path=str(artifact), artifact_sha256=r.file_sha(artifact))))
    r.write_json(r.state_dir(store.path) / "recovery.json", dict(snapshot=str(snapshot),
                 sha256=r.file_sha(snapshot), export_receipt=str(receipt), boundary=NOW.isoformat()))
    refs, _ = r.references(store.con)
    assert r.recovery_coverage(store.con, store.path, path, refs[str(path)])[0]
    artifact.write_bytes(b"corrupt")
    assert not r.recovery_coverage(store.con, store.path, path, refs[str(path)])[0]


def test_null_rejection_disposition_is_unknown(configured):
    store, _ = configured
    path = source(store)
    store.con.execute("UPDATE ingestion_runs SET rows_rejected=NULL")
    assert "unresolved_ingestion_disposition" in inspect(configured, path)["blockers"]


def test_latest_source_object_survives_later_success_without_a_response(configured):
    store, _ = configured
    source(store)
    store.con.execute("INSERT INTO ingestion_runs (run_id,provider,dataset,entity,started_at,finished_at,status,rows_rejected,raw_paths,raw_sha256) VALUES ('empty','hyperliquid','perp_funding_hourly_v2','BTC',?,?,'ok',0,'[]','[]')", [NOW, NOW])
    _, latest = r.references(store.con)
    assert 'run_funding' in latest and 'empty' not in latest


def test_qualification_never_stats_foreign_authority_archives(configured, monkeypatch):
    from pathlib import Path
    store, _ = configured
    source(store)
    original = r.references
    foreign = Path('/unreadable-other-authority/raw/hyperliquid/20261001/run_old_0000.json.gz')

    def references(con):
        refs, latest = original(con)
        refs[str(foreign)] = [dict(dataset='perp_funding_hourly_v2')]
        return refs, latest

    original_stat = Path.is_file

    def protected_stat(path):
        if path == foreign:
            raise AssertionError('foreign authority stat attempted')
        return original_stat(path)

    monkeypatch.setattr(r, 'references', references)
    monkeypatch.setattr(Path, 'is_file', protected_stat)
    assert r.qualify_funding(store.con, store.raw_dir)['passed']


def test_candidate_only_does_not_modify_files_or_database(configured):
    store, manifest = configured
    path = source(store)
    before = {str(p): p.read_bytes() for p in store.path.parent.rglob("*") if p.is_file()}
    rows = store.con.execute("SELECT count(*) FROM ingestion_runs").fetchone()
    result = r.report(store.con, store.path, manifest, store.raw_dir, NOW, qualify=True)
    after = {str(p): p.read_bytes() for p in store.path.parent.rglob("*") if p.is_file()}
    assert before == after and result["dry_run"] and rows == store.con.execute("SELECT count(*) FROM ingestion_runs").fetchone()
    assert "SECRET" not in r.canonical(result)
    assert path.exists()


def test_inactive_wakeups_suppressed_context_still_persisted(configured, monkeypatch):
    store, _ = configured
    monkeypatch.setattr(triggers, "outbox_enabled", lambda: True)
    for source_name in ("microstructure", "technical", "positioning"):
        assert triggers.enqueue(store, source_name, "key", ["BTC"], CAPTURE) is None
    assert triggers.enqueue(store, "context", "event", ["BTC"], CAPTURE, event_ids=["event"])
    assert store.con.execute("SELECT source FROM paper_nimble_triggers").fetchall() == [("context",)]


def test_active_pending_or_unknown_state_is_not_suppressed(configured, monkeypatch):
    store, _ = configured
    monkeypatch.setattr(triggers, "outbox_enabled", lambda: True)
    store.con.execute("INSERT INTO paper_nimble_research_pending VALUES ('pending','run',?,'{}')", [CAPTURE])
    assert triggers.enqueue(store, "microstructure", "pending", ["BTC"], CAPTURE)
    store.con.execute("DELETE FROM paper_nimble_research_pending")
    store.con.execute("INSERT INTO paper_nimble_runs VALUES ('run',?,'paper','p','risk','{}')", [CAPTURE])
    assert triggers.enqueue(store, "microstructure", "active", ["BTC"], CAPTURE)


def test_missing_manifest_does_not_suppress(store, monkeypatch):
    monkeypatch.setattr(triggers, "outbox_enabled", lambda: True)
    assert triggers.enqueue(store, "microstructure", "unconfigured", ["BTC"], CAPTURE)


def test_explicit_research_trigger_hold_overrides_inactive_suppression(configured, monkeypatch):
    store, manifest = configured
    manifest['holds'].append(dict(id='research-arrivals', authority='test', representation='execution_trigger',
                                 dataset='microstructure', assets=['BTC'], owner='arrival-study',
                                 range_mode='OPEN_ENDED', reason='arrival timing is research input'))
    save_manifest(store, manifest)
    monkeypatch.setattr(triggers, 'outbox_enabled', lambda: True)
    assert triggers.enqueue(store, 'microstructure', 'research-input', ['BTC'], CAPTURE)


def test_expiry_idempotent_with_durable_intent(configured, monkeypatch):
    store, manifest = configured
    path = source(store)
    # Recovery correctness has its own tests; isolate the destructive state machine.
    monkeypatch.setattr(r, "recovery_coverage", lambda *a: (True, "verified"))
    original_refs = r.references
    monkeypatch.setattr(r, "references", lambda c: (original_refs(c)[0], set()))
    result = r.expire(store.con, store.path, manifest, store.raw_dir, path, NOW)
    assert not path.exists() and result["state"] == "expired"
    assert result["captured_at"] == CAPTURE.isoformat()
    assert result["run_ids"] == ["run_funding"] and result["body_sha256"]
    assert "SECRET" not in r.canonical(result)
    journals = list((r.state_dir(store.path) / "expiry").glob("*.jsonl"))
    before = journals[0].read_bytes()
    assert r.expire(store.con, store.path, manifest, store.raw_dir, path, NOW) == result
    assert journals[0].read_bytes() == before
    assert "sha256" in r.journal_rows(store.path)[str(path)]


def test_protection_change_between_intent_and_unlink_preserves_source(configured, monkeypatch):
    store, manifest = configured
    path = source(store)
    monkeypatch.setattr(r, "recovery_coverage", lambda *a: (True, "verified"))
    original_refs, append = r.references, r.journal_append
    monkeypatch.setattr(r, "references", lambda c: (original_refs(c)[0], set()))

    def change(db, receipt):
        append(db, receipt)
        manifest["execution_off"] = False
        save_manifest(store, manifest)

    monkeypatch.setattr(r, "journal_append", change)
    with pytest.raises(ValueError, match="protection"):
        r.expire(store.con, store.path, manifest, store.raw_dir, path, NOW)
    assert path.exists()


def test_crash_after_unlink_reconciles_committed_intent(configured, monkeypatch):
    store, manifest = configured
    path = source(store)
    monkeypatch.setattr(r, "recovery_coverage", lambda *a: (True, "verified"))
    original_refs, append = r.references, r.journal_append
    monkeypatch.setattr(r, "references", lambda c: (original_refs(c)[0], set()))

    def interrupt(db, receipt):
        if receipt["state"] == "expired":
            raise OSError("simulated interrupted completion")
        append(db, receipt)

    monkeypatch.setattr(r, "journal_append", interrupt)
    with pytest.raises(OSError):
        r.expire(store.con, store.path, manifest, store.raw_dir, path, NOW)
    assert not path.exists()
    monkeypatch.setattr(r, "journal_append", append)
    assert r.expire(store.con, store.path, manifest, store.raw_dir, path, NOW)["reconciled"]


def test_symlink_or_hardlink_protected(configured):
    import os
    store, _ = configured
    path = source(store)
    os.link(path, path.with_name("run_second_0000.json.gz"))
    assert not inspect(configured, path)["eligible"]


def test_malformed_evidence_is_retained_and_its_bytes_remain_visible(configured):
    store, _ = configured
    path = source(store)
    path.write_bytes(b'incomplete original response')
    row = inspect(configured, path)
    assert not row['eligible'] and row['blockers']
    assert row['bytes'] == path.stat().st_size and row['allocated_bytes'] == path.stat().st_blocks * 512
    assert path.exists()


def test_hold_unknown_coordinates_intersect_and_fixed_ranges():
    hold = dict(authority="test", representation="provider_raw", dataset="funding", assets=["BTC"],
                knowledge={"start": CAPTURE.isoformat(), "end": NOW.isoformat()})
    assert r.hold_matches(hold, dict(authority="test", representation="provider_raw"))
    assert not r.hold_matches(hold, dict(authority="test", representation="provider_raw", knowledge_at=NOW))


def test_legacy_tool_cannot_bypass_phase1_protection(configured):
    from market_signal.ops.raw_archives import delete_verified
    store, _ = configured
    path = source(store)
    with pytest.raises(ValueError, match="Phase 1"):
        delete_verified(store.raw_dir, path, {"archive_type": "provider-raw"}, {}, store.path.parent / "audit")
    (r.state_dir(store.path) / "protection.json").unlink()
    with pytest.raises(ValueError, match="Phase 1"):
        delete_verified(store.raw_dir, path, {"archive_type": "provider-raw"}, {}, store.path.parent / "audit")
