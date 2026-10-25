"""Phase 1 only: reviewed protections, provider candidates and bounded expiry.

No market-table deletes, schema migrations, collector controls or DB compaction.
The CLI takes the existing authority runtime flock and opens DuckDB read-only.
Unconfigured/stale/unknown state always preserves source evidence and wakeups.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import stat
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

POLICY = "prism_conservative_retention_phase1_v1"
FUNDING = "hyperliquid_settled_funding"
FAMILIES = (FUNDING, "hyperliquid_other", "hyperliquid_intraday", "binance_futures",
            "fred", "tiingo", "sec_edgar", "free_sources", "other")
DEPENDENCIES = (
    "lab_experiments", "lab_datasets", "lab_dataset_blobs", "lab_snapshot_blobs",
    "lab_prospective_studies", "lab_structure_studies", "lab_forward_trackings",
    "lab_research_registrations", "lab_corroboration_registrations", "incubation_freezes",
    "paper_nimble_runs", "paper_runs", "paper_v2_runs", "copilot_watchlist",
)
OBLIGATIONS = ("paper_nimble_runs", "paper_nimble_accounts", "paper_nimble_positions",
               "paper_nimble_research_pending", "paper_nimble_trigger_receipts",
               "paper_runs", "paper_v2_runs", "paper_event_pending")
SAFE_NAME = re.compile(r"(?:run|ctxrun)_[a-zA-Z0-9_]+\.json(?:l)?\.gz\Z")
_RECOVERY_STACK = None
_RECOVERY_READERS = {}


def timestamp(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if t.tzinfo is None:
        raise ValueError("qualified timestamp required")
    return t.astimezone(UTC)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def state_dir(db):
    db = Path(db)
    return (db.parent.parent if db.parent.name == "import" else db.parent) / "retention-phase1"


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked retention destination")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp-" + os.urandom(6).hex())
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(canonical(value) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def load_manifest(db):
    path = state_dir(db) / "protection.json"
    # Refuse symlinked policy state, including ancestors.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked protection state")
    value = json.loads(path.read_text())
    if value.get("seal") != digest({k: v for k, v in value.items() if k != "seal"}):
        raise ValueError("protection identity mismatch")
    return value


def commit_manifest(db, value):
    """Small immutable review history, not a per-observation provenance archive."""
    root = state_dir(db)
    write_json(root / "reviews" / (value["seal"] + ".json"), value)
    write_json(root / "protection.json", value)


def authority(con):
    row = con.execute("SELECT runtime_id FROM runtime_events WHERE event_type='authority_claimed' "
                      "ORDER BY recorded_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("source authority unknown")
    return row[0]


def dependency_state(con):
    """Frozen registration state, not growing market/evaluation outcome rows."""
    result = {}
    for table in DEPENDENCIES:
        result[table] = list(con.execute(
            f'SELECT count(*),bit_xor(hash(t)) FROM "{table}" t'
        ).fetchone())
    schema = con.execute("SELECT table_name,column_name,data_type FROM information_schema.columns "
                         "WHERE table_catalog=current_database() AND table_schema='main' ORDER BY 1,2").fetchall()
    result["schema"] = digest(schema)
    base = Path(__file__).resolve().parents[1]
    result["consumer_code"] = {name: file_sha(base / name) for name in (
        "ops/retention_phase1.py", "ops/evaluation_triggers.py", "perps/data.py", "research/microdir/data.py",
        "research/lab/datasets.py", "research/lab/ledger.py", "paper/nimble/events.py")}
    return result


def validate_manifest(con, db, manifest):
    if manifest.get("policy_version") != POLICY or manifest.get("db") != str(Path(db).absolute()):
        raise ValueError("policy/database mismatch")
    if manifest.get("authority") != authority(con):
        raise ValueError("source authority mismatch")
    if manifest.get("dependencies") != dependency_state(con):
        raise ValueError("dependency or schema changed; reviewed protection required")
    if manifest.get("unknown_action") != "retain" or not manifest.get("holds"):
        raise ValueError("missing fail-closed protections")
    timestamp(manifest["activated_at"])


def hold_matches(hold, target):
    """Missing target coordinates conservatively intersect a declared hold."""
    for key in ("authority", "representation", "dataset", "family"):
        required = hold.get(key, "*")
        if required != "*" and target.get(key) is not None and required != target[key]:
            return False
    assets = hold.get("assets", ["*"])
    if "*" not in assets and target.get("assets") and not set(assets) & set(target["assets"]):
        return False
    for dimension in ("event", "knowledge"):
        bounds = hold.get(dimension, {})
        point = target.get(dimension + "_at")
        if point is not None:
            point = timestamp(point)
            if bounds.get("start") and point < timestamp(bounds["start"]):
                return False
            if bounds.get("end") and point >= timestamp(bounds["end"]):
                return False
    return True


def protections(manifest, target):
    return [h["id"] for h in manifest["holds"] if hold_matches(h, target)]


def prepare(con, db, now, *, execution_off=False):
    """Create review material; caller must inspect before activating any family."""
    now = timestamp(now)
    owner = authority(con)
    studies = con.execute("SELECT study_id,name FROM lab_prospective_studies ORDER BY 1").fetchall()
    cutover = con.execute("SELECT min(first_minute) FROM microstructure_cutover").fetchone()[0]
    holds = []

    def add(identifier, representation, dataset, reason, start=None, owner_id=None, family="*"):
        holds.append(dict(id=identifier, authority=owner, representation=representation,
                          dataset=dataset, family=family, schema_version=31, assets=["*"],
                          event={"start": str(start) if start else None, "end": None},
                          knowledge={"start": None, "end": None}, range_mode="OPEN_ENDED",
                          owner=owner_id or identifier, reason=reason))
    for study, _name in studies:
        for table in ("microstructure_minutes", "microstructure_revisions", "microstructure_cutover",
                      "microstructure_lp_thresholds", "microstructure_provider_runs"):
            add(study + ":" + table, "normalized", table,
                "Active cumulative Microdir: original cutover and future matching observations", cutover, study)
        for table, days in (("perp_funding", 120), ("context_hl_oi_hourly", 30),
                            ("perp_oi_history", 30), ("context_ls_ratios", 30), ("perp_snapshots", 30)):
            add(study + ":" + table, "normalized", table, "Microdir causal dependency/warmup",
                timestamp(cutover) - timedelta(days=days) if cutover else None, study)
        add(study + ":context", "context", "*", "Microdir causal Context, updates and timing", owner_id=study)
    add("frozen-experiments", "frozen_evidence", "*", "Exact input blobs, plans, trials and results")
    add("decisions-outcomes", "decision_evidence", "*", "Execution/evaluation/accounting/cohort dependencies")
    add("canonical-observations", "normalized", "*", "Phase 1 never expires normalized scientific data")
    add("context-timings", "execution_trigger", "context", "source_timings historical consumer")
    for category in ("incident", "migration", "rollback", "backup", "collector_owned"):
        add(category + "-custody", category, "*", "Protected obligation; never ordinary production expiry")
    for family in FAMILIES:
        if family != FUNDING:
            add("unresolved:" + family, "provider_raw", "*",
                "Unique evidence/dependency coverage not qualified", family=family)
    value = dict(policy_version=POLICY, activated_at=now.isoformat(), authority=owner,
                 db=str(Path(db).absolute()), unknown_action="retain", dependencies=dependency_state(con),
                 holds=holds, families={}, execution_off=bool(execution_off),
                 reviewed_trigger_sources=["microstructure", "technical", "positioning"],
                 legacy_action="retain_existing_custody", raw_window_completed_dates=30,
                 studies=[dict(study_id=s, name=n, cutover=str(cutover)) for s, n in studies])
    value["seal"] = digest(value)
    return value


def inactive_execution(store, source):
    """Opt-in suppression, within the producer's existing transaction/connection."""
    if source == "context":
        return False
    try:
        manifest = load_manifest(store.path)
        validate_manifest(store.con, store.path, manifest)
        if not manifest.get("execution_off") or source not in manifest["reviewed_trigger_sources"]:
            return False
        if protections(manifest, dict(authority=manifest["authority"], representation="execution_trigger",
                                      dataset=source, assets=None)):
            return False
        # Even historical/draining run state is preserved rather than guessed inactive.
        return all(store.con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == 0
                   for t in OBLIGATIONS)
    except Exception:
        return False


