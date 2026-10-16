"""Focused safety tests for continuing guard; fixtures only, no remote calls or mutations."""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ranking_guard as guard

root = Path(__file__).resolve().parent.parent
sample = json.loads((root / "seven-observations-audit-attempt2.json").read_text())["sample"]
budget = json.loads((root / "ranking-budget-attempt2.json").read_text())
assert not guard.watch.concerns(sample, sample, sample["epoch"] - 4000)
assert not guard.additional_concerns(sample, budget)
cases = ["healthy live metadata fixture"]
for label, edit in {
    "stale accepted evidence": lambda s: s["db"]["evaluations"][-1].update(at=s["epoch"] - 901),
    "wrong profile digest": lambda s: s["profile"].update(digest="changed"),
    "DB transaction over30s": lambda s: s["worker_events"][-1]["telemetry"].update(
        ranking_db_commit_completed_at=s["worker_events"][-1]["telemetry"][
            "ranking_db_commit_started_at"
        ]
        + 31
    ),
}.items():
    modified = copy.deepcopy(sample)
    edit(modified)
    assert guard.additional_concerns(modified, budget), label
    cases.append(label)
for label, edit in {
    "expired resource receipt": lambda b: b.update(measured_at=sample["epoch"] - 86401),
    "higher measured growth safety floor": lambda b: b.update(daily_growth_bytes=500_000_000),
}.items():
    modified = copy.deepcopy(budget)
    edit(modified)
    assert guard.additional_concerns(sample, modified), label
    cases.append(label)
for label, final, accepted in (
    (
        "13 genuine and7200s",
        {"clean": True, "evaluations": 13, "accepted_span_seconds": 7200},
        True,
    ),
    (
        "old single observation insufficient",
        {"clean": True, "evaluations": 1, "accepted_span_seconds": 7200},
        False,
    ),
    (
        "12 observations insufficient",
        {"clean": True, "evaluations": 12, "accepted_span_seconds": 7200},
        False,
    ),
    (
        "7199s insufficient",
        {"clean": True, "evaluations": 13, "accepted_span_seconds": 7199},
        False,
    ),
    (
        "failed window rejected",
        {"clean": False, "evaluations": 13, "accepted_span_seconds": 7200},
        False,
    ),
):
    try:
        guard.require_completed_window(final)
        actual = True
    except ValueError:
        actual = False
    assert actual == accepted, label
    cases.append(label)
decision = {
    "stage": "RANKING ONLY",
    "policy_id": guard.POLICY.identity,
    "desired_subscriptions": list(guard.watch.CORE),
    "pins": {},
    "rows": [{"asset": "SAT", "entry_eligible": False}],
    "states": {"SAT": {"rich_since": None}},
}
assert all(guard.decision_checks(decision).values())
for label, edit in {
    "warming stage": lambda d: d.update(stage="RANKING + WARMING"),
    "wrong policy": lambda d: d.update(policy_id="changed"),
    "satellite subscription": lambda d: d["desired_subscriptions"].append("SAT"),
    "unexpected risk pin": lambda d: d.update(pins={"BTC": ["risk"]}),
    "satellite entry eligibility": lambda d: d["rows"][0].update(entry_eligible=True),
    "satellite rich segment": lambda d: d["states"]["SAT"].update(rich_since=123),
}.items():
    modified = copy.deepcopy(decision)
    edit(modified)
    assert not all(guard.decision_checks(modified).values()), label
    cases.append(label)
print(
    json.dumps(
        {"fixture_only": True, "no_host_mutations": True, "passed": len(cases), "cases": cases},
        indent=2,
    )
)
