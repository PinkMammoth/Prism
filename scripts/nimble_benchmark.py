"""Synthetic engineering load, never scientific evidence or production account activation.

Run: PYTHONPATH=src python scripts/nimble_benchmark.py --output docs/evidence/phase27/benchmark.json
Reports full DB execution lifecycles at 10/50/100 trades/day equivalents, a filesystem-only
monitor, and 500 signal decisions. Real minute adapter cost is measured separately on a
production snapshot; these injected quotes do not claim market profitability or an SLA.
"""

from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from pathlib import Path

import pandas as pd

from market_signal.data.store import Store
from market_signal.paper.nimble import engine, spec
from market_signal.paper.nimble.model import ExecutableQuote
from market_signal.paper.nimble.worker import Worker
from market_signal.paper.v2.data import Observation

T0 = pd.Timestamp("2026-10-10T00:00:00Z")


def instant(seconds):
    return T0 + pd.Timedelta(seconds=seconds)


def quote(seconds, price=100):
    t = instant(seconds).isoformat()
    return ExecutableQuote("BTC", t, t, price - 0.01, price + 0.01)


class Geometry:
    def geometry(self, asset, o):
        return {
            "local_low": 99,
            "local_high": 101,
            "volatility_move": 0.02,
            "available_at": o.available_at,
            "microstructure": {"bid5": 1000, "ask5": 1000},
        }


def benchmark(n, root):
    db = root / f"{n}.duckdb"
    s = Store(db)
    s.con.execute("SET threads=1")
    engine.register(s, now=T0)
    rid = engine.create(s, now=T0)["run_id"]
    h = next(h for h in spec.baseline.bootstrap() if h.name == "continuation_core_long_v1")
    wall = time.perf_counter()
    cpu = time.process_time()
    latencies = []
    for i in range(n):
        offset = 60 + i * 180
        t = instant(offset).isoformat()
        o = Observation(
            h.hypothesis_id, "BTC", t, t, True, intended_side=h.side, evidence={}, context={}
        )
        start = time.perf_counter()
        with s.transaction():
            engine.admit(
                s,
                rid,
                [o],
                [quote(offset)],
                Geometry(),
                {"BTC": {"funding_rate": 0}},
                now=instant(offset),
            )
        for delay, price in ((2, 100), (60, 102), (62, 102)):
            with s.transaction():
                engine.monitor(
                    s,
                    rid,
                    [quote(offset + delay, price)],
                    contexts={"BTC": {"funding_rate": 0}},
                    now=instant(offset + delay),
                )
        latencies.append(time.perf_counter() - start)
    elapsed = time.perf_counter() - wall
    used = time.process_time() - cpu
    st = engine.state(s, rid)
    assert len(st["closed"]) == n and not st["open"]
    counts = {
        t: s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("paper_nimble_events", "paper_nimble_opportunities", "paper_nimble_theses")
    }
    payload_bytes = s.con.execute(
        "SELECT sum(length(payload)) FROM paper_nimble_events"
    ).fetchone()[0]
    s.con.execute("CHECKPOINT")
    s.close()
    return {
        "trades_per_day_equivalent": n,
        "wall_seconds": elapsed,
        "cpu_seconds": used,
        "cpu_daily_vcpu_equivalent": used / 86400,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "ledger_rows": counts,
        "event_payload_bytes": payload_bytes,
        "database_bytes": db.stat().st_size,
        "lifecycle_wall_ms_median": sorted(latencies)[len(latencies) // 2] * 1000,
        "simulated_entry_available_to_fill_seconds": 2,
        "simulated_exit_available_to_fill_seconds": 2,
        "net_pnl_synthetic_only": sum(p["net_pnl"] for p in st["closed"]),
    }


def signal_load(root):
    s = Store(root / "signals.duckdb")
    s.con.execute("SET threads=1")
    engine.register(s, now=T0)
    rid = engine.create(s, now=T0)["run_id"]
    h = spec.baseline.bootstrap()[-1]
    wall = time.perf_counter()
    cpu = time.process_time()
    for i in range(500):
        t = instant(i + 1).isoformat()
        o = Observation(
            h.hypothesis_id, "BTC", t, t, False, intended_side=h.side, evidence={}, context={}
        )
        with s.transaction():
            engine.admit(s, rid, [o], [], Geometry(), now=instant(i + 1))
    result = {
        "signals": 500,
        "wall_seconds": time.perf_counter() - wall,
        "cpu_seconds": time.process_time() - cpu,
        "opportunity_rows": s.con.execute(
            "SELECT count(*) FROM paper_nimble_opportunities"
        ).fetchone()[0],
    }
    s.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="prism-nimble-benchmark-") as temp:
        root = Path(temp)
        samples = [benchmark(n, root) for n in (10, 50, 100)]
        signals = signal_load(root)
        worker = Worker(root / "idle.duckdb")
        wall = time.perf_counter()
        cpu = time.process_time()
        for _ in range(1000):
            assert worker.tick()["db_opened"] is False
        idle = {
            "polls": 1000,
            "wall_seconds": time.perf_counter() - wall,
            "cpu_seconds": time.process_time() - cpu,
            "db_writes": 0,
        }
    result = {
        "kind": "LOCAL_SYNTHETIC_ENGINEERING_BENCHMARK",
        "policy_id": spec.policy_id(),
        "load": samples,
        "signals": signals,
        "unactivated_poll": idle,
        "limitations": [
            "injected deterministic sources",
            "scratch local disk",
            "not a profitability result",
            "not a Railway billed-resource measurement",
            "adapter/ingest cost is additional",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