def safe_file(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    rel = path.relative_to(root)
    if len(rel.parts) != 3 or not re.fullmatch(r"[a-zA-Z0-9_-]+", rel.parts[0]):
        raise ValueError("unapproved provider path")
    if not re.fullmatch(r"\d{8}", rel.parts[1]) or not SAFE_NAME.fullmatch(rel.parts[2]):
        raise ValueError("unapproved provider filename")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink protected")
    s = path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError("non-independent file protected")
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def envelope(path):
    # Never return URL/params/body or exception text in operational output.
    with gzip.open(path, "rb") as f:
        header = json.loads(f.readline(1024 * 1024))
    return header.get("envelope", header)


def family_for(path, header):
    folder = Path(path).parent.parent.name
    request = (header.get("params") or {}).get("_json") or {}
    if folder == "hyperliquid":
        return FUNDING if request.get("type") == "fundingHistory" else "hyperliquid_other"
    if folder.startswith("rss_"):
        return "free_sources"
    return folder if folder in FAMILIES else "other"


def references(con):
    refs, latest = {}, {}
    rows = con.execute("SELECT run_id,provider,dataset,entity,status,finished_at,rows_rejected,"
                       "raw_paths,raw_sha256 FROM ingestion_runs ORDER BY finished_at NULLS FIRST").fetchall()
    for rid, provider, dataset, entity, status, finished, rejected, paths, hashes in rows:
        paths, hashes = json.loads(paths or "[]"), json.loads(hashes or "[]")
        if status == "ok" and finished and paths:
            latest[(provider, dataset, entity)] = rid
        for i, path in enumerate(paths):
            refs.setdefault(path, []).append(dict(run_id=rid, provider=provider, dataset=dataset,
                                                  entity=entity, status=status, finished_at=str(finished) if finished else None,
                                                  rows_rejected=rejected, body_sha=hashes[i] if i < len(hashes) else None))
    return refs, set(latest.values())


def funding_coverage(con, path, refs):
    """Only the exact, reviewed four-field settled-funding response contract."""
    if len(refs) != 1 or refs[0]["dataset"] not in ("perp_funding", "perp_funding_hourly_v2"):
        return False, "unqualified_ingestion_dataset"
    with gzip.open(path, "rb") as f:
        header = json.loads(f.readline(1024 * 1024))
        body = f.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        return False, "body_limit_or_unknown_bundle"
    if hashlib.sha256(body).hexdigest() != header.get("sha256") or header.get("sha256") != refs[0]["body_sha"]:
        return False, "body_hash_mismatch"
    request = header.get("params", {}).get("_json", {})
    coin = refs[0]["entity"]
    endpoint = urlsplit(header.get("url", ""))
    if (header.get("provider") != "hyperliquid" or header.get("status") != 200
            or header.get("method") != "POST" or endpoint.hostname != "api.hyperliquid.xyz" or endpoint.path != "/info"
            or request.get("type") != "fundingHistory" or request.get("coin") != coin):
        return False, "endpoint_or_entity_mismatch"
    rows = json.loads(body)
    if not isinstance(rows, list) or not rows:
        return False, "empty_or_unknown_unique_evidence"
    for r in rows:
        if not isinstance(r, dict) or not {"time", "fundingRate"} <= r.keys() or r.keys() - {"coin", "time", "fundingRate", "premium"}:
            return False, "unique_or_unknown_source_fields"
        if r.get("coin", coin) != coin:
            return False, "source_entity_mismatch"
        at = datetime.fromtimestamp(int(r["time"]) / 1000, UTC)
        if at > timestamp(header["fetched_at"]):
            return False, "unsettled_or_filtered_observation"
        row = con.execute("SELECT funding_rate,premium,available_at,pit_method FROM perp_funding "
                          "WHERE coin=? AND source='hyperliquid' AND time=?", [coin, at]).fetchone()
        premium = float(r["premium"]) if r.get("premium") is not None else None
        rate = float(r["fundingRate"])
        if not math.isfinite(rate) or (premium is not None and not math.isfinite(premium)):
            return False, "nonfinite_observation"
        if (not row or row[0] != rate or row[1] != premium or not row[2] or not row[3]
                or timestamp(row[2]) > timestamp(header["fetched_at"])):
            return False, "normalized_revision_or_causal_coverage_missing"
    return True, "exact_settled_funding_values_and_availability_retained"


def recovery_coverage(con, db, path, refs):
    """Require an actual verified independent native snapshot, never a boolean assertion.

    A source-owned recovery.json identifies an existing verified backup and export receipt.
    Missing/stale/unresolvable proof blocks expiry; no backup is created by retention.
    """
    import duckdb
    explicit = state_dir(db) / "recovery.json"
    if not explicit.exists():
        backup, boundary = native_recovery(db)
        return backup_funding_coverage(backup, boundary, path, refs)
    proof = json.loads(explicit.read_text())
    snapshot = Path(proof["snapshot"])
    receipt = json.loads(Path(proof["export_receipt"]).read_text())
    if snapshot.resolve() == Path(db).resolve() or any(p.is_symlink() for p in (snapshot, *snapshot.parents)):
        return False, "independent_recovery_required"
    if (file_sha(snapshot) != proof["sha256"] or receipt.get("off_host_verified") is not True
            or receipt.get("sha256") != proof["sha256"]):
        return False, "verified_recovery_identity_missing"
    artifact = Path(receipt["archive_path"])
    if not artifact.is_file() or receipt.get("artifact_sha256") != file_sha(artifact):
        return False, "verified_recovery_artifact_missing"
    opener = gzip.open if artifact.suffix == ".gz" else open
    with opener(artifact, "rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != proof["sha256"]:
            return False, "recovery_artifact_snapshot_mismatch"
    if any(timestamp(r["finished_at"]) > timestamp(proof["boundary"]) for r in refs):
        return False, "capture_not_in_recovery_boundary"
    with duckdb.connect(str(snapshot), read_only=True, config={"memory_limit": "16MB", "threads": "1"}) as backup:
        ok, reason = backup_funding_coverage(backup, proof["boundary"], path, refs)
    if file_sha(snapshot) != proof["sha256"]:
        return False, "recovery_changed_during_verification"
    return ok, reason


def backup_funding_coverage(backup, boundary, path, refs):
    for r in refs:
        row = backup.execute("SELECT status,raw_sha256,finished_at,rows_rejected FROM ingestion_runs WHERE run_id=?",
                             [r["run_id"]]).fetchone()
        if (not row or row[0] != "ok" or r["body_sha"] not in json.loads(row[1] or "[]")
                or row[3] != 0 or not row[2] or timestamp(row[2]) > timestamp(boundary)):
            return False, "recovery_ingestion_reference_missing"
    return funding_coverage(backup, path, refs)


def native_recovery(db):
    """Resolve existing nightly verified Research custody; no upload/checkpoint/new backup.

    At an actual expiry boundary only, materialize one existing gzip in temporary scratch,
    verify it byte-for-byte, read it, then discard scratch at CLI exit. Once per pass.
    Lab has no equivalent fresh native receipt and therefore fails closed by default.
    """
    import shutil
    import tempfile

    import duckdb

    key = str(db)
    if key in _RECOVERY_READERS:
        return _RECOVERY_READERS[key]
    if key != "/var/lib/prism-research/import/prism.duckdb" or _RECOVERY_STACK is None:
        raise ValueError("verified recovery adapter unavailable")
    root = Path("/var/lib/prism-research-backup/generations")
    pointer = json.loads((root / "latest.json").read_text())
    name = pointer["generation"]
    if not re.fullmatch(r"generation-[0-9TZ]+", name):
        raise ValueError("unapproved recovery generation")
    generation = root / name
    proof = json.loads((generation / "manifest.json").read_text())
    receipt = json.loads((generation / "offhost-receipt.json").read_text())
    archive = generation / "research.duckdb.gz"
    if any(p.is_symlink() for p in (archive, *archive.parents)):
        raise ValueError("symlinked recovery artifact")
    if (receipt.get("off_host_verified") is not True or receipt.get("restore_verified") is not True
            or receipt.get("sha256") != proof["sha256"] or pointer["sha256"] != proof["sha256"]
            or receipt.get("compressed_sha256") != proof["compressed_sha256"]
            or file_sha(archive) != proof["compressed_sha256"]):
        raise ValueError("native off-host recovery verification missing")
    usage = shutil.disk_usage(tempfile.gettempdir())
    if usage.free - .7 * usage.total - 7000000000 < proof["bytes"]:
        raise ValueError("recovery scratch reserve unavailable")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0]) * 1024
    total = int(memory["MemTotal"].split()[0]) * 1024
    if available < .4 * total + 3000000000 + 1500000000:
        raise ValueError("recovery inspection headroom unavailable")
    scratch = Path(_RECOVERY_STACK.enter_context(tempfile.TemporaryDirectory(prefix="prism-retention-read-")))
    snapshot = scratch / "recovery.duckdb"
    with gzip.open(archive, "rb") as src, snapshot.open("wb") as dest:
        shutil.copyfileobj(src, dest, 1024 * 1024)
    if snapshot.stat().st_size != proof["bytes"] or file_sha(snapshot) != proof["sha256"]:
        raise ValueError("recovery decompression identity mismatch")
    backup = duckdb.connect(str(snapshot), read_only=True, config={"memory_limit": "16MB", "threads": "1", "enable_external_access": "false"})
    _RECOVERY_STACK.callback(backup.close)
    _RECOVERY_READERS[key] = (backup, proof["created_at"])
    return _RECOVERY_READERS[key]


