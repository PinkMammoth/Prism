"""Physical backup lifecycle. No trading writes; deletion requires an off-host receipt."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

NAME = re.compile(r"prism-(\d{8}T\d{6}Z)(?:-[\w-]+)?\.duckdb\Z")
KINDS = {"daily", "manual", "weekly"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".manifest.json")


def safe_location(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("path traversal")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink path")
    return path


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict) -> None:
    # Tiny metadata, committed only after verification succeeds.
    path = safe_location(path)
    if path.is_symlink():
        raise ValueError("symlink manifest")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp = Path(f.name)
    os.replace(tmp, path)
    sync_directory(path.parent)


def safe_path(root: Path, path: Path, active_db: Path) -> Path:
    root, path, active_db = root.absolute(), path.absolute(), active_db.absolute()
    if ".." in path.parts or ".." in root.parts:
        raise ValueError("path traversal")
    if any(p.is_symlink() for p in (root, *root.parents, path, *path.parents)):
        raise ValueError("symlink backup path")
    relative = path.relative_to(root)
    if len(relative.parts) != 2 or relative.parts[0] not in KINDS or not NAME.fullmatch(path.name):
        raise ValueError("not an approved backup path")
    if path.resolve() == active_db.resolve() or (
        active_db.exists() and os.path.samefile(path, active_db)
    ):
        raise ValueError("active database protected")
    if not path.is_file():
        raise ValueError("backup missing")
    if sidecar(path).is_symlink():
        raise ValueError("symlink manifest")
    return path


def inspect_backup(path: Path) -> dict:
    """Read every stored table on a copy, including all ledger/context/universe tables.

    Accept historical schemas without migrating evidence. Unknown future schemas fail.
    """
    from market_signal.data.store import MIGRATIONS, authority_claim
    from market_signal.ops import runtime as rt

    before = rt._sha256(path)
    with duckdb.connect(str(path), read_only=True) as con:
        version = con.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if not isinstance(version, int) or not 1 <= version <= len(MIGRATIONS):
            raise ValueError("unsupported schema version")
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        required = {
            name
            for ddl in MIGRATIONS[:version]
            for name in re.findall(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)", ddl, re.IGNORECASE
            )
        }
        missing = required - set(tables)
        if missing:
            raise ValueError(f"schema {version} missing tables: {sorted(missing)}")
        counts, fingerprints = {}, {}
        runs, policies = {}, {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            # Hash every stored value, rather than using a metadata-only count.
            names = [r[1] for r in con.execute(f"PRAGMA table_info({quoted})").fetchall()]
            args = ",".join('"' + n.replace('"', '""') + '"' for n in names)
            count, digest = con.execute(
                f"SELECT count(*), bit_xor(hash({args})) FROM {quoted}"
            ).fetchone()
            counts[table] = count
            fingerprints[table] = {"rows": count, "hash": str(digest)}
            columns = {r[1] for r in con.execute(f"PRAGMA table_info({quoted})").fetchall()}
            if table.startswith("paper") and table.endswith("runs") and "run_id" in columns:
                runs[table] = [r[0] for r in con.execute(f"SELECT run_id FROM {quoted}").fetchall()]
            if table.endswith("policies") and "policy_id" in columns:
                policies[table] = [
                    r[0] for r in con.execute(f"SELECT policy_id FROM {quoted}").fetchall()
                ]
        claim = authority_claim(con)
        # Data-only Lab snapshots deliberately have no paper run. Identify them from
        # persisted provenance, never from the verifier's shell/profile environment.
        lab_seeded = (
            "runtime_events" in tables
            and con.execute(
                "SELECT 1 FROM runtime_events WHERE event_type='lab_seeded' LIMIT 1"
            ).fetchone()
            is not None
        )
        lab_snapshot = (
            version >= 31
            and lab_seeded
            and claim is not None
            and claim[0] in {"vps-prism-autotrader-lab-rehearsal", "vps-prism-autotrader-lab"}
        )
        if not any(runs.values()) and not lab_snapshot:
            raise ValueError("no run metadata")
        if "paper_events" not in tables:
            raise ValueError("paper ledger missing")
        revision = None
        frontiers = {}
        for table, column in (
            ("perp_intraday_bars", "close_time"),
            ("perp_funding", "time"),
            ("perp_oi_history", "observed_at"),
            ("context_hl_oi_hourly", "captured_at"),
            ("context_ls_ratios", "ingested_at"),
            ("context_events", "first_seen_at"),
            ("microstructure_minutes", "minute_open"),
        ):
            if table in tables:
                columns = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
                if column in columns:
                    value = con.execute(f'SELECT max("{column}") FROM "{table}"').fetchone()[0]
                    frontiers[table] = {"column": column, "at": str(value) if value else None}
        last_completed = None
        if "runtime_cycles" in tables:
            row = con.execute(
                "SELECT cycle_id, job, runtime_id, finished_at FROM runtime_cycles "
                "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC, cycle_id DESC LIMIT 1"
            ).fetchone()
            if row:
                last_completed = dict(
                    zip(("cycle_id", "job", "runtime_id", "finished_at"), row, strict=True)
                )
        if "runtime_events" in tables:
            cols = {r[1] for r in con.execute("PRAGMA table_info(runtime_events)").fetchall()}
            if "git_commit" in cols:
                row = con.execute(
                    "SELECT git_commit FROM runtime_events WHERE git_commit IS NOT NULL ORDER BY recorded_at DESC LIMIT 1"
                ).fetchone()
                revision = row[0] if row else None
    if before != rt._sha256(path):
        raise ValueError("backup changed during verification")
    return {
        "sha256": before,
        "db_file_size": path.stat().st_size,
        "schema_version": version,
        "source_revision": revision,
        "run_ids": runs,
        "policy_ids": policies,
        "tables_read": counts,
        "table_fingerprints": fingerprints,
        "snapshot_kind": "autotrader_lab" if lab_snapshot else "paper_runtime",
        "source_authority": None
        if claim is None
        else {"runtime_id": claim[0], "since": str(claim[1])},
        "source_frontier": {"tables": frontiers, "last_completed_cycle": last_completed},
        "verification_result": "all tables readable; checksum stable",
    }


def register(
    root: Path,
    path: Path,
    active_db: Path,
    *,
    origin: str,
    reason: str = "",
    source_revision: str | None = None,
) -> dict:
    path = safe_path(root, path, active_db)
    result = inspect_backup(path)
    if sidecar(path).exists():
        old = json.loads(sidecar(path).read_text())
        if old.get("sha256") == result["sha256"] and old.get("export_receipt"):
            result["export_receipt"] = old["export_receipt"]
        if (
            old.get("sha256") == result["sha256"]
            and old.get("source_revision_basis") == "backup creator REVISION"
        ):
            result["source_revision"] = old["source_revision"]
            result["source_revision_basis"] = old["source_revision_basis"]
    if source_revision is not None:
        result["source_revision"] = source_revision
        result["source_revision_basis"] = "backup creator REVISION"
    else:
        result.setdefault("source_revision_basis", "latest recorded runtime event in snapshot")
    match = NAME.fullmatch(path.name)
    result.update(
        backup_timestamp=datetime.strptime(match[1], "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=UTC)
        .isoformat(),
        origin_environment=origin,
        verified_at=now(),
        rollback_reason=reason,
    )
    write_json(sidecar(path), result)
    return result


def inventory(root: Path, active_db: Path) -> list[dict]:
    root = safe_location(root)
    out = []
    for kind in sorted(KINDS):
        for path in sorted((root / kind).glob("prism-*.duckdb")):
            path = safe_path(root, path, active_db)
            manifest = json.loads(sidecar(path).read_text()) if sidecar(path).exists() else {}
            out.append(
                {
                    "path": str(path),
                    "timestamp": NAME.fullmatch(path.name)[1],
                    "bytes": path.stat().st_size,
                    "manifest": manifest,
                }
            )
    return sorted(out, key=lambda r: (r["timestamp"], r["path"]))


def export_backup(
    source: Path, destination: Path, manifest: dict, *, compress: bool = True
) -> dict:
    """Local atomic export, decompression and complete read test before emitting receipt."""
    from market_signal.ops import runtime as rt

    destination = safe_location(destination)
    source = safe_location(source)
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    if not manifest.get("sha256") or rt._sha256(source) != manifest["sha256"]:
        raise ValueError("source checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as d:
        artifact = Path(d) / "artifact"
        restored = Path(d) / "restored.duckdb"
        if compress:
            with source.open("rb") as fi, gzip.open(artifact, "wb", compresslevel=6) as fo:
                shutil.copyfileobj(fi, fo, 2**20)
            with gzip.open(artifact, "rb") as fi, restored.open("wb") as fo:
                shutil.copyfileobj(fi, fo, 2**20)
        else:
            shutil.copyfile(source, artifact)
            shutil.copyfile(artifact, restored)
        verified = inspect_backup(restored)
        if verified["sha256"] != manifest["sha256"]:
            raise ValueError("export checksum mismatch")
        receipt = {
            **{k: v for k, v in manifest.items() if k != "export_receipt"},
            "export_download_timestamp": now(),
            "archive_path": str(destination.resolve()),
            "artifact_sha256": rt._sha256(artifact),
            "artifact_bytes": artifact.stat().st_size,
            "compression": "gzip" if compress else "none",
            "restore_test": verified,
            "off_host_verified": True,
        }
        with artifact.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(artifact, destination)
        write_json(sidecar(destination), receipt)
    return receipt


def acknowledge(root: Path, path: Path, active_db: Path, receipt: dict) -> None:
    """Operator supplies a locally produced receipt after successful off-host restore."""
    from market_signal.ops import runtime as rt

    path = safe_path(root, path, active_db)
    manifest = json.loads(sidecar(path).read_text())
    digest = rt._sha256(path)
    if not (
        receipt.get("off_host_verified") is True
        and receipt.get("sha256") == digest
        and manifest.get("sha256") == digest
        and receipt.get("restore_test", {}).get("sha256") == digest
        and receipt.get("artifact_sha256")
        and receipt.get("archive_path")
        and receipt.get("export_download_timestamp")
    ):
        raise ValueError("invalid off-host receipt")
    manifest["export_receipt"] = receipt
    write_json(sidecar(path), manifest)


def retain(
    root: Path,
    active_db: Path,
    *,
    keep: int = 1,
    previous: Path | None = None,
    reason: str = "",
    apply: bool = False,
) -> dict:
    """One global copy, optional explicitly justified rollback copy. Dry-run default."""
    from market_signal.ops import runtime as rt

    if keep not in (1, 2) or (keep == 2 and (previous is None or not reason.strip())):
        raise ValueError("second backup requires an explicit path and recovery reason")
    rows = inventory(root, active_db)
    verified = [
        r for r in rows if r["manifest"].get("sha256") and r["manifest"].get("verification_result")
    ]
    if not verified:
        return {"keep": [], "removed": [], "blocked": rows, "dry_run": not apply}
    newest = verified[-1]
    protected = {newest["path"]}
    # Verify the safety net now, not only at the time its manifest was written.
    if inspect_backup(Path(newest["path"]))["sha256"] != newest["manifest"]["sha256"]:
        raise ValueError("newest backup checksum mismatch")
    if keep == 2:
        pin = str(safe_path(root, previous, active_db))
        if pin not in {r["path"] for r in verified}:
            raise ValueError("rollback pin must name a verified backup")
        row = next(r for r in verified if r["path"] == pin)
        if inspect_backup(Path(pin))["sha256"] != row["manifest"]["sha256"]:
            raise ValueError("previous backup checksum mismatch")
        protected.add(pin)
    removed, blocked = [], []
    for row in rows:
        if row["path"] in protected:
            continue
        manifest = row["manifest"]
        receipt = manifest.get("export_receipt", {})
        if not (
            receipt.get("off_host_verified") is True
            and receipt.get("sha256") == manifest.get("sha256")
            and receipt.get("restore_test", {}).get("sha256") == manifest.get("sha256")
        ):
            blocked.append({"path": row["path"], "reason": "verified off-host export required"})
            continue
        path = safe_path(root, Path(row["path"]), active_db)
        digest = rt._sha256(path)
        if digest != manifest["sha256"]:
            raise ValueError("deletion checksum mismatch")
        entry = {"path": str(path), "sha256": digest, "bytes": row["bytes"], "at": now()}
        if apply:
            # Persist exact intent before unlink; log completion afterwards.
            logpath = root / "retention.jsonl"
            if logpath.is_symlink():
                raise ValueError("symlink retention log")
            with logpath.open("a") as log:
                log.write(json.dumps({**entry, "action": "delete_intent"}) + "\n")
                log.flush()
                os.fsync(log.fileno())
                path.unlink()
                sync_directory(path.parent)
                log.write(json.dumps({**entry, "action": "deleted"}) + "\n")
                log.flush()
                os.fsync(log.fileno())
        removed.append(entry)
    return {
        "keep": sorted(protected),
        "removed": removed,
        "blocked": blocked,
        "dry_run": not apply,
        "second_backup_reason": reason if keep == 2 else None,
    }


def policy(root: Path) -> dict:
    path = safe_location(root / "retention-policy.json")
    if path.is_symlink():
        raise ValueError("symlink retention policy")
    value = json.loads(path.read_text()) if path.exists() else {"keep": 1}
    if value.get("previous"):
        value["previous"] = Path(value["previous"])
    if value.get("keep") not in (1, 2):
        raise ValueError("invalid retention policy")
    return value


def save_policy(
    root: Path, active_db: Path, *, keep: int, previous: Path | None, reason: str
) -> dict:
    result = retain(root, active_db, keep=keep, previous=previous, reason=reason)
    write_json(
        root / "retention-policy.json",
        {
            "keep": keep,
            "previous": str(previous.absolute()) if previous else None,
            "reason": reason,
        },
    )
    return result
