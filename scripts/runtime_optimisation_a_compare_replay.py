"""Compare the per-worker outputs from the opt-in differential replay pytest plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def merged(prefix):
    result = {}
    paths = sorted(prefix.parent.glob(prefix.stem + ".gw*" + prefix.suffix))
    if not paths:
        paths = [prefix]
    for path in paths:
        values = json.loads(path.read_text())
        assert not result.keys() & values.keys(), "duplicate replay test identity"
        result.update(values)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    before, after = merged(args.before), merged(args.after)
    assert before.keys() == after.keys(), "different passed fixture sets block acceptance"
    mismatches = [name for name in before if before[name] != after[name]]
    assert not mismatches, "evidence mismatch blocks rollout: " + repr(mismatches)
    report = {
        "exact_equivalence": True,
        "passed_fixtures": len(before),
        "fixtures_with_stored_evidence": sum(bool(value) for value in before.values()),
        "table_digests_compared": sum(
            len(tables) for stores in before.values() for tables in stores.values()
        ),
        "combined_evidence_sha256": hashlib.sha256(
            json.dumps(before, sort_keys=True).encode()
        ).hexdigest(),
        "fixtures": sorted(before),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "fixtures"}, indent=2))


if __name__ == "__main__":
    main()
