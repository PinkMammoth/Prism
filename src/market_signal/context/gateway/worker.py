"""Five-second ingest attempts under the existing runtime lock. No lock bypass."""

from __future__ import annotations

import os
import time
from contextlib import closing
from pathlib import Path

from market_signal.context.gateway.ingest import drain
from market_signal.context.gateway.server import publish_live
from market_signal.context.gateway.spool import Spool, default_root, now


def tick(db: Path, spool: Spool) -> dict:
    from market_signal.data.store import Store
    from market_signal.ops.runtime import RuntimeBusy, require_authoritative, runtime_lock

    if not spool.pending():
        return {"ingested": 0, "errors": 0, "backlog": 0}
    try:
        with runtime_lock(db, "context_gateway", wait=0):
            require_authoritative(db)
            with closing(Store(db, lock_timeout=0)) as store:
                return drain(store, spool)
    except RuntimeBusy:
        return {"ingested": 0, "errors": 0, "backlog": len(spool.pending()), "busy": True}


def quality_tick(db: Path):
    from market_signal.context.gateway.quality import describe
    from market_signal.context.work_research import collect
    from market_signal.data.store import Store
    from market_signal.ops.runtime import require_authoritative, runtime_lock

    with runtime_lock(db, "context_gateway_quality", wait=0):
        require_authoritative(db)
        with closing(Store(db, lock_timeout=0)) as store:
            research = collect(store, now=now())
            return {**describe(store, Spool(default_root())), "reaction_collection": research}


def run(db: Path):
    from market_signal.ops.runtime import require_authoritative

    require_authoritative(db)
    spool = Spool(default_root())
    interval = float(os.environ.get("PRISM_CONTEXT_INGEST_SECONDS", "5"))
    if not 1 <= interval <= 30:
        raise ValueError("ingest interval must be 1..30 seconds")
    last_quality, quality = 0.0, {}
    while True:
        try:
            result = tick(db, spool)
        except Exception as exc:
            result = {"errors": 1, "error": type(exc).__name__}
        if time.monotonic() - last_quality >= 60:
            try:
                quality = quality_tick(db)
                last_quality = time.monotonic()
            except Exception:
                pass  # Busy runtime; descriptive metrics can wait.
        publish_live(
            spool, "worker.json", {"heartbeat_at": now().isoformat(), "quality": quality, **result}
        )
        time.sleep(interval)


if __name__ == "__main__":
    from market_signal.config import get_settings

    run(get_settings().paths.db)
