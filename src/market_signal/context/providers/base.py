"""Provider-independent ingestion: ``ContextProvider`` → raw → normalise → classify → ledger.

A provider only *fetches and normalises*; it never writes. ``run_provider`` owns the rest:
timing, raw-payload archiving (SHA-256 provenance), ``ledger.ingest`` (dedupe + append), and
one ``context_provider_runs`` row per run (health). A failing provider writes no events and
records a ``failed`` run; other providers are unaffected. Re-running is idempotent: unchanged
items are duplicates, so ``first_seen_at`` is preserved across restarts and retries.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from market_signal.context import ledger
from market_signal.context.model import Observation
from market_signal.data.http import ProviderError, RawPayload
from market_signal.data.store import Store
from market_signal.models.domain import utcnow


@dataclass
class FetchResult:
    observations: list[Observation | dict] = field(default_factory=list)
    received: int = 0  # raw items seen
    filtered: int = 0  # raw items dropped as noise / unmapped (quality over quantity)
    raw: list[RawPayload] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)  # persisted in the run payload
    rate_limit: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class ContextProvider(Protocol):
    name: str  # stable id; also the default ``source_id`` prefix
    stale_hours: float  # health: no successful run for longer than this -> STALE

    def fetch(self, now: datetime, last_state: dict) -> FetchResult: ...


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def last_state(store: Store, provider: str) -> dict:
    """``state`` of the provider's newest successful run (e.g. a venue's previous universe)."""
    import json

    r = store.con.execute(
        "SELECT payload FROM context_provider_runs WHERE provider=? AND status IN ('ok','partial') "
        "ORDER BY finished_at DESC, run_id DESC LIMIT 1", [provider]).fetchone()  # fmt: skip
    return (json.loads(r[0]).get("state") or {}) if r else {}


def run_provider(store: Store, provider: ContextProvider, now: datetime | None = None) -> dict:
    """``now`` injects Prism's clock (tests, replays); the run then finishes at ``now`` + the
    measured elapsed time, so health stays consistent with the injected timeline."""
    started = pd.Timestamp(now or utcnow()).tz_convert("UTC").to_pydatetime()
    run_id = ledger.new_run_id(provider.name)
    t0 = time.monotonic()

    def finished() -> datetime:
        if now is None:
            return max(utcnow(), started)
        return started + timedelta(seconds=time.monotonic() - t0)

    try:
        fr = provider.fetch(started, last_state(store, provider.name))
    except (ProviderError, ValueError, KeyError) as exc:
        ledger.record_run(store, provider.name, started, status="failed", error=str(exc)[:500],
                          latency_ms=(time.monotonic() - t0) * 1000, run_id=run_id,
                          finished_at=finished())  # fmt: skip
        return {"provider": provider.name, "status": "failed", "error": str(exc)[:200]}
    latency = (time.monotonic() - t0) * 1000
    archived = store.archive_raw(fr.raw, run_id) if fr.raw else []
    res = ledger.ingest(store, fr.observations, run_id=run_id,
                        now=None if now is None else started)  # fmt: skip
    status = "partial" if res.rejected else "ok"
    ledger.record_run(store, provider.name, started, status=status, received=fr.received,
                      result=res, filtered=fr.filtered, latency_ms=latency, run_id=run_id,
                      payload={"state": fr.state, "rate_limit": fr.rate_limit, "notes": fr.notes,
                               "raw": [{"path": a.path, "sha256": a.sha256} for a in archived],
                               "errors": res.rejected[:20]},
                      finished_at=finished())  # fmt: skip
    return {"provider": provider.name, "status": status, "received": fr.received,
            "filtered": fr.filtered, **res.as_dict(), "latency_ms": round(latency, 1),
            "notes": fr.notes}  # fmt: skip


# --------------------------------------------------------------------------- health

HEALTH_VERSION = "provider_health_v1"


def provider_health(store: Store, stale_hours: dict[str, float], now: datetime | None = None,
                    failure_alert_after: int = 3) -> list[dict]:  # fmt: skip
    """Per provider: last success, last event, latency, recent errors, staleness and the last
    reported rate-limit state. Context from a STALE/FAILING provider must be flagged by its
    consumers (``snapshot`` does), never used silently."""
    t = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    try:
        runs = store.con.execute(
            "SELECT provider, finished_at, status, latency_ms, error, payload, new_events, new_updates "
            "FROM context_provider_runs WHERE finished_at <= ? ORDER BY provider, finished_at",
            [t.to_pydatetime()]).df()  # fmt: skip
    except Exception:
        runs = pd.DataFrame()
    out = []
    for name, hrs in sorted(stale_hours.items()):
        g = runs[runs["provider"] == name] if not runs.empty else runs
        ok = g[g["status"].isin(["ok", "partial"])] if len(g) else g
        last_ok = pd.Timestamp(ok["finished_at"].iloc[-1]).tz_convert("UTC") if len(ok) else None
        ev = g[(g["new_events"] + g["new_updates"]) > 0] if len(g) else g
        streak = 0
        for s in reversed(list(g["status"])) if len(g) else []:
            if s != "failed":
                break
            streak += 1
        age = None if last_ok is None else (t - last_ok).total_seconds() / 3600
        state = ("NEVER_RUN" if not len(g) else "FAILING" if streak >= failure_alert_after
                 else "STALE" if age is None or age > hrs else "OK")  # fmt: skip
        import json

        out.append({
            "provider": name, "state": state, "stale_after_hours": hrs,
            "last_success": None if last_ok is None else last_ok.isoformat(),
            "hours_since_success": None if age is None else round(age, 2),
            "last_event_run": None if not len(ev) else pd.Timestamp(ev["finished_at"].iloc[-1]).isoformat(),
            "last_latency_ms": None if not len(g) or pd.isna(g["latency_ms"].iloc[-1]) else float(g["latency_ms"].iloc[-1]),
            "consecutive_failures": streak,
            "last_error": None if not len(g) or g["status"].iloc[-1] != "failed" else g["error"].iloc[-1],
            "rate_limit": (json.loads(g["payload"].iloc[-1]).get("rate_limit") or {}) if len(g) else {},
            "version": HEALTH_VERSION,
        })  # fmt: skip
    return out
