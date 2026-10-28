"""Temporary bridge scheduler. Preserve the standard backup cycle and export immediately.

The hourly retry also handles a missed export. Neither path deletes evidence without
bridge_archive's checksum and full-restore receipt. Logs are bounded; failures exit nonzero.
"""

import json
import logging
import math
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bridge_archive import send


def headroom():
    from market_signal.ops import runtime

    db = Path("/data/prism.duckdb")
    disk = runtime.disk_state(db)
    minimum = math.ceil(1.2 * db.stat().st_size + runtime.DISK_CRITICAL * disk.total)
    return {
        "free_bytes": disk.free,
        "normal_backup_guard_bytes": minimum,
        "safety_margin_bytes": disk.free - minimum,
        "safe": disk.free >= minimum + 256 * 1024**2,
    }


def main():
    print(json.dumps({"status": "retired", "authority": "vps-prism-research"}))
    return 0


def historical_main():
    """Retained for incident evidence; no production entrypoint calls this procedure."""
    if sys.argv[1:] not in (["nightly"], ["retry"]):
        raise SystemExit("expected nightly or retry")
    log = logging.getLogger("railway_bridge")
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler("/data/logs/railway-bridge.log", maxBytes=1024**2, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    backup_exit = None
    try:
        if sys.argv[1] == "nightly":
            result = subprocess.run(
                ["market", "ops", "cycle", "backup", "--trigger", "schedule", "--wait", "3600"],
                timeout=4200,
            )
            backup_exit = result.returncode
        # Bound lock wait, export and verification; a timeout leaves the source intact.
        signal.alarm(360)
        records = send(json.loads(Path("/data/railway-bridge/config.json").read_text()), apply=True)
        signal.alarm(0)
        summary = {
            "mode": sys.argv[1],
            "backup_exit": backup_exit,
            "exported": len(records),
            "reclaimed_bytes": sum(r["logical_bytes"] for r in records),
            "headroom": headroom(),
        }
        log.info(json.dumps(summary))
        print(json.dumps(summary), flush=True)
        if not summary["headroom"]["safe"]:
            log.error(
                "capacity margin below normal backup guard plus 256 MiB; operator action required"
            )
        return int(backup_exit not in (None, 0) or not summary["headroom"]["safe"])
    except Exception:
        log.exception("backup bridge failed; inspect retained source and receipt")
        print(
            "Railway archive bridge FAILED; source is retained unless a verified deletion was recorded",
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
