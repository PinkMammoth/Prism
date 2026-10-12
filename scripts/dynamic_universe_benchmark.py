"""Isolated Tranche A workload measurements; satellite rows are explicitly cloned load.

No Tranche B imports, no production mutation, no scientific inference. Each size gets
its own process/RSS peak. Full adapter runs on a copied production backup; cloned BTC
history represents scaling workload only. Network extrapolations are labelled separately.
"""

from __future__ import annotations

import argparse
import gzip
import json
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from market_signal.data.store import Store
from market_signal.microstructure.engine import Engine
from market_signal.paper.nimble.data import Sources
from market_signal.paper.v2 import spec
from market_signal.universe.engine import Market, evaluate, rank
from market_signal.universe.policy import CORE, POLICY


class Reads:
    def __init__(self, con):
        self.con, self.selects, self.rows = con, 0, 0

    def execute(self, sql, params=None):
        self.selects += sql.lstrip().upper().startswith("SELECT")
        self.cursor = self.con.execute(sql, params or [])
        return self

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.rows += len(rows)
        return rows

    def fetchone(self):
        row = self.cursor.fetchone()
        self.rows += row is not None
        return row

    def df(self):
        df = self.cursor.df()
        self.rows += len(df)
        return df

    def __getattr__(self, key):
        return getattr(self.cursor, key)


class Proxy:
    def __init__(self, store, reads):
        self.store, self.con = store, reads

    def __getattr__(self, key):
        return getattr(self.store, key)


def synthetic_market(asset, now):
    return Market(
        asset,
        now,
        now - 10 * 86400,
        100,
        50e6,
        20e6,
        spread_bps=2,
        depth_usd=100000,
        rv_bps=200,
        range_bps=400,
        return_bps=150,
        volume_acceleration=1.2,
        oi_change=0.02,
        trades_per_minute=20,
        warmup={"lightweight": True},
        quote_at=now,
    )


