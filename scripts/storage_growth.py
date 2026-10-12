"""Lightweight off-host Railway inventory and bounded physical-growth attribution."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import stat
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def category(path: str) -> str:
    p = Path(path)
    if path == "/data/prism.duckdb":
        return "authoritative_db"
    if path == "/data/prism.duckdb.wal":
        return "duckdb_wal"
    if ".tmp" in path or path.endswith((".part", ".tmp")):
        return "temporary_files"
    if path == "/data/backups/raw-archive-deletions.jsonl":
        return "journals_receipts_state"
    if path.startswith("/data/backups/"):
        return "recovery_backups" if path.endswith(".duckdb") else "backup_metadata"
    if path.startswith("/data/raw/"):
        return "provider_raw/" + p.parts[3]
    if path.startswith("/data/microstructure/raw/"):
        return "microstructure_raw"
    if path.startswith("/data/microstructure/spool/"):
        return "microstructure_spool"
    if path.startswith("/data/microstructure/lp/"):
        return "large_print_calibration"
    if path.startswith("/data/logs/"):
        return "logs"
    if path.startswith(("/data/baselines/", "/data/exports/", "/data/evidence/")):
        return "evidence_exports"
    return "journals_receipts_state"


def inventory(root: Path = Path("/data")) -> dict:
    files, errors, seen = [], [], set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in names:
            path = Path(directory) / name
            try:
                s = path.lstat()
                if not stat.S_ISREG(s.st_mode):
                    errors.append({"path": str(path), "reason": "non-regular file"})
                    continue
                identity = (s.st_dev, s.st_ino)
                allocated = s.st_blocks * 512 if identity not in seen else 0
                seen.add(identity)
                files.append(
                    {
                        "path": str(path),
                        "bytes": s.st_size,
                        "allocated": allocated,
                        "inode": s.st_ino,
                        "device": s.st_dev,
                        "links": s.st_nlink,
                        "mtime": s.st_mtime,
                        "category": category(str(path)),
                    }
                )
            except FileNotFoundError:
                errors.append({"path": str(path), "reason": "disappeared during sample"})
    fs = os.statvfs(root)
    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "files": sorted(files, key=lambda r: r["path"]),
        "free_bytes": fs.f_bavail * fs.f_frsize,
        "total_bytes": fs.f_blocks * fs.f_frsize,
        "filesystem_used_bytes": (fs.f_blocks - fs.f_bfree) * fs.f_frsize,
        "errors": errors,
    }


def sample_time(sample: dict) -> datetime:
    value = sample["measured_at"]
    return (
        datetime.fromtimestamp(value, UTC)
        if isinstance(value, (int, float))
        else datetime.fromisoformat(value)
    )


def attribution(samples: list[dict]) -> dict:
    if len(samples) < 2:
        raise ValueError("at least two inventories required")
    samples = sorted(samples, key=sample_time)
    elapsed = (sample_time(samples[-1]) - sample_time(samples[0])).total_seconds()
    if elapsed <= 0:
        raise ValueError("positive bounded window required")
    rows = defaultdict(lambda: {"bytes_added": 0, "bytes_removed": 0, "peak_size": 0})
    series = []
    for sample in samples:
        totals = defaultdict(lambda: {"allocated": 0, "logical": 0, "files": 0})
        by_path = {}
        for f in sample["files"]:
            c = f.get("category") or category(f["path"])
            totals[c]["allocated"] += f["allocated"]
            totals[c]["logical"] += f["bytes"]
            totals[c]["files"] += 1
            by_path[f["path"]] = (c, f["allocated"], f.get("inode"))
        for c, t in totals.items():
            rows[c]["peak_size"] = max(rows[c]["peak_size"], t["allocated"])
        series.append((totals, by_path))
    for (_, before), (_, after) in itertools.pairwise(series):
        for path in before.keys() | after.keys():
            c = (after.get(path) or before[path])[0]
            old, new = before.get(path, (c, 0, None)), after.get(path, (c, 0, None))
            if old[2] is not None and new[2] is not None and old[2] != new[2]:
                rows[c]["bytes_added"] += new[1]
                rows[c]["bytes_removed"] += old[1]
                continue
            delta = new[1] - old[1]
            rows[c]["bytes_added"] += max(delta, 0)
            rows[c]["bytes_removed"] += max(-delta, 0)
    for c, r in rows.items():
        first = series[0][0][c]
        last = series[-1][0][c]
        r.update(
            start_size=first["allocated"],
            end_size=last["allocated"],
            net_growth=last["allocated"] - first["allocated"],
            file_count_change=last["files"] - first["files"],
            start_files=first["files"],
            end_files=last["files"],
            start_logical=first["logical"],
            end_logical=last["logical"],
        )
        r["net_bytes_per_day"] = r["net_growth"] * 86400 / elapsed
        r["added_bytes_per_day"] = r["bytes_added"] * 86400 / elapsed
    return {
        "start": sample_time(samples[0]).isoformat(),
        "end": sample_time(samples[-1]).isoformat(),
        "window_seconds": elapsed,
        "sample_count": len(samples),
        "categories": dict(sorted(rows.items(), key=lambda item: -item[1]["net_growth"])),
        "free_space": {
            "start": samples[0]["free_bytes"],
            "end": samples[-1]["free_bytes"],
            "minimum": min(s["free_bytes"] for s in samples),
        },
        "limits": "Interval deltas and sampled peaks only; intra-sample churn/peaks are unobserved. DB file growth is not retained-row growth.",
    }


DB_SAMPLE_CODE = """
import duckdb
from pathlib import Path
from market_signal.ops import runtime as rt
db=Path('/data/prism.duckdb')
with rt.runtime_lock(db,'storage-read-only-diagnostics',60):
    with duckdb.connect(str(db),read_only=True) as con:
        layout=con.execute('PRAGMA database_size')
        names=[d[0] for d in layout.description]
        sizes=[dict(zip(names,row)) for row in layout.fetchall()]
        tables={}; blocks={}
        for (table,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog=current_database() AND table_schema='main' AND table_type='BASE TABLE'").fetchall():
            quoted='"'+table.replace('"','""')+'"'
            tables[table]=con.execute('SELECT count(*) FROM '+quoted).fetchone()[0]
            info=con.execute('SELECT block_id,compression FROM pragma_storage_info(?)',[table]).fetchall()
            blocks[table]={'data_block_ids':sorted({r[0] for r in info if r[0] is not None and r[0]>=0}),
                           'compression':sorted({r[1] for r in info if r[1]})}
        result['db_metadata']={'sampled_at':ns['datetime'].now(ns['UTC']).isoformat(),
         'database_size':sizes,'table_rows':tables,'table_data_blocks':blocks,
         'limits':'Data block IDs may be shared across tables; indexes/metadata are not assigned to a table. Free blocks are reusable inside DB, not filesystem free.'}
"""


def remote_sample(service: str, project: str, *, db_metadata: bool = False) -> dict:
    # No production file is created. Inventory uses stat only, without DB scans/hashes.
    source = Path(__file__).read_text()
    code = (
        "ns={'__name__':'storage_inventory'}; exec("
        + repr(source)
        + ",ns); result=ns['inventory']();\n"
    )
    if db_metadata:
        code += "try:\n" + "\n".join("    " + line for line in DB_SAMPLE_CODE.splitlines())
        code += "\nexcept Exception as exc: result['db_metadata_error']=str(exc)\n"
    code += "\nprint(ns['json'].dumps(result,default=str))"
    result = subprocess.run(
        [
            "railway",
            "ssh",
            "--project",
            project,
            "--environment",
            "production",
            "--service",
            service,
            "--",
            "/app/.venv/bin/python",
            "-c",
            code,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(
            "Railway inventory failed: " + result.stderr[-4000:] + result.stdout[-1000:]
        )
    return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--service", default="prism-runtime")
    parser.add_argument("--project", required=True)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=3600)
    parser.add_argument("--summarise", action="store_true")
    parser.add_argument("--db-metadata", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.samples <= 25 or args.interval < 60:
        parser.error("samples must be 1–25 and interval at least 60 seconds")
    args.destination.mkdir(parents=True, exist_ok=True)
    if not args.summarise:
        for i in range(args.samples):
            sample = remote_sample(args.service, args.project, db_metadata=args.db_metadata)
            name = datetime.fromisoformat(sample["measured_at"]).strftime("%Y%m%dT%H%M%SZ")
            path = args.destination / (name + ".json")
            with path.open("x") as f:
                json.dump(sample, f, indent=2)
            print(str(path), flush=True)
            if i + 1 < args.samples:
                time.sleep(args.interval)
    samples = [
        json.loads(p.read_text())
        for p in args.destination.glob("*.json")
        if p.name != "attribution.json"
    ]
    if len(samples) >= 2:
        result = attribution(samples)
        (args.destination / "attribution.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
