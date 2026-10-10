"""Descriptive provider outcomes only; never a trading confidence or admission rule."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from statistics import median

from market_signal.context.gateway.health import latencies
from market_signal.context.taxonomy import Confidence, materiality, severity
from market_signal.context.work_monitors import LEGACY_PROVIDER, PROVIDER_IDS


def describe(store, spool=None) -> dict:
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
            "AND s.tier <= 2 AND u.observed_at >= ? ORDER BY u.observed_at",
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
        "monitors": monitor_metrics(store, spool),
    }


def monitor_metrics(store, spool=None):
    receipts = spool.records() if spool else []
    audits = spool.audits() if spool else []
    observations = store.con.execute(
        "SELECT receipt_id,provider,event_id,observed_at,payload FROM context_work_observations"
    ).fetchall()
    committed = {
        rid: json.loads(raw)
        for rid, raw in store.con.execute(
            "SELECT receipt_id,result FROM context_gateway_ingests"
        ).fetchall()
    }
    metrics = {}
    for pid in PROVIDER_IDS:
        rr = [r for r in receipts if r.get("provider", LEGACY_PROVIDER) == pid]
        aa = [a for a in audits if a.get("provider") == pid]
        oo = [o for o in observations if o[1] == pid]
        done = [
            (r, spool.read("done", r["receipt_id"]) or committed.get(r["receipt_id"])) for r in rr
        ]
        ingested = [committed[o[0]] for o in oo if o[0] in committed]
        # One logical episode counts once, even after many updates/receipts.
        events = {}
        for _, _, eid, observed, _ in oo:
            events[eid] = min(events.get(eid, observed), observed)
        corroborated, denied = 0, 0
        for eid, observed in events.items():
            future = store.con.execute(
                "SELECT u.confidence FROM context_event_updates u JOIN context_sources s USING(source_id) "
                "WHERE u.event_id=? AND s.tier<=2 AND u.observed_at>?",
                [eid, observed],
            ).fetchall()
            corroborated += any(c[0] in ("OFFICIAL", "CONFIRMED") for c in future)
            denied += any(c[0] == "DENIED" for c in future)
        distributions, assets, affected = Counter(), Counter(), Counter()
        for _, _, _, _, raw in oo:
            item = json.loads(raw)["item"]
            m = materiality(item["subcategory"], Confidence(item["confidence"]))
            distributions[severity(m["score"])] += 1
            assets[len(item.get("assets", []))] += 1
        for rid, _, eid, observed, _ in oo:
            available = committed.get(rid, {}).get("context_available_at", observed)
            count = store.con.execute(
                "SELECT count(DISTINCT asset) FROM context_asset_links WHERE event_id=? AND linked_at<=?",
                [eid, available],
            ).fetchone()[0]
            affected[count] += 1
        timing = [latencies(r, c) for r, c in done if c and not r["payload"].get("test", False)]
        medians = {}
        for name in (
            "source_event_to_gateway_s",
            "publication_to_gateway_s",
            "event_to_discovery_s",
            "information_to_discovery_s",
            "information_to_gateway_s",
            "source_to_discovery_s",
            "discovery_to_send_s",
            "discovery_to_gateway_s",
            "send_to_gateway_s",
            "gateway_to_available_s",
        ):
            measured = [v[name] for v in timing if v[name] is not None and v[name] >= 0]
            medians[name] = {"median": median(measured) if measured else None, "n": len(measured)}
        metrics[pid] = {
            "descriptive_only": True,
            "window": "all_time",
            "submissions": len(aa),
            "accepted": len(rr),
            "test_receipts": sum(r["payload"].get("test", False) for r in rr),
            "rejected": sum(
                a["kind"] in ("schema_rejected", "rejected", "spool_failure") for a in aa
            ),
            "transport_retries": sum(a["kind"] == "duplicate" for a in aa),
            "duplicates": sum(r.get("duplicates", 0) for r in ingested),
            "new_logical_events": sum(r.get("new_events", 0) for r in ingested),
            "updates": sum(r.get("new_updates", 0) for r in ingested),
            "corroborations": store.con.execute(
                "SELECT count(*) FROM context_event_updates WHERE source_id=? AND kind='corroboration'",
                [pid],
            ).fetchone()[0],
            "logical_events_observed": len(events),
            "later_independently_corroborated": corroborated,
            "later_denied_invalidated": denied,
            "latency_s": medians,
            "affected_direct_asset_count_distribution": dict(assets),
            "affected_resolved_asset_count_distribution": dict(affected),
            "materiality_distribution": dict(distributions),
            "sender_times_are_claims": True,
            "gateway_statistics_available": spool is not None,
        }
    return metrics
