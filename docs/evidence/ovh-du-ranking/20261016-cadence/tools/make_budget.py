"""Issue a resource-only six-rich-assets receipt from a fresh sanitized host sample."""

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    host = json.loads(args.host.read_text())
    age = (datetime.now(UTC) - datetime.fromisoformat(host["at"])).total_seconds()
    disk = host["disk"]
    mem = host["memory"]
    floor = math.ceil(0.7 * disk["total"])
    reserve = 7_000_000_000
    daily = 250_000_000
    available = disk["free"] - floor - reserve
    assert 0 <= age < 3600, "refresh host measurement before issuing receipt"
    assert available / daily >= 30 and mem["MemAvailable"] >= 0.5 * mem["MemTotal"]
    assert host["research_backup"]["status"] == "ok"
    assert (
        not host["profile"]["execution_allowed"] and not host["profile"]["external_actions_allowed"]
    )
    assert not host["profile"]["collector_dynamic"]
    budget = {
        "policy_id": "universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff",
        "measured_at": int(datetime.fromisoformat(host["at"]).timestamp()),
        "validated": True,
        "rich_assets": 6,
        "minimum_runway_days": 30,
        "daily_growth_bytes": daily,
        "available_bytes": available,
        "worker_cpu_seconds_per_minute": 2.0,
        "release_revision": "c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c",
        "scope": "Resource admission only: ranking, six existing rich/core markets. No warming/execution/sizing permission. Software validation is a separate gate.",
        "host_measurement": {
            "at": host["at"],
            "disk_free_bytes": disk["free"],
            "disk_total_bytes": disk["total"],
            "minimum_free_fraction": 0.7,
            "minimum_free_bytes": floor,
            "transient_backup_reserve_bytes": reserve,
            "ram_available_bytes": mem["MemAvailable"],
            "ram_total_bytes": mem["MemTotal"],
            "future_baseline_paper_ram_reserve_bytes": 3_000_000_000,
            "radar_unit_cap_bytes": 2**30,
        },
        "growth_basis": "Lab DB51MB/day short-interval upper sensitivity (prior longer-window41MB/day; current allocation includes first-use bootstrap) +15.579MB/day prior mature ranking ledger/outcomes +67MB/day growing backup +64MB/day existing raw/spool +6MB/day provider/free source +37MB/day logs/overhead +0.1MB/day frozen Research backup metadata; round240.679MB/day upper sensitivity up to250MB/day.",
        "runway_days_model": available / daily,
        "unattributed_150MB_daily_stress_runway_days": available / (daily + 150_000_000),
        "temporary_restore_peak_not_persistent_growth": True,
        "warming_cost_included": False,
        "expected_worker_peak_rss_mib_prior": 362,
        "whole_cli_first_peak_rss_mib": 380696 / 1024,
        "expected_worker_cpu_seconds_per_600s_prior": 2.49,
        "whole_cli_cpu_seconds_first_live_evaluation": 7.501337,
        "cpu_budget_margin": "2CPU-s/min admission estimate versus0.750 whole-CLI first run and0.249 prior internal benchmark; hard stop remains30CPU-s/min.",
        "warnings": [
            "Runway below60-day warning; preserve30-day stop and70%-free floor.",
            "Historical145.6MB host step remains unattributed; current free bytes already charge it.",
            "Final real ranking cost must replace provisional benchmark assumptions after observation.",
        ],
        "input_evidence_sha256": hashlib.sha256(args.host.read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(budget, indent=2) + "\n")
    print(
        json.dumps(
            {
                "path": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "runway_days": budget["runway_days_model"],
                "installed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