def candidate(con, db, manifest, root, path, now, refs, latest, *, qualify=False, validated=None):
    now = timestamp(now)
    item = dict(path=str(path), family="other", age_days=None, bytes=0, allocated_bytes=0,
                eligible=False, holds=[], blockers=[], ingestion="unknown", normalized="unknown",
                recovery="not_verified", dependency="unknown", provenance="unknown", legacy=True)
    try:
        identity = safe_file(root, path)
        item.update(identity=identity, bytes=identity[2], allocated_bytes=path.stat().st_blocks * 512)
        header = envelope(path)
        family = family_for(path, header)
        captured = timestamp(header["fetched_at"])
        item.update(family=family, identity=identity, bytes=identity[2], allocated_bytes=path.stat().st_blocks * 512,
                    age_days=(now - captured).total_seconds() / 86400,
                    captured_at=captured.isoformat(), legacy=captured < timestamp(manifest["activated_at"]))
        blocked = item["blockers"]
        if captured.date() != datetime.strptime(path.parent.name, "%Y%m%d").date():
            blocked.append("capture_directory_mismatch")
        if captured.date() >= (now.date() - timedelta(days=30)):
            blocked.append("diagnostic_window")
        if item["legacy"]:
            blocked.append("legacy_custody_requires_separate_qualification")
        if validated is False:
            raise ValueError("reviewed dependency frontier changed")
        if validated is None:
            validate_manifest(con, db, manifest)
        item["dependency"] = "reviewed_registration_frontier_unchanged"
        item["holds"] = protections(manifest, dict(authority=manifest["authority"], representation="provider_raw",
                                                  dataset="*", family=family, knowledge_at=captured))
        if item["holds"]:
            blocked.append("explicit_protection")
        contract = manifest.get("families", {}).get(family, {})
        if family != FUNDING or contract.get("state") != "active":
            blocked.append("family_not_enrolled")
        elif captured < timestamp(contract["activated_at"]):
            blocked.append("before_family_cutover")
        current = refs.get(str(path), [])
        if (not current or any(r["status"] != "ok" or not r["finished_at"] or r["rows_rejected"] != 0
                               or not captured <= timestamp(r["finished_at"]) <= now for r in current)):
            blocked.append("unresolved_ingestion_disposition")
        else:
            item["ingestion"] = "terminal_ok_no_rejections"
            item["provenance"] = "run_and_body_hash_retained"
        if any(r["run_id"] in latest for r in current):
            blocked.append("latest_source_observation")
        if family == FUNDING and (qualify or not blocked):
            ok, reason = funding_coverage(con, path, current)
            item["normalized"] = reason
            if not ok:
                blocked.append("unique_or_unreconciled_scientific_evidence")
        if not blocked:
            ok, reason = recovery_coverage(con, db, path, current)
            item["recovery"] = reason
            if not ok:
                blocked.append("recovery_coverage_unknown")
        if not blocked:
            item["sha256"] = file_sha(path)
            item["eligible"] = True
            item["eligible_reason"] = "expired_prospective_enrolled_settled_unheld_recoverable_original"
    except Exception as exc:
        # Deliberately omit arbitrary exception messages: provider payloads can contain credentials.
        item["blockers"].append("unknown_or_invalid_evidence:" + type(exc).__name__)
    return item


