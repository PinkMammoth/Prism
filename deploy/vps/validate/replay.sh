#!/usr/bin/env bash
# Deterministic replay/equivalence on this host (no network, no database authority).
#   deploy/vps/validate/replay.sh <out-dir>
# Run the SAME commit here and on the reference host, then compare:
#   python scripts/runtime_optimisation_a_compare_replay.py A/replay.json B/replay.json cmp.json
#   diff A/digests.json B/digests.json
set -euo pipefail
OUT=${1:?out dir}
mkdir -p "$OUT"
cd "$(dirname "$0")/../../.."
export PRISM_RUNTIME_ROLE=scratch PRISM_PROFILE= PYTHONHASHSEED=0 TZ=UTC PYTHONPATH=.:${PYTHONPATH:-}
git rev-parse HEAD 2>/dev/null > "$OUT/revision" || cat REVISION > "$OUT/revision"
# 1. Differential evidence digests of the deterministic execution fixtures (Tranche A plugin).
PRISM_RUNTIME_A_REPLAY_MODE=after PRISM_RUNTIME_A_REPLAY_OUT="$OUT/replay.json" \
  uv run --frozen pytest -q -p tests.runtime_a_replay_plugin \
  tests/test_runtime_optimisation_a.py tests/test_runtime_optimisation_a_edges.py \
  tests/test_runtime_optimisation_a_execution_edges.py > "$OUT/replay.log" 2>&1
# 2. Semantic suites: decisions, ledgers, features, event cohorts, Nimble, paper, profiles.
uv run --frozen pytest -q --junitxml="$OUT/semantic.xml" \
  tests/test_nimble_paper.py tests/test_nimble_runtime.py tests/test_event_reaction.py \
  tests/test_exploratory_paper_v2.py tests/test_microstructure.py tests/test_intraday.py \
  tests/test_microstructure_direction.py tests/test_runtime.py tests/test_runtime_profiles.py \
  tests/test_dynamic_universe.py tests/test_backup_lifecycle.py \
  > "$OUT/semantic.log" 2>&1
# 3. Frozen synthetic engineering loads (ledger rows/bytes are deterministic; timings are not).
uv run --frozen python scripts/nimble_benchmark.py --output "$OUT/nimble_benchmark.json" > /dev/null
uv run --frozen python scripts/event_reaction_benchmark.py --output "$OUT/event_benchmark.json" > /dev/null
uv run --frozen python - "$OUT" <<'PY'
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[1])
def volatile(k):  # host-dependent timing/memory/physical size; counts and digests stay
    return any(t in k for t in ("second", "wall", "cpu", "rss", "db_bytes", "database_bytes")) or k.endswith(
        ("_s", "_ms", "_at"))
def strip(x):
    if isinstance(x, dict):
        return {k: strip(v) for k, v in x.items() if not volatile(k)}
    if isinstance(x, list):
        return [strip(v) for v in x]
    return x
d = {"replay_sha256": hashlib.sha256((out / "replay.json").read_bytes()).hexdigest()}
for name in ("nimble_benchmark", "event_benchmark"):
    blob = json.dumps(strip(json.loads((out / f"{name}.json").read_text())), sort_keys=True)
    d[name] = hashlib.sha256(blob.encode()).hexdigest()
(out / "digests.json").write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
print(json.dumps(d, indent=2))
PY
