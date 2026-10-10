"""Bounded five-second single-writer handoff; no DuckDB held while idle."""

from __future__ import annotations

import time
from contextlib import closing

from market_signal.context.free_sources.ingest import drain
from market_signal.context.free_sources.spool import Spool, default_root, replace_durable
from market_signal.context.gateway.spool import now


def tick(db, spool, *, research=False):
    from market_signal.data.store import DatabaseBusy, Store
    from market_signal.ops.runtime import RuntimeBusy, require_authoritative, runtime_lock

    if not spool.pending() and not research:
        return {"ingested": 0, "backlog": 0, "db_opened": False}
    try:
        with runtime_lock(db, "free_event_ingest", wait=0):
            require_authoritative(db)
            with closing(Store(db, lock_timeout=0)) as store:
                result = drain(store, spool)
                if research:
                    from market_signal.context.work_research import collect

                    result["reactions"] = collect(store, now=now())
                return result
    except (RuntimeBusy, DatabaseBusy):
        return {"busy": True, "backlog": len(spool.pending())}


def run(db):
    from market_signal.ops.runtime import require_authoritative

    require_authoritative(db)
    spool, last = Spool(default_root()), 0.0
    while True:
        due = time.monotonic() - last >= 60
        try:
            result = tick(db, spool, research=due)
            if due and not result.get("busy"):
                last = time.monotonic()
        except Exception as exc:
            result = {"error": type(exc).__name__}
        replace_durable(spool.root / "worker.json", {"heartbeat_at": now().isoformat(), **result})
        time.sleep(5)


if __name__ == "__main__":
    from market_signal.config import get_settings

    run(get_settings().paths.db)
