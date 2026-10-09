"""Local operator view: spool/receipt diagnostics without database access."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from statistics import median

from market_signal.context.gateway.spool import Spool, now


def latencies(receipt: dict, completion: dict | None) -> dict:
    p, r = receipt["payload"], receipt
    times = {
        "source": p["item"].get("published_at")
        or (None if p["item"].get("scheduled") else p["item"].get("event_time")),
        "discovery": p["item"]["first_seen_at"],
        "gateway": r["gateway_received_at"],
        "durable": (completion or {}).get("spool_fsynced_at"),
        "available": (completion or {}).get("context_available_at"),
    }
    pairs = {
        "source_to_discovery_s": ("source", "discovery"),
        "discovery_to_gateway_s": ("discovery", "gateway"),
        "gateway_to_durable_s": ("gateway", "durable"),
        "durable_to_available_s": ("durable", "available"),
        "gateway_to_available_s": ("gateway", "available"),
        "end_to_end_s": ("source", "available"),
    }
    return {
        name: (datetime.fromisoformat(times[b]) - datetime.fromisoformat(times[a])).total_seconds()
        if times[a] and times[b]
        else None
        for name, (a, b) in pairs.items()
    }


def inspect(spool: Spool, rid: str) -> dict:
    receipt = spool.read("receipts", rid)
    if not receipt:
        raise ValueError("unknown receipt")
    completion = spool.read("done", rid)
    return {
        **receipt,
        "completion": completion,
        "durability": spool.read("seals", rid),
        "latency": latencies(receipt, completion or spool.read("seals", rid)),
        "attempts": [
            json.loads(p.read_text()) for p in (spool.root / "attempts").glob(f"{rid}_*.json")
        ],
    }


def status(spool: Spool) -> dict:
    t = now()
    audit = [a for a in spool.audits() if datetime.fromisoformat(a["at"]) >= t - timedelta(days=1)]
    records = spool.records()
    completed = [spool.read("done", r["receipt_id"]) for r in records]
    pending = [r for r, c in zip(records, completed, strict=True) if c is None]
    values = sorted(
        latencies(r, c)["gateway_to_available_s"]
        for r, c in zip(records, completed, strict=True)
        if c and not r["payload"].get("test", False)
    )
    counts = {
        kind: sum(a["kind"] == kind for a in audit)
        for kind in (
            "accepted",
            "duplicate",
            "rejected",
            "auth_rejected",
            "schema_rejected",
            "spool_failure",
        )
    }
    live_path = spool.root / "server.json"
    live = json.loads(live_path.read_text()) if live_path.exists() else {}
    worker_path = spool.root / "worker.json"
    worker = json.loads(worker_path.read_text()) if worker_path.exists() else {}
    running = bool(live and (t - datetime.fromisoformat(live["heartbeat_at"])).total_seconds() < 20)
    worker_running = bool(
        worker and (t - datetime.fromisoformat(worker["heartbeat_at"])).total_seconds() < 20
    )
    age = max(
        [(t - datetime.fromisoformat(r["gateway_received_at"])).total_seconds() for r in pending],
        default=0,
    )
    alerts = []
    if not running:
        alerts.append("gateway_unavailable")
    if age > 60:
        alerts.append("backlog_older_than_60s")
    if pending and (not worker_running or (worker.get("errors") and age > 30)):
        alerts.append("ingest_stalled")
    minute = [a for a in audit if datetime.fromisoformat(a["at"]) > t - timedelta(minutes=1)]
    for kind in ("auth_rejected", "schema_rejected"):
        if sum(a["kind"] == kind for a in minute) >= 10:
            alerts.append(f"{kind}_spike")
    failure = live.get("last_spool_failure_at")
    if (failure and (t - datetime.fromisoformat(failure)).total_seconds() < 120) or any(
        a["kind"] == "spool_failure" for a in minute
    ):
        alerts.append("spool_write_failure")
    accepted_count = sum(
        datetime.fromisoformat(r["gateway_received_at"]) >= t - timedelta(days=1) for r in records
    )
    attempts = len(list((spool.root / "attempts").glob("*.json")))
    return {
        "running": running,
        "started_at": live.get("started_at"),
        "auth_state": live.get("auth_state", "UNCONFIGURED"),
        "provider": "chatgpt_work_v1",
        "accepted_24h": accepted_count,
        "duplicates_24h": counts["duplicate"],
        "rejected_24h": sum(
            counts[k] for k in ("rejected", "auth_rejected", "schema_rejected", "spool_failure")
        ),
        "last_accepted_submission": next(
            (a["at"] for a in reversed(audit) if a["kind"] == "accepted"), None
        ),
        "last_rejected_submission": next(
            (
                a["at"]
                for a in reversed(audit)
                if "rejected" in a["kind"] or a["kind"] == "spool_failure"
            ),
            None,
        ),
        "spool_backlog": len(pending),
        "oldest_uningested_age_s": age,
        "worker_running": worker_running,
        "last_successful_ingest": max(
            (c["context_available_at"] for c in completed if c), default=None
        ),
        "p50_ingest_latency_s": values[len(values) // 2] if values else None,
        "p95_ingest_latency_s": values[min(len(values) - 1, int(len(values) * 0.95))]
        if values
        else None,
        "alerts": alerts,
        "counts": counts,
        "retry_error_attempts": attempts,
        "provider_quality": {
            **worker.get("quality", {}),
            "accepted_rate_24h": accepted_count
            / max(
                1,
                accepted_count
                + sum(
                    counts[k]
                    for k in ("rejected", "auth_rejected", "schema_rejected", "spool_failure")
                ),
            ),
            "duplicate_rate_24h": counts["duplicate"]
            / max(1, accepted_count + counts["duplicate"]),
            "median_gateway_latency_s": median(values) if values else None,
        },
    }
