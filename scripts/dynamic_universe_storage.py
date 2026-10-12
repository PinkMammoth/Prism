"""Measured storage inputs plus explicit conservative sensitivities, not an activation budget."""

import json
from pathlib import Path

import duckdb

from market_signal.universe.policy import POLICY


def main():
    root = Path("docs/evidence/dynamic-universe-v1")
    live = json.loads((root / "production-resources.json").read_text())
    bench = json.loads((root / "benchmark.json").read_text())
    con = duckdb.connect(bench["snapshot"], read_only=True)
    rows = con.execute("SELECT count(*) FROM microstructure_minutes").fetchone()[0]
    storage = con.execute("PRAGMA storage_info('microstructure_minutes')").df()
    blocks = storage[storage.block_id >= 0].block_id.nunique()
    con.close()
    row_bytes = blocks * 262144 / rows  # coarse allocated block attribution, conservatively shared
    status = live["microstructure_status"]
    seconds = (status["written_ms"] - status["started_ms"]) / 1000
    raw_daily_six = status["raw_bytes_written"] / seconds * 86400
    sample = bench["all_market_discovery"]["compressed_evaluation_bytes"]
    ledger_cost = json.loads((root / "ledger-cost.json").read_text())
    sample = max(sample, ledger_cost["payload_bytes"])
    evidence_daily = sample * (86400 / POLICY.cadence_seconds)
    # At most 30 candidates × 5 horizons × 144 evaluations; conservatively 320 stored bytes/row.
    outcome_daily = POLICY.candidate_cap * len(POLICY.outcome_minutes) * 144 * 320
    scenarios = []
    for assets in (6, 12, 18, 20, 30):
        micro_daily = assets * 1440 * row_bytes
        sample_bench = next(x for x in bench["sizes"] if x["assets"] == assets)
        spool_daily = sample_bench["spool_bytes_per_minute"] * 1440
        raw_daily = raw_daily_six * assets / 6
        retained_raw_spool = (
            9 * spool_daily + 2 * raw_daily
        )  # whole-day retention + boundary margin
        persistent_daily = micro_daily + evidence_daily + outcome_daily + assets * 24 * 200
        # Up to 3 daily + 2 weekly + 3 manual retained copies; links can reduce actual physical bytes.
        with_backups = 9 * persistent_daily
        scenarios.append(
            {
                "rich_assets": assets,
                "micro_rows_per_day": assets * 1440,
                "micro_bytes_per_day": micro_daily,
                "raw_bytes_per_day_linear": raw_daily,
                "spool_bytes_per_day_synthetic": spool_daily,
                "retained_raw_spool_bytes_sensitivity": retained_raw_spool,
                "universe_evidence_bytes_per_day": evidence_daily,
                "universe_outcomes_bytes_per_day_upper_sensitivity": outcome_daily,
                "persistent_bytes_per_day": persistent_daily,
                "persistent_bytes_per_30_days": persistent_daily * 30,
                "live_plus_8_backups_growth_per_30_days_upper": with_backups * 30,
                "free_space_runway_days_incremental_upper_backup_model": live["disk"]["free"]
                / with_backups,
            }
        )
    newest_daily = sorted(
        (b for b in live["backups"] if "pre-" not in b["name"] and "dashboard" not in b["name"]),
        key=lambda b: b["mtime"],
    )
    growth = newest_daily[-1]["bytes"] - newest_daily[-2]["bytes"]
    result = {
        "policy_id": POLICY.identity,
        "measured_at": live["measured_at"],
        "current_physical_free_bytes": live["disk"]["free"],
        "database_bytes": live["database_bytes"],
        "current_backup_bytes": live["backup_bytes"],
        "allocated_micro_bytes_per_row_backup": row_bytes,
        "current_collector_cpu_fraction": status["cpu_s"] / seconds,
        "current_collector_rss_mib": status["max_rss_mb"],
        "current_collector_messages_per_second": status["msgs_per_s_60s"],
        "current_collector_network_bytes_per_second": status["bytes_per_s_60s"],
        "current_raw_bytes_per_day_six_linear": raw_daily_six,
        "recent_daily_backup_size_delta_bytes": growth,
        "backup_headroom_required_bytes_now": 1.2 * live["database_bytes"]
        + 0.05 * live["disk"]["total"],
        "budget_validated": False,
        "activation": "BLOCKED_BY_STORAGE_BUDGET_NO_PRODUCTION_MUTATION",
        "scenarios": scenarios,
        "limits": [
            "Snapshot block attribution, synthetic spool, 4-hour collector traffic sample and linear asset scaling are sensitivities, not a production forecast.",
            "Upper backup model retains eight full copies; weekly hard links and rotation can reduce usage, while a new copy requires transient space before pruning.",
            "Runway excludes existing research/paper/base DB growth; existing baseline may exhaust space sooner. Recent daily growth predates full effect of Paper v2 retirement.",
            "No current 30-day budget can be validated. No backups, evidence or data were deleted.",
        ],
    }
    (root / "storage.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
