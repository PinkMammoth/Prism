"""Lossless raw archive verification and conservative, operator-only deletion.

Provider originals are historical evidence, never disposable. Microstructure files
can be exported and read-tested but this v1 does not authorise their deletion.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_signal.ops import backups

PROVIDER_NAME = re.compile(r"(?:run|ctxrun)_[a-zA-Z0-9_]+\.json(?:l)?\.gz\Z")
DAY = re.compile(r"\d{8}\Z")
HOUR = re.compile(r"\d{2}\.jsonl(?:\.gz)?\Z")
VERSION = 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(2**20):
            h.update(chunk)
    return h.hexdigest()


def identity(path: Path) -> dict:
    s = path.stat()
    return {
        "device": s.st_dev,
        "inode": s.st_ino,
        "size": s.st_size,
        "mtime_ns": s.st_mtime_ns,
        "ctime_ns": s.st_ctime_ns,
    }


def safe_source(root: Path, path: Path, archive_type: str) -> Path:
    root, path = backups.safe_location(root), backups.safe_location(path)
    rel = path.relative_to(root)
    if archive_type == "provider-raw":
        valid = (
            len(rel.parts) == 3
            and re.fullmatch(r"[a-zA-Z0-9_-]+", rel.parts[0])
            and DAY.fullmatch(rel.parts[1])
            and PROVIDER_NAME.fullmatch(rel.parts[2])
        )
    elif archive_type in {"microstructure-raw", "microstructure-spool"}:
        valid = (
            len(rel.parts) == 2
            and DAY.fullmatch(rel.parts[0])
            and HOUR.fullmatch(rel.parts[1])
            and int(rel.parts[1][:2]) < 24
        )
        valid = valid and path.name.endswith(".gz") == (archive_type == "microstructure-raw")
    elif archive_type == "large-print-calibration":
        valid = len(rel.parts) == 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.name)
    else:
        raise ValueError("unsupported archive type")
    if not valid or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("not an approved independent raw file")
    return path


def read_test(path: Path, archive_type: str) -> dict:
    """Strict decompression/parse plus existing RawPayload/Spool/replay readers.

    No tolerant damaged-gzip acceptance. Replay capability is checked, without
    claiming that an isolated hourly journal can recreate a complete DB state.
    """
    opener = gzip.open if path.name.endswith(".gz") else Path.open
    with opener(path, "rb") as f:
        body = f.read()
    if not body or (
        not body.endswith(b"\n") and archive_type not in {"provider-raw", "large-print-calibration"}
    ):
        raise ValueError("empty or incomplete archive")
    times, payload_hashes, providers = [], [], set()
    if archive_type == "provider-raw":
        from market_signal.data.http import RawPayload

        first, separator, rest = body.partition(b"\n")
        if not separator:
            raise ValueError("provider envelope missing")
        header = json.loads(first)
        if "envelope" in header:
            pairs = [
                (r["envelope"], r["body"].encode())
                for r in (json.loads(line) for line in body.splitlines())
            ]
            format_name = "provider-response-bundle-v1"
        else:
            pairs = [(header, rest)]
            format_name = "provider-envelope-v1"
        for envelope, payload in pairs:
            digest = hashlib.sha256(payload).hexdigest()
            if digest != envelope["sha256"]:
                raise ValueError("provider response checksum mismatch")
            if envelope["provider"].startswith("rss_"):
                from market_signal.context.providers.rss import parse_feed

                parse_feed(payload)
            else:
                json.loads(payload)
            timestamp = datetime.fromisoformat(envelope["fetched_at"])
            if timestamp.tzinfo is None:
                raise ValueError("unqualified causal timestamp")
            restored = RawPayload(
                envelope["provider"],
                envelope["method"],
                envelope["url"],
                envelope["params"],
                envelope["status"],
                payload,
                timestamp,
            )
            payload_hashes.append(digest)
            providers.add(restored.provider)
            times.append(timestamp.astimezone(UTC).isoformat())
        count = len(pairs)
        reader = "existing RawPayload; JSON/RSS bodies parsed with existing readers; all body hashes verified"
    elif archive_type == "microstructure-raw":
        from market_signal.microstructure.engine import replay
        from market_signal.microstructure.spool import Spool

        events = [tuple(json.loads(line)) for line in body.splitlines()]
        if Spool.read_raw(path) != events:
            raise ValueError("existing raw reader differs from strict parse")
        if any(len(e) < 2 or not isinstance(e[1], (int, float)) for e in events):
            raise ValueError("unsupported event format")
        times = [datetime.fromtimestamp(e[1] / 1000, UTC).isoformat() for e in events]
        coins = tuple(
            sorted(
                {
                    e[2]
                    for e in events
                    if e[0] in {"T", "F", "D", "C", "A"} and isinstance(e[2], str)
                }
            )
        )
        # Closed hourly journals often have no collector start event. Supply a
        # fresh start for a bounded read exercise; this is not historical state.
        seeded = not any(e[0] == "S" for e in events)
        exercise = [("S", events[0][1], {}), *events] if seeded else events
        output = replay(exercise, coins, "off-host-read-test")
        count = len(events)
        reader = f"Spool.read_raw and Engine.replay; {len(output)} emitted records; seed supplied={seeded}; hour-only replay is not full-state equivalence"
        format_name = "microstructure-normalized-event-jsonl-v1"
    elif archive_type == "microstructure-spool":
        from market_signal.microstructure.spool import Spool

        records, offset, bad = Spool.read_lines(path)
        if (
            bad
            or offset != len(body)
            or not all(isinstance(r, dict) and r.get("kind") for r in records)
        ):
            raise ValueError("invalid or incomplete spool records")
        count = len(records)
        timestamps = [
            r[k]
            for r in records
            for k in ("minute_open", "at", "available_at")
            if isinstance(r.get(k), int)
        ]
        times = [datetime.fromtimestamp(t / 1000, UTC).isoformat() for t in timestamps]
        reader, format_name = (
            "Spool.read_lines; all complete records",
            "microstructure-spool-jsonl-v1",
        )
    elif archive_type == "large-print-calibration":
        from market_signal.microstructure.engine import LargePrints

        data = json.loads(body)
        day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        if not isinstance(data, dict) or any(
            not isinstance(v, dict) or "bins" not in v or "minutes" not in v for v in data.values()
        ):
            raise ValueError("invalid calibration histogram")
        restored = LargePrints({path.stem: data})
        for coin in data:
            restored.threshold(coin, (day + timedelta(days=1)).strftime("%Y-%m-%d"))
        times = [day.isoformat()]
        count = len(data)
        reader, format_name = (
            "LargePrints.threshold; complete day histogram",
            "large-print-day-histogram-v1",
        )
    else:
        raise ValueError("unsupported archive type")
    return {
        "schema_format_version": VERSION,
        "format": format_name,
        "first_timestamp": min(times) if times else None,
        "last_timestamp": max(times) if times else None,
        "record_count": count,
        "logical_size": len(body),
        "logical_sha256": hashlib.sha256(body).hexdigest(),
        "payload_sha256": payload_hashes,
        "providers": sorted(providers),
        "reader": reader,
        "verification_status": "passed",
    }


def provider_eligibility(path: Path, root: Path, context: dict, parsed: dict) -> dict:
    """Require successful terminal runs in a verified recovery snapshot.

    Keep latest successful run per (provider,dataset,entity). Preserve unmatched,
    failed/rejected, active and unsettled/dependency-referenced evidence on Railway.
    """
    path = safe_source(root, path, "provider-raw")
    refs = context.get("references", {}).get(str(path), [])
    reasons = []
    if not context.get("verified_backup_sha256") or not context.get("snapshot_timestamp"):
        reasons.append("verified recovery boundary required")
    if not refs or sorted(r["sha256"] for r in refs) != sorted(parsed["payload_sha256"]):
        reasons.append("exact complete ingestion references/hash match required")
    if any(
        r.get("status") != "ok" or not r.get("finished_at") or r.get("rows_rejected", 0)
        for r in refs
    ):
        reasons.append("successful settled ingest without rejected rows required")
    if any(r["run_id"] in context.get("latest_run_ids", []) for r in refs):
        reasons.append("latest per-source/dataset/entity recovery observation protected")
    if str(path) in context.get("pending_paths", []) or str(path) in context.get("open_paths", []):
        reasons.append("pending or active source protected")
    if not context.get("dependency_review") or str(path) in context.get("dependency_paths", []):
        reasons.append("explicit replay/research dependency clearance required")
    if not context.get("active_file_scan_complete"):
        reasons.append("complete active-file scan required")
    boundary = context.get("snapshot_timestamp")
    if boundary and datetime.fromisoformat(parsed["last_timestamp"]) > datetime.fromisoformat(
        boundary
    ):
        reasons.append("capture exceeds committed recovery boundary")
    if boundary and any(
        r.get("finished_at")
        and datetime.fromisoformat(r["finished_at"]) > datetime.fromisoformat(boundary)
        for r in refs
    ):
        reasons.append("ingest completion exceeds committed recovery boundary")
    return {
        "class": "A" if reasons else "B",
        "export_eligible": not reasons,
        "deletion_eligible_after_verified_export": not reasons,
        "reasons": reasons,
        "ingest_revision_boundary": {
            "backup_sha256": context.get("verified_backup_sha256"),
            "snapshot_timestamp": context.get("snapshot_timestamp"),
            "references": refs,
            "dependency_review": context.get("dependency_review"),
        },
    }


def export_file(source: Path, destination: Path, manifest: dict) -> dict:
    """Commit an off-host artifact and machine-readable verification receipt atomically."""
    source, destination = backups.safe_location(source), backups.safe_location(destination)
    if destination.exists() or destination.with_name(destination.name + ".manifest.json").exists():
        raise ValueError("destination already exists")
    if sha256(source) != manifest["sha256"] or source.stat().st_size != manifest["source_size"]:
        raise ValueError("source checksum/size mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as d:
        staged = Path(d) / source.name
        shutil.copyfile(source, staged)
        result = read_test(staged, manifest["archive_type"])
        if sha256(staged) != manifest["sha256"] or result != manifest["read_test"]:
            raise ValueError("restore/read verification mismatch")
        receipt = {
            **manifest,
            "export_time": datetime.now(UTC).isoformat(),
            "archive_path": str(destination),
            "compressed_size": staged.stat().st_size,
            "artifact_sha256": sha256(staged),
            "off_host_verified": True,
            "verification_status": "passed",
            "restore_read_test": result,
        }
        with staged.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(staged, destination)
        backups.sync_directory(destination.parent)
        backups.write_json(destination.with_name(destination.name + ".manifest.json"), receipt)
    return receipt


def delete_verified(
    root: Path, path: Path, receipt: dict, context: dict, logpath: Path, *, apply: bool = False
) -> dict:
    """Provider-only, exact-checksum deletion. Caller holds the runtime writer lock."""
    if receipt.get("archive_type") != "provider-raw":
        raise ValueError("microstructure raw and spool deletion protected in v1")
    # A Phase 1 enrollment must not be bypassed by the older operator-only tool.
    # Legacy custody remains intact; a new reviewed pass must reconcile its holds.
    if (Path(root).parent / "retention-phase1").exists():
        raise ValueError("Phase 1 protection active; legacy expiry requires separate qualification")
    path = safe_source(root, path, "provider-raw")
    logpath = backups.safe_location(logpath)
    if logpath == path or logpath.is_relative_to(root):
        raise ValueError("deletion audit must be outside source archive directories")
    if str(path) != receipt.get("source_path") or identity(path) != receipt.get("source_identity"):
        raise ValueError("source path/identity mismatch")
    if not (
        receipt.get("off_host_verified") is True
        and receipt.get("verification_status") == "passed"
        and receipt.get("artifact_sha256") == receipt.get("sha256")
        and receipt.get("restore_read_test") == receipt.get("read_test")
    ):
        raise ValueError("verified off-host restore receipt required")
    if sha256(path) != receipt["sha256"] or identity(path) != receipt["source_identity"]:
        raise ValueError("source changed; deletion stopped")
    eligible = provider_eligibility(path, root, context, receipt["read_test"])
    if not eligible["deletion_eligible_after_verified_export"]:
        raise ValueError("eligibility changed: " + "; ".join(eligible["reasons"]))
    entry = {
        "path": str(path),
        "sha256": receipt["sha256"],
        "bytes": path.stat().st_size,
        "archive_path": receipt["archive_path"],
        "at": datetime.now(UTC).isoformat(),
        "dry_run": not apply,
        "ingest_revision_boundary": eligible["ingest_revision_boundary"],
    }
    if apply:
        logpath.parent.mkdir(parents=True, exist_ok=True)
        with logpath.open("a") as log:
            log.write(json.dumps({**entry, "action": "delete_intent"}) + "\n")
            log.flush()
            os.fsync(log.fileno())
            # Recheck after opening the log and immediately before unlink.
            safe_source(root, path, "provider-raw")
            if identity(path) != receipt["source_identity"]:
                raise ValueError("source changed before deletion")
            path.unlink()
            backups.sync_directory(path.parent)
            log.write(json.dumps({**entry, "action": "deleted"}) + "\n")
            log.flush()
            os.fsync(log.fileno())
    return entry


def recovery_context(db: Path, dependency_review: str = "") -> dict:
    """Read an immutable verified full backup; verify its identity around the read.

    Deletion callers hold the runtime writer lock. Listing needs no live DB access.
    """
    import duckdb

    from market_signal.ops import runtime as rt

    rows = backups.inventory(rt.backup_dir(db), db)
    verified = [r for r in rows if r["manifest"].get("verification_result")]
    if not verified:
        raise ValueError("no verified recovery backup")
    row = verified[-1]
    path, manifest = Path(row["path"]), row["manifest"]
    before = identity(path)
    if sha256(path) != manifest["sha256"]:
        raise ValueError("recovery backup checksum mismatch")
    refs, latest, pending = {}, {}, []
    with duckdb.connect(str(path), read_only=True) as con:
        runs = con.execute(
            "SELECT run_id,provider,dataset,entity,status,finished_at,rows_rejected,raw_paths,raw_sha256 FROM ingestion_runs"
        ).fetchall()
        offsets = dict(
            con.execute("SELECT path,bytes FROM microstructure_ingest_offsets").fetchall()
        )
        minutes = con.execute(
            "SELECT count(*),max(minute_open) FROM microstructure_minutes"
        ).fetchone()
        contexts = con.execute(
            "SELECT run_id,provider,status,finished_at,rejected,payload FROM context_provider_runs"
        ).fetchall()
    if identity(path) != before:
        raise ValueError("recovery snapshot changed during boundary read")
    for run_id, provider, dataset, entity, status, finished, rejected, paths, hashes in runs:
        paths, hashes = json.loads(paths or "[]"), json.loads(hashes or "[]")
        if len(paths) != len(hashes):
            continue  # fail closed: no inferred reference for incomplete provenance
        key = (provider, dataset, entity)
        if status == "ok" and finished and (key not in latest or finished > latest[key][0]):
            latest[key] = (finished, run_id)
        for source, digest in zip(paths, hashes, strict=True):
            filename, _, fragment = source.partition("#")
            ref = {
                "run_id": run_id,
                "provider": provider,
                "dataset": dataset,
                "entity": entity,
                "status": status,
                "finished_at": str(finished) if finished else None,
                "rows_rejected": rejected,
                "fragment": fragment,
                "sha256": digest,
            }
            refs.setdefault(filename, []).append(ref)
            if status == "running":
                pending.append(filename)
    for run_id, provider, status, finished, rejected, payload in contexts:
        payload = json.loads(payload)
        key = (provider, "context_provider", None)
        if status == "ok" and finished and (key not in latest or finished > latest[key][0]):
            latest[key] = (finished, run_id)
        for record in payload.get("raw", []):
            source, digest = record["path"], record["sha256"]
            filename, _, fragment = source.partition("#")
            refs.setdefault(filename, []).append(
                {
                    "run_id": run_id,
                    "provider": provider,
                    "dataset": "context_provider",
                    "entity": None,
                    "status": status,
                    "finished_at": str(finished) if finished else None,
                    "rows_rejected": rejected,
                    "fragment": fragment,
                    "sha256": digest,
                    "reference_table": "context_provider_runs.payload.raw",
                }
            )
    open_paths, scan_complete = set(), True
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                    if target.startswith(("/data/raw/", "/data/microstructure/")):
                        open_paths.add(target)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        except PermissionError:
            scan_complete = False
    return {
        "verified_backup_path": str(path),
        "verified_backup_sha256": manifest["sha256"],
        "snapshot_timestamp": manifest["backup_timestamp"],
        "references": refs,
        "latest_run_ids": [r[1] for r in latest.values()],
        "pending_paths": pending,
        "open_paths": sorted(open_paths),
        "active_file_scan_complete": scan_complete,
        "dependency_review": dependency_review,
        "dependency_paths": [],
        "spool_offsets": offsets,
        "microstructure_minutes": minutes[0],
        "last_microstructure_minute": minutes[1],
    }


def make_manifest(root: Path, path: Path, archive_type: str, context: dict) -> dict:
    path = safe_source(root, path, archive_type)
    before = identity(path)
    parsed = read_test(path, archive_type)
    digest = sha256(path)
    if identity(path) != before:
        raise ValueError("active/changing source file")
    if archive_type == "provider-raw":
        eligibility = provider_eligibility(path, root, context, parsed)
        source = path.relative_to(root).parts[0]
    else:
        if archive_type == "large-print-calibration":
            hour = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            hour = datetime.strptime(path.parent.name + path.name[:2], "%Y%m%d%H").replace(
                tzinfo=UTC
            )
        now = datetime.now(UTC)
        closed = hour < now.replace(minute=0, second=0, microsecond=0) and str(
            path
        ) not in context.get("open_paths", [])
        if archive_type == "large-print-calibration":
            closed = closed and hour.date() < now.date()
        eligibility = {
            "class": "A",
            "export_eligible": closed and context.get("active_file_scan_complete", False),
            "deletion_eligible_after_verified_export": False,
            "reasons": [
                "v1 preserves Railway microstructure restart/revision/calibration working set"
            ],
            "ingest_revision_boundary": {
                "backup_sha256": context.get("verified_backup_sha256"),
                "snapshot_timestamp": context.get("snapshot_timestamp"),
                "committed_spool_bytes": context.get("spool_offsets", {}).get(str(path)),
                "last_microstructure_minute": context.get("last_microstructure_minute"),
            },
        }
        source = "hyperliquid"
    return {
        "manifest_version": VERSION,
        "source_path": str(path),
        "archive_type": archive_type,
        "source_provider": source,
        "source_size": before["size"],
        "sha256": digest,
        "source_identity": before,
        "first_timestamp": parsed["first_timestamp"],
        "last_timestamp": parsed["last_timestamp"],
        "schema_format_version": parsed["schema_format_version"],
        "read_test": parsed,
        "eligibility": eligibility,
        "ingest_revision_boundary": eligibility["ingest_revision_boundary"],
        "deletion_eligibility": eligibility["deletion_eligible_after_verified_export"],
    }
