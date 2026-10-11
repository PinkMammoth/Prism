"""Offline paired worker replay/benchmark on disposable copies, with injected quotes/clocks.

PRISM_RUNTIME_ROLE=scratch PYTHONPATH=src:. python scripts/runtime_optimisation_a_worker_replay.py SNAPSHOT OUTPUT
No deployed service is contacted. A tape is replayed after a single seed has fixed run IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from tests.test_nimble_paper import Source as Geometry
from tests.test_runtime_optimisation_a import Reads, baseline, reference

from market_signal.data.store import Store
from market_signal.paper.nimble import engine, spec, triggers, worker
from market_signal.paper.nimble.data import Sources
from market_signal.paper.nimble.model import ExecutableQuote
from market_signal.paper.v2 import spec as baseline_spec
from market_signal.paper.v2.data import Observation
from market_signal.research.lab.common import canonical_json


class Metrics(Reads):
    def __getattr__(self, name):
        return getattr(self.con, name)


def memory():
    # VmHWM belongs to this exec image; getrusage can retain pre-exec fork peaks.
    stats = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] in ("VmRSS:", "VmHWM:"):
            stats[fields[0]] = int(fields[1])
    return {"rss_kib": stats["VmRSS:"], "process_peak_rss_kib": stats["VmHWM:"]}


def ledger(store):
    tables = [
        r[0]
        for r in store.con.execute("SHOW TABLES").fetchall()
        if r[0].startswith(("paper_", "context_snapshots"))
    ]
    return {
        name: hashlib.sha256(
            repr(store.con.execute(f"SELECT * FROM {name} ORDER BY ALL").fetchall()).encode()
        ).hexdigest()
        for name in tables
    }


def quotes(at, price=100):
    return [
        ExecutableQuote(asset, at.isoformat(), at.isoformat(), price - 0.01, price + 0.01)
        for asset in baseline_spec.COINS
    ]


def compact_tape(snapshot, destination, now):
    # Only offline fixture extraction: no production retention or warmup changes.
    tables = (
        "lab_software",
        "lab_prospective_studies",
        "lab_prospective_checkpoints",
        "lab_prospective_results",
        "perp_intraday_bars",
        "perp_intraday_revisions",
        "perp_funding",
        "perp_snapshots",
        "perp_oi_history",
        "context_hl_oi_hourly",
        "context_ls_ratios",
        "context_sources",
        "macro_observations",
        "context_events",
        "context_event_updates",
        "context_asset_links",
        "context_provider_runs",
        "microstructure_provider_runs",
        "microstructure_cutover",
        "microstructure_minutes",
        "microstructure_revisions",
    )
    with closing(Store(destination)) as store:
        quoted = str(snapshot.resolve()).replace("'", "''")
        store.con.execute(f"ATTACH '{quoted}' AS archive (READ_ONLY)")
        available = {
            r[0]
            for r in store.con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog='archive'"
            ).fetchall()
        }
        for table in tables:
            if table not in available:
                continue
            if table == "perp_intraday_bars":
                store.con.execute(
                    f"INSERT INTO {table} BY NAME SELECT * FROM archive.{table} WHERE "
                    "(timeframe='1h' AND open_time>=?) OR (timeframe='15m' AND open_time>=?)",
                    [
                        (now - pd.Timedelta(days=46)).to_pydatetime(),
                        (now - pd.Timedelta(days=3)).to_pydatetime(),
                    ],
                )
            else:
                store.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM archive.{table}")
        store.con.execute("DETACH archive")
    # Prove extraction retained the actual causal adapter outputs at the tape cutoffs.
    from dataclasses import asdict

    from tests.test_runtime_optimisation_a import adapter

    oracle = baseline()
    with (
        closing(Store(snapshot, read_only=True)) as original,
        closing(Store(destination)) as compact,
    ):
        for seconds in (0, 60):
            at = now + pd.Timedelta(seconds=seconds)
            for phase in (22, 23, 24):
                hs = tuple(h for h in baseline_spec.bootstrap() if h.source_phase == phase)
                a, _ = adapter(oracle, original, at)
                b, _ = adapter(oracle, compact, at)
                before = a.observations(hs, now - pd.Timedelta(days=1))
                after = b.observations(hs, now - pd.Timedelta(days=1))
                assert canonical_json([asdict(o) for o in before]) == canonical_json(
                    [asdict(o) for o in after]
                ), "compact tape must preserve inputs"
    return destination


def prepare(path, root, now, scenario):
    activation = now - pd.Timedelta(days=1)
    with closing(Store(path, root / "raw")) as store:
        engine.register(store, now=activation)
        rid = engine.create(store, now=activation)["run_id"]
        if scenario in ("one_position", "three_positions"):
            assets = ["BTC"] if scenario == "one_position" else ["BTC", "ETH", "SOL"]
            hh = next(
                h
                for h in baseline_spec.bootstrap()
                if h.source_key == "continuation_core" and h.side == "long"
            )
            at = now - pd.Timedelta(minutes=2)
            observations = [
                Observation(
                    hh.hypothesis_id,
                    asset,
                    at.isoformat(),
                    at.isoformat(),
                    True,
                    intended_side="long",
                    context={"as_of": at.isoformat()},
                )
                for asset in assets
            ]
            with store.transaction():
                engine.admit(
                    store,
                    rid,
                    observations,
                    quotes(at),
                    Geometry(),
                    {a: {"funding_rate": 0} for a in assets},
                    now=at,
                )
                later = at + pd.Timedelta(seconds=2)
                engine.monitor(
                    store,
                    rid,
                    quotes(later),
                    [],
                    [],
                    {a: {"funding_rate": 0} for a in assets},
                    now=later,
                )
            assert len(engine.positions(store, rid)) == len(assets)
        if scenario in ("quiet_minute", "duplicate_retry"):
            source = Sources(store, now)
            source.run_id = rid
            with store.transaction():
                observations = source.observations(
                    baseline_spec.bootstrap(), activation, assets=list(baseline_spec.COINS)
                )
                engine.admit(store, rid, observations, quotes(now), source, {}, now=now)
        origins = {
            "due_15m_micro": ["microstructure"],
            "due_1h": ["technical", "positioning"],
            "duplicate_retry": ["microstructure"],
            "event_wake": ["context"],
        }.get(scenario, [])
        for origin in origins:
            triggers.enqueue(store, origin, "runtime-a-" + scenario, list(baseline_spec.COINS), now)
    worker.publish(worker.marker(path), {"run_id": rid})
    return rid


def replay(path, rid, now, legacy):
    oracle = baseline()
    old_sources = reference("baseline_nimble_data", "market_signal.paper.nimble").Sources
    old_sources.__bases__ = (oracle.Sources,)
    old_spec = reference("baseline_spec", "market_signal.paper.v2")
    old_policy = reference("baseline_nimble_spec", "market_signal.paper.nimble")
    old_policy.baseline = old_spec
    all_counts = []
    original_store = Store

    def opened(*args, **kwargs):
        st = original_store(*args, **kwargs)
        counts = Metrics(st.con)
        st.con = counts
        all_counts.append(counts)
        return st

    with ExitStack() as patches:
        patches.enter_context(patch.object(worker, "Store", opened))
        patches.enter_context(
            patch.object(worker, "default_root", lambda db: Path(db).parent / "microstructure")
        )
        patches.enter_context(
            patch.object(
                worker, "time", SimpleNamespace(perf_counter=lambda: 0.0, process_time=lambda: 0.0)
            )
        )
        patches.enter_context(patch.object(worker, "require_authoritative", lambda db: None))
        patches.enter_context(patch.object(worker, "runtime_id", lambda: "offline-replay"))
        patches.enter_context(
            patch.object(
                worker,
                "quotes_for_store",
                lambda store, at: (
                    quotes(at, 102 if at >= now + pd.Timedelta(seconds=60) else 100),
                    {a: {"funding_rate": 0} for a in baseline_spec.COINS},
                ),
            )
        )
        patches.enter_context(
            patch.object(
                worker,
                "live_cache",
                lambda root, at, **kw: (
                    quotes(at, 102 if at >= now + pd.Timedelta(seconds=60) else 100),
                    {},
                ),
            )
        )
        patches.enter_context(patch.object(worker, "Sources", old_sources if legacy else Sources))
        if legacy:
            patches.enter_context(patch.object(baseline_spec, "bootstrap", old_spec.bootstrap))
            patches.enter_context(
                patch.object(
                    spec, "canonical_definition", lambda: canonical_json(old_policy.definition())
                )
            )
        obj = worker.Worker(path, run_id=rid)
        results = []
        cpu, wall = time.process_time(), time.perf_counter()
        for seconds in (0, 2, 60, 62):
            at = now + pd.Timedelta(seconds=seconds)
            result = obj.tick(now=at)
            assert result["state"] in ("OK", "IDLE"), result
            results.append(result)
        sample = {
            "cpu_s": time.process_time() - cpu,
            "wall_s": time.perf_counter() - wall,
            "selects": sum(len(c.queries) for c in all_counts),
            "query_shapes": len({q for c in all_counts for q in c.queries}),
            "rows_returned": sum(c.rows for c in all_counts),
            "statements_other_than_select": sum(c.writes for c in all_counts),
            "write_statements": sum(c.mutations for c in all_counts),
            **memory(),
        }
    with closing(Store(path, read_only=True)) as st:
        digest = ledger(st)
    return results, digest, sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    if args.run_id:
        run_samples = []
        first_result = None
        with tempfile.TemporaryDirectory(prefix="prism-runtime-a-child-") as directory:
            for repetition in range(6):
                active = Path(directory) / "active.duckdb"
                shutil.copyfile(args.snapshot, active)
                outputs, digest, sample = replay(
                    active, args.run_id, pd.Timestamp("2026-10-10T08:00:00Z"), args.legacy
                )
                causal_result = (outputs, digest)
                if first_result is None:
                    first_result = causal_result
                else:
                    assert causal_result == first_result, "repeat must preserve complete evidence"
                if repetition:
                    run_samples.append(sample)
        args.output.write_text(
            json.dumps({"outputs": outputs, "digest": digest, "samples": run_samples})
        )
        return
    now = pd.Timestamp("2026-10-10T08:00:00Z")
    result = {
        "snapshot_sha256": hashlib.file_digest(args.snapshot.open("rb"), "sha256").hexdigest(),
        "rss_method": "Linux /proc/self/status VmRSS and VmHWM of each isolated exec image",
        "as_of": now.isoformat(),
        "warm_repetitions": 5,
        "tape_offsets_seconds": [0, 2, 60, 62],
        "scenarios": {},
    }
    with tempfile.TemporaryDirectory(prefix="prism-runtime-a-worker-") as folder:
        root = Path(folder)
        tape = compact_tape(args.snapshot, root / "tape.duckdb", now)
        for scenario in (
            "quiet_minute",
            "due_15m_micro",
            "due_1h",
            "event_wake",
            "one_position",
            "three_positions",
            "duplicate_retry",
        ):
            seed = root / "seed.duckdb"
            shutil.copyfile(tape, seed)
            rid = prepare(seed, root, now, scenario)
            samples = {"before": [], "after": []}
            outputs = {}
            for name, legacy in [("before", True), ("after", False)]:
                child_result = root / (name + ".json")
                command = [sys.executable, __file__, str(seed), str(child_result), "--run-id", rid]
                if legacy:
                    command.append("--legacy")
                subprocess.run(command, check=True)
                measured = json.loads(child_result.read_text())
                outputs[name] = (measured["outputs"], measured["digest"])
                samples[name] = measured["samples"]
            assert outputs["before"] == outputs["after"], scenario
            result["scenarios"][scenario] = {
                "exact_worker_results_and_ledger": True,
                "warm_samples": samples,
                **{
                    name: {key: statistics.median(v[key] for v in values) for key in values[0]}
                    for name, values in samples.items()
                },
            }
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(scenario, result["scenarios"][scenario], flush=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