def report(con, db, manifest, root, now, *, qualify=False):
    try:
        validate_manifest(con, db, manifest)
        valid = True
    except Exception:
        valid = False
    refs, latest = references(con)
    rows = [candidate(con, db, manifest, root, p, now, refs, latest, qualify=qualify, validated=valid)
            for p in sorted(Path(root).glob("*/*/*.gz"))]
    families = {}
    for row in rows:
        s = families.setdefault(row["family"], dict(files=0, bytes_considered=0, bytes_eligible=0,
                                                    bytes_protected=0, legacy_bytes=0, bytes_expired=0,
                                                    blockers=Counter()))
        s["files"] += 1
        s["bytes_considered"] += row["allocated_bytes"]
        s["bytes_eligible" if row["eligible"] else "bytes_protected"] += row["allocated_bytes"]
        s["legacy_bytes"] += row["allocated_bytes"] if row["legacy"] else 0
        s["blockers"].update(row["blockers"])
    return dict(policy_version=POLICY, activated_at=manifest["activated_at"], at=timestamp(now).isoformat(),
                authority=manifest["authority"], dry_run=True, families=families, candidates=rows)


def qualify_funding(con, root):
    """Read exact recent evidence for each represented entity before enrollment."""
    refs, _latest = references(con)
    samples = {}
    for path, current in sorted(refs.items()):
        p = Path(path)
        if not p.is_relative_to(Path(root).absolute()):
            continue  # Imported legacy receipts may name another authority's private paths.
        if len(current) != 1 or current[0]["dataset"] != "perp_funding_hourly_v2" or not p.is_file():
            continue
        safe_file(root, p)
        if (family_for(p, envelope(p)) == FUNDING and
                (current[0]["entity"] not in samples or str(current[0]["finished_at"]) >
                 str(samples[current[0]["entity"]][1][0]["finished_at"]))):
            samples[current[0]["entity"]] = (p, current)
    if not samples:
        raise ValueError("no settled funding qualification samples")
    result = []
    for entity, (path, current) in sorted(samples.items()):
        ok, reason = funding_coverage(con, path, current)
        result.append(dict(entity=entity, path=str(path), covered=ok, normalized=reason,
                           ingestion=current[0]["status"], body_sha=current[0]["body_sha"]))
    return dict(contract="exact_settled_funding_four_fields_v1", samples=result,
                passed=all(r["covered"] and r["ingestion"] == "ok" for r in result))


