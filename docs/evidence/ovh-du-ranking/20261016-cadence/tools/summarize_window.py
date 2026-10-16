"""Analyze actual observer samples; no simulation and no host mutations."""

import argparse
import json
import math
import statistics
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {
        "min": values[0],
        "max": values[-1],
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": values[min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)],
    }


def iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat() if at is not None else None


def summarize(root):
    samples = [json.loads(line) for line in (root / "samples.jsonl").read_text().splitlines()]
    window = json.loads((root / "window.json").read_text())
    start = datetime.fromisoformat(window["start"]).timestamp()
    samples = [s for s in samples if s["epoch"] >= start]
    first, last = samples[0], samples[-1]
    events = last["worker_events"]
    evs = [e for e in last.get("db", {}).get("evaluations", []) if e["at"] >= start]
    by_id = {e["id"]: e for e in evs}
    rows = []
    previous = None
    for event in events:
        t = event.get("telemetry") or {}
        ev = by_id.get(event.get("evaluation_id"), {})
        evidence = t.get("evidence_clock")
        transaction = t.get("ranking_db_commit_completed_at")
        prior = previous or {}
        row = {
            "evaluation_id": event.get("evaluation_id"),
            "status": event["status"],
            "scheduled_deadline": iso(event["scheduled_deadline"]),
            "evaluation_start": iso(event["evaluation_started_at"]),
            "refresh_start": iso(t.get("refresh_started_at")),
            "fetch_start": iso(t.get("source_fetch_started_at")),
            "refresh_complete": iso(t.get("source_refresh_completed_at")),
            "evidence_clock": iso(evidence),
            "rank_complete": iso(t.get("ranking_computed_at")),
            "ranking_transaction_complete": iso(transaction),
            "outcomes_transaction_complete": iso(t.get("outcomes_db_commit_completed_at")),
            "next_deadline": iso(event["next_scheduled_deadline"]),
            "evidence_gap_seconds": t.get("continuity_gap_seconds"),
            "previous_evidence_clock": iso(t.get("previous_evidence_clock")),
            "gap_in_window": t.get("previous_evidence_clock") is not None
            and t["previous_evidence_clock"] >= start,
            "completed_ranking_transaction_gap_seconds": transaction - prior["transaction"]
            if transaction is not None and prior.get("transaction") is not None
            else None,
            "start_lateness_seconds": event["evaluation_started_at"] - event["scheduled_deadline"],
            "fetch_seconds": t["source_refresh_completed_at"] - t["source_fetch_started_at"]
            if t
            else None,
            "ranking_transaction_seconds": transaction - t["ranking_db_commit_started_at"]
            if transaction is not None
            else None,
            "outcomes_transaction_seconds": t["outcomes_db_commit_completed_at"]
            - t["outcomes_db_commit_started_at"]
            if t
            else None,
            "child_cpu_seconds": event.get("child_cpu_seconds"),
            "child_peak_rss_kib_cumulative": event.get("children_peak_rss_kib_since_worker_start"),
            "candidate_count": ev.get("candidates"),
            "candidate_assets": ev.get("candidate_assets"),
            "discovered": ev.get("discovered"),
            "active": ev.get("active"),
            "deployer": ev.get("deployer"),
            "inactive": ev.get("inactive"),
            "healthy": ev.get("healthy"),
            "lightweight_ready": ev.get("lightweight_ready"),
            "source_errors": event.get("source_error_count"),
            "api": t.get("api"),
            "missingness": ev.get("missing_input_counts"),
            "checks": ev.get("checks"),
            "skipped_deadlines": event.get("skipped_deadlines"),
            "missed_before_start": event.get("missed_before_start"),
        }
        rows.append(row)
        previous = {"transaction": transaction}
    duration = last["epoch"] - first["epoch"]
    db0, db1 = first.get("db", {}), last.get("db", {})
    measurements = {
        "duration_seconds": duration,
        "available_ram_bytes": stats([s["memory"]["MemAvailable"] for s in samples]),
        "disk_free_bytes": stats([s["disk"]["free"] for s in samples]),
        "whole_host_used_growth_bytes": first["disk"]["free"] - last["disk"]["free"],
        "db_file_growth_bytes": last["db_bytes"] - first["db_bytes"],
        "evaluation_payload_bytes": db1.get("evaluation_payload_bytes", 0)
        - db0.get("evaluation_payload_bytes", 0),
        "evaluation_payload_bytes_per_evaluation": stats([e["payload_bytes"] for e in evs]),
        "outcome_count_and_payload_bytes_before": db0.get("outcomes"),
        "outcome_count_and_payload_bytes_after": db1.get("outcomes"),
        "cpu_busy_percent": stats([s.get("cpu_busy_percent") for s in samples]),
        "cpu_steal_percent": stats([s.get("cpu_steal_percent") for s in samples]),
        "load_1m": stats([s["load"][0] for s in samples]),
        "load_5m": stats([s["load"][1] for s in samples]),
        "worker_child_cpu_seconds": stats([r["child_cpu_seconds"] for r in rows]),
        "worker_child_cpu_seconds_total": sum(r["child_cpu_seconds"] or 0 for r in rows),
        "worker_cgroup_memory_peak_bytes": max(
            int(s["units"]["prism-universe"].get("memory.peak", 0)) for s in samples
        ),
        "worker_cgroup_cpu_seconds": (
            int(last["units"]["prism-universe"]["CPUUsageNSec"])
            - int(first["units"]["prism-universe"]["CPUUsageNSec"])
        )
        / 1e9,
        "requests_total": sum((r["api"] or {}).get("requests", 0) for r in rows),
        "captured_response_body_bytes_total": sum(
            (r["api"] or {}).get("captured_response_body_bytes", 0) for r in rows
        ),
        "ranking_transaction_seconds": stats([r["ranking_transaction_seconds"] for r in rows]),
        "outcomes_transaction_seconds": stats([r["outcomes_transaction_seconds"] for r in rows]),
        "max_evidence_gap_seconds": max(
            (
                r["evidence_gap_seconds"]
                for r in rows
                if r["gap_in_window"] and r["evidence_gap_seconds"] is not None
            ),
            default=None,
        ),
        "max_completed_transaction_gap_seconds": max(
            (
                r["completed_ranking_transaction_gap_seconds"]
                for r in rows
                if r["completed_ranking_transaction_gap_seconds"] is not None
            ),
            default=None,
        ),
        "max_sample_seconds": max(
            (b["epoch"] - a["epoch"] for a, b in pairwise(samples)), default=0
        ),
        "all_ranking_replay_checks_pass": all(all(e["checks"].values()) for e in evs),
        "all_stop_conditions": [e for s in samples for e in s.get("stop_conditions", [])],
        "observer_read_error_samples": sum("db_error_type" in s for s in samples),
        "maximum_free_source_backlog": max(s["free_ingest"].get("backlog") or 0 for s in samples),
        "core_collector_pids": sorted(
            {s["units"]["prism-microstructure"]["MainPID"] for s in samples}
        ),
        "core_collector_restart_counts": sorted(
            {s["units"]["prism-microstructure"]["NRestarts"] for s in samples}
        ),
        "sample_count": len(samples),
    }
    return {
        "window": window,
        "first_sample": first["at"],
        "last_sample": last["at"],
        "evaluations": rows,
        "measurements": measurements,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), indent=2))
