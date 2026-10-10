"""Authoritative runtime only. The public gateway never imports this module."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime

from market_signal.context import ledger
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.model import Observation
from market_signal.research.lab.common import content_id


class _InTransaction:
    """Ledger commits inside the receipt's outer atomic transaction."""

    def __init__(self, store):
        self.con = store.con

    def transaction(self):
        return nullcontext()


def drain(store, spool: Spool, *, limit: int = 50) -> dict:
    count, errors = 0, 0
    for rec in spool.pending()[:limit]:
        rid = rec["receipt_id"]
        try:
            if content_id("", rec["payload"]) != rec["payload_sha256"]:
                raise ValueError("payload hash mismatch")
            with spool.lock():
                seal = spool.seal(rid)
            prior = store.con.execute(
                "SELECT result FROM context_gateway_ingests WHERE receipt_id=?", [rid]
            ).fetchone()
            if prior:
                result = json.loads(prior[0])
            else:
                received = datetime.fromisoformat(rec["gateway_received_at"])
                started = max(now(), received, datetime.fromisoformat(seal["spool_fsynced_at"]))
                result = {
                    "receipt_id": rid,
                    "gateway_received_at": received.isoformat(),
                    **seal,
                    "ingest_started_at": started.isoformat(),
                    "logical_event_id": None,
                }
                # TEST receipts exercise durable transport and transactions, but never the ledger.
                with store.transaction():
                    if rec["payload"].get("test", False):
                        result["ingest_status"] = "TEST_EXCLUDED"
                    else:
                        obs = Observation.model_validate(rec["observation"]).model_copy(
                            update={"update_kind": "corroboration"}
                        )
                        ledger.register_source(
                            _InTransaction(store),
                            "chatgpt_work_v1",
                            obs.source_type,
                            payload={
                                "provider_version": "chatgpt_work_v1",
                                "gateway_version": "context_gateway_v1",
                            },
                            now=received,
                        )
                        out = ledger.ingest(
                            _InTransaction(store),
                            [obs],
                            run_id=rid,
                            now=received,
                            corroboration_only=True,
                        )
                        if out.rejected:
                            raise ValueError("ledger rejected validated observation")
                        if out.new_events:
                            eid = out.new_events[0]
                        elif out.new_updates:
                            eid = store.con.execute(
                                "SELECT event_id FROM context_event_updates WHERE update_id=?",
                                [out.new_updates[0]],
                            ).fetchone()[0]
                        else:
                            obs = obs.model_copy(
                                update={
                                    "entities": tuple(
                                        ledger.resolved_entities(obs, ledger.load_entities())
                                    )
                                }
                            )
                            eid, _ = ledger._find_event(store, obs, received)
                        result.update(
                            ingest_status="INGESTED", logical_event_id=eid, **out.as_dict()
                        )
                        ledger.record_run(
                            _InTransaction(store),
                            "chatgpt_work_v1",
                            received,
                            result=out,
                            received=1,
                            status="ok",
                            latency_ms=(max(now(), started) - received).total_seconds() * 1000,
                            run_id=rid,
                            payload={
                                "receipt_id": rid,
                                "sender_version": rec["payload"]["sender_version"],
                                "integration_id": rec["auth_identity"],
                            },
                        )
                    result["ingest_recorded_at"] = max(now(), started).isoformat()
                    store.con.execute(
                        "INSERT INTO context_gateway_ingests VALUES (?, ?, ?)",
                        [rid, result["ingest_recorded_at"], json.dumps(result)],
                    )
                # Context is available after COMMIT, never before. Durable completion is a conservative
                # upper bound. If killed here, recovery publishes an upper bound with recovery=true.
                result["context_available_at"] = max(
                    now(), datetime.fromisoformat(result["ingest_recorded_at"])
                ).isoformat()
            if "context_available_at" not in result:
                result.update(
                    context_available_at=max(
                        now(), datetime.fromisoformat(result["ingest_recorded_at"])
                    ).isoformat(),
                    recovered_after_commit=True,
                )
            # Information-only outbox; the public server cannot reach a paper evaluator.
            from market_signal.ops.evaluation_triggers import gateway_wakeup

            gateway_wakeup(store, result, rec["payload"])
            spool.complete(rid, result)
            count += 1
        except Exception as exc:
            spool.attempt(rid, type(exc).__name__)  # no payload or secret in diagnostics
            errors += 1
            break  # ordered catch-up; avoid hammering a locked/broken DB
    return {"ingested": count, "errors": errors, "backlog": len(spool.pending())}
