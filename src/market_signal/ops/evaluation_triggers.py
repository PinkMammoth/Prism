"""Durable evaluation outbox. Producers enqueue information; only the writer consumes it."""

from __future__ import annotations

import json

import pandas as pd

from market_signal.research.lab.common import canonical_json, content_id


def ts(value):
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def enqueue(store, source, key, assets, available_at, *, event_ids=(), metadata=None):
    if source not in ("context", "microstructure", "positioning", "technical"):
        raise ValueError("unregistered trigger source")
    payload = {
        "assets": sorted(set(assets)),
        "event_ids": sorted(set(event_ids)),
        "metadata": metadata or {},
    }
    tid = content_id("nimbletrigger_", {"source": source, "key": key})
    store.con.execute(
        "INSERT INTO paper_nimble_triggers VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
        [tid, source, ts(available_at).to_pydatetime(), canonical_json(payload)],
    )
    return tid


def gateway_wakeup(store, receipt, payload):
    if receipt.get("ingest_status") != "INGESTED" or not receipt.get("logical_event_id"):
        return None
    eid = receipt["logical_event_id"]
    assets = [
        r[0]
        for r in store.con.execute(
            "SELECT DISTINCT asset FROM context_asset_links WHERE event_id=? AND linked_at<=?",
            [eid, ts(receipt["context_available_at"]).to_pydatetime()],
        ).fetchall()
    ]
    tid = enqueue(
        store,
        "context",
        receipt["receipt_id"],
        assets,
        receipt["context_available_at"],
        event_ids=[eid],
        metadata={
            "sender_at": payload.get("sent_at"),
            "gateway_received_at": receipt["gateway_received_at"],
            "context_available_at": receipt["context_available_at"],
        },
    )
    # Filesystem notice is only a hint; DB outbox remains authoritative and restart-safe.
    import os

    notice = store.path.parent / "paper_nimble_notice.json"
    temp = notice.with_suffix(".tmp")
    temp.write_text(canonical_json({"trigger_id": tid}))
    os.replace(temp, notice)
    return tid


def pending(store, rid, now, activation):
    cur = store.con.execute(
        "SELECT t.trigger_id,t.source,t.available_at,t.payload FROM paper_nimble_triggers t "
        "LEFT JOIN paper_nimble_trigger_receipts r ON t.trigger_id=r.trigger_id AND r.run_id=? "
        "WHERE r.trigger_id IS NULL AND t.available_at>? AND t.available_at<=? "
        "ORDER BY t.available_at,t.trigger_id LIMIT 1000",
        [rid, ts(activation).to_pydatetime(), ts(now).to_pydatetime()],
    )
    return [
        dict(trigger_id=i, source=s, available_at=ts(a).isoformat(), **json.loads(p))
        for i, s, a, p in cur.fetchall()
    ]


def discover(store, now, activation):
    """Cheap minute fallback for bar/positioning providers; latest capture per asset only.

    Hooks make context/microstructure immediate. This recovers restart/provider processes
    that committed before a trigger could be emitted, without replaying historical entries.
    """
    a, t = ts(activation).to_pydatetime(), ts(now).to_pydatetime()
    for coin, captured in store.con.execute(
        "SELECT coin,max(captured_at) FROM context_hl_oi_hourly WHERE captured_at>? "
        "AND captured_at<=? GROUP BY coin",
        [a, t],
    ).fetchall():
        enqueue(store, "positioning", [coin, ts(captured).isoformat()], [coin], captured)
    for coin, close, observed in store.con.execute(
        "SELECT coin,close_time,first_observed_at FROM perp_intraday_bars "
        "WHERE source='hyperliquid' AND timeframe='1h' AND close_time>? "
        "AND first_observed_at<=? AND close_time<=? "
        "QUALIFY row_number() OVER (PARTITION BY coin ORDER BY close_time DESC)=1",
        [a, t, t],
    ).fetchall():
        enqueue(
            store, "technical", [coin, ts(close).isoformat()], [coin], max(ts(close), ts(observed))
        )
