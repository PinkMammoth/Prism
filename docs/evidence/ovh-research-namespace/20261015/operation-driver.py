"""Administrative rehearsal driver; only /var/lib/prism-research, never /data."""

import argparse
import contextlib
import json
import os
import resource
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from market_signal.data.store import Store
from market_signal.ops import research_namespace as ns
from market_signal.ops.backups import write_json

ROOT = Path("/var/lib/prism-research")


def sizes():
    paths = [
        p
        for name in ("import", "scratch", "backups")
        for p in (ROOT / name).rglob("*")
        if p.is_file()
    ]
    return {
        "logical_bytes": sum(p.stat().st_size for p in paths),
        "allocated_bytes": sum(p.stat().st_blocks * 512 for p in paths),
        "free_bytes": shutil.disk_usage(ROOT).free,
        "temporary_bytes": sum(
            p.stat().st_size for p in paths if ".tmp" in str(p) or ".pending-" in str(p)
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["migrate", "backup"])
    args = parser.parse_args()
    samples = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            with contextlib.suppress(FileNotFoundError):
                samples.append(sizes())
            stop.wait(0.2)

    thread = threading.Thread(target=sample)
    thread.start()
    start = time.monotonic()
    cpu = time.process_time()
    before = sizes()
    try:
        if args.operation == "migrate":
            source = ROOT / "scratch/source.duckdb"
            copy = ROOT / "scratch/migration.duckdb"
            assert not copy.exists()
            original = ns.inspect(source)
            shutil.copyfile(source, copy)
            os.environ["PRISM_RUNTIME_ROLE"] = "scratch"
            os.environ["PRISM_RUNTIME_ID"] = ""
            migration_start = time.monotonic()
            store = Store(copy)
            store.con.execute("CHECKPOINT")
            store.close()
            migration_seconds = time.monotonic() - migration_start
            after = ns.inspect(copy)
            differences = ns.compare_tables(original["tables"], after["tables"])
            expected = {
                "schema_version",
                "universe_policies",
                "universe_evaluations",
                "universe_outcomes",
                "microstructure_subscription_events",
            }
            assert {d["table"] for d in differences} == expected
            assert after["schema_version"] == 31
            assert all(after["tables"][t]["rows"] == 0 for t in expected - {"schema_version"})
            assert ns.sha256(source) == original["sha256"]
            rollback = ROOT / "scratch/rollback.duckdb"
            shutil.copyfile(source, rollback)
            rolled = ns.inspect(rollback)
            assert rolled["sha256"] == original["sha256"] and rolled["tables"] == original["tables"]
            rollback.unlink()
            destination = ROOT / "import/prism.duckdb"
            assert not destination.exists()
            received_at = datetime.now(UTC).isoformat()
            os.chmod(copy, 0o440)
            os.chown(copy, 0, os.stat(ROOT).st_gid)
            os.link(copy, destination)
            ns.sync_directory(destination.parent)
            available_at = datetime.now(UTC).isoformat()
            copy.unlink()
            receipt = json.loads((ROOT / "state/source-receipt.json").read_text())
            receipt.update(
                {
                    "sha256": after["sha256"],
                    "source_snapshot_sha256": original["sha256"],
                    "consumer_id": "vps-prism-research-offline",
                    "received_at_here": received_at,
                    "available_at_here": available_at,
                    "authority_mode": "none",
                    "runtime_role": "reader",
                    "source_authority_is_provenance_only": True,
                }
            )
            write_json(ROOT / "state/import-receipt.json", receipt)
            os.chown(ROOT / "state/import-receipt.json", 0, os.stat(ROOT).st_gid)
            os.chmod(ROOT / "state/import-receipt.json", 0o640)
            result = {
                "schema_before": 30,
                "schema_after": 31,
                "migration_seconds": migration_seconds,
                "source_sha256": original["sha256"],
                "candidate_sha256": after["sha256"],
                "differences": differences,
                "rollback_byte_and_table_parity": True,
                "received_at_here": received_at,
                "available_at_here": available_at,
                "candidate_bytes": after["bytes"],
                "source_preserved": True,
            }
        else:
            receipt = json.loads((ROOT / "state/import-receipt.json").read_text())
            result = ns.backup_sealed(
                ROOT / "import/prism.duckdb",
                ROOT / "backups",
                {
                    "sha256": receipt["sha256"],
                    "source_snapshot_sha256": receipt["source_snapshot_sha256"],
                    "source_runtime": "railway-prism-runtime",
                    "consumer_id": receipt["consumer_id"],
                    "source_revision": receipt["source"]["source_manifest"]["source_revision"],
                    "source_frontier": receipt["last_finished_cycle"],
                    "target_software": json.loads(
                        Path("/opt/prism/research/current/NAMESPACE_SOURCE.json").read_text()
                    ),
                    "source_snapshot_metadata": receipt["source"],
                    "profile_identity": "prism_research_vps_runtime_v1",
                    "authority_mode": "none",
                    "runtime_role": "reader",
                    "received_at_here": receipt["received_at_here"],
                    "available_at_here": receipt["available_at_here"],
                },
            )
            generation = (
                ROOT
                / "backups"
                / json.loads((ROOT / "backups/latest.json").read_text())["generation"]
            )
            proof = ns.restore(
                generation / "research.duckdb.gz", result, ROOT / "scratch/restored.duckdb"
            )
            result["independent_fresh_scratch_restore"] = proof
    finally:
        stop.set()
        thread.join()
    samples.append(sizes())
    result["resources"] = {
        "wall_seconds": time.monotonic() - start,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "before": before,
        "after": sizes(),
        "sample_interval_seconds": 0.2,
        "peak_research_logical_bytes": max(s["logical_bytes"] for s in samples),
        "peak_research_allocated_bytes": max(s["allocated_bytes"] for s in samples),
        "peak_temporary_bytes": max(s["temporary_bytes"] for s in samples),
        "minimum_host_free_bytes": min(s["free_bytes"] for s in samples),
    }
    write_json(ROOT / "state" / (args.operation + "-proof.json"), result)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                in [
                    "schema_after",
                    "candidate_sha256",
                    "resources",
                    "compressed_bytes",
                    "restore_seconds",
                ]
            },
            default=str,
        )
    )


if __name__ == "__main__":
    main()
