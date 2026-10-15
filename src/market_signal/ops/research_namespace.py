"""Offline migration verification and bounded recovery; never starts a Prism runtime.

All DB reads use DuckDB read_only=True, never Store. Local restore points are for
sealed, WAL-free candidates only. Future live backup must use the owner's existing
runtime lock/checkpoint protocol before calling this module on the sealed result.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from market_signal.ops.backups import sync_directory, write_json


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(2**20), b""):
            h.update(block)
    return h.hexdigest()


def safe(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("path traversal")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink recovery path")
    return path


def connect(path):
    return duckdb.connect(
        str(path), read_only=True, config={"threads": "1", "memory_limit": "256MB"}
    )


def inspect(path):
    path = safe(path)
    before = sha256(path)
    tables = {}
    with connect(path) as con:
        schema = con.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        for (table,) in con.execute("SHOW TABLES").fetchall():
            quoted = '"' + table.replace('"', '""') + '"'
            columns = con.execute(f"PRAGMA table_info({quoted})").fetchall()
            args = ",".join('"' + c[1].replace('"', '""') + '"' for c in columns)
            n, value_hash = con.execute(
                f"SELECT count(*),bit_xor(hash({args})) FROM {quoted}"
            ).fetchone()
            tables[table] = {"rows": n, "value_hash": value_hash}
        authority = con.execute(
            "SELECT runtime_id,recorded_at FROM runtime_events WHERE event_type='authority_claimed' "
            "ORDER BY recorded_at DESC,event_id DESC LIMIT 1"
        ).fetchone()
        frontier = con.execute("SELECT max(finished_at) FROM runtime_cycles").fetchone()[0]
    if sha256(path) != before:
        raise ValueError("candidate changed during inspection")
    return {
        "sha256": before,
        "bytes": path.stat().st_size,
        "schema_version": schema,
        "duckdb_version": duckdb.__version__,
        "tables": tables,
        "authority": list(authority) if authority else None,
        "last_finished_cycle": frontier,
    }


def compare_tables(expected, actual):
    return [
        {"table": t, "expected": expected.get(t), "actual": actual.get(t), "classification": "BUG"}
        for t in sorted(expected.keys() | actual.keys())
        if expected.get(t) != actual.get(t)
    ]


def restore(archive, manifest, destination):
    """Bounded, exclusive scratch restore; verify content before atomic publication."""
    archive, destination = safe(archive), safe(destination)
    if destination.exists() or sha256(archive) != manifest["compressed_sha256"]:
        raise ValueError("existing destination or archive checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    safe(partial)
    if partial.exists():
        raise ValueError("existing partial restore requires inspection")
    started = time.monotonic()
    try:
        total = 0
        with gzip.open(archive, "rb") as src, partial.open("xb") as dst:
            for block in iter(lambda: src.read(2**20), b""):
                total += len(block)
                if total > manifest["bytes"]:
                    raise ValueError("restore exceeds declared size")
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        actual = inspect(partial)
        if (
            actual["sha256"] != manifest["sha256"]
            or actual["bytes"] != manifest["bytes"]
            or compare_tables(manifest["tables"], actual["tables"])
        ):
            raise ValueError("restore fingerprint mismatch")
        os.link(partial, destination)  # fail rather than overwrite even with a competing restore
        sync_directory(destination.parent)
        return {"seconds": time.monotonic() - started, "verified": True, "inspection": actual}
    finally:
        partial.unlink(missing_ok=True)


def backup_sealed(db, root, source):
    """One published compressed restore point; staging is transient and serialized.

    Requires an explicit expected content SHA and unchanged sealed source. No live
    checkpoint or DB writes. Previous generation is removed only after restore proof
    and atomic pointer publication. An interrupted operation fails closed on retry.
    """
    db, root = safe(db), safe(root)
    if root == db.parent or root in db.parents:
        raise ValueError("backup root must be separate from candidate")
    root.mkdir(parents=True, exist_ok=True)
    with safe(root / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = list(root.glob(".pending-*"))
        if pending:
            raise ValueError("interrupted backup staging requires inspection")
        wal = db.with_name(db.name + ".wal")
        if wal.exists() and wal.stat().st_size:
            raise ValueError("sealed candidate has WAL")
        previous = None
        pointer = safe(root / "latest.json")
        if pointer.exists():
            previous = json.loads(pointer.read_text())["generation"]
            if Path(previous).name != previous or not previous.startswith("generation-"):
                raise ValueError("unsafe previous generation")
            safe(root / previous)
        if {p.name for p in root.glob("generation-*")} - ({previous} if previous else set()):
            raise ValueError("unpublished generation requires inspection")
        started = time.monotonic()
        before = inspect(db)
        if before["sha256"] != source["sha256"]:
            raise ValueError("source checksum mismatch")
        if shutil.disk_usage(root).free < before["bytes"] * 3 + 2**30:
            raise ValueError("backup/restore staging capacity floor")
        with tempfile.TemporaryDirectory(dir=root, prefix=".pending-") as tmp:
            stage = Path(tmp)
            archive = stage / "research.duckdb.gz"
            with db.open("rb") as src, archive.open("xb") as raw:
                with gzip.GzipFile(
                    filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
                ) as dst:
                    shutil.copyfileobj(src, dst, 2**20)
                raw.flush()
                os.fsync(raw.fileno())
            if sha256(db) != before["sha256"]:
                raise ValueError("source changed while backing up")
            manifest = {
                **before,
                "authority": json.loads(json.dumps(before["authority"], default=str)),
                "last_finished_cycle": str(before["last_finished_cycle"]),
                "compressed_sha256": sha256(archive),
                "compressed_bytes": archive.stat().st_size,
                "source": source,
                "created_at": datetime.now(UTC).isoformat(),
                "mode": "non-authoritative-sealed-candidate",
                "off_host_verified": False,
                "lock_seconds": 0,
                "lock_scope": "backup files only; no live DB owner lock",
                "backup_seconds": time.monotonic() - started,
            }
            proof = restore(archive, manifest, stage / "restore.duckdb")
            (stage / "restore.duckdb").unlink()
            manifest["restore_seconds"] = proof["seconds"]
            write_json(stage / "manifest.json", manifest)
            generation = "generation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            os.rename(stage, root / generation)
            sync_directory(root)
            write_json(pointer, {"generation": generation, "sha256": before["sha256"]})
        if previous:
            shutil.rmtree(root / previous)
            sync_directory(root)
        return manifest


def offhost(config, generation, scratch):
    """Explicitly configured independent restic repository; never initializes one.

    Credentials are inherited from a root-protected environment file, never config,
    argv or reports. Retention is scoped to a dedicated repository + host/tag, after
    checksum and every-table restore proof. No local copy is removed by this workflow.
    """
    if not config.get("approved_independent_destination") or not config.get("dedicated_repository"):
        raise ValueError("independent destination approval/configuration required")
    repository = os.environ.get("RESTIC_REPOSITORY", "")
    if not repository.startswith(("s3:", "b2:", "sftp:", "rest:")):
        raise ValueError("remote repository required")
    if not os.environ.get("RESTIC_PASSWORD_FILE"):
        raise ValueError("protected restic password file required")
    generation, scratch = safe(generation), safe(scratch)
    manifest = json.loads((generation / "manifest.json").read_text())
    if sha256(generation / "research.duckdb.gz") != manifest["compressed_sha256"]:
        raise ValueError("local archive corrupt")
    if scratch.exists():
        raise ValueError("fresh off-host restore destination required")
    host, tag = "vps-prism-research", "prism-research-foundation-v1"

    def run(args, **kwargs):
        result = subprocess.run(
            ["restic", "--json", *args], capture_output=True, text=True, timeout=1800, **kwargs
        )
        if result.returncode:
            # Do not print provider diagnostics; they may include credentials/URLs.
            raise RuntimeError(f"restic {args[0]} failed (status {result.returncode})")
        return result.stdout

    output = run(["backup", "--host", host, "--tag", tag, "."], cwd=generation)
    summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
    snapshot = next(r["snapshot_id"] for r in summaries if r.get("message_type") == "summary")
    run(["check", "--read-data"])
    run(["restore", snapshot, "--target", str(scratch)])
    recovered = json.loads((scratch / "manifest.json").read_text())
    if recovered != manifest:
        raise ValueError("off-host manifest mismatch")
    proof = restore(scratch / "research.duckdb.gz", manifest, scratch / "verified.duckdb")
    # Only an explicitly dedicated repository is safe for retention/prune.
    if config.get("dedicated_repository") is not True:
        raise ValueError("dedicated repository required before retention")
    run(
        [
            "forget",
            "--host",
            host,
            "--tag",
            tag,
            "--group-by",
            "host,tags",
            "--keep-daily",
            "7",
            "--keep-weekly",
            "4",
            "--keep-monthly",
            "3",
            "--prune",
        ]
    )
    receipt = {
        "snapshot_id": snapshot,
        "verified_at": datetime.now(UTC).isoformat(),
        "sha256": manifest["sha256"],
        "restore_verified": proof["verified"],
        "off_host_verified": True,
        "destination_id": config["destination_id"],
    }
    write_json(generation / "offhost-receipt.json", receipt)
    return receipt
