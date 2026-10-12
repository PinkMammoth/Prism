"""Passive soak sampler + report for a VPS runtime (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md).

Read-only observation: systemd unit state/restarts/CPU/memory, host CPU steal/RAM/swap/disk,
/data growth by category, microstructure minute completeness from the collector spool, and
collector/worker heartbeats. It opens the DuckDB file only read-only with a short lock wait;
a busy database is itself recorded (lock-contention evidence), never waited out.

  python scripts/vps_soak.py sample --interval 300 --out /data/validation/soak.jsonl   (loop)
  python scripts/vps_soak.py sample --once --out ...
  python scripts/vps_soak.py report /data/validation/soak.jsonl [--out report.json]

Run the sampler as its own supervised process (deploy/vps/validate/prism-soak.service), not
as a timer: the scheduler remains the host's only scheduling authority.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

UNITS = ("prism-microstructure", "prism-free-sources", "prism-free-source-ingest",
         "prism-context-gateway", "prism-context-ingest", "prism-nimble", "prism-scheduler")  # fmt: skip
CORE = ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")
DATA = Path(os.environ.get("PRISM_DATA_DIR", "/data"))


def _now() -> datetime:
    return datetime.now(UTC)


def _unit(name: str) -> dict:
    props = "ActiveState,SubState,NRestarts,MemoryCurrent,CPUUsageNSec,ExecMainStartTimestamp"
    try:
        out = subprocess.run(["systemctl", "show", f"{name}.service", "-p", props],
                             capture_output=True, text=True, timeout=10).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    for k in ("NRestarts", "MemoryCurrent", "CPUUsageNSec"):
        try:
            d[k] = int(d[k])
        except (KeyError, ValueError):
            d[k] = None
    return d


def _tree_bytes(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total


def _proc() -> dict:
    with open("/proc/stat") as f:
        cpu = [int(x) for x in f.readline().split()[1:]]
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    return {"cpu_jiffies": cpu, "mem_available": mem["MemAvailable"],
            "mem_total": mem["MemTotal"], "swap_used": mem["SwapTotal"] - mem["SwapFree"],
            "loadavg": os.getloadavg()}  # fmt: skip


def _micro(since: datetime) -> dict:
    """Minute status counts per coin in the spool since ``since`` (revision 0 only)."""
    root = Path(os.environ.get("PRISM_MICRO_DIR") or DATA / "microstructure") / "spool"
    since_ms = int(since.timestamp() * 1000)
    out: dict[str, dict[str, int]] = {c: {} for c in CORE}
    seen = set()
    for f in sorted(root.glob("*/*.jsonl"))[-3:]:
        with f.open("rb") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") != "minute" or r.get("revision") != 0:
                    continue
                key = (r.get("coin"), r.get("minute_open"))
                if r["minute_open"] < since_ms or key in seen or r["coin"] not in out:
                    continue
                seen.add(key)
                st = out[r["coin"]]
                st[r.get("status", "?")] = st.get(r.get("status", "?"), 0) + 1
    return out


def _heartbeat(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    hb = d.get("heartbeat_at") or d.get("at") or d.get("updated_at")
    age = None
    if hb:
        with contextlib.suppress(ValueError):
            age = (_now() - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds()
    return {"age_s": age, "state": d.get("state"), "error": d.get("error")}


def _db(db: Path, since: datetime) -> dict:
    if not db.exists():
        return {"present": False}
    import duckdb

    t0 = time.monotonic()
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.IOException as exc:
        return {"present": True, "busy": True, "error": str(exc)[:200]}
    try:
        rows = con.execute(
            "SELECT job, status, count(*), max(epoch(finished_at) - epoch(started_at)) "
            "FROM runtime_cycles WHERE started_at >= ? GROUP BY 1, 2",
            [since],
        ).fetchall()
        return {"present": True, "busy": False, "open_s": round(time.monotonic() - t0, 3),
                "cycles": [list(r) for r in rows]}  # fmt: skip
    except duckdb.Error as exc:
        return {"present": True, "busy": False, "error": str(exc)[:200]}
    finally:
        con.close()


def sample_once(prev_at: datetime | None) -> dict:
    now = _now()
    since = prev_at or now - timedelta(minutes=5)
    du = shutil.disk_usage(DATA)
    cats = {n: DATA / n for n in ("microstructure", "free_event_sources", "context_gateway",
                                    "raw", "backups", "logs", "state")}  # fmt: skip
    db = Path(os.environ.get("PRISM_DB_PATH") or DATA / "prism.duckdb")
    return {
        "at": now.isoformat(),
        "units": {u: _unit(u) for u in UNITS},
        "host": _proc(),
        "disk": {"total": du.total, "free": du.free},
        "bytes": {
            "db": db.stat().st_size if db.exists() else 0,
            "wal": _tree_bytes(db.with_name(db.name + ".wal")) if db.exists() else 0,
            **{k: _tree_bytes(p) for k, p in cats.items() if p.exists()},
        },
        "micro_minutes": _micro(since - timedelta(minutes=2)),
        "heartbeats": {
            "microstructure": _heartbeat(DATA / "microstructure" / "status.json"),
            "free_sources": _heartbeat(DATA / "free_event_sources" / "collector.json"),
            "free_ingest": _heartbeat(DATA / "free_event_sources" / "worker.json"),
            "gateway_worker": _heartbeat(DATA / "context_gateway" / "worker.json"),
            "nimble": _heartbeat(DATA / "paper_nimble_worker.json"),
        },
        "db": _db(db, since),
    }


def sample(args) -> None:
    prev = None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        s = sample_once(prev)
        with args.out.open("a") as f:
            f.write(json.dumps(s, default=str) + "\n")
        prev = datetime.fromisoformat(s["at"])
        if args.once:
            return
        time.sleep(args.interval)


def report(args) -> dict:
    rows = [json.loads(line) for line in Path(args.path).read_text().splitlines() if line]
    if len(rows) < 2:
        raise SystemExit("need at least two samples")
    a, b = rows[0], rows[-1]
    hours = (
        datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])
    ).total_seconds() / 3600
    cpu0, cpu1 = a["host"]["cpu_jiffies"], b["host"]["cpu_jiffies"]
    d = [y - x for x, y in zip(cpu0, cpu1, strict=False)]
    tot = sum(d) or 1
    units = {}
    for u in UNITS:
        u0, u1 = a["units"].get(u, {}), b["units"].get(u, {})
        mem = [r["units"].get(u, {}).get("MemoryCurrent") for r in rows]
        mem = [m for m in mem if isinstance(m, int) and m < 2**60]
        cpu_ns = (u1.get("CPUUsageNSec") or 0) - (u0.get("CPUUsageNSec") or 0)
        units[u] = {
            "final_state": u1.get("ActiveState"),
            "restarts": (u1.get("NRestarts") or 0) - (u0.get("NRestarts") or 0),
            "cpu_equivalents": cpu_ns / 1e9 / (hours * 3600) if hours else None,
            "mem_peak_sample": max(mem) if mem else None,
        }
    status: dict[str, int] = {}
    for r in rows[1:]:
        for coin in r["micro_minutes"].values():
            for k, n in coin.items():
                status[k] = status.get(k, 0) + n
    minutes = sum(status.values())
    busy = sum(1 for r in rows if r["db"].get("busy"))
    growth = {k: (b["bytes"].get(k, 0) - a["bytes"].get(k, 0)) / hours * 24
              for k in b["bytes"]}  # fmt: skip
    hb_max = {}
    for r in rows:
        for k, v in r["heartbeats"].items():
            if v and v.get("age_s") is not None:
                hb_max[k] = max(hb_max.get(k, 0), v["age_s"])
    out = {
        "kind": "soak_report",
        "from": a["at"],
        "to": b["at"],
        "hours": round(hours, 2),
        "samples": len(rows),
        "host_cpu_busy_fraction": 1 - (d[3] + d[4]) / tot,
        "host_steal_fraction": d[7] / tot if len(d) > 7 else None,
        "mem_available_min": min(r["host"]["mem_available"] for r in rows),
        "swap_used_max": max(r["host"]["swap_used"] for r in rows),
        "disk_free_final": b["disk"]["free"],
        "growth_bytes_per_day": growth,
        "units": units,
        "micro_minute_status": status,
        "micro_complete_fraction": status.get("COMPLETE", 0) / minutes if minutes else None,
        "micro_expected_minutes": round(hours * 60 * len(CORE)),
        "db_busy_samples": busy,
        "heartbeat_max_age_s": hb_max,
    }
    text = json.dumps(out, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--interval", type=int, default=300)
    s.add_argument("--out", type=Path, default=DATA / "validation" / "soak.jsonl")
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("report")
    s.add_argument("path")
    s.add_argument("--out")
    args = p.parse_args()
    (sample if args.cmd == "sample" else report)(args)


if __name__ == "__main__":
    main()
