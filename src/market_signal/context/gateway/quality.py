"""Descriptive provider outcomes only; never a trading confidence or admission rule."""

from __future__ import annotations

import json
from datetime import datetime
from statistics import median


def describe(store) -> dict:
    rows = store.con.execute("SELECT receipt_id, result FROM context_gateway_ingests").fetchall()
    outcomes = [json.loads(row[1]) for row in rows]
    outcomes = [
        r for r in outcomes if r["ingest_status"] == "INGESTED" and r.get("logical_event_id")
    ]
    corroborated, denied, times = 0, 0, []
    for r in outcomes:
        updates = store.con.execute(
            "SELECT u.observed_at, u.confidence FROM context_event_updates u "
            "JOIN context_sources s ON s.source_id=u.source_id WHERE u.event_id=? "
            "AND u.source_id <> 'chatgpt_work_v1' AND s.tier <= 2 AND u.observed_at >= ? ORDER BY u.observed_at",
            [r["logical_event_id"], datetime.fromisoformat(r["gateway_received_at"])],
        ).fetchall()
        confirm = [at for at, confidence in updates if confidence in ("CONFIRMED", "OFFICIAL")]
        corroborated += bool(confirm)
        denied += any(confidence == "DENIED" for _, confidence in updates)
        if confirm:
            times.append(
                (confirm[0] - datetime.fromisoformat(r["gateway_received_at"])).total_seconds()
            )
    return {
        "provider": "chatgpt_work_v1",
        "descriptive_only": True,
        "ingested_receipts": len(outcomes),
        "corroborated_later": corroborated,
        "denied_later": denied,
        "median_time_to_corroboration_s": median(times) if times else None,
    }
