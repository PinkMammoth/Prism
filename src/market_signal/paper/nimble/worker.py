"""One cheap paper worker. No DB held between ticks; existing runtime lock always applies."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import closing
from pathlib import Path

from market_signal.data.store import DatabaseBusy, Store
from market_signal.microstructure.ingest import ingest
from market_signal.microstructure.spool import Spool, default_root
from market_signal.models.domain import utcnow
from market_signal.ops.runtime import RuntimeBusy, require_authoritative, runtime_id, runtime_lock
from market_signal.paper.v2 import spec as baseline
from market_signal.paper.v2.data import ts
from market_signal.research.lab.common import canonical_json

from . import engine, spec, triggers
from .data import Sources, live_cache, quotes_for_store

log = logging.getLogger("prism.nimble")


def marker(db):
    return db.parent / "paper_nimble_active.json"


def publish(path, payload):
    temp = path.with_suffix(".tmp")
    temp.write_text(canonical_json(payload))
    os.replace(temp, path)


class Worker:
    def __init__(self, db: Path):
        self.db = db
        self.root = default_root(db)
        self.spool = Spool(self.root)
        self.rid = None
        self.cached_positions = []
        self.cached_account = {}
        self.last_minute = None
        self.signal_signature = None
        self.last_offsets = {}
        self.last_discovery = None
        self.failures = 0
        self.last_success = None

    def _spool_changed(self):
        files = self.spool.files()
        # Only the newest two hourly files may have fresh writes.
        offsets = {str(p): p.stat().st_size for p in files[-2:]}
        return offsets != self.last_offsets, offsets

    def _action(self, qs, ctx, now):
        for p in self.cached_positions:
            q = engine._quote(qs, p["asset"], now)
            if p["state"] == "PENDING":
                if (ts(now) - ts(p["intent_at"])).total_seconds() > 60 or (
                    q and ts(q.at) > ts(p["intent_at"])
                ):
                    return True
            elif p["state"] == "EXIT_PENDING":
                if q and ts(q.at) > ts(p["exit_intent_at"]):
                    return True
            else:
                rate = (ctx.get(p["asset"]) or {}).get("funding_rate") or 0.0
                if engine.exit_condition(
                    p,
                    q,
                    (),
                    now,
                    rate,
                    self.cached_account.get("status", "ACTIVE"),
                    self.cached_account.get("stop_reason", "ADMIN_STOP"),
                ):
                    return True
        return False

    def tick(self, *, now=None, force=False):
        at = ts(now or utcnow())
        started = time.perf_counter()
        cpu = time.process_time()
        m = marker(self.db)
        if not self.rid and not m.exists() and not force:
            return {"state": "NOT_ACTIVATED", "db_opened": False}
        qs, ctx = live_cache(self.root, at, runtime_id=runtime_id())
        minute = at.floor("min")
        minute_due = force or self.last_minute != minute
        changed, offsets = self._spool_changed()
        # Context worker atomically touches a notice after committing the outbox.
        notice = self.db.parent / "paper_nimble_notice.json"
        signature = notice.stat().st_mtime_ns if notice.exists() else None
        wake = signature != self.signal_signature
        action = self._action(qs, ctx, at)
        if not (minute_due or wake or action or changed):
            return {"state": "IDLE", "db_opened": False}
        try:
            with runtime_lock(self.db, "paper_nimble", wait=0):
                require_authoritative(self.db)
                with closing(Store(self.db, lock_timeout=0)) as store:
                    rs = engine.runs(store)
                    if not rs:
                        return {"state": "NOT_ACTIVATED", "db_opened": True}
                    run = rs[-1]
                    self.rid = run["run_id"]
                    engine.load(store, self.rid)
                    if changed:
                        result = ingest(store, self.spool, now=at.to_pydatetime())
                        if result["status"] != "ok":
                            raise ValueError("microstructure ingest conflict")
                    at = ts(now or utcnow())
                    qs, ctx = quotes_for_store(store, at)
                    if changed or minute_due:
                        triggers.discover(store, at, run["activated_at"])
                    pending = triggers.pending(store, self.rid, at, run["activated_at"])
                    source = Sources(store, at)
                    source.run_id = self.rid
                    ps = engine.positions(store, self.rid)
                    assets = sorted({p["asset"] for p in ps if p["state"] != "PENDING"})
                    updates = source.updates(assets) if minute_due or changed or wake else []
                    # Bounded two-day funding read includes all maximum 8h holds/accounting retries.
                    # For older unsettled closes, retain their actual entry boundary.
                    unresolved = store.con.execute(
                        "SELECT min(json_extract_string(payload,'$.entry_at')) "
                        "FROM paper_nimble_positions WHERE run_id=? AND state='CLOSED' "
                        "AND json_extract(payload,'$.accounting_final')=false",
                        [self.rid],
                    ).fetchone()[0]
                    since = min(ts(unresolved) if unresolved else at, at - pd_delta(days=2))
                    rates = source.funding(since)
                    totals = {}
                    evaluated = []
                    at = ts(now or utcnow())
                    qs, ctx = quotes_for_store(store, at)
                    with store.transaction():
                        engine.monitor(
                            store,
                            self.rid,
                            qs,
                            rates,
                            updates,
                            ctx,
                            now=at,
                            mark=minute_due or action,
                        )
                        # Capital is now released for a confirmed close, before fresh admission.
                        grouped = {}
                        for t in pending:
                            grouped.setdefault(t["source"], set()).update(t["assets"])
                        for origin, affected in grouped.items():
                            phase = {"technical": 22, "positioning": 23, "microstructure": 24}.get(
                                origin
                            )
                            # No released event-entry identities: record dispatch with zero entries.
                            # Context updates still invalidate/corroborate affected live positions.
                            hs = [h for h in baseline.bootstrap() if h.source_phase == phase]
                            # 24B features stay on their ORIGINAL 15m research grid. Every complete
                            # minute can wake the engine; ledger checks skip earlier evaluated grids.
                            target_assets = sorted(set(baseline.COINS) & affected)
                            obs = (
                                source.observations(hs, run["activated_at"], assets=target_assets)
                                if hs
                                else []
                            )
                            decision_at = ts(now or utcnow())
                            entry_quotes, entry_contexts = quotes_for_store(store, decision_at)
                            r = engine.admit(
                                store,
                                self.rid,
                                obs,
                                entry_quotes,
                                source,
                                entry_contexts,
                                now=decision_at,
                            )
                            for k, v in r.items():
                                totals[k] = totals.get(k, 0) + v
                            evaluated.append(
                                {
                                    "source": origin,
                                    "assets": target_assets,
                                    "hypotheses": len(hs),
                                    "observations": len(obs),
                                }
                            )
                        # Research curves require only minute work, and use independent horizon data.
                        if minute_due:
                            outstanding = engine.rows(
                                store,
                                "SELECT * FROM paper_nimble_research_pending WHERE run_id=?",
                                [self.rid],
                            )
                            if outstanding:
                                r_assets = sorted(
                                    {json.loads(p["payload"])["asset"] for p in outstanding}
                                )
                                r_since = min(ts(p["reference_at"]) for p in outstanding)
                                research_qs = source.research_quotes(r_assets, r_since)
                                engine.research(store, self.rid, research_qs, now=at)
                            engine.reconcile(store, self.rid, rates, at)
                            engine.degrade(store, self.rid, at)
                        for t in pending:
                            if t["source"] == "context":
                                for p in engine.positions(store, self.rid):
                                    if p["asset"] in t["assets"]:
                                        engine.event(
                                            store,
                                            self.rid,
                                            "corroboration",
                                            "context:" + t["trigger_id"] + ":" + p["trade_id"],
                                            {
                                                "trade_id": p["trade_id"],
                                                "event_ids": t["event_ids"],
                                                "source_latency": t["metadata"],
                                                "context": source.context(p["asset"]),
                                                "exposure_change": 0,
                                            },
                                            at,
                                        )
                            store.con.execute(
                                "INSERT INTO paper_nimble_trigger_receipts VALUES (?,?,?,?) "
                                "ON CONFLICT DO NOTHING",
                                [
                                    self.rid,
                                    t["trigger_id"],
                                    at.to_pydatetime(),
                                    canonical_json(
                                        {
                                            "source": t["source"],
                                            "assets": t["assets"],
                                            "notice_latency_seconds": (
                                                at - ts(t["available_at"])
                                            ).total_seconds(),
                                            "targeted": True,
                                            "event_ids": t["event_ids"],
                                            "metadata": t["metadata"],
                                        }
                                    ),
                                ],
                            )
                        if action and not minute_due:
                            engine._mark(store, self.rid, qs, rates, updates, ctx, at)
                    self.cached_positions = engine.positions(store, self.rid)
                    self.cached_account = engine.account(store, self.rid)
                    self.last_minute = minute
                    self.signal_signature = signature
                    self.last_offsets = offsets
                    self.last_success = at.isoformat()
            self.failures = 0
            return {
                "state": "OK",
                "db_opened": True,
                "run_id": self.rid,
                "evaluated": evaluated,
                "admission": totals,
                "wall_seconds": time.perf_counter() - started,
                "cpu_seconds": time.process_time() - cpu,
                "open_positions": len(self.cached_positions),
                "last_success_at": self.last_success,
            }
        except (RuntimeBusy, DatabaseBusy):
            return {"state": "BUSY", "db_opened": False, "last_success_at": self.last_success}

    def run(self):
        require_authoritative(self.db)
        while True:
            try:
                result = self.tick()
            except Exception as exc:
                self.failures += 1
                result = {
                    "state": "ERROR",
                    "error": type(exc).__name__,
                    "failure_streak": self.failures,
                }
                log.exception("nimble worker failed")
                if self.failures == 3:
                    from market_signal.ops.runtime import infra_alert

                    infra_alert("paper_nimble failed three times: " + type(exc).__name__)
            publish(
                self.db.parent / "paper_nimble_worker.json",
                {"heartbeat_at": utcnow().isoformat(), **result},
            )
            time.sleep(spec.EXECUTION["monitor_seconds"])


def pd_delta(**kwargs):
    import pandas as pd

    return pd.Timedelta(**kwargs)


if __name__ == "__main__":
    from market_signal.config import get_settings

    logging.basicConfig(level=logging.INFO)
    Worker(get_settings().paths.db).run()
