"""Schema 30->31 rehearsal and restore proof on local, disposable backup copies only."""

import argparse
import gzip
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import duckdb

from market_signal.data.store import MIGRATIONS, Store, authority_claim
from market_signal.ops import backups as b
from market_signal.ops import runtime as rt


def fingerprints(path):
    with duckdb.connect(str(path), read_only=True) as con:
        out = {}
        for (table,) in con.execute("SHOW TABLES").fetchall():
            columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
            args = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            count, digest = con.execute(
                f'SELECT count(*),bit_xor(hash({args})) FROM "{table}"'
            ).fetchone()
            out[table] = {"rows": count, "value_hash": digest}
        return out, list(authority_claim(con))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    receipt = json.loads(b.sidecar(args.archive).read_text())
    if (
        not receipt.get("off_host_verified")
        or rt._sha256(args.archive) != receipt["artifact_sha256"]
    ):
        raise ValueError("verified archive required")
    if len(MIGRATIONS) != 31 or receipt["schema_version"] != 30:
        raise ValueError("expected source 30 / target 31")
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        raise ValueError("local operator only")
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="prism-schema31-", dir=args.archive.parent
    ) as directory:
        root = Path(directory)
        source, migrated, restored = [
            root / name for name in ("source.duckdb", "migration.duckdb", "restore.duckdb")
        ]
        with gzip.open(args.archive, "rb") as fi, source.open("wb") as fo:
            shutil.copyfileobj(fi, fo, 2**20)
        if rt._sha256(source) != receipt["sha256"]:
            raise ValueError("decompressed checksum mismatch")
        before, authority_before = fingerprints(source)
        shutil.copyfile(source, migrated)
        peaks = {
            "total_allocated": 0,
            "db_allocated": 0,
            "wal_allocated": 0,
            "temporary_allocated": 0,
            "samples": 0,
        }
        stop = threading.Event()

        def sample():
            totals = {
                "total_allocated": 0,
                "db_allocated": 0,
                "wal_allocated": 0,
                "temporary_allocated": 0,
            }
            for p in root.rglob("*"):
                try:
                    if not p.is_file() or p == source:
                        continue
                    size = p.stat().st_blocks * 512
                except FileNotFoundError:
                    continue
                key = (
                    "db_allocated"
                    if p == migrated
                    else "wal_allocated"
                    if p.name.endswith(".wal")
                    else "temporary_allocated"
                )
                totals[key] += size
                totals["total_allocated"] += size
            for k, value in totals.items():
                peaks[k] = max(peaks[k], value)
            peaks["samples"] += 1

        def monitor():
            while not stop.is_set():
                sample()
                stop.wait(0.001)

        initial = migrated.stat().st_blocks * 512
        thread = threading.Thread(target=monitor)
        thread.start()
        old_role = os.environ.get("PRISM_RUNTIME_ROLE")
        os.environ["PRISM_RUNTIME_ROLE"] = "scratch"  # disposable local copy only
        started = time.perf_counter()
        try:
            store = Store(migrated)
            store.con.execute("CHECKPOINT")
            store.close()
        finally:
            seconds = time.perf_counter() - started
            sample()
            stop.set()
            thread.join()
            if old_role is None:
                os.environ.pop("PRISM_RUNTIME_ROLE", None)
            else:
                os.environ["PRISM_RUNTIME_ROLE"] = old_role
        after, authority_after = fingerprints(migrated)
        changed = [t for t in before if t != "schema_version" and before[t] != after.get(t)]
        expected_new = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", MIGRATIONS[30])
        with duckdb.connect(str(migrated), read_only=True) as con:
            target_schema = con.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        # Restore original schema-30 bytes to a separate copy; never reverse migrations in place.
        shutil.copyfile(source, restored)
        restore = b.inspect_backup(restored)
        restore_fp, restore_authority = fingerprints(restored)
        report = {
            "source_receipt": receipt,
            "code_target_schema": len(MIGRATIONS),
            "target_schema": target_schema,
            "migration_seconds": seconds,
            "initial_db_bytes": source.stat().st_size,
            "final_db_bytes": migrated.stat().st_size,
            "disk_peaks": peaks,
            "peak_increment_allocated_bytes": max(0, peaks["total_allocated"] - initial),
            "peak_limits": "1ms sampled local peak, not a mathematical upper bound or production filesystem trace",
            "before": before,
            "after": after,
            "changed_existing_tables": changed,
            "authority_before": authority_before,
            "authority_after": authority_after,
            "new_tables": {t: after.get(t) for t in expected_new},
            "restore": restore,
            "restore_exact": restore_fp == before
            and restore_authority == authority_before
            and restore["sha256"] == receipt["sha256"],
            "source_unchanged": rt._sha256(source) == receipt["sha256"]
            and rt._sha256(args.archive) == receipt["artifact_sha256"],
        }
        report["passed"] = (
            target_schema == 31
            and not changed
            and authority_before == authority_after
            and set(after) - set(before) == set(expected_new)
            and all(after[t]["rows"] == 0 for t in expected_new)
            and report["restore_exact"]
            and report["source_unchanged"]
        )
        (args.evidence / "schema31-rehearsal.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n"
        )
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "passed",
                        "migration_seconds",
                        "initial_db_bytes",
                        "final_db_bytes",
                        "peak_increment_allocated_bytes",
                        "restore_exact",
                    )
                },
                indent=2,
            )
        )
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