def trigger_report(con, db):
    from types import SimpleNamespace
    store = SimpleNamespace(con=con, path=Path(db))
    result = []
    for source, count, oldest, newest, old in con.execute(
        "SELECT source,count(*),min(available_at),max(available_at),count(*) FILTER "
        "(WHERE CAST(available_at AS DATE)<CAST(current_timestamp AS DATE)-30) "
        "FROM paper_nimble_triggers GROUP BY 1"
    ).fetchall():
        inactive = inactive_execution(store, source)
        result.append(dict(source=source, rows=count, oldest=str(oldest), newest=str(newest),
                           safely_inactive=inactive, expiry_candidate_rows=old if inactive else 0,
                           protected_reason="context_timing_consumer" if source == "context" else
                           "diagnostic_window" if inactive else "active_or_unknown_dependency"))
    return result


def baseline(con, db, root):
    sizes = Counter()
    for p in Path(root).glob("*/*/*.gz"):
        try:
            sizes[family_for(p, envelope(p))] += p.lstat().st_blocks * 512
        except Exception:
            sizes["unclassified_protected"] += p.lstat().st_blocks * 512
    tables = ("microstructure_minutes", "microstructure_revisions", "lab_prospective_studies",
              "lab_prospective_checkpoints", "lab_prospective_results", "lab_experiments",
              "paper_nimble_runs", "paper_nimble_accounts", "paper_nimble_positions",
              "paper_nimble_opportunities", "paper_nimble_trigger_receipts", "perp_funding")
    triggers = con.execute("SELECT source,count(*),sum(length(payload)) FROM paper_nimble_triggers GROUP BY 1").fetchall()
    blocks = con.execute("SELECT DISTINCT block_id FROM pragma_storage_info('paper_nimble_triggers') WHERE block_id>=0").fetchall()
    return dict(at=datetime.now(UTC).isoformat(), authority=authority(con), db=str(db),
                db_bytes=Path(db).stat().st_size, provider_allocated_bytes=dict(sizes),
                table_rows={t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables},
                triggers=[dict(source=s, rows=n, logical_payload_bytes=b) for s, n, b in triggers],
                trigger_identified_block_bytes=len(blocks) * 262144,
                microdir_cutover=str(con.execute("SELECT min(first_minute) FROM microstructure_cutover").fetchone()[0]),
                database_counters=con.execute("PRAGMA database_size").fetchall(),
                planning_growth_bytes_day=50000000, unchanged_admission_allowance_bytes_day=400000000)


