"""Deterministic reporting over persisted paper ledgers and projections.

Only SELECT queries. Trade payloads stay on the server; callers paginate normalized rows.
No migrations, policy registration, mark generation, or execution state recovery.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from market_signal.paper.nimble.report import capture_metrics, metrics
from market_signal.research.lab.common import content_id

SYSTEMS = {"v2": "Paper v2 fixed-horizon baseline", "nimble": "paper_nimble_v1"}
EXIT_REASONS = (
    "TAKE_PROFIT",
    "PRICE_INVALIDATION",
    "STATE_INVALIDATION",
    "THESIS_EXPIRED",
    "COST_DECAY",
    "MAX_HOLD",
    "CATASTROPHE_GUARD",
)
SORT_FIELDS = {
    "exit_at",
    "entry_at",
    "asset",
    "side",
    "net_pnl",
    "trade_return",
    "hold_minutes",
    "exit_reason",
}


def timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    parsed = cast(pd.Timestamp, pd.Timestamp(value))
    if pd.isna(parsed):
        raise ValueError("Missing or invalid observation timestamp")
    return cast(
        pd.Timestamp,
        parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC"),
    )


def required_timestamp(value: Any) -> pd.Timestamp:
    result = timestamp(value)
    if result is None:
        raise ValueError("Required observation timestamp is missing")
    return result


def rows(con, sql, args=()) -> list[dict[str, Any]]:
    result = con.execute(sql, list(args))
    return [
        dict(zip([c[0] for c in result.description], row, strict=True)) for row in result.fetchall()
    ]


def _json(value) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else (value or {})


def _mean(values):
    valid = [v for v in values if v is not None and pd.notna(v)]
    return sum(valid) / len(valid) if valid else None


def _sum(trades, field):
    values = [t.get(field) for t in trades]
    return sum(values) if values and all(v is not None for v in values) else None


def _latest(con, table, rid, kind):
    result = con.execute(
        f"SELECT payload FROM {table} WHERE run_id=? AND event_type=? ORDER BY seq DESC LIMIT 1",
        [rid, kind],
    ).fetchone()
    return _json(result[0]) if result else {}


def runs(con) -> list[dict[str, Any]]:
    """Names/identities/activation and policy capital come from stored production metadata."""
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    out = []
    for system, name in SYSTEMS.items():
        prefix = "paper_v2" if system == "v2" else "paper_nimble"
        if f"{prefix}_runs" not in tables:
            continue
        for run in rows(con, f"SELECT * FROM {prefix}_runs ORDER BY activated_at DESC,run_id"):
            definition = _json(run["definition"])
            policy = (
                con.execute(
                    "SELECT definition FROM paper_v2_policies WHERE policy_id=?",
                    [run["risk_policy_id"]],
                ).fetchone()
                if "paper_v2_policies" in tables
                else None
            )
            starting = _json(policy[0]).get("starting_equity") if policy else None
            if starting is None and system == "nimble":
                starting = (
                    definition.get("baseline", {})
                    .get("definition", {})
                    .get("risk", {})
                    .get("starting_equity")
                )
            if system == "v2":
                frozen = _json(
                    con.execute(
                        "SELECT definition FROM paper_v2_universes WHERE universe_id=?",
                        [run["universe_id"]],
                    ).fetchone()[0]
                )
                account = _latest(con, f"{prefix}_events", run["run_id"], "mark")
                state = _latest(con, f"{prefix}_events", run["run_id"], "run_state").get(
                    "state", "ACTIVE"
                )
                evaluation = con.execute(
                    "SELECT max(evaluated_at) FROM paper_v2_evaluations WHERE run_id=?",
                    [run["run_id"]],
                ).fetchone()[0]
            else:
                frozen = _json(
                    con.execute(
                        "SELECT definition FROM paper_nimble_policies WHERE policy_id=?",
                        [run["policy_id"]],
                    ).fetchone()[0]
                )
                result = con.execute(
                    "SELECT payload FROM paper_nimble_accounts WHERE run_id=?", [run["run_id"]]
                ).fetchone()
                account = _json(result[0]) if result else {}
                state = account.get("status", "UNKNOWN")
                evaluation = None
            last = con.execute(
                f"SELECT max(recorded_at) FROM {prefix}_events WHERE run_id=?", [run["run_id"]]
            ).fetchone()[0]
            out.append(
                {
                    **run,
                    "definition": definition,
                    "system": system,
                    "name": definition.get("name") or definition.get("version") or name,
                    "label": name,
                    "status": state,
                    "starting_equity": starting,
                    "equity": account.get("equity", starting),
                    "last_update": max(filter(None, [last, evaluation]), default=None),
                    "account": account,
                    "hypotheses": {
                        h.get("hypothesis_id") or content_id("paperhyp_v2_", h): h
                        for h in frozen.get("hypotheses", [])
                    },
                    "baseline_run_id": definition.get("baseline", {}).get("run", {}).get("run_id")
                    if definition.get("baseline", {}).get("run")
                    else None,
                }
            )
    return out


def freshness(path: Path, run_metadata, *, now=None, live=False, synthetic=False):
    """A recent local file mtime is never evidence of production sync or snapshot age."""
    at = required_timestamp(now or datetime.now(UTC))
    manifest = {}
    manifest_path = path.with_suffix(".snapshot.json")
    if manifest_path.is_file():
        try:
            candidate = json.loads(manifest_path.read_text())
            stat = path.stat()
            if (
                candidate.get("bytes") == stat.st_size
                and candidate.get("local_mtime_ns") == stat.st_mtime_ns
            ):
                manifest = candidate
        except (ValueError, OSError):
            pass
    latest = max(
        (required_timestamp(r["last_update"]) for r in run_metadata if r["last_update"]),
        default=None,
    )
    snapshot_at = timestamp(manifest.get("snapshot_at")) or latest
    # Also require recent paper evaluation, even if a newer backup was downloaded.
    stale = (
        snapshot_at is None
        or latest is None
        or at - min(snapshot_at, latest) > pd.Timedelta(minutes=30)
    )
    source = (
        "Synthetic demo" if synthetic else "Live / production-backed" if live else "Local snapshot"
    )
    return {
        "source": source,
        "stale": bool(stale),
        "snapshot_at": snapshot_at,
        "snapshot_basis": "verified backup timestamp"
        if manifest
        else "last paper ledger/evaluation (snapshot time unknown)",
        "last_production_sync": timestamp(manifest.get("downloaded_at")),
        "production_path": manifest.get("production_path"),
        "path": str(path),
        "last_update": latest,
        "age_seconds": max(0, (at - snapshot_at).total_seconds()) if snapshot_at else None,
        "effective_at": at if live else (snapshot_at or at),
    }


def _positions(con, run) -> list[dict[str, Any]]:
    rid = run["run_id"]
    if run["system"] == "v2":
        return rows(
            con,
            """
            SELECT event_type AS state,payload,NULL AS thesis,NULL AS research,
                   NULL AS family FROM paper_v2_events WHERE run_id=?
            AND event_type IN ('opened','closed')
            QUALIFY row_number() OVER (PARTITION BY payload->>'trade_id' ORDER BY seq DESC)=1
            ORDER BY payload->>'trade_id'
        """,
            [rid],
        )
    return rows(
        con,
        """
        SELECT p.state,p.payload,t.payload AS thesis,r.payload AS research,
               o.payload->'hypothesis'->>'family' AS family
        FROM paper_nimble_positions p
        LEFT JOIN paper_nimble_theses t ON t.trade_id=p.trade_id AND t.run_id=p.run_id
        LEFT JOIN paper_nimble_opportunities o ON o.opportunity_id=(p.payload->>'opportunity_id') AND o.run_id=p.run_id
        LEFT JOIN paper_nimble_research r ON r.opportunity_id=o.opportunity_id
            AND r.horizon_minutes=try_cast(o.payload->>'research_primary_minutes' AS INTEGER)
        WHERE p.run_id=? AND p.state IN ('OPEN','EXIT_PENDING','CLOSED') ORDER BY p.trade_id
    """,
        [rid],
    )


def normalize(record, run) -> dict[str, Any]:
    p = _json(record["payload"])
    p["state"] = {"opened": "OPEN", "closed": "CLOSED"}.get(
        record["state"], record["state"].upper()
    )
    p["run"] = run["label"]
    p["run_id"] = run["run_id"]
    p["hypothesis_id"] = p.get("hypothesis_id", p.get("primary_hypothesis_id"))
    p["hypothesis_name"] = run.get("hypotheses", {}).get(p["hypothesis_id"], {}).get("name")
    p["family"] = record.get("family") or run.get("hypotheses", {}).get(p["hypothesis_id"], {}).get(
        "family"
    )
    p["entry_at"] = timestamp(p.get("entry_at"))
    p["exit_at"] = timestamp(p.get("exit_at"))
    p["hold_minutes"] = (
        (p["exit_at"] - p["entry_at"]).total_seconds() / 60
        if p["exit_at"] and p["entry_at"]
        else None
    )
    if p["state"] == "CLOSED":
        if run["system"] == "nimble":
            p.update(capture_metrics(p, _json(record.get("research"))))
        else:
            p["mfe_capture_ratio"] = p["mae_avoidance"] = None
        if p.get("price_pnl") is None and all(
            p.get(k) is not None for k in ("units", "exit_ref", "entry_ref")
        ):
            p["price_pnl"] = (
                (1 if p["side"] == "long" else -1) * p["units"] * (p["exit_ref"] - p["entry_ref"])
            )
        p["gross_return"] = (
            p["gross_pnl"] / p["notional"]
            if p.get("gross_pnl") is not None and p.get("notional")
            else None
        )
        p["liquidation_adjustment"] = p.get("liquidation_adjustment")
        if p["liquidation_adjustment"] is None and all(
            p.get(k) is not None for k in ("net_pnl", "gross_pnl", "fees", "funding")
        ):
            p["liquidation_adjustment"] = p["net_pnl"] - (
                p["gross_pnl"] - p["fees"] - p["funding"] - p.get("funding_reserve", 0)
            )
    else:
        mark = run["account"].get("positions", {}).get(p["trade_id"], {})
        p["mark_ref"] = p.get("mark_ref", mark.get("price"))
        p["unrealised"] = p.get("unrealised", mark.get("unrealised"))
        p["mark_at"] = p.get("last_quote_received_at", mark.get("at"))
        p["mark_stale"] = mark.get("stale", False)
    thesis = _json(record.get("thesis")) or p.get("thesis", {})
    p["target"] = thesis.get("objective", {}).get("price")
    p["invalidation"] = thesis.get("invalidation", {}).get("price")
    p["invalidation_rule"] = thesis.get("invalidation", {}).get("state_rule")
    p["maximum_hold_minutes"] = thesis.get("max_hold_minutes", p.get("horizon_minutes"))
    p["exit_state"] = (
        p.get("exit_reason") or p["state"]
        if run["system"] == "nimble"
        else f"Fixed horizon {p.get('horizon_minutes', 'N/A')} min"
    )
    p["funding_accrued"] = p.get("funding_state", {}).get("known_funding")
    p["funding_reserve"] = p.get(
        "funding_reserve",
        p.get("funding_state", {}).get("adverse_reserve", 0 if run["system"] == "v2" else None),
    )
    p["accounting_final"] = p.get("accounting_final", True)
    fields = (
        "state",
        "run",
        "run_id",
        "trade_id",
        "asset",
        "side",
        "hypothesis_id",
        "hypothesis_name",
        "family",
        "entry_at",
        "exit_at",
        "hold_minutes",
        "entry_fill",
        "exit_fill",
        "entry_ref",
        "exit_ref",
        "units",
        "notional",
        "entry_fee",
        "exit_fee",
        "price_pnl",
        "gross_pnl",
        "gross_return",
        "fees",
        "slippage_cost",
        "funding",
        "funding_reserve",
        "liquidation_adjustment",
        "trade_return",
        "net_pnl",
        "exit_reason",
        "horizon_minutes",
        "mfe",
        "mae",
        "mfe_capture_ratio",
        "mae_avoidance",
        "accounting_final",
        "mark_ref",
        "mark_at",
        "mark_stale",
        "unrealised",
        "target",
        "invalidation",
        "invalidation_rule",
        "maximum_hold_minutes",
        "exit_due_at",
        "exit_state",
        "funding_accrued",
        "entry_latency_seconds",
        "exit_latency_seconds",
    )
    return {k: p.get(k) for k in fields}


def summary(trades, *, start, end, starting_equity=None):
    closed = [t for t in trades if t["state"] == "CLOSED" and start <= t["exit_at"] <= end]
    entered = [t for t in trades if start <= t["entry_at"] <= end]
    days = (end - start).total_seconds() / 86400
    # Share Phase 27's authoritative closed-trade statistics; unknown inputs stay N/A.
    stat = metrics(closed, equity=starting_equity or 1, days=max(days, 1 / 86400))
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] < 0]
    for key, field in (
        ("price_pnl", "price_pnl"),
        ("gross_pnl", "gross_pnl"),
        ("net_pnl", "net_pnl"),
        ("fees", "fees"),
        ("slippage", "slippage_cost"),
        ("funding", "funding"),
        ("liquidation_adjustment", "liquidation_adjustment"),
        ("unsettled_adverse_reserve", "funding_reserve"),
    ):
        stat[key] = _sum(closed, field)
    stat.update(
        total_trades=len(entered),
        closed_trades=len(closed),
        trades_per_day=len(entered) / days if days > 0 else None,
        average_winner=_mean(wins),
        average_loser=_mean(losses),
        average_return=_mean([t.get("trade_return") for t in closed]),
        mfe=_mean([t.get("mfe") for t in closed]),
        mae=_mean([t.get("mae") for t in closed]),
    )
    # Notional-seconds overlapped with window / starting capital-seconds, both sides.
    stat["capital_utilization"] = (
        sum(
            t["notional"]
            * max(0, (min(t["exit_at"] or end, end) - max(t["entry_at"], start)).total_seconds())
            for t in trades
        )
        / (days * 86400 * starting_equity)
        if days > 0 and starting_equity
        else None
    )
    stat["cost_drag"] = (
        (stat["fees"] + stat["slippage"] + stat["funding"])
        if all(stat[k] is not None for k in ("fees", "slippage", "funding"))
        else None
    )
    return stat


class Report:
    """A server-side report snapshot. Raw event/signal histories never reach the browser."""

    def __init__(self, con, run: dict[str, Any], *, start: Any = None, end: Any = None):
        self.run = run
        self.start = max(
            timestamp(start) or required_timestamp(run["activated_at"]),
            required_timestamp(run["activated_at"]),
        )
        self.end = required_timestamp(end or datetime.now(UTC))
        self.trades = [normalize(r, run) for r in _positions(con, run)]
        self.prefix = "paper_v2" if run["system"] == "v2" else "paper_nimble"
        # Bounded aggregate per hypothesis/asset/side, rather than raw opportunities.
        side = "side" if run["system"] == "v2" else "payload->>'side'"
        self.opportunities = rows(
            con,
            f"""
            SELECT hypothesis_id,asset,{side} AS side,payload->'hypothesis'->>'family' AS family,
                count(*) FILTER (WHERE fired) AS signals,
                count(*) FILTER (WHERE admission='ADMITTED') AS admissions
            FROM {self.prefix}_opportunities WHERE run_id=? AND evaluated_at>=? AND evaluated_at<=?
            GROUP BY ALL ORDER BY hypothesis_id,asset,side
        """,
            [run["run_id"], self.start, self.end],
        )
        families = {o["hypothesis_id"]: o["family"] for o in self.opportunities}
        for t in self.trades:
            t["family"] = t["family"] or families.get(t["hypothesis_id"])
        self.stats = summary(
            self.trades, start=self.start, end=self.end, starting_equity=run["starting_equity"]
        )
        self.cumulative = summary(
            self.trades,
            start=timestamp(run["activated_at"]),
            end=self.end,
            starting_equity=run["starting_equity"],
        )
        mark_kind = "mark" if run["system"] == "v2" else "account_mark"
        self.stats["max_drawdown"] = con.execute(
            f"""
            SELECT max(try_cast(payload->>'drawdown' AS DOUBLE)) FROM {self.prefix}_events
            WHERE run_id=? AND event_type=? AND recorded_at>=? AND recorded_at<=?
        """,
            [run["run_id"], mark_kind, self.start, self.end],
        ).fetchone()[0]
        self.stats["open_positions"] = sum(
            t["state"] in ("OPEN", "EXIT_PENDING") for t in self.trades
        )
        self.stats["starting_equity"] = run["starting_equity"]
        self.stats["current_equity"] = run["equity"]
        self.stats["realized_equity"] = (
            run["account"].get("cash")
            if run["system"] == "nimble"
            else (
                run["starting_equity"]
                + sum(t["net_pnl"] for t in self.trades if t["state"] == "CLOSED")
                - sum(t.get("entry_fee", 0) for t in self.trades if t["state"] == "OPEN")
            )
            if run["starting_equity"] is not None
            else None
        )
        self.rejections = rows(
            con,
            f"SELECT rejection_reason,count(*) AS count FROM {self.prefix}_opportunities WHERE run_id=? AND fired AND admission='REJECTED' AND evaluated_at>=? AND evaluated_at<=? GROUP BY rejection_reason ORDER BY count DESC,rejection_reason LIMIT 10",
            [run["run_id"], self.start, self.end],
        )

    def closed_page(
        self,
        *,
        asset=None,
        side=None,
        hypothesis=None,
        reason=None,
        sort="exit_at",
        descending=True,
        page=0,
        page_size=50,
    ):
        if sort not in SORT_FIELDS:
            raise ValueError("unsupported sort")
        if not 1 <= page_size <= 200 or page < 0:
            raise ValueError("invalid page")
        selected = [
            t
            for t in self.trades
            if t["state"] == "CLOSED"
            and self.start <= t["exit_at"] <= self.end
            and (not asset or t["asset"] == asset)
            and (not side or t["side"] == side)
            and (not hypothesis or t["hypothesis_id"] == hypothesis)
            and (not reason or t["exit_reason"] == reason)
        ]
        selected.sort(
            key=lambda t: (t.get(sort) is not None, t.get(sort), t["trade_id"]), reverse=descending
        )
        offset = page * page_size
        return selected[offset : offset + page_size], len(selected)

    def open_positions(self, *, as_of=None):
        return [
            {
                **t,
                "age_minutes": max(
                    0, ((timestamp(as_of) or self.end) - t["entry_at"]).total_seconds() / 60
                ),
            }
            for t in self.trades
            if t["state"] in ("OPEN", "EXIT_PENDING")
        ]

    def groups(self, dimensions=("hypothesis_id", "family", "asset", "side")):
        if not dimensions or not set(dimensions) <= {"hypothesis_id", "family", "asset", "side"}:
            raise ValueError("unsupported grouping")
        grouped, counts = defaultdict(list), defaultdict(Counter)
        for t in self.trades:
            grouped[tuple(t.get(d) for d in dimensions)].append(t)
        for o in self.opportunities:
            o = {
                **o,
                "family": o["family"]
                or self.run.get("hypotheses", {}).get(o["hypothesis_id"], {}).get("family"),
            }
            key = tuple(o.get(d) for d in dimensions)
            counts[key].update(signals=o["signals"], admissions=o["admissions"])
        result = []
        if dimensions == ("side",):
            grouped.setdefault(("long",), [])
            grouped.setdefault(("short",), [])
        for key in sorted(grouped.keys() | counts.keys(), key=str):
            stat = summary(
                grouped[key],
                start=self.start,
                end=self.end,
                starting_equity=self.run["starting_equity"],
            )
            result.append(
                {
                    **dict(zip(dimensions, key, strict=True)),
                    "hypothesis_name": self.run.get("hypotheses", {})
                    .get(key[dimensions.index("hypothesis_id")], {})
                    .get("name")
                    if "hypothesis_id" in dimensions
                    else None,
                    **stat,
                    "signals": counts[key]["signals"],
                    "admissions": counts[key]["admissions"],
                }
            )
        return result

    def exits(self):
        # Aggregate all qualifying closed rows, independently of page limits.
        closed = [
            t
            for t in self.trades
            if t["state"] == "CLOSED" and self.start <= t["exit_at"] <= self.end
        ]
        reasons = set(EXIT_REASONS) | {t["exit_reason"] for t in closed}
        return [
            {
                "exit_reason": reason,
                "count": len(ts),
                "net_pnl": _sum(ts, "net_pnl"),
                "average_return": _mean([t.get("trade_return") for t in ts]),
                "average_hold_minutes": _mean([t["hold_minutes"] for t in ts]),
            }
            for reason in sorted(reasons)
            if (ts := [t for t in closed if t["exit_reason"] == reason]) or reason in EXIT_REASONS
        ]

    def activity(self, con, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("invalid limit")
        return rows(
            con,
            f"""
            SELECT * FROM (
              SELECT evaluated_at AS timestamp,opportunity_id AS id,asset,
                     CASE WHEN admission='ADMITTED' THEN 'admission' WHEN admission='REJECTED' THEN 'rejected opportunity' ELSE 'signal' END AS activity,
                     hypothesis_id,admission AS detail,rejection_reason
              FROM {self.prefix}_opportunities WHERE run_id=? AND fired AND evaluated_at>=? AND evaluated_at<=?
              UNION ALL
              SELECT recorded_at,event_key,payload->>'asset',event_type,
                     coalesce(payload->>'hypothesis_id',payload->>'primary_hypothesis_id'),
                     coalesce(payload->>'exit_reason',payload->>'state'),NULL
              FROM {self.prefix}_events WHERE run_id=? AND event_type IN ('opened','closed','exit_intent') AND recorded_at>=? AND recorded_at<=?
            ) ORDER BY timestamp DESC,id DESC LIMIT ?
        """,
            [
                self.run["run_id"],
                self.start,
                self.end,
                self.run["run_id"],
                self.start,
                self.end,
                limit,
            ],
        )

    def curve(self, con, *, max_points=1000):
        """Cash steps use original accounting events, including delayed funding settlement.

        MTM is observed points only. Bucket min/max and endpoints retain their actual times.
        No buckets average returns or synthesize timestamps; activation is explicit.
        """
        mark_kind = "mark" if self.run["system"] == "v2" else "account_mark"
        result = rows(
            con,
            f"""
          WITH ledger AS (
            SELECT recorded_at AS timestamp,seq,event_type,
              CASE WHEN event_type='opened' THEN -try_cast(payload->>'entry_fee' AS DOUBLE)
                   WHEN event_type='closed' THEN try_cast(payload->>'net_pnl' AS DOUBLE)+try_cast(payload->>'entry_fee' AS DOUBLE)
                   WHEN event_type='funding_adjustment' THEN try_cast(payload->>'funding_cash_adjustment' AS DOUBLE)
                   ELSE 0 END AS delta,
              try_cast(payload->>'equity' AS DOUBLE) AS equity
            FROM {self.prefix}_events WHERE run_id=? AND recorded_at<=?
          ), observations AS (
            SELECT timestamp,seq,'Realized cash (after entry fees)' AS series,
                   ? + sum(delta) OVER (ORDER BY seq ROWS UNBOUNDED PRECEDING) AS value
            FROM ledger
            UNION ALL
            SELECT timestamp,seq,'Observed mark-to-market (provisional)',equity
            FROM ledger WHERE event_type=? AND equity IS NOT NULL
          ), windowed AS (
            SELECT * FROM observations WHERE timestamp>=? AND value IS NOT NULL
          ), buckets AS (
            SELECT *,count(*) OVER (PARTITION BY series) AS observed_samples,
                     ntile(?) OVER (PARTITION BY series ORDER BY timestamp,seq) AS bucket
            FROM windowed
          ), ranked AS (
            SELECT *,row_number() OVER (PARTITION BY series,bucket ORDER BY timestamp,seq) AS first,
                     row_number() OVER (PARTITION BY series,bucket ORDER BY timestamp DESC,seq DESC) AS last,
                     row_number() OVER (PARTITION BY series,bucket ORDER BY value,seq) AS low,
                     row_number() OVER (PARTITION BY series,bucket ORDER BY value DESC,seq DESC) AS high
            FROM buckets
          ) SELECT timestamp,seq,series,value,observed_samples FROM ranked
            WHERE first=1 OR last=1 OR low=1 OR high=1 ORDER BY timestamp,seq,series
        """,
            [
                self.run["run_id"],
                self.end,
                self.run["starting_equity"],
                mark_kind,
                self.start,
                max(1, max_points // 4),
            ],
        )
        # Include activation only at its actual time, never backfill the selected window.
        if (
            self.start <= required_timestamp(self.run["activated_at"]) <= self.end
            and self.run["starting_equity"] is not None
        ):
            result.insert(
                0,
                {
                    "timestamp": timestamp(self.run["activated_at"]),
                    "seq": 0,
                    "series": "Realized cash (after entry fees)",
                    "value": self.run["starting_equity"],
                },
            )
        return result


def comparison(baseline: Report, nimble: Report, con=None):
    start = max(baseline.start, nimble.start)
    end = min(baseline.end, nimble.end)
    stats = [
        summary(
            [t for t in r.trades if t["entry_at"] >= start],
            start=start,
            end=end,
            starting_equity=r.run["starting_equity"],
        )
        for r in (baseline, nimble)
    ]
    differences = {
        k: stats[1][k] - stats[0][k]
        if stats[1][k] is not None and stats[0][k] is not None
        else None
        for k in (
            "net_pnl",
            "average_hold_minutes",
            "capital_utilization",
            "trades_per_day",
            "cost_drag",
            "mfe_capture_ratio_mean",
        )
    }
    drawdowns: list[Any] = (
        [
            con.execute(
                f"SELECT max(try_cast(payload->>'drawdown' AS DOUBLE)) FROM {r.prefix}_events WHERE run_id=? AND event_type=? AND recorded_at>=? AND recorded_at<=?",
                [
                    r.run["run_id"],
                    "mark" if r.run["system"] == "v2" else "account_mark",
                    start,
                    end,
                ],
            ).fetchone()[0]
            for r in (baseline, nimble)
        ]
        if con is not None
        else [None, None]
    )
    differences["max_drawdown"] = (
        drawdowns[1] - drawdowns[0] if all(d is not None for d in drawdowns) else None
    )
    return {
        "start": start,
        "end": end,
        "samples": [s["closed_trades"] for s in stats],
        "differences": differences,
        "conclusion": "Insufficient sample for conclusion."
        if min(s["closed_trades"] for s in stats) < 30
        else "Descriptive comparison only; statistical superiority has not been established.",
    }
