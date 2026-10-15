"""Explain access-clock differences on the actual sealed candidate, without raw records."""

import json
from collections import Counter
from pathlib import Path

import pandas as pd

from market_signal.intelligence.clocks import AccessClock
from market_signal.intelligence.repository import ReadRepository
from market_signal.intelligence.snapshot import context_package
from market_signal.ops import research_namespace as ns
from market_signal.ops.backups import write_json

root = Path("/var/lib/prism-research")
validation = json.loads((root / "state/candidate-validation.json").read_text())
receipt = json.loads((root / "state/import-receipt.json").read_text())
result = []
with ns.connect(root / "import/prism.duckdb") as con:
    for proof in validation["report"]["clock_proof"]:
        asset = proof["asset"]
        clocks = {eid: AccessClock(**clock) for eid, clock in proof["catalog"].items()}
        repository = ReadRepository(
            con,
            origin_id="railway-prism-runtime",
            consumer_id="vps-prism-research-offline",
            clocks=clocks,
        )
        source = ReadRepository(
            con, origin_id="railway-prism-runtime", consumer_id="railway-prism-runtime"
        )
        before = context_package(source, asset, validation["frontier"], basis="origin-observed")
        imported_origin = context_package(
            repository, asset, validation["frontier"], basis="origin-observed"
        )
        assert set(before["evidence"]) == set(imported_origin["evidence"])
        assert all(
            before["evidence"][eid]["record"] == imported_origin["evidence"][eid]["record"]
            for eid in before["evidence"]
        )
        after = context_package(
            repository, asset, receipt["available_at_here"], basis="consumer-available"
        )
        removed = set(before["evidence"]) - set(after["evidence"])
        added = set(after["evidence"]) - set(before["evidence"])
        assert not added
        # Advancing to the real apply clock advances each documented selection window.
        counts = Counter(before["evidence"][eid]["table"] for eid in removed)
        assert counts == {
            "perp_intraday_bars": 6,
            "context_ls_ratios": 4,
            "perp_oi_history": 1,
            "perp_funding": 1,
        }
        for eid in removed:
            evidence = before["evidence"][eid]
            table, row = evidence["table"], evidence["record"]
            if table == "perp_intraday_bars":
                duration = pd.Timedelta(days=40) if row["coin"] == asset else pd.Timedelta(hours=30)
                timestamp = pd.Timestamp(row["close_time"])
            elif table in {"context_ls_ratios", "perp_oi_history"}:
                duration = pd.Timedelta(days=30)
                timestamp = pd.Timestamp(row["observed_at"])
            else:
                duration = pd.Timedelta(days=120)
                timestamp = pd.Timestamp(row["time"])
            assert pd.Timestamp(validation["frontier"]) - duration <= timestamp
            assert timestamp < pd.Timestamp(receipt["available_at_here"]) - duration
        result.append(
            {
                "asset": asset,
                "origin_observed_records_and_ids_preserved": True,
                "origin_id": "railway-prism-runtime",
                "consumer_id": "vps-prism-research-offline",
                "source_cutoff": validation["frontier"],
                "consumer_cutoff": receipt["available_at_here"],
                "source_evidence_count": len(before["evidence"]),
                "consumer_evidence_count": len(after["evidence"]),
                "differences": [
                    {
                        "classification": "EXPECTED_RUNTIME_CLOCK_DIFFERENCE",
                        "reason": "actual OVH apply cutoff advances documented rolling windows: one asset bar (40 days), five cohort bars (30 hours), four ratios and one OI sample (30 days), and one funding settlement (120 days) leave selection; every removed record verified against both exact window bounds",
                        "removed_by_table": dict(
                            Counter(before["evidence"][eid]["table"] for eid in removed)
                        ),
                        "added_count": len(added),
                    }
                ],
                "exact_original_commit_instants": "unknown; retained source seal verification provides conservative upper bound",
                "consumer_availability_is_not_source_ingestion": True,
            }
        )
write_json(root / "state/clock-comparison.json", {"passed": True, "comparisons": result})
