"""Continue ranking safety after the completed window; no new observation gate or job starts."""

import argparse
import contextlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import ranking_watch as watch

from market_signal.data.store import Store, authority_claim
from market_signal.universe import budget as resource_budget
from market_signal.universe import ledger
from market_signal.universe.policy import POLICY


def decision_checks(ev):
    return {
        "ranking_only": ev["stage"] == "RANKING ONLY",
        "policy_matches": ev["policy_id"] == POLICY.identity,
        "core_only_plan": set(ev["desired_subscriptions"]) == set(watch.CORE),
        "no_pins": not ev["pins"],
        "no_satellite_entry": not any(
            r["entry_eligible"] for r in ev["rows"] if r["asset"] not in watch.CORE
        ),
        "no_satellite_rich_since": not any(
            s["rich_since"] is not None for a, s in ev["states"].items() if a not in watch.CORE
        ),
    }


def light_db():
    # Full arithmetic/history replay was proved in the bounded window and remains off-host.
    # Keep only current safety assertions in this continuing guard, closing the reader promptly.
    with contextlib.redirect_stdout(sys.stderr):
        store = Store("/data/prism.duckdb", read_only=True, lock_timeout=20)
        try:
            c = store.con
            tables = [r[0] for r in c.execute("SHOW TABLES").fetchall()]
            out = {"authority": authority_claim(c)}
            out["paper_rows"] = {
                t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for t in tables
                if t.startswith("paper_")
                and t not in ("paper_nimble_policies", "paper_nimble_triggers")
            }
            out["sizing_tables"] = {
                t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for t in tables
                if "cohort" in t
            }
            out["cycles"] = c.execute(
                "SELECT job,trigger,started_at,finished_at,status FROM runtime_cycles "
                "WHERE started_at>=current_timestamp-INTERVAL '1 day' ORDER BY started_at"
            ).fetchall()
            ev = ledger.latest(store)
            assert ev, "accepted ranking evidence must exist"
            out["evaluations"] = [
                {"id": ev["evaluation_id"], "at": ev["at"], "checks": decision_checks(ev)}
            ]
            return out
        finally:
            store.close()


def additional_concerns(s, budget):
    errors = []
    if s["profile"]["digest"] != "e7571ad1a2a35fb42dc8c1ea77759e31887a1c7679445561664c1dee68c12207":
        errors.append("reviewed v2 profile digest mismatch")
    required = 0.7 * s["disk"]["total"] + 7_000_000_000 + 30 * budget["daily_growth_bytes"]
    if s["disk"]["free"] < required:
        errors.append("current measured growth runway below30days")
    if s["epoch"] - budget["measured_at"] > POLICY.budget_max_age_seconds:
        errors.append("ranking resource receipt expired")
    evs = s.get("db", {}).get("evaluations", [])
    if evs and s["epoch"] - evs[-1]["at"] > POLICY.light_max_gap_seconds:
        errors.append("accepted ranking evidence stale >900s")
    for event in s.get("worker_events", []):
        timing = event.get("telemetry") or {}
        for phase in ("ranking", "outcomes"):
            begin = timing.get(phase + "_db_commit_started_at")
            end = timing.get(phase + "_db_commit_completed_at")
            if begin is not None and end is not None and end - begin > 30:
                errors.append(phase + " DB transaction exceeded30s")
    return errors


def require_completed_window(final):
    if not (
        final["clean"] and final["evaluations"] >= 13 and final["accepted_span_seconds"] >= 7200
    ):
        raise ValueError("13 fresh genuine evaluations spanning7200s must pass first")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--budget-sha", required=True)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    final = json.loads((args.window / "final.json").read_text())
    require_completed_window(final)
    baseline = json.loads((args.window / "baseline.json").read_text())
    baseline["budget_sha256"] = args.budget_sha
    start = datetime.fromisoformat(final["start"]).timestamp()
    budget = resource_budget.validate(
        datetime.now(UTC), path=Path("/data/state/du-budget.json"), rich_assets=6
    )
    assert budget["rich_assets"] == 6 and budget["release_revision"] == baseline["release_revision"]
    watch.db_collect = light_db
    previous = None
    sustained = {}
    while True:
        s = watch.sample(
            start,
            cached_db=previous.get("db") if previous else None,
            last_db_at=previous.get("db_audit_at", 0) if previous else 0,
            last_event_count=previous.get("db_audit_event_count", -1) if previous else -1,
        )
        errors = watch.concerns(s, baseline, start) + additional_concerns(s, budget)
        conditions = {
            "RAM below40% for5min": (
                s["memory"]["MemAvailable"] < 0.4 * s["memory"]["MemTotal"],
                300,
            ),
            "core feed unhealthy >60s": (
                not s["micro"]["connected"] or (s["micro"].get("last_message_age_s") or 0) > 60,
                60,
            ),
            "load above6 for5min": (s["load"][1] > 6, 300),
        }
        if previous:
            delta = [b - a for a, b in zip(previous["cpu_jiffies"], s["cpu_jiffies"], strict=True)]
            total = sum(delta)
            busy = 100 * (total - delta[3] - delta[4]) / total if total else 0
            steal = 100 * delta[7] / total if total else 0
            s.update(cpu_busy_percent=busy, cpu_steal_percent=steal)
            conditions.update(
                {
                    "CPU busy >80% for10min": (busy > 80, 600),
                    "CPU steal >5% for5min": (steal > 5, 300),
                }
            )
            cpu = (
                (
                    int(s["units"]["prism-universe"]["CPUUsageNSec"])
                    - int(previous["units"]["prism-universe"]["CPUUsageNSec"])
                )
                / 1e9
                / (s["epoch"] - previous["epoch"])
                * 60
            )
            if cpu >= 30:
                errors.append("radar CPU budget exceeded")
        for name, (condition, seconds) in conditions.items():
            if condition:
                sustained[name] = sustained.get(name, s["epoch"])
                if s["epoch"] - sustained[name] >= seconds:
                    errors.append(name)
            else:
                sustained.pop(name, None)
        s["stop_conditions"] = errors
        s["mode"] = "continuous safety only; completed scientific window preserved"
        watch.save(args.root, "latest.json", s)
        if args.check:
            assert not errors, errors
            print(json.dumps({"checks_only": True, "safe": True, "no_new_window": True}))
            return
        if errors:
            watch.save(args.root, "stop.json", {"at": s["at"], "reason": errors})
            watch.rollback(args.root, errors)
            return
        previous = s
        time.sleep(30)


if __name__ == "__main__":
    main()
