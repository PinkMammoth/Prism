"""Prospective isolated free-source measurement; never opens the production database."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from market_signal.context.free_sources.collector import Collector
from market_signal.context.free_sources.spool import Spool
from market_signal.context.gateway.spool import now


def run(stage=4):
    started = now()
    cpu = time.process_time()
    with tempfile.TemporaryDirectory(prefix="prism-free-benchmark-") as directory:
        root = Path(directory)
        spool = Spool(root / "spool")
        c = Collector(spool, stage=stage, jitter=lambda: 0)
        cold = c.tick(force=True)
        warm = c.tick(force=True)
        # Idle scheduling measures per-tick CPU after sources' due times are cached.
        idle_cpu = time.process_time()
        for _ in range(100):
            c.tick()
        idle_per_tick = (time.process_time() - idle_cpu) / 100
        c.close()
        proc = {
            line.split(":")[0]: line.split(":")[1].strip()
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        }
        collector_peak = int(proc["VmHWM"].split()[0]) * 1024
        collector_rss = int(proc["VmRSS"].split()[0]) * 1024
        collector_cpu = time.process_time() - cpu
        source_rows = []
        for s in c.sources:
            a, b = cold[s["id"]], warm[s["id"]]
            source_rows.append(
                dict(
                    provider=s["id"],
                    url=s["url"],
                    cadence_seconds=s["cadence_seconds"],
                    requests_day_ceiling=86400 / s["cadence_seconds"],
                    cold_status=a.get("http_status"),
                    warm_status=b.get("http_status"),
                    cold_wire_bytes=a.get("totals", {}).get("wire_bytes", 0),
                    warm_wire_bytes=b.get("totals", {}).get("wire_bytes", 0)
                    - a.get("totals", {}).get("wire_bytes", 0),
                    cold_poll_seconds=a.get("poll_wall_seconds"),
                    validators=dict(etag=b.get("etag"), last_modified=b.get("last_modified")),
                    error=b.get("error"),
                )
            )
        receipts = spool.pending()
        pub = []
        info = []
        for r in receipts:
            at = datetime.fromisoformat(r["gateway_received_at"])
            for o in r["observations"]:
                if o.get("published_at"):
                    pub.append((at - datetime.fromisoformat(o["published_at"])).total_seconds())
                information = o["attributes"]["facts"]["information_time"]
                info.append((at - datetime.fromisoformat(information)).total_seconds())
        spool_bytes = sum(p.stat().st_size for p in spool.root.rglob("*") if p.is_file())
        from market_signal.context.free_sources.ingest import drain
        from market_signal.context.work_research import collect
        from market_signal.data.store import Store

        st = Store(root / "scratch.duckdb")
        ingest_start = time.process_time()
        result = drain(st, spool)
        ingest_cpu = time.process_time() - ingest_start
        research = collect(st, now=now())
        counts = {
            table: st.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in [
                "context_events",
                "context_event_updates",
                "paper_nimble_triggers",
                "paper_nimble_positions",
                "context_work_research_pending",
            ]
        }
        st.close()
        completions = [spool.read("done", r["receipt_id"]) for r in receipts]
        local_latencies = [
            (
                datetime.fromisoformat(r["context_available_at"])
                - datetime.fromisoformat(r["gateway_received_at"])
            ).total_seconds()
            for r in completions
            if r
        ]
        return dict(
            version="free_source_benchmark_v1",
            environment="isolated_local_live_endpoints",
            started_at=started.isoformat(),
            finished_at=now().isoformat(),
            sources=source_rows,
            collector_peak_rss_bytes=collector_peak,
            collector_current_rss_bytes=collector_rss,
            collector_process_cpu_seconds=collector_cpu,
            idle_cpu_seconds_per_tick=idle_per_tick,
            ingest_cpu_seconds=ingest_cpu,
            spool_bytes=spool_bytes,
            scratch_db_bytes=(root / "scratch.duckdb").stat().st_size,
            publication_to_receipt_seconds=pub,
            information_time_to_receipt_seconds=info,
            measured_availability_to_receipt_seconds=None,
            receipt_to_context_seconds=local_latencies,
            drain=result,
            research=research,
            counts=counts,
            notes=[
                "Cold and warm fetches are short samples, not a 24h bandwidth measurement.",
                "Source publication/update times do not establish first public availability.",
                "Initial accepted items are prospective Prism receipts, never backdated.",
            ],
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stage", type=int, default=4)
    args = p.parse_args()
    result = run(args.stage)
    args.output.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in (
                    "sources",
                    "publication_to_receipt_seconds",
                    "information_time_to_receipt_seconds",
                )
            },
            indent=2,
        )
    )
