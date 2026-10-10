"""Source operating health and descriptive cross-provider discovery evidence."""

from __future__ import annotations

import json
from datetime import datetime
from statistics import median

from market_signal.context.free_sources.registry import active, load
from market_signal.context.gateway.spool import now


def status(spool, cfg=None, *, stage=None):
    cfg = cfg or load()
    selected = {s["id"] for s in active(cfg, stage)}
    rows = []
    for s in cfg["sources"]:
        st = spool.state(s["id"])
        counts = {}
        for k, h in st.get("hours", {}).items():
            if (now() - datetime.fromisoformat(k)).total_seconds() > 90000:
                continue
            for k, v in h.items():
                counts[k] = counts.get(k, 0) + v
        age = (
            (now() - datetime.fromisoformat(st["last_successful_poll"])).total_seconds()
            if st.get("last_successful_poll")
            else None
        )
        state = (
            "DISABLED"
            if not s["enabled"]
            else "STAGED"
            if s["id"] not in selected
            else "NEVER_POLLED"
            if not st
            else "RATE_LIMITED"
            if st.get("rate_limited_until")
            else "FAILING"
            if st.get("consecutive_failures")
            else "STALE"
            if age is not None and age > 3 * s["cadence_seconds"]
            else "OK"
        )
        rows.append(
            {
                **s,
                "health_state": state,
                "polling_active": s["id"] in selected,
                **{
                    k: st.get(k)
                    for k in (
                        "last_poll",
                        "last_successful_poll",
                        "last_content_change",
                        "http_status",
                        "consecutive_failures",
                        "rate_limit",
                        "rate_limited_until",
                        "next_poll",
                        "error",
                    )
                },
                "events_24h": counts.get("accepted", 0),
                "bytes_24h": counts.get("wire_bytes", 0),
                "counts_24h": counts,
                "counts_all_time": st.get("totals", {}),
                "window": "rolling_hour_buckets_up_to_25_hours",
            }
        )
    pending = spool.pending()
    processes = {}
    for name in ("collector", "worker"):
        p = spool.root / f"{name}.json"
        h = json.loads(p.read_text()) if p.exists() else {}
        processes[name] = {
            **h,
            "healthy": bool(h.get("heartbeat_at"))
            and (now() - datetime.fromisoformat(h["heartbeat_at"])).total_seconds() < 60
            and not h.get("error"),
        }
    return {
        "version": cfg["version"],
        "data_cost_gbp_month": 0,
        "processes": processes,
        "backlog": len(pending),
        "oldest_pending_age_seconds": (
            now() - datetime.fromisoformat(pending[0]["gateway_received_at"])
        ).total_seconds()
        if pending
        else None,
        "sources": rows,
    }


def metrics(store, spool):
    records = store.con.execute(
        "SELECT provider,event_id,received_at,payload,outcome FROM context_free_observations ORDER BY received_at,observation_id"
    ).fetchall()
    reports = []
    # Every original event/update is retained, including Work and legacy Phase 23 providers.
    history = store.con.execute(
        "SELECT event_id,source_id,first_seen_at FROM context_events UNION ALL SELECT event_id,source_id,observed_at FROM context_event_updates ORDER BY 3,2"
    ).fetchall()
    by_event = {}
    for eid, pid, at in history:
        providers = by_event.setdefault(eid, {})
        providers.setdefault(pid, at)
    comparisons = []
    for eid, providers in by_event.items():
        if len(providers) < 2:
            continue
        order = sorted(providers.items(), key=lambda x: (x[1], x[0]))
        comparisons.append(
            {
                "event_id": eid,
                "first_provider": order[0][0],
                "order": [
                    dict(
                        provider=p,
                        receipt_at=t.isoformat(),
                        delay_from_first_seconds=(t - order[0][1]).total_seconds(),
                    )
                    for p, t in order
                ],
            }
        )
    for row in status(spool)["sources"]:
        pid = row["id"]
        mine = [
            (eid, at, json.loads(raw), json.loads(out))
            for p, eid, at, raw, out in records
            if p == pid
        ]
        pub = [
            (at - datetime.fromisoformat(raw["published_at"])).total_seconds()
            for _, at, raw, _ in mine
            if raw.get("published_at") and datetime.fromisoformat(raw["published_at"]) <= at
        ]
        availability = [
            (
                at - datetime.fromisoformat(raw["attributes"]["facts"]["source_availability_at"])
            ).total_seconds()
            for _, at, raw, _ in mine
            if raw.get("attributes", {}).get("facts", {}).get("source_availability_at")
        ]
        later = sum(
            any(p != pid and t > at for p, t in by_event.get(eid, {}).items())
            for eid, at, _, _ in mine
        )
        reactions = []
        eids = {eid for eid, _, _, _ in mine}
        for eid, raw in store.con.execute(
            "SELECT event_id,payload FROM context_work_reactions"
        ).fetchall():
            if eid in eids:
                outcome = json.loads(raw)
                if outcome.get("status") == "ok":
                    reactions.append(outcome)
        outside = sorted(
            {
                a
                for _, _, raw, _ in mine
                for a in raw.get("assets", [])
                if a not in {"AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL"}
            }
        )
        # Entities can carry explicitly mapped tokens not present in the six-asset executor.
        for eid in eids:
            outside.extend(
                a[0]
                for a in store.con.execute(
                    "SELECT asset FROM context_asset_links WHERE event_id=?", [eid]
                ).fetchall()
                if a[0] not in {"AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL"}
            )
        reports.append(
            {
                "provider": pid,
                "poll_counts": row["counts_all_time"],
                "accepted_observations": len(mine),
                "new_logical_events": sum(o["new_events"] for _, _, _, o in mine),
                "duplicate": sum(o["duplicates"] for _, _, _, o in mine),
                "corroboration_or_update": sum(o["new_updates"] for _, _, _, o in mine),
                "median_publication_to_receipt_seconds": median(pub) if pub else None,
                "publication_latency_n": len(pub),
                "median_availability_to_receipt_seconds": median(availability)
                if availability
                else None,
                "later_corroboration_observations": later,
                "later_corroboration_rate": later / len(mine) if mine else None,
                "reaction_samples": len(reactions),
                "median_absolute_forward_return_pct": median(
                    abs(r["return_pct"]) for r in reactions
                )
                if reactions
                else None,
                "outside_execution_universe_assets": sorted(set(outside)),
                "false_report_rate": None,
                "noise_rate": row["counts_all_time"].get("rejected", 0)
                / row["counts_all_time"]["detected"]
                if row["counts_all_time"].get("detected")
                else None,
            }
        )
    return {"descriptive_only": True, "providers": reports, "discovery_comparisons": comparisons}
