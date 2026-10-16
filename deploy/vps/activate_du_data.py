"""Apply a separately approved Lab v2 data-stage change after its measured gates.

Run on the VPS as root. This script is never called by the scheduler or rehearsal observer.
It cannot create an account, claim full authority or enable execution.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from market_signal.data.store import Store, authority_claim
from market_signal.ops import profiles
from market_signal.universe import budget


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=("ranking", "warming"))
    p.add_argument("--budget", type=Path, required=True)
    p.add_argument("--gate", type=Path, default=Path("/data/validation/lab-remediation/final.json"))
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error("run as root after separate activation approval")
    gate = json.loads(args.gate.read_text())
    if not (
        gate.get("clean") is True
        and gate.get("duration_seconds", 0) >= 21600
        and gate.get("scheduled_macro_successes", 0) >= 2
        and gate.get("scheduled_backup_successes", 0) >= 1
    ):
        raise ValueError("targeted remediation gate has not passed")
    path = Path("/etc/prism/prism.env")
    current = dict(dotenv_values(path))
    expected = {
        "PRISM_AUTHORITY": "rehearsal",
        "PRISM_RUNTIME_ROLE": "authoritative",
        "PRISM_RUNTIME_ID": "vps-prism-autotrader-lab-rehearsal",
        "PRISM_DEPLOYMENT_TARGET": "vps",
        "PRISM_DB_PATH": "/data/prism.duckdb",
    }
    if any(current.get(k) != v for k, v in expected.items()):
        raise ValueError("existing Lab identity/authority differs; refusing")
    if current.get("PRISM_PROFILE") not in ("autotrader_lab", "autotrader_lab_v2"):
        raise ValueError("not the Lab rehearsal profile")
    changes = {
        "PRISM_PROFILE": "autotrader_lab_v2",
        "PRISM_UNIVERSE_STAGE": args.stage,
        "PRISM_UNIVERSE_BUDGET": str(args.budget.resolve()),
        "PRISM_NIMBLE_RUNTIME": "off",
    }
    resolved = profiles.resolve({**current, **changes})
    if resolved.execution_allowed or resolved.external_actions_allowed:
        raise ValueError("unexpected execution/external-action capability")
    budget.validate(
        datetime.now(UTC), path=args.budget, rich_assets=12 if args.stage == "warming" else 6
    )
    disk = shutil.disk_usage("/data")
    mem = {
        x.split(":", 1)[0]: int(x.split()[1]) * 1024
        for x in Path("/proc/meminfo").read_text().splitlines()
        if x.startswith(("MemTotal:", "MemAvailable:"))
    }
    if (
        disk.free < 0.7 * disk.total
        or mem["MemAvailable"] < 0.4 * mem["MemTotal"]
        or os.getloadavg()[1] > 3
    ):
        raise ValueError("host warning/stop headroom gate not satisfied")
    store = Store("/data/prism.duckdb", read_only=True, lock_timeout=30)
    try:
        if authority_claim(store.con)[0] != expected["PRISM_RUNTIME_ID"]:
            raise ValueError("database authority mismatch")
        for (table,) in store.con.execute("SHOW TABLES").fetchall():
            if (
                table.startswith("paper_")
                and table not in ("paper_nimble_policies", "paper_nimble_triggers")
                and store.con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            ):
                raise ValueError("unexpected existing paper execution/account data")
    finally:
        store.close()
    saved = path.with_name("prism.env.pre-du-v1")
    if not saved.exists():
        if current.get("PRISM_PROFILE") != "autotrader_lab":
            raise ValueError("missing original v1 rollback environment")
        shutil.copy2(path, saved)
    lines = [
        line
        for line in path.read_text().splitlines()
        if line.strip().removeprefix("export ").split("=", 1)[0] not in changes
    ]
    lines += [f"{k}={v}" for k, v in changes.items()]
    stat = path.stat()
    fd, temp = tempfile.mkstemp(prefix=".du-profile-", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chown(temp, stat.st_uid, stat.st_gid)
    os.chmod(temp, 0o640)
    os.replace(temp, path)
    subprocess.run(["systemctl", "stop", "prism-nimble"], check=True)
    subprocess.run(
        [
            "systemctl",
            "restart",
            "prism-scheduler",
            "prism-free-source-ingest",
            "prism-free-sources",
        ],
        check=True,
    )
    if args.stage == "warming":
        subprocess.run(["systemctl", "restart", "prism-microstructure"], check=True)
    subprocess.run(["systemctl", "restart", "prism-universe"], check=True)
    print(
        json.dumps(
            {
                "applied": changes,
                "authority_unchanged": expected,
                "execution_allowed": False,
                "rollback_environment": str(saved),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
