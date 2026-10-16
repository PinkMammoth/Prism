"""Allowlisted host resource evidence; never reads secret values or record bodies."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return {"status": result.returncode, "stdout": result.stdout}


def collect():
    disk = shutil.disk_usage("/data")
    out = {
        "at": datetime.now(UTC).isoformat(),
        "disk": dict(zip(("total", "used", "free"), disk, strict=True)),
        "memory": {
            line.split(":")[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))
        },
        "load": os.getloadavg(),
        "cpu_jiffies": [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]],
        "units": {},
        "release": str(Path("/opt/prism/current").resolve()),
    }
    for name in (
        "microstructure",
        "free-sources",
        "free-source-ingest",
        "scheduler",
        "universe",
        "nimble",
        "context-gateway",
        "context-ingest",
        "research-backup",
    ):
        props = "ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,CPUUsageNSec,MemoryCurrent,MemoryPeak,Result"
        result = command(["systemctl", "show", f"prism-{name}", "-p", props])
        out["units"][name] = dict(
            line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line
        )
    roots = (
        "/data",
        "/var/lib/prism-research",
        "/var/lib/prism-research-backup",
        "/var/lib/prism",
        "/opt/prism",
        "/var/log",
        "/tmp",
        "/var/tmp",
    )
    out["physical_usage"] = command(["du", "-x", "-B1", "--max-depth=2", *roots])
    out["filesystem_top_level"] = command(["du", "-x", "-B1", "--max-depth=1", "/"])
    out["db_files"] = {}
    for name in (
        "/data/prism.duckdb",
        "/data/prism.duckdb.wal",
        "/var/lib/prism-research/import/prism.duckdb",
    ):
        path = Path(name)
        if path.exists():
            stat = path.stat()
            out["db_files"][name] = {
                "bytes": stat.st_size,
                "physical_bytes": stat.st_blocks * 512,
                "mtime_ns": stat.st_mtime_ns,
            }
    allowed = {
        "PRISM_PROFILE",
        "PRISM_AUTHORITY",
        "PRISM_RUNTIME_ROLE",
        "PRISM_RUNTIME_ID",
        "PRISM_UNIVERSE_STAGE",
        "PRISM_NIMBLE_RUNTIME",
        "PRISM_UNIVERSE_BUDGET",
        "PRISM_DB_PATH",
        "PRISM_DEPLOYMENT_TARGET",
    }
    out["environment"] = {
        k: v
        for line in Path("/etc/prism/prism.env").read_text().splitlines()
        if "=" in line
        for k, v in [line.split("=", 1)]
        if k in allowed
    }
    out["profile"] = json.loads(Path("/data/state/profile.json").read_text())
    status = Path("/var/lib/prism-research-backup/state/last-run.json")
    if status.exists():
        s = json.loads(status.read_text())
        out["research_backup"] = {
            k: s.get(k)
            for k in ("status", "stage", "started_at", "finished_at", "receipt", "removed_local")
        }
    for label, name in (
        ("micro", "/data/microstructure/status.json"),
        ("free_collector", "/data/free_event_sources/collector.json"),
        ("free_ingest", "/data/free_event_sources/worker.json"),
    ):
        data = json.loads(Path(name).read_text())
        if label == "micro":
            out[label] = data
        else:
            keys = ("heartbeat_at", "backlog", "ingested", "active", "providers", "db_opened")
            out[label] = {k: data.get(k) for k in keys}
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
