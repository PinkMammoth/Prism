"""Phase 30 frozen synthetic engineering load; never tune thresholds or claim alpha."""

from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from market_signal.context import ledger
from market_signal.context.model import GenericAttrs, Observation
from market_signal.data.store import Store
from market_signal.paper.nimble import engine, event_spec, events
from market_signal.paper.nimble.model import ExecutableQuote
from market_signal.paper.nimble.worker import Worker

T = pd.Timestamp("2026-10-10T00:00:00Z")


def q(at, price=100):
    return ExecutableQuote("BTC", at.isoformat(), at.isoformat(), price - 0.01, price + 0.01)


def reference(source, asset, seen):
    return dict(
        price=100,
        bid=99.99,
        ask=100.01,
        quote_at=(pd.Timestamp(seen) - pd.Timedelta(seconds=60)).isoformat(),
        available_at=(pd.Timestamp(seen) - pd.Timedelta(seconds=55)).isoformat(),
        volatility_move=0.02,
        local_low=99,
        local_high=101,
    )


def run(n, *, burst=False):
    with tempfile.TemporaryDirectory() as root:
        store = Store(Path(root) / "load.duckdb")
        engine.register(store, now=T, events=True)
        ids = [
            engine.create(store, events=True, variant=v, now=T)["run_id"]
            for v in ("immediate", "confirmed")
        ]
        source = SimpleNamespace(store=store, context=lambda asset: {"positioning": {}})
        wall, cpu = time.perf_counter(), time.process_time()
        with (
            patch.object(events, "reference", reference),
            patch.object(
                events,
                "confirmation",
                lambda *args: {
                    "available_at": source.now.isoformat(),
                    "observed_at": source.now.isoformat(),
                },
            ),
        ):
            for i in range(n):
                at = T + pd.Timedelta(seconds=60 + (i if burst else i * 86400 / max(n, 1)))
                source.now = at
                o = Observation(
                    source_id="coinbase_status_v1",
                    source_type="exchange_notice",
                    confidence="OFFICIAL",
                    title="Trading outage",
                    subcategory="exchange_outage",
                    entities=("coinbase",),
                    scope="exchange",
                    published_at=T + pd.Timedelta(seconds=60) if burst else at,
                    source_ref=f"https://status.coinbase.com/incidents/{0 if burst else i}",
                    dedup_key=f"statuspage:coinbase:{0 if burst else i}",
                    attributes=GenericAttrs(
                        facts={
                            "impact": "major",
                            "status": "investigating",
                            "component_states_json": json.dumps(
                                [
                                    {
                                        "id": "tjhgmj0ysvln",
                                        "name": "Trading",
                                        "status": "major_outage",
                                    }
                                ]
                            ),
                        }
                    ),
                )
                result = ledger.ingest(store, [o], now=at.to_pydatetime())
                eid = (
                    result.new_events[0]
                    if result.new_events
                    else store.con.execute(
                        "SELECT event_id FROM context_events ORDER BY first_seen_at LIMIT 1"
                    ).fetchone()[0]
                )
                trigger = {
                    "source": "context",
                    "event_ids": [eid],
                    "available_at": at.isoformat(),
                    "metadata": {},
                }
                for rid in ids:
                    with store.transaction():
                        events.evaluate(
                            store, rid, [trigger], [q(at)], source, at, {"BTC": {"funding_rate": 0}}
                        )
                        engine.monitor(
                            store,
                            rid,
                            [q(at + pd.Timedelta(seconds=2))],
                            contexts={"BTC": {"funding_rate": 0}},
                            now=at + pd.Timedelta(seconds=2),
                        )
                        if not burst:
                            engine.monitor(
                                store,
                                rid,
                                [q(at + pd.Timedelta(seconds=4), 96)],
                                now=at + pd.Timedelta(seconds=4),
                            )
                            engine.monitor(
                                store,
                                rid,
                                [q(at + pd.Timedelta(seconds=6), 96)],
                                now=at + pd.Timedelta(seconds=6),
                            )
        if n == 0:
            for minute in range(1, 61):
                at = T + pd.Timedelta(minutes=minute)
                for rid in ids:
                    with store.transaction():
                        events.evaluate(store, rid, [], [q(at)], source, at)
                        engine.monitor(store, rid, [q(at)], now=at)
        elapsed, used = time.perf_counter() - wall, time.process_time() - cpu
        erows, pbytes = store.con.execute(
            "SELECT count(*),coalesce(sum(length(payload::VARCHAR)),0) FROM paper_event_evidence"
        ).fetchone()
        ecount, ebytes = store.con.execute(
            "SELECT count(*),coalesce(sum(length(payload::VARCHAR)),0) FROM paper_nimble_events"
        ).fetchone()
        store.con.execute("CHECKPOINT")
        size = store.path.stat().st_size
        store.close()
        return dict(
            events=n,
            quiet_active_minutes_sampled=60 if n == 0 else 0,
            quiet_cpu_daily_estimate=used * 24 if n == 0 else None,
            cohorts=2,
            burst=burst,
            wall_seconds=elapsed,
            cpu_seconds=used,
            peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            evidence_rows=erows,
            execution_ledger_rows=ecount,
            payload_bytes=pbytes + ebytes,
            db_bytes=size,
            average_added_vcpu_daily=used / 86400,
            limitations="synthetic causal quotes/geometry; includes DB admission and lifecycle; excludes existing technical adapters, network/writer wait and steady production billing",
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    quiet = Worker(Path("/tmp/phase30-unactivated-benchmark/db.duckdb"))
    wall, cpu = time.perf_counter(), time.process_time()
    for _ in range(1000):
        quiet.tick(now=T)
    result = {
        "version": event_spec.VERSION,
        "policy_id": event_spec.policy_id(),
        "quiet_1000_filesystem_polls": {
            "wall_seconds": time.perf_counter() - wall,
            "cpu_seconds": time.process_time() - cpu,
            "db_writes": 0,
        },
        "loads": [run(0), run(10), run(100), run(100, burst=True)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
