"""Ranking admission uses measured base costs plus measured lightweight DU evidence."""

import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT.parents[1]
base = json.loads(
    (EVIDENCE / "ovh-autotrader-rehearsal/20261015/rehearsal-summary.json").read_text()
)
target = json.loads(
    (EVIDENCE / "ovh-lab-remediation/20261015/reviewed-window-metrics.json").read_text()
)
host = json.loads((ROOT / "host-admission.json").read_text())
du = json.loads((EVIDENCE / "dynamic-universe-v1/storage.json").read_text())["scenarios"][0]
base_db = base["growth"]["db"]["delta"] * 24 / base["hours"]
target_hours = 26072.776982 / 3600
target_db = (target["db_end"] - target["db_start"]) * 24 / target_hours
current_hours = (
    datetime.fromisoformat(host["at"]) - datetime.fromisoformat("2026-10-16T01:40:01.048838+00:00")
).total_seconds() / 3600
current_db = (
    (host["db_files"]["/data/prism.duckdb"]["bytes"] - target["db_end"]) * 24 / current_hours
)
base_upper = max(base_db, target_db, current_db)
rank_evidence = du["universe_evidence_bytes_per_day"]
rank_outcomes = du["universe_outcomes_bytes_per_day_upper_sensitivity"]
components = {
    "lab_db_existing_upper": math.ceil(base_upper / 1e6) * 1_000_000,
    "ranking_evaluations": rank_evidence,
    "ranking_outcomes_upper": rank_outcomes,
    "one_growing_lab_backup": math.ceil(
        (math.ceil(base_upper / 1e6) * 1_000_000 + rank_evidence + rank_outcomes) / 1e6
    )
    * 1_000_000,
    "core_micro_raw_and_spool_no_rotation_credit": 64_000_000,
    "provider_raw_and_free_sources": 6_000_000,
    "logs_journal_host_overhead_allowance": 37_000_000,
    "frozen_research_db": 0,
    "frozen_research_backup_metadata_allowance": 100_000,
}
daily = math.ceil(sum(components.values()) / 50_000_000) * 50_000_000
total, free = host["disk"]["total"], host["disk"]["free"]
floor = math.ceil(total * 0.7)
reserve = 7_000_000_000
available = free - floor - reserve
result = {
    "policy_id": "universepolicy_5f88b1dcdf20d7d19047edffab329076d7a63936f8c0916c21ffc83bcf4d56ff",
    "measured_at": int(datetime.fromisoformat(host["at"]).timestamp()),
    "validated": False,
    "rich_assets": 6,
    "minimum_runway_days": 30,
    "daily_growth_bytes": daily,
    "available_bytes": available,
    "worker_cpu_seconds_per_minute": 0.249,
    "scope": "Ranking only; existing six rich markets; no warming, execution or sizing.",
    "host_measurement": {
        "at": host["at"],
        "disk_free_bytes": free,
        "disk_total_bytes": total,
        "filesystem_used_bytes": host["disk"]["used"],
        "minimum_free_fraction": 0.7,
        "minimum_free_bytes": floor,
        "transient_reserve_bytes": reserve,
        "future_baseline_paper_ram_reserve_bytes": 3_000_000_000,
        "ram_available_bytes": host["memory"]["MemAvailable"],
        "ram_total_bytes": host["memory"]["MemTotal"],
    },
    "persistent_growth_components_bytes_per_day": components,
    "sum_growth_components_bytes_per_day": sum(components.values()),
    "round_up_contingency_bytes_per_day": daily - sum(components.values()),
    "runway_days_model": available / daily,
    "historical_unattributed_145_6MB_step": {
        "recurring_not_established": True,
        "charged_in_current_free_space": True,
        "extra_150MB_daily_stress_runway_days": available / (daily + 150_000_000),
        "rule": "Do not erase the observed historical step or call it measured recurring daily growth.",
    },
    "temporary_peak_model": {
        "research_download_plus_restore_bytes": 1_781_891_383,
        "research_local_generation_overlap_bytes": 135_614_775,
        "lab_backup_creation_reserve_bytes": math.ceil(
            host["db_files"]["/data/prism.duckdb"]["bytes"] * 1.2
        ),
        "included_in_7GB_reserve": True,
        "temporary_is_not_persistent_daily_growth": True,
    },
    "measured_growth": {
        "base_lab_db_bytes_per_day": base_db,
        "target_lab_db_bytes_per_day": target_db,
        "current_since_target_end_lab_db_bytes_per_day": current_db,
        "current_since_target_end_hours": current_hours,
        "base_micro_raw_spool_bytes_per_day": base["growth"]["microstructure"]["delta"]
        * 24
        / base["hours"],
        "base_provider_raw_bytes_per_day": base["growth"]["raw"]["delta"] * 24 / base["hours"],
        "base_runtime_log_bytes_per_day": base["growth"]["logs"]["delta"] * 24 / base["hours"],
    },
    "ranking_cost": {
        "peak_worker_mib_prior": 362,
        "unit_memory_cap_bytes": 2**30,
        "prior_cpu_seconds_per_600s": 2.49,
        "prior_discovery_requests": 72,
        "prior_discovery_wall_seconds": 36.1,
        "prior_response_bytes_per_evaluation": 288851,
        "network_response_bytes_per_day_prior": 288851 * 144,
        "requests_per_day_prior": 72 * 144,
        "request_rate_per_second_cap": 2,
        "logical_inserts_per_cycle_prior_mature": 92,
        "rich_subscription_increment": 0,
    },
    "review_required_before_setting_validated": True,
}
(ROOT / "ranking-resource-model.json").write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {
            "daily_growth_bytes": daily,
            "runway_days": result["runway_days_model"],
            "growth_components": components,
        },
        indent=2,
    )
)
