"""Offline source/candidate/restore parity runner. Output contains hashes, never records.

Run under the disabled Research environment and a host resource/network sandbox.
The manifest's old snapshot counts are planning evidence, not today's parity baseline.
"""

import argparse
import hashlib
import json
import resource
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from market_signal.context.snapshot import context_snapshot
from market_signal.copilot.engine import list_watches
from market_signal.intelligence.clocks import AccessClock
from market_signal.intelligence.repository import ReadRepository
from market_signal.intelligence.snapshot import context_package, resolve_evidence, validate_package
from market_signal.ops import research_namespace as ns
from market_signal.ops.backups import write_json
from market_signal.research.lab.forward import forward_summary
from market_signal.research.lab.ledger import Ledger

ORIGIN, CONSUMER = "railway-prism-runtime", "vps-prism-research-offline"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def deny_network(*args, **kwargs):
    raise RuntimeError("offline validation forbids network")


def reports(db, cutoff, *, receipt=None):
    socket.socket = deny_network
    results, latency, clock_proof = {}, {}, []

    def measured(name, operation):
        start = time.monotonic()
        value = operation()
        results[name] = {"sha256": digest(value)}
        latency[name] = time.monotonic() - start
        return value

    with ns.connect(db) as con:
        store = SimpleNamespace(con=con, path=db)
        ledger = Ledger(store)
        measured("copilot/governed-watches", lambda: list_watches(store))
        for (tracking,) in con.execute(
            "SELECT tracking_id FROM lab_forward_trackings ORDER BY 1"
        ).fetchall():
            measured(
                "research/forward/" + tracking,
                lambda t=tracking: forward_summary(
                    ledger, t, as_of=pd.Timestamp(cutoff).to_pydatetime()
                ),
            )
        for days in (0, 1, 7):
            at = pd.Timestamp(cutoff) - pd.Timedelta(days=days)
            for asset in ("BTC", "AAVE"):
                prefix = f"{asset}/{at.isoformat()}"
                measured(
                    "context/" + prefix,
                    lambda a=asset, t=at: context_snapshot(
                        store, a, t.to_pydatetime(), record=False
                    ),
                )
                repository = ReadRepository(con, origin_id=ORIGIN, consumer_id=ORIGIN)
                package = measured(
                    "intelligence/" + prefix,
                    lambda a=asset, t=at, r=repository: context_package(
                        r, a, t, basis="origin-observed"
                    ),
                )
                validate_package(package)
                evidence_ids = sorted(package["evidence"])
                for eid in (
                    evidence_ids[:1]
                    + evidence_ids[len(evidence_ids) // 2 : len(evidence_ids) // 2 + 1]
                    + evidence_ids[-1:]
                ):
                    resolve_evidence(package, eid)
                results["intelligence/" + prefix].update(
                    package_id=package["package_id"],
                    evidence_count=len(package["evidence"]),
                    positioning_sha256=digest(package["positioning"]),
                )
                if receipt and days == 0:
                    # A sealed snapshot proves commits no later than verification. This
                    # conservative upper bound does not invent exact historical commits.
                    bound = receipt["source"]["source_manifest"]["verified_at"]
                    received, available = receipt["received_at_here"], receipt["available_at_here"]
                    catalog = {
                        eid: AccessClock(
                            ORIGIN,
                            e["clocks"]["origin_observed_at"],
                            bound,
                            CONSUMER,
                            received,
                            available,
                        )
                        for eid, e in package["evidence"].items()
                    }
                    replica = ReadRepository(
                        con, origin_id=ORIGIN, consumer_id=CONSUMER, clocks=catalog
                    )
                    checks = {}
                    for basis, when in (
                        ("origin-observed", at),
                        ("origin-committed", at),
                        ("origin-committed", pd.Timestamp(bound)),
                        (
                            "consumer-available",
                            pd.Timestamp(received) - pd.Timedelta(microseconds=1),
                        ),
                        ("consumer-available", pd.Timestamp(available)),
                    ):
                        p = context_package(replica, asset, when, basis=basis)
                        checks[basis + "/" + when.isoformat()] = len(p["evidence"])
                        if basis == "consumer-available" and when < pd.Timestamp(received):
                            assert not p["evidence"], "import backdated OVH availability"
                        if basis == "origin-committed" and when < pd.Timestamp(bound):
                            assert not p["evidence"], "unproven historical commit used"
                        if when == pd.Timestamp(available):
                            assert p["evidence"], "clock test needs nonempty post-import evidence"
                    # Wrong consumer must not gain access from someone else's receipt.
                    wrong = ReadRepository(
                        con, origin_id=ORIGIN, consumer_id="other", clocks=catalog
                    )
                    assert not context_package(wrong, asset, available, basis="consumer-available")[
                        "evidence"
                    ]
                    catalog_dict = {eid: c.as_dict() for eid, c in catalog.items()}
                    clock_proof.append(
                        {
                            "asset": asset,
                            "checks": checks,
                            "catalog_sha256": digest(catalog_dict),
                            "catalog": catalog_dict,
                            "batch_sha256": receipt["sha256"],
                            "commit_basis": "sealed source verification upper bound; exact historical commit unknown",
                        }
                    )
        # Query actual histories at identical source cutoffs. All source tables are
        # additionally checked in full by inspect(), including payload/blob values.
        queries = {
            "context/update-chains": "SELECT event_id,count(*),min(seq),max(seq) FROM context_event_updates GROUP BY 1 ORDER BY 1",
            "positioning/oi-coverage": "SELECT source,coin,count(*),min(observed_at),max(observed_at),count(*) FILTER(WHERE oi_notional IS NULL) FROM perp_oi_history GROUP BY 1,2 ORDER BY 1,2",
            "positioning/funding-coverage": "SELECT source,coin,count(*),min(time),max(time),count(*) FILTER(WHERE funding_rate IS NULL) FROM perp_funding GROUP BY 1,2 ORDER BY 1,2",
            "macro/pit-vintages": "SELECT series_id,source,count(*),min(realtime_start),max(realtime_start),max(available_at),count(*) FILTER(WHERE value IS NULL) FROM macro_observations WHERE available_at<=? GROUP BY 1,2 ORDER BY 1,2",
            "macro/intraday-coverage": "SELECT source,coin,timeframe,count(*),min(close_time),max(close_time) FROM perp_intraday_bars WHERE first_observed_at<=? GROUP BY 1,2,3 ORDER BY 1,2,3",
            "macro/source-health": "SELECT provider,dataset,status,count(*),max(finished_at) FROM ingestion_runs GROUP BY 1,2,3 ORDER BY 1,2,3",
            "phase24b/checkpoints": "SELECT * FROM lab_prospective_checkpoints ORDER BY 1",
            "phase24b/definitions": "SELECT * FROM lab_prospective_studies ORDER BY 1",
            "phase24b/outcomes": "SELECT * FROM lab_prospective_results ORDER BY 1",
            "research/definitions": "SELECT * FROM lab_strategies ORDER BY 1",
            "research/datasets": "SELECT * FROM lab_datasets ORDER BY 1",
            "research/experiments": "SELECT * FROM lab_experiments ORDER BY 1",
            "research/science": "SELECT * FROM lab_research_results ORDER BY 1",
            "research/lifecycle": "SELECT * FROM incubation_transitions ORDER BY 1",
        }
        for name, sql in queries.items():
            value = measured(
                name, lambda q=sql: con.execute(q, [cutoff] if "?" in q else []).fetchall()
            )
            results[name]["rows"] = len(value)
    return {"reads": results, "latency_seconds": latency, "clock_proof": clock_proof}


def ownership(plan, inspection):
    missing = sorted(set(plan["tables"]) - inspection["tables"].keys())
    unknown = sorted(inspection["tables"].keys() - plan["tables"].keys())
    result = {}
    factual = {
        "assets",
        "bars",
        "bar_revisions",
        "corporate_actions",
        "crypto_metrics",
        "data_quality_issues",
        "fundamental_facts",
        "ingestion_runs",
        "macro_observations",
        "series_changes",
    }
    state = {
        "runtime_cycles",
        "runtime_events",
        "schema_version",
        "microstructure_cutover",
        "microstructure_ingest_offsets",
        "context_work_research_pending",
        "paper_nimble_triggers",
        "paper_nimble_trigger_receipts",
    }
    for table, fp in inspection["tables"].items():
        original = plan["tables"].get(table, {})
        if table in state or table.endswith("deliveries") or table == "paper_notifications":
            owner = "scheduled/runtime state that must NOT transfer as authority"
        elif table.startswith(("universe_", "microstructure_subscription_")):
            owner = "Lab-only"
        elif table.startswith(("paper_", "intraday_execution_", "perp_paper_")):
            owner = "legacy execution evidence"
        elif table.startswith("context_"):
            owner = "Context-owned"
        elif table in factual or table.startswith("perp_"):
            owner = "shared factual history"
        else:
            owner = "Research-owned"
        result[table] = {
            **fp,
            "owner_class": owner,
            "import_role": "read-only imported history",
            "prepared_disposition": original.get("disposition"),
            "may_resume_authority": False,
            "ambiguity": "manual portfolio/journal/alert authority needs explicit design"
            if table in {"positions", "journal_entries", "alert_rules", "alert_events"}
            else None,
        }
    return {
        "tables": result,
        "prepared_table_count": len(plan["tables"]),
        "missing": missing,
        "new_schema31_tables": unknown,
        "ambiguous": [t for t, r in result.items() if r["ambiguity"]],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("ownership", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--import-clocks", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    cpu = time.process_time()
    receipt = json.loads(args.receipt.read_text())
    imported = json.loads(args.import_clocks.read_text()) if args.import_clocks else None
    inspection = ns.inspect(args.db)
    cutoff = receipt["last_finished_cycle"]
    report = reports(args.db, cutoff, receipt=imported)
    result = {
        "at": datetime.now(UTC).isoformat(),
        "frontier": cutoff,
        "inspection": inspection,
        "report": report,
        "ownership": ownership(json.loads(args.ownership.read_text()), inspection),
    }
    if args.reference:
        reference = json.loads(args.reference.read_text())
        differences = ns.compare_tables(reference["inspection"]["tables"], inspection["tables"])
        expected = {
            "schema_version",
            "universe_policies",
            "universe_evaluations",
            "universe_outcomes",
            "microstructure_subscription_events",
        }
        for d in differences:
            if d["table"] in expected:
                d["classification"] = "EXPECTED_PATH/ENVIRONMENT_DIFFERENCE"
                d["reason"] = "rehearsed schema 30->31: version row and four empty additive tables"
        read_diffs = [
            {"read": k, "classification": "BUG"}
            for k in reference["report"]["reads"].keys() | report["reads"].keys()
            if reference["report"]["reads"].get(k) != report["reads"].get(k)
        ]
        result["differences"] = differences + read_diffs
        result["passed"] = not any(
            d["classification"] in {"BUG", "UNKNOWN"} for d in result["differences"]
        )
    else:
        result["differences"] = ns.compare_tables(
            receipt["restore_test"]["all_tables"], inspection["tables"]
        )
        result["passed"] = not result["differences"] and inspection["sha256"] == receipt["sha256"]
    assert ns.sha256(args.db) == inspection["sha256"], "offline reads changed database"
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result["resources"] = {
        "wall_seconds": time.monotonic() - started,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_bytes": usage.ru_maxrss * 1024,
        "db_bytes": args.db.stat().st_size,
    }
    write_json(args.output, result)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