def journal_rows(db):
    result = {}
    for path in sorted((state_dir(db) / "expiry").glob("*.jsonl")):
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    # An interrupted append can only leave a trailing partial line.
                    continue
                result[r["path"]] = r
    return result


def journal_append(db, value):
    path = state_dir(db) / "expiry" / (datetime.now(UTC).strftime("%Y%m%d") + ".jsonl")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlinked expiry journal")
    path.parent.mkdir(exist_ok=True, mode=0o700)
    # A leading newline separates a prior crash's incomplete tail from this record.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
        f.write("\n" + canonical(value) + "\n")
        f.flush()
        os.fsync(f.fileno())
    sync_dir(path.parent)


def expire(con, db, manifest, root, path, now):
    """Caller holds authority flock. Durable intent precedes unlink; retry reconciles it."""
    if not Path(path).exists():
        old = journal_rows(db).get(str(path))
        if not old:
            raise ValueError("missing source without retention intent")
        if old.get("path") != str(path) or old.get("policy_version") != POLICY or not old.get("sha256"):
            raise ValueError("invalid interrupted retention intent")
        if old.get("state") == "expired":
            return old
        old.update(state="expired", reconciled=True)
        journal_append(db, old)
        return old
    fresh = load_manifest(db)
    if fresh["seal"] != manifest["seal"]:
        raise ValueError("protection changed before expiry")
    refs, latest = references(con)
    item = candidate(con, db, fresh, root, Path(path), now, refs, latest)
    if not item["eligible"]:
        return item
    intent = dict(policy_version=POLICY, path=str(path), sha256=item["sha256"],
                  bytes=item["bytes"], allocated_bytes=item["allocated_bytes"],
                  family=item["family"], captured_at=item["captured_at"],
                  body_sha256=[r["body_sha"] for r in refs[str(path)]],
                  run_ids=[r["run_id"] for r in refs[str(path)]],
                  normalized_evidence="perp_funding: exact values, premium, availability and PIT method",
                  at=timestamp(now).isoformat(), protection_seal=fresh["seal"], state="intent",
                  disposition="ENVELOPE_EXPIRED_COMPACT_EVIDENCE_RETAINED")
    journal_append(db, intent)
    # Recheck protection, registration state and file bytes after committing intent.
    current = load_manifest(db)
    validate_manifest(con, db, current)
    if current["seal"] != fresh["seal"] or safe_file(root, path) != item["identity"] or file_sha(path) != item["sha256"]:
        raise ValueError("identity/protection changed before removal")
    Path(path).unlink()
    sync_dir(Path(path).parent)
    intent["state"] = "expired"
    journal_append(db, intent)
    return intent


