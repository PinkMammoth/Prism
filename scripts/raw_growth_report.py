"""Attribute retained provider archives by captured UTC day, with explicit limits."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path


def report(manifest: dict, inventory: dict) -> dict:
    end = datetime.fromisoformat(inventory["measured_at"]).astimezone(UTC).date()
    days = [(end - timedelta(days=i)).isoformat() for i in range(7, 0, -1)]
    files = {r["path"]: r for r in inventory["files"]}
    families = defaultdict(
        lambda: defaultdict(lambda: {"bytes": 0, "allocated": 0, "logical": 0, "files": 0})
    )
    eligible = defaultdict(int)
    for row in manifest["rows"]:
        family = row["source_provider"]
        day = datetime.fromisoformat(row["first_timestamp"]).astimezone(UTC).date().isoformat()
        item = families[family][day]
        item["bytes"] += row["source_size"]
        item["allocated"] += files.get(row["source_path"], {}).get("allocated", 0)
        item["logical"] += row["read_test"]["logical_size"]
        item["files"] += 1
        eligible[family] += row["source_size"] if row["eligibility"]["export_eligible"] else 0
    ranked = []
    for family, series in families.items():
        recent = [series[d] for d in days]
        total = {
            k: sum(v[k] for v in series.values())
            for k in ("bytes", "allocated", "logical", "files")
        }
        ranked.append(
            {
                "source": family,
                "retained": total,
                "eligible_bytes": eligible[family],
                "last_complete_day": dict(series[days[-1]]),
                "seven_day_median_bytes": statistics.median(v["bytes"] for v in recent),
                "seven_day_max_bytes": max(v["bytes"] for v in recent),
                "seven_day_median_files": statistics.median(v["files"] for v in recent),
                "compression_ratio": total["logical"] / total["bytes"],
                "daily": dict(sorted(series.items())),
            }
        )
    return {
        "complete_days": days,
        "sources": sorted(ranked, key=lambda r: -r["last_complete_day"]["bytes"]),
        "parse_errors": manifest["errors"],
        "limits": "Captured-day sums of retained, parse-verified files; not direct historical filesystem samples. Missing/pruned files are unobserved. Current partial day excluded. First capture day assigns bundles; physical interval samples are reported separately.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", type=Path)
    p.add_argument("inventory", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    args.output.write_text(
        json.dumps(
            report(json.loads(args.manifest.read_text()), json.loads(args.inventory.read_text())),
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
