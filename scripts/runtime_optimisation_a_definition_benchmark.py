"""Local definition benchmark: PYTHONPATH=src:. python scripts/runtime_optimisation_a_definition_benchmark.py."""

import json
import statistics
import time
from pathlib import Path

from tests.test_runtime_optimisation_a import reference

from market_signal.paper.nimble import event_spec
from market_signal.paper.nimble import spec as nimble
from market_signal.paper.v2 import spec
from market_signal.research.lab.common import canonical_json

old = reference("baseline_spec", "market_signal.paper.v2")
old_n = reference("baseline_nimble_spec", "market_signal.paper.nimble")
old_n.baseline = old
old_e = reference("baseline_event_spec", "market_signal.paper.nimble")
old_e.spec = old_n
result = {}
for label, a, b in [
    ("bootstrap", old.bootstrap, spec.bootstrap),
    ("v2_canonical", lambda: canonical_json(old.definition()), spec.canonical_definition),
    ("nimble_canonical", lambda: canonical_json(old_n.definition()), nimble.canonical_definition),
    (
        "event_canonical",
        lambda: canonical_json(old_e.definition()),
        event_spec.canonical_definition,
    ),
]:
    if label != "bootstrap":
        assert a() == b()
    else:
        assert [h.model_dump(mode="json") for h in a()] == [h.model_dump(mode="json") for h in b()]
    samples = {"before": [], "after": []}
    for rep in range(6):
        for name, fn in [("before", a), ("after", b)]:
            cpu, wall = time.process_time(), time.perf_counter()
            for _ in range(100):
                fn()
            if rep:
                samples[name].append(
                    {
                        "cpu_s_per_call": (time.process_time() - cpu) / 100,
                        "wall_s_per_call": (time.perf_counter() - wall) / 100,
                    }
                )
    result[label] = {
        name: {key: statistics.median(v[key] for v in values) for key in values[0]}
        for name, values in samples.items()
    }
print(json.dumps(result, indent=2))
Path("docs/evidence/runtime-optimisation-a/definitions.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
