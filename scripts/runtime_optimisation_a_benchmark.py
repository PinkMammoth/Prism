"""Offline paired adapter benchmark. Never point this at a live writer database.

Usage: PYTHONPATH=src python scripts/runtime_optimisation_a_benchmark.py SNAPSHOT OUTPUT
Copies the snapshot before migrations; timings exclude copy/migration/import/profiling.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import resource
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from tests.test_runtime_optimisation_a import adapter, baseline

from market_signal.data.store import Store
from market_signal.paper.v2 import data, spec
from market_signal.research.lab.common import canonical_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--as-of", default="2026-10-10T07:50:00Z")
    parser.add_argument("--after-source", type=Path)
    args = parser.parse_args()
    after_module = data
    if args.after_source:
        name = "market_signal.paper.v2._runtime_a_benchmark"
        loader = importlib.util.spec_from_file_location(name, args.after_source)
        after_module = importlib.util.module_from_spec(loader)
        sys.modules[name] = after_module
        loader.loader.exec_module(after_module)
    result = {
        "snapshot_sha256": hashlib.file_digest(args.snapshot.open("rb"), "sha256").hexdigest(),
        "as_of": args.as_of,
        "repetitions": 5,
        "scenarios": {},
    }
    with tempfile.TemporaryDirectory(prefix="prism-runtime-a-") as root:
        path = Path(root) / "copy.duckdb"
        shutil.copyfile(args.snapshot, path)
        store = Store(path, Path(root) / "raw")
        store.con.execute("SET threads=1")
        now = pd.Timestamp(args.as_of)
        for phase, label in [
            (22, "due_1h_technical"),
            (23, "due_1h_positioning"),
            (24, "due_15m_micro"),
        ]:
            hypotheses = tuple(h for h in spec.bootstrap() if h.source_phase == phase)
            samples = {"before": [], "after": []}
            outputs = {}
            for repetition in range(6):
                for name, module in [("before", baseline()), ("after", after_module)]:
                    source, counts = adapter(module, store, now)
                    cpu, wall = time.process_time(), time.perf_counter()
                    obs = source.observations(hypotheses, now - pd.Timedelta(days=1))
                    sample = {
                        "cpu_s": time.process_time() - cpu,
                        "wall_s": time.perf_counter() - wall,
                        "selects": len(counts.queries),
                        "query_shapes": len(set(counts.queries)),
                        "rows_returned": counts.rows,
                        "writes": 0,
                        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    }
                    outputs[name] = canonical_json([asdict(o) for o in obs])
                    if repetition:
                        samples[name].append(sample)
                assert outputs["before"] == outputs["after"], label
            result["scenarios"][label] = {
                name: {key: statistics.median(s[key] for s in values) for key in values[0]}
                for name, values in samples.items()
            }
        store.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
