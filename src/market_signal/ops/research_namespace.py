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


def generations(root):
    """Published local generations: (latest name, newest off-host-verified name)."""
    root = safe(root)
    pointer = safe(root / "latest.json")
    latest = json.loads(pointer.read_text())["generation"] if pointer.exists() else None
    if latest and (Path(latest).name != latest or not latest.startswith("generation-")):
        raise ValueError("unsafe previous generation")
    verified = sorted(
        p.name for p in root.glob("generation-*") if (p / "offhost-receipt.json").exists()
    )
    return latest, verified[-1] if verified else None


def prune_local(root, *, keep_verified=True):
    """Bounded local retention: latest plus, until it is verified, the last off-host copy."""
    root = safe(root)
    latest, verified = generations(root)
    keep = {latest} | ({verified} if keep_verified and verified else set())
    removed = []
    for path in sorted(root.glob("generation-*")):
        if path.name not in keep:
            shutil.rmtree(safe(path))
            removed.append(path.name)
    if removed:
        sync_directory(root)
    return removed


def backup_sealed(db, root, source, *, keep_verified=False):
    """One published compressed restore point; staging is transient and serialized.

    Requires an explicit expected content SHA and unchanged sealed source. No live
    checkpoint or DB writes. Older generations are removed only after restore proof
    and atomic pointer publication; ``keep_verified`` also retains the newest
    off-host-verified generation until the new one is verified off host. An
    interrupted operation fails closed on retry.
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
        previous, verified = generations(root)
        pointer = safe(root / "latest.json")
        if {p.name for p in root.glob("generation-*")} - {previous, verified}:
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
        prune_local(root, keep_verified=keep_verified)
        return manifest


RESTIC_HOST, RESTIC_TAG = "vps-prism-research", "prism-research-foundation-v1"
REMOTE_SCHEMES = ("rclone:", "s3:", "b2:", "sftp:", "rest:")
# Fixed, credential-free failure labels. Provider stderr may contain tokens/URLs and is
# never echoed; only these classifications are surfaced.
FAILURE_CLASSES = (
    (("invalid_grant", "token expired", "oauth2", "unauthorized", "error 401"), "authentication"),
    (("ratelimitexceeded", "userratelimit", "quotaexceeded", "storagequota", "error 403"), "quota"),
    (("wrong password", "no key found"), "repository key"),
    (("unable to open config", "repository does not exist", "is there a repository"), "missing"),
)


def _classify(stderr):
    text = (stderr or "").lower()
    return next((label for words, label in FAILURE_CLASSES if any(w in text for w in words)), None)


def _tool(name, args, *, timeout=3600, **kwargs):
    """Run restic/rclone; surface only status + a fixed failure class, never stderr."""
    command = [name, "--json", "--no-cache", *args] if name == "restic" else [name, *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        label = _classify(result.stderr)
        raise RuntimeError(
            f"{name} {args[0]} failed (status {result.returncode})"
            + (f": {label} failure" if label else "")
        )
    return result.stdout


def _json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _remote_environment(config):
    if not config.get("approved_independent_destination") or not config.get("dedicated_repository"):
        raise ValueError("independent destination approval/configuration required")
    repository = os.environ.get("RESTIC_REPOSITORY", "")
    if not repository.startswith(REMOTE_SCHEMES):
        raise ValueError("remote repository required")
    if not os.environ.get("RESTIC_PASSWORD_FILE"):
        raise ValueError("protected restic password file required")
    if repository.startswith("rclone:") and not os.environ.get("RCLONE_CONFIG"):
        raise ValueError("protected rclone config required")


def offhost_snapshots():
    output = _tool("restic", ["snapshots", "--host", RESTIC_HOST, "--tag", RESTIC_TAG])
    return json.loads(output or "[]")


def offhost_restore(snapshot, scratch, *, expected=None, keep=False):
    """Recover one snapshot from the off-host repository into a fresh path and verify it.

    Uses only the repository (``--no-cache``): no local generation is consulted. The
    authoritative manifest is the one inside the encrypted, authenticated snapshot;
    ``expected`` additionally pins it to a known manifest. ``keep`` retains the
    materialized DB for read-parity validation; otherwise scratch is removed after proof.
    """
    scratch = safe(scratch)
    if scratch.exists():
        raise ValueError("fresh off-host restore destination required")
    started = time.monotonic()
    try:
        _tool("restic", ["restore", snapshot, "--target", str(scratch)])
        downloaded = time.monotonic() - started
        manifest = json.loads((scratch / "manifest.json").read_text())
        if expected is not None and manifest != expected:
            raise ValueError("off-host manifest mismatch")
        archive = scratch / "research.duckdb.gz"
        if archive.stat().st_size != manifest["compressed_bytes"]:
            raise ValueError("off-host archive size mismatch")
        proof = restore(archive, manifest, scratch / "verified.duckdb")
        result = {
            "snapshot": snapshot,
            "download_seconds": downloaded,
            "materialize_verify_seconds": proof["seconds"],
            "compressed_sha256": manifest["compressed_sha256"],
            "sha256": proof["inspection"]["sha256"],
            "tables": len(proof["inspection"]["tables"]),
            "schema_version": proof["inspection"]["schema_version"],
            "verified": proof["verified"],
            "manifest": manifest,
            "restored_db": str(scratch / "verified.duckdb") if keep else None,
        }
        return result
    finally:
        if not keep and scratch.exists():
            shutil.rmtree(scratch)


def offhost(config, generation, scratch, *, keep_restore=False):
    """Upload one verified generation to an independent restic repository and prove it.

    Order: local checksum -> encrypted upload -> remote size/listing -> repository data
    check -> fresh recovery from the repository with checksum + every-table proof ->
    plaintext manifest copy (byte-verified) -> bounded retention scoped to a dedicated
    repository + host/tag. Never initializes a repository or removes a local copy.
    Credentials come from root-protected environment/config files, never argv/reports.
    """
    _remote_environment(config)
    generation, scratch = safe(generation), safe(scratch)
    manifest = json.loads((generation / "manifest.json").read_text())
    if sha256(generation / "research.duckdb.gz") != manifest["compressed_sha256"]:
        raise ValueError("local archive corrupt")
    if scratch.exists():
        raise ValueError("fresh off-host restore destination required")
    timings, started = {}, time.monotonic()
    output = _tool(
        "restic", ["backup", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, "."], cwd=generation
    )
    summary = next(r for r in _json_lines(output) if r.get("message_type") == "summary")
    snapshot = summary["snapshot_id"]
    timings["upload_seconds"] = time.monotonic() - started

    mark = time.monotonic()
    listing = {
        n["name"]: n["size"]
        for n in _json_lines(_tool("restic", ["ls", snapshot]))
        if n.get("struct_type") == "node" and n.get("type") == "file"
    }
    local_sizes = {p.name: p.stat().st_size for p in generation.iterdir() if p.is_file()}
    if listing != local_sizes or "manifest.json" not in listing:
        raise ValueError("remote listing/size mismatch")
    subset = config.get("read_data_subset")
    _tool("restic", ["check", f"--read-data-subset={subset}" if subset else "--read-data"])
    timings["remote_check_seconds"] = time.monotonic() - mark

    mark = time.monotonic()
    recovered = offhost_restore(snapshot, scratch, expected=manifest, keep=keep_restore)
    timings["restore_seconds"] = time.monotonic() - mark

    remote_manifests = config.get("manifest_remote")
    manifest_copy = None
    if remote_manifests:
        target = f"{remote_manifests}/{generation.name}.json"
        _tool("rclone", ["copyto", str(generation / "manifest.json"), target])
        echoed = _tool("rclone", ["cat", target]).encode()
        if hashlib.sha256(echoed).hexdigest() != sha256(generation / "manifest.json"):
            raise ValueError("remote manifest copy mismatch")
        manifest_copy = target

    mark = time.monotonic()
    retention = config.get("retention", {"daily": 7, "weekly": 4, "monthly": 3})
    forget = ["forget", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, "--group-by", "host,tags"]
    for unit in ("daily", "weekly", "monthly"):
        forget += [f"--keep-{unit}", str(int(retention[unit]))]
    _tool("restic", [*forget, "--prune"])
    retained = offhost_snapshots()
    # Never let retention remove the generation just proven recoverable.
    if snapshot not in {s["id"] for s in retained}:
        raise RuntimeError("retention removed the newly verified snapshot")
    pruned_manifests = []
    if remote_manifests:
        keep = {Path(p).name for s in retained for p in s.get("paths", [])}
        for name in _tool("rclone", ["lsf", "--files-only", remote_manifests]).split():
            generation_name = name.removesuffix(".json")
            if (
                name.startswith("generation-")
                and name.endswith(".json")
                and generation_name not in keep
            ):
                _tool("rclone", ["deletefile", f"{remote_manifests}/{name}"])
                pruned_manifests.append(name)
    timings["retention_seconds"] = time.monotonic() - mark

    receipt = {
        "snapshot_id": snapshot,
        "generation": generation.name,
        "verified_at": datetime.now(UTC).isoformat(),
        "sha256": manifest["sha256"],
        "compressed_sha256": manifest["compressed_sha256"],
        "compressed_bytes": manifest["compressed_bytes"],
        "remote_listing": listing,
        "data_added_packed": summary.get("data_added_packed"),
        "read_data": f"subset {subset}" if subset else "full",
        "restore_verified": recovered["verified"],
        "restored_db": recovered["restored_db"],
        "manifest_copy": manifest_copy,
        "retained_snapshots": len(retained),
        "pruned_manifests": pruned_manifests,
        "timings": timings,
        "off_host_verified": True,
        "destination_id": config["destination_id"],
    }
    write_json(generation / "offhost-receipt.json", receipt)
    return receipt


def scheduled(config, source):
    """Nightly pipeline: sealed backup -> off-host proof -> local prune. Fails loudly.

    The previous off-host-verified local generation survives until the new generation
    is verified off host. The status file names the failing stage, never credentials.
    """
    root, status = safe(config["local_root"]), safe(config["status_path"])
    stage, started = "backup", datetime.now(UTC).isoformat()
    try:
        backup_sealed(safe(config["db"]), root, source, keep_verified=True)
        latest, _ = generations(root)
        stage = "offhost"
        scratch = safe(config["scratch_root"]) / f"restore-{latest}"
        receipt = offhost(config, root / latest, scratch)
        stage = "local-prune"
        removed = prune_local(root, keep_verified=False)
    except Exception as exc:
        write_json(
            status,
            {
                "status": "failed",
                "stage": stage,
                "error": str(exc)[:300],
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    result = {
        "status": "ok",
        "stage": "done",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "receipt": receipt,
        "removed_local": removed,
    }
    write_json(status, result)
    return result