@contextmanager
def locked_read(db, wait=120):
    """Existing runtime flock, no lock-file content write and no writable DB connection."""
    import duckdb
    lock = Path(str(db) + ".runtime.lock")
    fd = os.open(lock, os.O_RDONLY)
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= wait:
                    raise TimeoutError("authority runtime busy") from None
                time.sleep(.5)
        con = duckdb.connect(str(db), read_only=True, config={"memory_limit": "64MB", "threads": "1", "enable_external_access": "false"})
        try:
            yield con
        finally:
            con.close()
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "report", "enroll", "apply", "qualify", "baseline", "suppress", "triggers"])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--family", choices=FAMILIES)
    parser.add_argument("--execution-off", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-files", type=int, default=25)
    args = parser.parse_args()
    with locked_read(args.db) as con:
        now = datetime.now(UTC)
        if args.action == "baseline":
            result = baseline(con, args.db, args.raw)
        elif args.action == "triggers":
            result = dict(policy_version=POLICY, triggers=trigger_report(con, args.db))
        elif args.action == "qualify":
            result = qualify_funding(con, args.raw)
        elif args.action == "prepare":
            destination = state_dir(args.db) / "protection.json"
            if destination.exists():
                raise ValueError("existing protection cannot be replaced implicitly")
            manifest = prepare(con, args.db, now, execution_off=args.execution_off)
            commit_manifest(args.db, manifest)
            result = manifest
        else:
            manifest = load_manifest(args.db)
            validate_manifest(con, args.db, manifest)
            if args.action == "suppress":
                for table in OBLIGATIONS:
                    if con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]:
                        raise ValueError("execution/evaluation obligation; suppression withheld")
                manifest["execution_off"] = True
                manifest["seal"] = digest({k: v for k, v in manifest.items() if k != "seal"})
                commit_manifest(args.db, manifest)
                result = dict(policy_version=POLICY, execution_off=True, triggers=trigger_report(con, args.db))
            elif args.action == "enroll":
                if args.family != FUNDING:
                    raise ValueError("family withheld: no implemented sufficient-evidence validator")
                if args.family not in manifest["families"]:
                    qualification = qualify_funding(con, args.raw)
                    if not qualification["passed"]:
                        raise ValueError("funding source coverage qualification failed")
                    manifest["families"][args.family] = dict(state="active", activated_at=now.isoformat(),
                                                           contract="exact_settled_funding_four_fields_v1",
                                                           qualification=qualification)
                    manifest["seal"] = digest({k: v for k, v in manifest.items() if k != "seal"})
                    commit_manifest(args.db, manifest)
                result = manifest
            else:
                result = report(con, args.db, manifest, args.raw, now)
                if args.action == "apply":
                    if args.family != FUNDING or not 0 < args.max_files <= 100:
                        raise ValueError("bounded explicitly enrolled funding-family pass required")
                    expired = []
                    for path, entry in journal_rows(args.db).items():
                        if entry.get("state") == "intent" and not Path(path).exists():
                            expire(con, args.db, manifest, args.raw, Path(path), now)
                    for row in result["candidates"]:
                        if row["eligible"] and row["family"] == args.family:
                            receipt = expire(con, args.db, manifest, args.raw, Path(row["path"]), now)
                            expired.append(receipt)
                            if len(expired) >= args.max_files:
                                break
                    result.update(dry_run=False, expired=expired)
                    result["families"].setdefault(args.family, {})["bytes_expired"] = sum(r.get("allocated_bytes", 0) for r in expired if r.get("state") == "expired")
                    summary = {k: v for k, v in result.items() if k not in ("candidates", "expired")}
                    summary["last_successful_run"] = now.isoformat()
                    write_json(state_dir(args.db) / "last-run.json", summary)
        if args.summary:
            result.pop("candidates", None)
        print(canonical(result))


if __name__ == "__main__":
    try:
        with ExitStack() as _RECOVERY_STACK:
            main()
    except Exception as exc:
        print(canonical(dict(policy_version=POLICY, failure=type(exc).__name__, action="retained")))
        raise SystemExit(1) from None
