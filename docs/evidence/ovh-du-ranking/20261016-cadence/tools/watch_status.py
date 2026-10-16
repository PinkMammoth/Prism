"""Small allowlisted progress receipt from the independently running observer."""

import json
import subprocess
from pathlib import Path

root = Path("/data/validation/du-ranking-cadence/attempt-2")
d = json.loads((root / "latest.json").read_text())
evs = d.get("db", {}).get("evaluations", [])
events = d.get("worker_events", [])
result = {
    "at": d["at"],
    "evaluations": len(evs),
    "evidence_times": [e["at"] for e in evs],
    "last_event": events[-1] if events else None,
    "checks_all_pass": all(all(e["checks"].values()) for e in evs),
    "stop_conditions": d.get("stop_conditions"),
    "db_error_type": d.get("db_error_type"),
    "available_ram": d["memory"]["MemAvailable"],
    "disk_free": d["disk"]["free"],
    "cpu_busy_percent": d.get("cpu_busy_percent"),
    "load": d["load"],
    "core_connected": d["micro"]["connected"],
    "core_pid": d["micro"]["pid"],
    "free_backlog": d["free_ingest"].get("backlog"),
    "profile": d["profile"]["identity"],
    "observer_active": subprocess.run(
        ["systemctl", "is-active", "prism-du-ranking-watch-attempt2"],
        capture_output=True,
        text=True,
    ).stdout.strip(),
}
for name in ("final.json", "rollback.json", "observer-error.json"):
    if (root / name).exists():
        result[name] = json.loads((root / name).read_text())
print(json.dumps(result, indent=2))
