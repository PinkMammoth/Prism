"""Reproduce storage sensitivities from measured inventory and verified backup timestamps."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def main():
    evidence = Path("docs/evidence/storage-recovery")
    before = json.loads((evidence / "inventory-before.json").read_text())
    after = json.loads((evidence / "inventory-after.json").read_text())
    dynamic = json.loads(Path("docs/evidence/dynamic-universe-v1/storage.json").read_text())
    scenario = next(s for s in dynamic["scenarios"] if s["rich_assets"] == 12)
    manifests = [
        json.loads(p.read_text())
        for p in Path("data/backup-archive/storage-recovery-20261012").glob("*.gz.manifest.json")
    ]
    latest = max(manifests, key=lambda m: m["backup_timestamp"])
    previous = max(
        (m for m in manifests if m["backup_timestamp"] < latest["backup_timestamp"]),
        key=lambda m: m["backup_timestamp"],
    )
    elapsed = (
        datetime.fromisoformat(latest["backup_timestamp"])
        - datetime.fromisoformat(previous["backup_timestamp"])
    ).total_seconds()
    recent_baseline = (latest["db_file_size"] - previous["db_file_size"]) * 86400 / elapsed
    provider_days = defaultdict(int)
    for row in before["files"]:
        if row["path"].startswith("/data/raw/"):
            provider_days[datetime.fromtimestamp(row["mtime"], UTC).strftime("%Y-%m-%d")] += row[
                "allocated"
            ]
    provider_expected = (
        sum(provider_days[d] for d in ("2026-10-09", "2026-10-10", "2026-10-11")) / 3
    )
    provider_conservative = max(
        v for d, v in provider_days.items() if "2026-10-05" <= d <= "2026-10-11"
    )
    db = next(r["bytes"] for r in after["files"] if r["path"] == "/data/prism.duckdb")
    current_micro = sum(
        r["allocated"]
        for r in after["files"]
        if r["path"].startswith(("/data/microstructure/raw/", "/data/microstructure/spool/"))
    )
    reserve_increment = max(0, scenario["retained_raw_spool_bytes_sensitivity"] - current_micro)
    current_logs = sum(
        r["allocated"] for r in after["files"] if r["path"].startswith("/data/logs/")
    )
    logs_increment = max(0, 6 * 5 * 2**20 - current_logs)
    headroom = 1.2 * db + 0.05 * after["total_bytes"]
    comfortable_budget = after["free_bytes"] - headroom - reserve_increment - logs_increment
    rows = []
    cases = [
        (
            "expected measured-growth sensitivity",
            dynamic["recent_daily_backup_size_delta_bytes"],
            provider_expected,
            0,
        ),
        (
            "conservative recent-growth sensitivity",
            max(recent_baseline, dynamic["recent_daily_backup_size_delta_bytes"]),
            provider_conservative,
            0,
        ),
        (
            "conservative plus stalled spool retention",
            max(recent_baseline, dynamic["recent_daily_backup_size_delta_bytes"]),
            provider_conservative,
            scenario["raw_bytes_per_day_linear"] + scenario["spool_bytes_per_day_synthetic"],
        ),
        ("optimistic: zero baseline DB growth", 0, provider_expected, 0),
        ("dynamic-only: zero baseline and provider archive growth", 0, 0, 0),
    ]
    for name, baseline, provider, extra in cases:
        growth = baseline + scenario["persistent_bytes_per_day"]
        persistent_growth = 2 * growth + provider + extra
        comfortable_burn = persistent_growth + 1.2 * growth
        runway = max(0, comfortable_budget / comfortable_burn)
        # Require 30 days of growth, reserves and a future backup guard at the end.
        required_capacity = (
            (after["total_bytes"] - after["free_bytes"])
            + 30 * comfortable_burn
            + reserve_increment
            + logs_increment
            + 1.2 * db
        ) / 0.95
        rows.append(
            {
                "name": name,
                "baseline_db_bytes_per_day": baseline,
                "dynamic_db_bytes_per_day": scenario["persistent_bytes_per_day"],
                "provider_archive_bytes_per_day": provider,
                "unbounded_micro_bytes_per_day": extra,
                "physical_growth_bytes_per_30_days": 30 * persistent_growth,
                "comfortable_runway_days": runway,
                "comfortable_runway_months": runway / 30,
                "exhaustion_runway_days": max(
                    0,
                    (after["free_bytes"] - reserve_increment - logs_increment) / persistent_growth,
                ),
                "required_filesystem_capacity_30_days_bytes": required_capacity,
            }
        )
    result = {
        "measured_at": after["measured_at"],
        "free_bytes": after["free_bytes"],
        "active_db_bytes": db,
        "retained_full_backups": 1,
        "backup_headroom_required_now_bytes": headroom,
        "raw_spool_total_reserve_bytes": scenario["retained_raw_spool_bytes_sensitivity"],
        "current_micro_allocated_bytes": current_micro,
        "additional_raw_spool_reserve_bytes": reserve_increment,
        "additional_log_reserve_bytes": logs_increment,
        "comfortable_budget_bytes": comfortable_budget,
        "recent_baseline_bytes_per_day": recent_baseline,
        "baseline_measurement_seconds": elapsed,
        "gate": "BLOCKED",
        "scenarios": rows,
        "limits": [
            "Growth measurements span retirement/deployments and DuckDB checkpoints; they are sensitivities, not stationary forecasts.",
            "A verified full local copy and live DB each grow; backup creation needs transient headroom that grows with the live DB.",
            "Provider journal rates are allocated bytes grouped by capture mtime; calendar partial days excluded.",
            "Raw/spool reserve assumes existing retention works; additional stalled-retention sensitivity is explicit.",
            "Capacity is usable filesystem bytes, not a request to purchase or a nominal Railway plan size.",
            "No dynamic activation or new storage purchase was performed.",
        ],
    }
    (evidence / "runway.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