def child(snapshot, n, *, mature=False):
    assets = [*CORE, *[f"LOAD{i:02}" for i in range(max(0, n - 6))]][:n]
    now = 1_800_000_000
    markets = [synthetic_market(a, now) for a in assets]
    cpu, wall = time.process_time(), time.perf_counter()
    for _ in range(100):
        rank(markets, now)
    result = {
        "assets": n,
        "ranking_cpu_ms": (time.process_time() - cpu) * 10,
        "ranking_wall_ms": (time.perf_counter() - wall) * 10,
    }
    rows, journal = [], []
    engine = Engine(tuple(assets), rows.append, run_id="load")
    start = now * 1000
    engine.start(start)
    engine.feed(("O", start, "load"))
    for a in assets:
        for feed in ("trades", "book5", "book20", "ctx"):
            engine.feed(("A", start, a, feed))
    cpu, wall = time.process_time(), time.perf_counter()
    for second in range(65):
        t = start + second * 1000
        for a in assets:
            engine.feed(("F", t, a, t, 99.99, 100.01, 1e5, 1e5))
            engine.feed(("D", t, a, t, 5e5, 5e5))
            engine.feed(("C", t, a, 10000, 100, 100, 0, 99.98, 100.02))
            for trade in range(2):
                engine.feed(("T", t, a, t, 100, 10, trade == 0, second * 2 + trade, None))
    engine.tick(start + 70000)
    journal = engine.journal
    result.update(
        collector_cpu_s_per_simulated_65s=time.process_time() - cpu,
        collector_wall_s=time.perf_counter() - wall,
        collector_events=len(journal),
        micro_minute_rows=sum(r["kind"] == "minute" for r in rows),
        spool_bytes_per_minute=sum(len(json.dumps(r)) + 1 for r in rows),
        compressed_journal_bytes_per_simulated_65s=len(gzip.compress(json.dumps(journal).encode())),
        network_normalized_event_bytes_per_simulated_65s=len(json.dumps(journal).encode()),
        network_note="Synthetic normalized tape: 2 fills/s and 1 fast/full book+ctx/s per asset; not live websocket wire rate",
    )
    if n <= 20:
        with tempfile.TemporaryDirectory(prefix="prism-universe-bench-") as temp:
            db = Path(temp) / "load.duckdb"
            shutil.copyfile(snapshot, db)
            s = Store(db)
            s.con.execute("SET threads=1")
            asof = s.con.execute("SELECT max(ingested_at) FROM microstructure_minutes").fetchone()[
                0
            ]
            if asof is None:
                raise ValueError("benchmark backup has no microstructure history")
            at = pd.Timestamp(asof)
            for table in ("microstructure_minutes", "perp_intraday_bars", "context_hl_oi_hourly"):
                columns = [r[1] for r in s.con.execute(f"PRAGMA table_info('{table}')").fetchall()]
                if "coin" not in columns:
                    continue
                for asset in assets[6:]:
                    expressions = ["?" if c == "coin" else c for c in columns]
                    s.con.execute(
                        f"INSERT INTO {table} SELECT {','.join(expressions)} FROM {table} WHERE coin='BTC'",
                        [asset],
                    )
            if mature:
                from market_signal.microstructure import definitions as md
                from market_signal.research.microdir.synthetic import Scenario
                from market_signal.research.microdir.synthetic import market as micro_market

                frame = micro_market(Scenario("engineering_load", days=8, coins=("BTC",)))["BTC"]
                frame["feature_version"], frame["revision"], frame["flags"] = (
                    md.FEATURE_VERSION,
                    0,
                    None,
                )
                frame["trade_cov"], frame["book_samples"], frame["depth20_samples"] = 1.0, 60, 60
                frame["n_dup"], frame["n_late"], frame["session_id"], frame["content_sha"] = (
                    0,
                    0,
                    "synthetic_load",
                    "synthetic_load",
                )
                frame["ingested_at"] = frame["finalized_at"]
                for column in md.MINUTE_COLUMNS:
                    if column not in frame:
                        frame[column] = None
                s.con.execute("DELETE FROM microstructure_minutes")
                s.con.execute(
                    "UPDATE microstructure_cutover SET first_minute=?",
                    [frame["minute_open"].min().to_pydatetime()],
                )
                for asset in assets:
                    frame["coin"] = asset
                    s.con.register("mature_seed", frame[list(md.MINUTE_COLUMNS)])
                    s.con.execute("INSERT INTO microstructure_minutes SELECT * FROM mature_seed")
                    s.con.unregister("mature_seed")
                at = frame["finalized_at"].max() + pd.Timedelta(minutes=1)
                result["mature_fixture"] = (
                    "8 days synthetic micro per asset; no paper activation/history claim; technical/positioning unavailable at January fixture clock"
                )
            samples = []
            for _i in range(3):
                reads = Reads(s.con)
                source = Sources(Proxy(s, reads), at)
                source.run_id = "load-not-a-trading-run"
                cpu, wall = time.process_time(), time.perf_counter()
                obs = source.observations(
                    spec.bootstrap(), at - pd.Timedelta(days=1), assets=assets
                )
                samples.append(
                    {
                        "cpu_s": time.process_time() - cpu,
                        "wall_s": time.perf_counter() - wall,
                        "db_selects": reads.selects,
                        "db_rows_read": reads.rows,
                        "observations": len(obs),
                    }
                )
            result["full_tranche_a_adapter"] = {
                k: statistics.median(x[k] for x in samples[1:]) for k in samples[0]
            }
            result["full_tranche_a_adapter"]["as_of"] = at.isoformat()
            result["full_tranche_a_adapter"]["note"] = (
                "28 exact frozen predicates; cloned BTC satellite workload, not native satellite evidence; no cross-run shared cache"
            )
            result["full_tranche_a_adapter"]["db_writes"] = 0
            s.con.execute("CHECKPOINT")
            result["duckdb_bytes_load_fixture"] = db.stat().st_size
            s.close()
    result["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("snapshot", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--child", type=int)
    p.add_argument("--mature", action="store_true")
    args = p.parse_args()
    if args.child:
        print(json.dumps(child(args.snapshot, args.child, mature=args.mature)))
        return
    results = []
    for n in (6, 12, 18, 20, 30):
        proc = subprocess.run(
            [sys.executable, __file__, str(args.snapshot), str(args.output), "--child", str(n)],
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(json.loads(proc.stdout))
    mature = []
    for n in (12, 18):
        proc = subprocess.run(
            [
                sys.executable,
                __file__,
                str(args.snapshot),
                str(args.output),
                "--child",
                str(n),
                "--mature",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        mature.append(json.loads(proc.stdout))
    snapshot = json.loads(
        Path("docs/evidence/dynamic-universe-v1/snapshot-sanity.json").read_text()
    )
    markets = [Market(**r) for r in snapshot["markets_data"]]
    at = snapshot["received_at"]
    cpu, wall = time.process_time(), time.perf_counter()
    for _ in range(100):
        decision = evaluate(markets, {}, at)
    full = {
        "markets": len(markets),
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "rank_cpu_ms": (time.process_time() - cpu) * 10,
        "rank_wall_ms": (time.perf_counter() - wall) * 10,
        "compressed_evaluation_bytes": len(gzip.compress(json.dumps(decision).encode())),
        "snapshot_http_requests": snapshot["http_requests"],
        "snapshot_response_bytes": snapshot["http_response_bytes"],
        "snapshot_fetch_cpu_s": snapshot["cpu_seconds"],
        "snapshot_fetch_wall_s": snapshot["wall_seconds"],
    }
    out = {
        "policy_id": POLICY.identity,
        "snapshot": str(args.snapshot),
        "sizes": results,
        "mature_micro_workloads": mature,
        "all_market_discovery": full,
        "note": "Engineering workload only. RSS is isolated process peak; storage/network extrapolations need production validation.",
    }
    args.output.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
