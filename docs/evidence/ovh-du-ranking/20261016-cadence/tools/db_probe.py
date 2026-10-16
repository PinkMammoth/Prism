"""Read-only Lab counts and deterministic ranking replay; no stored record bodies exported."""

import gzip
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime

sys.path.insert(0, "/opt/prism/current/src")
from market_signal.data.store import Store, authority_claim
from market_signal.universe.engine import Market, State, evaluate, rank
from market_signal.universe.policy import CORE, POLICY


def collect():
    started = time.monotonic()
    out = {"at": datetime.now(UTC).isoformat()}
    store = Store("/data/prism.duckdb", read_only=True, lock_timeout=20)
    try:
        c = store.con
        out["authority"] = authority_claim(c)
        out["paper_rows"] = {
            t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for (t,) in c.execute("SHOW TABLES").fetchall()
            if t.startswith("paper_")
            and t not in ("paper_nimble_policies", "paper_nimble_triggers")
        }
        out["sizing_tables"] = {
            t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for (t,) in c.execute("SHOW TABLES").fetchall()
            if "cohort" in t
        }
        out["cycles"] = c.execute(
            "SELECT job,trigger,started_at,finished_at,status FROM runtime_cycles WHERE started_at>='2026-10-16T06:00:00Z' ORDER BY started_at"
        ).fetchall()
        out["fred"] = c.execute(
            "SELECT provider,status,started_at,finished_at,received,new_updates FROM context_provider_runs WHERE provider IN ('fred_calendar','fred_actuals') AND started_at>='2026-10-16T00:00:00Z' ORDER BY started_at"
        ).fetchall()
        out["micro_frontiers"] = c.execute(
            "SELECT coin,count(*),max(minute_open) FROM microstructure_minutes GROUP BY 1 ORDER BY 1"
        ).fetchall()
        out["bars"] = c.execute(
            "SELECT coin,timeframe,max(close_time) FROM perp_intraday_bars WHERE source='hyperliquid' AND timeframe IN ('15m','1h') GROUP BY 1,2 ORDER BY 1,2"
        ).fetchall()
        out["outcomes"] = c.execute(
            "SELECT count(*),coalesce(sum(octet_length(encode(CAST(payload AS VARCHAR)))),0) FROM universe_outcomes"
        ).fetchone()
        evaluations = c.execute(
            "SELECT evaluation_id,evaluated_at,payload FROM universe_evaluations ORDER BY evaluated_at"
        ).fetchall()
        out["evaluation_count"] = len(evaluations)
        out["evaluation_payload_bytes"] = sum(len(row[2]) for row in evaluations)
        # Materialize the snapshot, then release DuckDB before CPU-only replay.
        # The observer must not hold a live reader lock for all historical replays.
        store.close()
        store = None
        out["snapshot_seconds"] = time.monotonic() - started
        replay_started = time.monotonic()
        out["evaluations"] = []
        previous = {}
        for eid, _at, payload in evaluations:
            ev = json.loads(gzip.decompress(payload))
            markets = [Market(**r["market"]) for r in ev["rows"]]
            ranked = rank(markets, ev["at"])
            replay = evaluate(
                markets,
                {a: State(**v) for a, v in previous.items()},
                ev["at"],
                ev["pins"],
                warming=False,
            )
            fields = (
                "asset",
                "rank",
                "satellite_rank",
                "score",
                "components",
                "reasons",
                "execution_reasons",
                "cost",
            )
            actual = [{k: r[k] for k in fields} for r in ev["rows"]]
            computed = [{k: r[k] for k in fields} for r in ranked]
            # BELOW_RANK_CUTOFF and TIER_CAPACITY are transition annotations, checked by full replay.
            actual_rank = [
                {
                    **r,
                    "execution_reasons": [
                        x
                        for x in r["execution_reasons"]
                        if x not in ("BELOW_RANK_CUTOFF", "TIER_CAPACITY")
                    ],
                }
                for r in actual
            ]
            missing = Counter(
                k
                for m in markets
                for k in (
                    "rv_bps",
                    "range_bps",
                    "return_bps",
                    "oi_change",
                    "spread_bps",
                    "depth_usd",
                    "trades_per_minute",
                )
                if getattr(m, k) is None
            )
            checks = {
                "scores_and_ranks_match": actual_rank == computed,
                "states_match": replay["states"] == ev["states"],
                "changes_match": replay["changes"] == ev["changes"],
                "core_only_plan": set(ev["desired_subscriptions"]) == set(CORE),
                "no_pins": not ev["pins"],
                "no_satellite_entry_eligible": not any(
                    r["entry_eligible"] for r in ev["rows"] if r["asset"] not in CORE
                ),
                "no_satellite_rich_since": not any(
                    s["rich_since"] is not None for a, s in ev["states"].items() if a not in CORE
                ),
                "ranking_only": ev["stage"] == "RANKING ONLY",
                "policy_matches": ev["policy_id"] == POLICY.identity,
                "rank_deterministic_reversed_input": rank(list(reversed(markets)), ev["at"])
                == ranked,
                "ranking_independent_of_pins": [
                    {k: r[k] for k in ("asset", "rank", "score", "components")}
                    for r in evaluate(markets, {}, ev["at"], {"BTC": ["AUDIT_ONLY"]})["rows"]
                ]
                == [{k: r[k] for k in ("asset", "rank", "score", "components")} for r in ranked],
                "deployer_quarantined": all(not m.supported for m in markets if ":" in m.asset),
                "inactive_excluded": all(
                    "MARKET_INACTIVE" in r["reasons"] and r["rank"] is None
                    for r in ev["rows"]
                    if not r["market"]["active"]
                ),
            }
            out["evaluations"].append(
                {
                    "id": eid,
                    "at": ev["at"],
                    "discovered": ev["discovered_count"],
                    "candidates": len(ev["candidates"]),
                    "candidate_assets": ev["candidates"],
                    "light_collection": ev["light_collection"],
                    "source_error_classes": dict(
                        Counter(v.split(":")[0] for v in ev["source_errors"].values())
                    ),
                    "source_error_assets": sorted(ev["source_errors"]),
                    "active": sum(m.active for m in markets),
                    "native_supported": sum(m.active and m.supported for m in markets),
                    "deployer": sum(":" in m.asset for m in markets),
                    "inactive": sum(not m.active for m in markets),
                    "healthy": sum(m.healthy for m in markets),
                    "lightweight_ready": sum(m.warmup.get("lightweight", False) for m in markets),
                    "missing_input_counts": dict(missing),
                    "payload_bytes": len(payload),
                    "discovery_to_commit_seconds": ev["at"] - min(m.observed_at for m in markets),
                    "checks": checks,
                }
            )
            previous = ev["states"]
    finally:
        if store is not None:
            store.close()
    out["replay_seconds"] = time.monotonic() - replay_started
    out["read_seconds"] = time.monotonic() - started
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
