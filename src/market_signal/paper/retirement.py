"""Append-only v1 retirement and frozen comparison baseline. Never rewrites old events."""

from __future__ import annotations

import hashlib
import json

from market_signal.models.domain import utcnow
from market_signal.paper import engine, observe
from market_signal.research.lab.common import canonical_json

REASON = (
    "Retired because the conservative daily strategy/admission design produced no useful "
    "exploratory trading activity and is superseded by Paper Trader v2."
)
KNOWN_RUN = "paperrun_27b0a336e707c389a3fb574cd9d09a7800b563f0691612bf4fb4f1fd97029279"


def frozen(store, run_id: str) -> dict | None:
    row = store.con.execute(
        "SELECT payload FROM paper_retirements WHERE run_id=?", [run_id]
    ).fetchone()
    return json.loads(row[0]) if row else None


def retire(store, run_id: str, *, now=None) -> dict:
    """STOPPED is the existing terminal equivalent. Open positions wind down, then retry.

    During draining, old engine manages fixed exits without reading/creating signals.
    Jobs cannot be disabled until flat. A retirement row is written only when flat.
    """
    old = frozen(store, run_id)
    if old:
        return old
    at = engine._ts(now or utcnow())
    st = engine.account(store, run_id)
    if not st.terminal:
        engine.set_status(store, run_id, "STOPPED", reason=REASON, now=at.to_pydatetime())
    st = engine.account(store, run_id)
    if not st.flat:
        return {
            "run_id": run_id,
            "state": "DRAINING",
            "reason": REASON,
            "positions": len(st.positions),
            "orders": len(st.orders),
            "next_step": "keep v1 management job until flat; repeat retire-v1",
        }
    payload = observe.snapshot_payload(observe.RunView(store, run_id, at))
    payload.update(
        state="RETIRED",
        retired_at=at.isoformat(),
        reason=REASON,
        definition=json.loads(
            store.con.execute(
                "SELECT definition FROM paper_runs WHERE run_id=?", [run_id]
            ).fetchone()[0]
        ),
    )
    # Freeze every historical table, not only its account; future writes are forbidden.
    payload["historical_digests"] = digests(store, run_id)
    store.con.execute(
        "INSERT INTO paper_retirements VALUES (?,?,?,?)",
        [run_id, at.to_pydatetime(), REASON, canonical_json(payload)],
    )
    return payload


def digests(store, run_id: str) -> dict:
    result = {}
    for table in (
        "paper_runs",
        "paper_events",
        "paper_cycles",
        "paper_snapshots",
        "paper_briefs",
        "paper_notifications",
        "paper_evidence",
    ):
        rows = store.con.execute(f"SELECT * FROM {table} WHERE run_id=?", [run_id]).fetchall()
        data = sorted(canonical_json([str(x) for x in r]) for r in rows)
        result[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256("\n".join(data).encode()).hexdigest(),
        }
    return result


def v1_work_needed(store) -> bool:
    """Also skips stopped/flat runs on pre-Phase25 production without retirement rows."""
    return any(
        not (st := engine.account(store, r["run_id"])).terminal or not st.flat
        for r in engine.runs(store)
    )
