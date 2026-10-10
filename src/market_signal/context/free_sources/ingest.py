"""Authoritative, atomic source receipt -> Phase 23 ledger -> existing event outbox."""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime

from market_signal.context import ledger
from market_signal.context.gateway.ingest import _InTransaction
from market_signal.context.gateway.spool import now
from market_signal.context.model import Observation
from market_signal.research.lab.common import canonical_json, content_id


def _exact_key(store, obs):
    # A later official report may confirm a Work/news event with the identical incident URL.
    # Keep the existing logical key rather than creating a competing official episode.
    if obs.source_ref:
        r = store.con.execute(
            "SELECT e.dedup_key FROM context_events e WHERE e.event_id IN "
            "(SELECT event_id FROM context_events WHERE source_ref=? UNION ALL "
            "SELECT event_id FROM context_event_updates WHERE source_ref=?) "
            "ORDER BY first_seen_at,event_id LIMIT 1",
            [obs.source_ref, obs.source_ref],
        ).fetchone()
        if r and r[0]:
            return obs.model_copy(update={"dedup_key": r[0]})
    return obs


def drain(store, spool, *, limit=20):
    count = errors = 0
    for rec in spool.pending()[:limit]:
        rid = rec["receipt_id"]
        try:
            if content_id("", rec["observations"]) != rec["observations_sha256"]:
                raise ValueError("receipt content mismatch")
            prior = store.con.execute(
                "SELECT result FROM context_free_ingests WHERE receipt_id=?", [rid]
            ).fetchone()
            if prior:
                result = json.loads(prior[0])
            else:
                received = datetime.fromisoformat(rec["gateway_received_at"])
                result = {
                    "receipt_id": rid,
                    "gateway_received_at": received.isoformat(),
                    "provider": rec["source"]["id"],
                    "outcomes": [],
                }
                total = ledger.IngestResult()
                with store.transaction():
                    proxy = _InTransaction(store)
                    for index, raw in enumerate(rec["observations"]):
                        obs = _exact_key(store, Observation.model_validate(raw))
                        if obs.source_id != rec["source"]["id"]:
                            raise ValueError("receipt provider mismatch")
                        ledger.register_source(
                            proxy, obs.source_id, obs.source_type, rec["source"], now=received
                        )
                        out = ledger.ingest(proxy, [obs], run_id=rid, now=received)
                        if out.rejected:
                            raise ValueError("ledger rejected receipt")
                        eid, _ = ledger._find_event(store, obs, received)
                        total.new_events.extend(out.new_events)
                        total.new_updates.extend(out.new_updates)
                        total.duplicates += out.duplicates
                        outcome = {
                            "index": index,
                            "logical_event_id": eid,
                            **out.as_dict(),
                            "observation": raw,
                        }
                        result["outcomes"].append(outcome)
                        store.con.execute(
                            "INSERT INTO context_free_observations VALUES (?,?,?,?,?,?)",
                            [
                                rid + ":" + str(index),
                                obs.source_id,
                                eid,
                                received,
                                canonical_json(raw),
                                canonical_json(out.as_dict()),
                            ],
                        )
                    result.update(
                        ingest_recorded_at=max(now(), received).isoformat(), **total.as_dict()
                    )
                    ledger.record_run(
                        proxy,
                        rec["source"]["id"],
                        received,
                        status="ok",
                        result=total,
                        received=len(rec["observations"]),
                        run_id=rid,
                        payload=rec["stats"],
                    )
                    store.con.execute(
                        "INSERT INTO context_free_ingests VALUES (?,?,?)",
                        [rid, result["ingest_recorded_at"], canonical_json(result)],
                    )
            # DB already committed. A crash here recovers outbox/enrollment idempotently.
            available = max(now(), datetime.fromisoformat(result["ingest_recorded_at"])).isoformat()
            result["context_available_at"] = available
            for outcome in result["outcomes"]:
                if not (outcome["new_events"] or outcome["new_updates"]):
                    continue
                completion = dict(
                    receipt_id=rid + ":" + str(outcome["index"]),
                    provider=result["provider"],
                    logical_event_id=outcome["logical_event_id"],
                    ingest_status="INGESTED",
                    context_available_at=available,
                    gateway_received_at=result["gateway_received_at"],
                )
                from market_signal.context.work_research import enroll
                from market_signal.ops.evaluation_triggers import gateway_wakeup

                gateway_wakeup(store, completion, {})
                raw = outcome["observation"]
                enroll(
                    store,
                    completion,
                    {
                        "receipt_id": completion["receipt_id"],
                        "auth_identity": "public_free_source",
                        "gateway_received_at": result["gateway_received_at"],
                        "payload": {"item": raw},
                    },
                    origin="free",
                )
            spool.complete(rid, result)
            count += 1
        except Exception as exc:
            with suppress(OSError):
                spool.attempt(rid, type(exc).__name__)
            errors += 1
            break  # Preserve causal order during writer failure/catch-up.
    return {"ingested": count, "errors": errors, "backlog": len(spool.pending())}
