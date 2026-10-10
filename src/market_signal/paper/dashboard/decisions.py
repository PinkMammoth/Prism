"""Bounded SELECT-only inspection of immutable paper decisions and event evidence.

Execution metrics remain in Report/Phase 27 reporting. Waiting/source observations are
separate stages, never counted as rejected trades or combined across independent runs.
"""

from __future__ import annotations

from typing import Any

from .report import Report, _json, rows

EXECUTION_KINDS = (
    "pending",
    "entry_intent",
    "opened",
    "closed",
    "exit_intent",
    "expired",
    "cancelled",
    "support",
    "funding_adjustment",
    "entry_rejected",
    "exit_deferred",
)


class Decisions:
    def __init__(self, con, report: Report):
        self.con = con
        self.report = report
        self.run = report.run
        self.prefix = report.prefix
        self.tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        self.has_events = "paper_event_evidence" in self.tables
        self.is_event_cohort = bool(self.run.get("event_variant"))

    def _ledger(self):
        """Only normalized table columns reach the browser; payloads are fetched on selection."""
        side = "side" if self.run["system"] == "v2" else "payload->>'side'"
        parts = [
            f"""
            SELECT 'opportunity' AS source,opportunity_id AS id,evaluated_at AS timestamp,
                asset,{side} AS side,hypothesis_id,'signal evaluation' AS kind,
                admission AS outcome,rejection_reason AS reason,trade_id,
                payload->'context'->'event_entry'->'state'->>'event_id' AS event_id
            FROM {self.prefix}_opportunities WHERE run_id=? AND fired
            AND evaluated_at>=? AND evaluated_at<=?
        """,
            f"""
            SELECT 'execution',cast(seq AS VARCHAR),recorded_at,payload->>'asset',
                payload->>'side',coalesce(payload->>'hypothesis_id',payload->>'primary_hypothesis_id'),
                event_type,coalesce(payload->>'state',event_type),
                coalesce(payload->>'exit_reason',payload->>'reason'),payload->>'trade_id',
                payload->'event_entry'->'state'->>'event_id'
            FROM {self.prefix}_events WHERE run_id=? AND event_type IN
            ({",".join(repr(k) for k in EXECUTION_KINDS)})
            AND recorded_at>=? AND recorded_at<=?
        """,
        ]
        args = [self.run["run_id"], self.report.start, self.report.end] * 2
        if self.has_events and self.is_event_cohort:
            parts.append("""
                SELECT 'event evidence',evidence_id,recorded_at,asset,NULL,hypothesis_id,kind,
                    CASE WHEN kind='waiting' THEN 'WAITING'
                         WHEN kind='source_viability' THEN 'OBSERVED_GATES'
                         WHEN payload->>'reason' IS NOT NULL THEN 'REJECTED'
                         WHEN kind='decision' THEN coalesce(payload->>'admission','DECISION')
                         ELSE upper(kind) END,
                    payload->>'reason',payload->>'trade_id',event_id
                FROM paper_event_evidence WHERE run_id=? AND recorded_at>=? AND recorded_at<=?
            """)
            args.extend([self.run["run_id"], self.report.start, self.report.end])
        return " UNION ALL ".join(parts), args

    def page(
        self,
        *,
        source=None,
        reason=None,
        asset=None,
        event_id=None,
        trade_id=None,
        page=0,
        page_size=50,
    ):
        if not 1 <= page_size <= 200 or page < 0:
            raise ValueError("invalid page")
        sql, args = self._ledger()
        predicates = []
        for field, value in (
            ("source", source),
            ("reason", reason),
            ("asset", asset),
            ("event_id", event_id),
            ("trade_id", trade_id),
        ):
            if value:
                predicates.append(field + "=?")
                args.append(value)
        query = f"SELECT * FROM ({sql}) WHERE " + (" AND ".join(predicates) or "TRUE")
        total = self.con.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        result = rows(
            self.con,
            query + " ORDER BY timestamp DESC,source,id DESC LIMIT ? OFFSET ?",
            [*args, page_size, page * page_size],
        )
        for row in result:
            hypothesis = self.run["hypotheses"].get(row["hypothesis_id"], {})
            row.update(
                run=self.run["label"],
                hypothesis_name=hypothesis.get("name"),
                side=row["side"] or hypothesis.get("side"),
                family=hypothesis.get("family"),
            )
        return result, total

    def reasons(self):
        sql, args = self._ledger()
        return rows(
            self.con,
            f"""
            SELECT source,kind,reason,count(*) AS count FROM ({sql}) WHERE reason IS NOT NULL
            GROUP BY ALL ORDER BY count DESC,source,kind,reason LIMIT 30
        """,
            args,
        )

    def detail(self, source, identity) -> dict[str, Any]:
        """One recorded payload, with linked trade state/frozen thesis fetched separately."""
        if source == "opportunity":
            table, field = self.prefix + "_opportunities", "opportunity_id"
        elif source == "execution":
            table, field = self.prefix + "_events", "cast(seq AS VARCHAR)"
        elif source == "event evidence" and self.has_events and self.is_event_cohort:
            table, field = "paper_event_evidence", "evidence_id"
        else:
            raise ValueError("unsupported decision source")
        record = self.con.execute(
            f"SELECT payload FROM {table} WHERE run_id=? AND {field}=?",
            [self.run["run_id"], identity],
        ).fetchone()
        if not record:
            raise ValueError("Decision unavailable in selected run")
        payload = _json(record[0])
        tid = payload.get("trade_id")
        result: dict[str, Any] = {"recorded_payload": payload}
        event = (
            payload.get("event_entry")
            or (payload.get("context") or {}).get("event_entry")
            or (payload.get("evidence") or {}).get("event")
            or (payload if source == "event evidence" else {})
        )
        facts = {
            k: payload.get(k)
            for k in (
                "signal_at",
                "available_at",
                "evaluated_at",
                "decision_at",
                "fired",
                "causal",
                "healthy",
                "warm",
                "context_available",
                "admission",
                "trade_id",
                "economic_at_evaluator",
                "remaining_target_move",
                "estimated_cost",
                "venue_available",
                "confirmation_present",
                "freshness_viable",
            )
            if k in payload
        }
        facts["reason"] = payload.get("reason") or payload.get("rejection_reason")
        facts.update(
            {
                k: event[k]
                for k in (
                    "variant",
                    "family",
                    "first_evaluator_notice_at",
                    "confirmation_delay_seconds",
                )
                if k in event
            }
        )
        provider = event.get("report") or {}
        facts.update(
            {k: provider[k] for k in ("source_id", "published_at", "observed_at") if k in provider}
        )
        result["facts"] = [
            {"Field": k.replace("_", " "), "Recorded value": str(v) if v is not None else None}
            for k, v in facts.items()
        ]
        result["source_timings"] = event.get("source_timings", [])
        if tid and self.run["system"] == "nimble":
            trade = self.con.execute(
                "SELECT payload FROM paper_nimble_positions WHERE run_id=? AND trade_id=?",
                [self.run["run_id"], tid],
            ).fetchone()
            thesis = self.con.execute(
                "SELECT payload FROM paper_nimble_theses WHERE run_id=? AND trade_id=?",
                [self.run["run_id"], tid],
            ).fetchone()
            result["latest_trade_state"] = _json(trade[0]) if trade else None
            result["frozen_entry_thesis"] = _json(thesis[0]) if thesis else None
        elif tid:
            trade = self.con.execute(
                """
                SELECT payload FROM paper_v2_events WHERE run_id=? AND (payload->>'trade_id')=?
                AND event_type IN ('pending','opened','closed') ORDER BY seq DESC LIMIT 1
            """,
                [self.run["run_id"], tid],
            ).fetchone()
            result["latest_trade_state"] = _json(trade[0]) if trade else None
        return result

    def event_overview(self):
        if not self.is_event_cohort:
            return None
        if not self.has_events:
            return {"available": False}
        rid, start, end = self.run["run_id"], self.report.start, self.report.end
        counts = rows(
            self.con,
            """
            SELECT count(DISTINCT event_id) AS logical_events,
                count(*) FILTER (WHERE kind='classification') AS classification_observations,
                count(*) FILTER (WHERE kind='classification' AND (payload->>'reason') IS NOT NULL) AS classification_rejections,
                count(*) FILTER (WHERE kind='enrolled') AS enrollments,
                count(*) FILTER (WHERE kind='waiting') AS confirmation_wait_observations,
                count(*) FILTER (WHERE kind='decision') AS terminal_event_decisions,
                count(*) FILTER (WHERE kind='decision' AND (payload->>'reason') IS NOT NULL) AS rejected_event_decisions
            FROM paper_event_evidence WHERE run_id=? AND recorded_at>=? AND recorded_at<=?
        """,
            [rid, start, end],
        )[0]
        counts["pending_current"] = (
            self.con.execute(
                "SELECT count(*) FROM paper_event_pending WHERE run_id=?", [rid]
            ).fetchone()[0]
            if "paper_event_pending" in self.tables
            else None
        )
        by_hypothesis = {
            r["hypothesis_id"]: r
            for r in rows(
                self.con,
                """
            SELECT hypothesis_id,count(DISTINCT event_id) AS logical_events,
                count(*) FILTER (WHERE kind='enrolled') AS enrollments,
                count(*) FILTER (WHERE kind='waiting') AS confirmation_wait_observations,
                count(*) FILTER (WHERE kind='decision') AS terminal_event_decisions
            FROM paper_event_evidence WHERE run_id=? AND recorded_at>=? AND recorded_at<=?
            GROUP BY hypothesis_id
        """,
                [rid, start, end],
            )
        }
        performance = {r["hypothesis_id"]: r for r in self.report.groups(("hypothesis_id",))}
        hypotheses = []
        for hid, h in self.run["hypotheses"].items():
            if h.get("source_phase") != 30:
                continue
            stat = performance.get(hid, {})
            hypotheses.append(
                {
                    "hypothesis_id": hid,
                    "hypothesis_name": h.get("name"),
                    "family": h.get("family"),
                    "side": h.get("side"),
                    "variant": h.get("variant"),
                    **{
                        k: by_hypothesis.get(hid, {}).get(k, 0)
                        for k in (
                            "logical_events",
                            "enrollments",
                            "confirmation_wait_observations",
                            "terminal_event_decisions",
                        )
                    },
                    **{
                        k: stat.get(k, 0)
                        for k in ("signals", "admissions", "total_trades", "closed_trades")
                    },
                    **{
                        k: stat.get(k)
                        for k in (
                            "win_rate",
                            "expectancy_per_trade",
                            "net_pnl",
                            "average_hold_minutes",
                            "mfe",
                            "mae",
                        )
                    },
                }
            )
        return {
            "available": True,
            "counts": counts,
            "hypotheses": hypotheses,
            "components": self.report.groups(("execution_component",)),
        }
