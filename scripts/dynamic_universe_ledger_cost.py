"""Scratch measurement of real decision payload/readiness cost, without API or execution."""

import argparse
import gzip
import json
import resource
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from market_signal.data.store import Store
from market_signal.universe import source
from market_signal.universe.engine import Market
from market_signal.universe.policy import POLICY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", type=int, default=1)
    args = parser.parse_args()
    root = Path("docs/evidence/dynamic-universe-v1")
    snap = json.loads((root / "snapshot-sanity.json").read_text())
    markets = [Market(**m) for m in snap["markets_data"]]
    light = {m.asset for m in markets if m.quote_at is not None}
    with tempfile.TemporaryDirectory(prefix="prism-ledger-load-") as tmp:
        path = Path(tmp) / "scratch.duckdb"
        store = Store(path)
        statements = []
        store.con = Trace(store.con, statements)
        fetched = {
            "markets": markets,
            "listing_first_seen": {m.asset: m.first_seen for m in markets},
            "errors": snap["errors"],
            "light": light,
        }
        for step in range(args.history - 1):
            at = snap["received_at"] + step * POLICY.cadence_seconds
            copied = [
                replace(
                    m,
                    observed_at=m.observed_at + step * POLICY.cadence_seconds,
                    quote_at=m.quote_at + step * POLICY.cadence_seconds
                    if m.quote_at is not None
                    else None,
                    price=m.price * (1 + 0.001 * step),
                )
                for m in markets
            ]
            source.commit(store, {**fetched, "markets": copied}, at)
        statements.clear()
        step = args.history - 1
        at = snap["received_at"] + step * POLICY.cadence_seconds
        copied = [
            replace(
                m,
                observed_at=m.observed_at + step * POLICY.cadence_seconds,
                quote_at=m.quote_at + step * POLICY.cadence_seconds
                if m.quote_at is not None
                else None,
                price=m.price * (1 + 0.001 * step),
            )
            for m in markets
        ]
        cpu, wall = time.process_time(), time.perf_counter()
        decision = source.commit(store, {**fetched, "markets": copied}, at)
        result = {
            "policy_id": POLICY.identity,
            "markets": len(markets),
            "evaluations": args.history,
            "prospective_outcomes_rows": store.con.execute(
                "SELECT count(*) FROM universe_outcomes"
            ).fetchone()[0],
            "light_assets": len(light),
            "cpu_seconds": time.process_time() - cpu,
            "wall_seconds": time.perf_counter() - wall,
            "db_selects": sum(s.lstrip().upper().startswith("SELECT") for s in statements),
            "db_mutations": sum(
                s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements
            ),
            "payload_bytes": store.con.execute(
                "SELECT octet_length(payload) FROM universe_evaluations"
            ).fetchone()[0],
            "uncompressed_bytes": len(
                gzip.decompress(
                    store.con.execute(
                        "SELECT payload FROM universe_evaluations ORDER BY evaluated_at DESC LIMIT 1"
                    ).fetchone()[0]
                )
            ),
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "stage": decision["stage"],
            "note": "Empty scratch feature history; real readiness reasons and 34 portability entries. Multi-evaluation mode advances a synthetic clock and mark path to exercise outcome retention. No production, network, backfill or warmup claim. Store startup/migration excluded.",
        }
        store.con.execute("CHECKPOINT")
        result["scratch_db_bytes_with_all_empty_schemas"] = path.stat().st_size
        store.close()
    (root / ("ledger-cost.json" if args.history == 1 else "ledger-history-cost.json")).write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


class Trace:
    def __init__(self, con, statements):
        self.con, self.statements = con, statements

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return self.con.execute(sql, params or [])

    def __getattr__(self, key):
        return getattr(self.con, key)


if __name__ == "__main__":
    main()
