"""Settle legacy obligations on a disposable local backup copy; never trade or retire.

This is a rehearsal, not a production finalizer. No activation, monitor, admit, worker,
kill, notification or archive-state transition is called. Preserve the source archive.
"""

import argparse
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from market_signal.data.store import Store
from market_signal.ops import backups as b
from market_signal.ops import runtime as rt
from market_signal.paper.nimble import engine
from market_signal.paper.nimble.data import Sources


def counters(store, rid):
    return {
        "pending_research": store.con.execute(
            "SELECT count(*) FROM paper_nimble_research_pending WHERE run_id=?", [rid]
        ).fetchone()[0],
        "unsettled_closes": store.con.execute(
            "SELECT count(*) FROM paper_nimble_positions WHERE run_id=? AND state='CLOSED' AND json_extract(payload,'$.accounting_final')=false",
            [rid],
        ).fetchone()[0],
        "nonterminal_positions": store.con.execute(
            "SELECT count(*) FROM paper_nimble_positions WHERE run_id=? AND state NOT IN ('CLOSED','CANCELLED')",
            [rid],
        ).fetchone()[0],
        "opportunities": store.con.execute(
            "SELECT count(*) FROM paper_nimble_opportunities WHERE run_id=?", [rid]
        ).fetchone()[0],
        "ledger_events": store.con.execute(
            "SELECT count(*) FROM paper_nimble_events WHERE run_id=?", [rid]
        ).fetchone()[0],
        "account": engine.account(store, rid),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    receipt = json.loads(b.sidecar(args.archive).read_text())
    if (
        not receipt.get("off_host_verified")
        or rt._sha256(args.archive) != receipt["artifact_sha256"]
    ):
        raise ValueError("verified archive required")
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        raise ValueError("local rehearsal only")
    now = pd.Timestamp(receipt["capture_finished_at"])
    with tempfile.TemporaryDirectory(prefix="nimble-retirement-", dir=args.archive.parent) as temp:
        path = Path(temp) / "rehearsal.duckdb"
        with gzip.open(args.archive, "rb") as fi, path.open("wb") as fo:
            shutil.copyfileobj(fi, fo, 2**20)
        if rt._sha256(path) != receipt["sha256"]:
            raise ValueError("source checksum mismatch")
        old_role = os.environ.get("PRISM_RUNTIME_ROLE")
        os.environ["PRISM_RUNTIME_ROLE"] = "scratch"  # disposable local copy only
        try:
            store = Store(path)
            source = Sources(store, now)
            runs = engine.runs(store)
            before = {r["run_id"]: counters(store, r["run_id"]) for r in runs}
            if any(r["nonterminal_positions"] for r in before.values()):
                raise ValueError("not flat; refuse settlement-only rehearsal")
            for run in runs:
                rid = run["run_id"]
                engine.load(store, rid)
                unresolved = engine.rows(
                    store,
                    "SELECT payload FROM paper_nimble_positions WHERE run_id=? AND state='CLOSED' AND json_extract(payload,'$.accounting_final')=false",
                    [rid],
                )
                since = min(
                    (pd.Timestamp(json.loads(p["payload"])["entry_at"]) for p in unresolved),
                    default=now,
                )
                rates = source.funding(since)
                pending = engine.rows(
                    store, "SELECT * FROM paper_nimble_research_pending WHERE run_id=?", [rid]
                )
                quotes = source.research_quotes(
                    sorted({json.loads(p["payload"])["asset"] for p in pending}),
                    min((pd.Timestamp(p["reference_at"]) for p in pending), default=now),
                )
                with store.transaction():
                    engine.reconcile(store, rid, rates, now)
                    engine.research(store, rid, quotes, now=now)
            after = {r["run_id"]: counters(store, r["run_id"]) for r in runs}
            # Repeat with exactly the same inputs to demonstrate bounded/idempotent settlement.
            for run in runs:
                rid = run["run_id"]
                source_now = source.funding(pd.Timestamp(run["activated_at"]))
                with store.transaction():
                    engine.reconcile(store, rid, source_now, now)
                    engine.research(store, rid, [], now=now)
            repeated = {r["run_id"]: counters(store, r["run_id"]) for r in runs}
            cur = store.con.execute(
                "SELECT count(*) AS n,count(*) FILTER(WHERE json_extract(payload,'$.missing_path')=true) AS missing_path,max(cast(json_extract(payload,'$.observation_delay_seconds') AS DOUBLE)) AS max_delay FROM paper_nimble_research"
            )
            quality = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
            store.close()
        finally:
            if old_role is None:
                os.environ.pop("PRISM_RUNTIME_ROLE", None)
            else:
                os.environ["PRISM_RUNTIME_ROLE"] = old_role
    report = {
        "source_sha256": receipt["sha256"],
        "asof": str(now),
        "before": before,
        "after": after,
        "idempotent": after == repeated,
        "research_quality_all_history": quality,
        "source_unchanged": rt._sha256(args.archive) == receipt["artifact_sha256"],
        "production_writes": False,
        "retirement_applied": False,
    }
    args.evidence.mkdir(parents=True, exist_ok=True)
    (args.evidence / "nimble-settlement-rehearsal.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                "idempotent": report["idempotent"],
                "before": {
                    k: {n: v for n, v in x.items() if n != "account"} for k, x in before.items()
                },
                "after": {
                    k: {n: v for n, v in x.items() if n != "account"} for k, x in after.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
