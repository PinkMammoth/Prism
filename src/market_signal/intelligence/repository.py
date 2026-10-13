"""Bounded SELECT-only adapter over existing market/context tables or a minimal export.

No Store construction, schema creation, migrations, claims or recording on a read.
An evidence manifest embeds the selected source records, so resolution needs no live DB.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from market_signal.intelligence.clocks import AccessClock, ClockBasis, utc
from market_signal.intelligence.definitions import DEFINITION
from market_signal.research.lab.common import content_id

TABLES = frozenset(
    {
        "context_events",
        "context_event_updates",
        "context_asset_links",
        "context_sources",
        "context_hl_oi_hourly",
        "context_ls_ratios",
        "perp_oi_history",
        "perp_snapshots",
        "perp_funding",
        "perp_intraday_bars",
        "perp_intraday_revisions",
        "macro_observations",
        "ingestion_runs",
    }
)


def normalise(value):
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return normalise(value.item())
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalise(v) for v in value]
    return value


def evidence_id(table, key, record):
    return content_id("intevidence_", {"table": table, "key": key, "record": normalise(record)})


class ReadRepository:
    def __init__(self, connection, *, origin_id="local", consumer_id="local", clocks=None):
        if not origin_id or not consumer_id:
            raise ValueError("origin and consumer identities required")
        self._con = connection
        self.origin_id, self.consumer_id = origin_id, consumer_id
        self.clocks = dict(clocks or {})
        if any(not isinstance(c, AccessClock) for c in self.clocks.values()):
            raise ValueError("access catalog values must be AccessClock records")
        self.tables = {
            r[0]
            for r in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        self._owned = False

    @classmethod
    def open(cls, path, **kwargs):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        con = duckdb.connect(str(path), read_only=True)
        try:
            result = cls(con, **kwargs)
        except Exception:
            con.close()
            raise
        result._owned = True
        return result

    def close(self):
        if self._owned:
            self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def rows(self, table, *, where="", params=(), order=""):
        """SQL fragments are internal code-owned constants, never API input."""
        if table not in TABLES:
            raise ValueError("not an intelligence source table")
        if re.search(
            r";|--|/\*|\b(insert|delete|update|create|drop|alter|attach|copy|call|pragma)\b",
            where + " " + order,
            re.I,
        ):
            raise ValueError("only read predicates are permitted")
        if table not in self.tables:
            return []
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + where
        if order:
            sql += " ORDER BY " + order
        sql += f" LIMIT {DEFINITION['max_rows'] + 1}"
        cur = self._con.execute(sql, list(params))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        if len(rows) > DEFINITION["max_rows"]:
            raise ValueError("intelligence source row budget exceeded")
        return rows


class ReadOperation:
    def __init__(self, repository, cutoff, basis):
        self.repo, self.cutoff = repository, utc(cutoff)
        self.basis = ClockBasis(basis)
        self.evidence = {}
        # A static policy notice prevents later unclocked backfills from changing
        # an earlier package merely by introducing a new diagnostic.
        self.warnings = {"clock unproven: any input lacking access evidence is withheld"}
        self._rows = 0
        self._ingestions = {}
        self._ingestion_records = {}

    def rows(self, table, **kwargs):
        if table not in self.repo.tables:
            self.warnings.add(f"source unavailable: {table}")
        rows = self.repo.rows(table, **kwargs)
        self._rows += len(rows)
        if self._rows > DEFINITION["max_rows"]:
            raise ValueError("intelligence operation row budget exceeded")
        return rows

    def include(self, table, key, row, observed_at, *, market_time=None):
        """Only accessible evidence enters the manifest; future rows affect no diagnostics."""
        clean = normalise(row)
        eid = evidence_id(table, key, clean)
        explicit = self.repo.clocks.get(eid)
        observed = utc(observed_at) if observed_at is not None else None
        if observed is not None and observed > self.cutoff:
            return None
        if explicit is not None:
            if explicit.origin_id != self.repo.origin_id:
                raise ValueError("access catalog origin mismatch")
            # Receipt evidence may conservatively delay a historical/source-rule timestamp.
            if observed is not None and utc(explicit.origin_observed_at) < observed:
                raise ValueError("access catalog backdates source observation")
            clock = explicit
        elif observed is not None:
            clock = AccessClock(self.repo.origin_id, observed.isoformat())
        else:
            clock = None
        at = clock.time(self.basis, self.repo.consumer_id) if clock else None
        if market_time is not None and utc(market_time) > self.cutoff:
            return None
        if at is None:
            return None
        if at > self.cutoff:
            return None
        self.evidence[eid] = {
            "evidence_id": eid,
            "table": table,
            "key": key,
            "record": clean,
            "clocks": clock.as_dict(),
            "selected_basis": self.basis.value,
            "selected_available_at": at.isoformat(),
        }
        run = self._ingestion_records.get(row.get("ingest_run_id"))
        if run and table != "ingestion_runs":
            rid = evidence_id("ingestion_runs", run["run_id"], run)
            self.evidence[rid] = {
                "evidence_id": rid,
                "table": "ingestion_runs",
                "key": run["run_id"],
                "record": normalise(run),
                "clocks": clock.as_dict(),
                "selected_basis": self.basis.value,
                "selected_available_at": at.isoformat(),
            }
            self.evidence[eid]["provenance_evidence_ids"] = [rid]
        if len(self.evidence) > DEFINITION["max_rows"]:
            raise ValueError("intelligence manifest row budget exceeded")
        return eid

    def observed_ingestion(self, row):
        """Funding/macro release rules are not receipt clocks.

        Completed ingestion is a conservative observation bound, not proven commit.
        Mutable funding rows bind to their CURRENT run: unavailable earlier values
        cannot be reconstructed without a previously frozen package.
        """
        run_id = row.get("ingest_run_id")
        if not run_id:
            return None
        if run_id in self._ingestions:
            return self._ingestions[run_id]
        runs = self.rows("ingestion_runs", where="run_id=?", params=[run_id])
        observed = (
            runs[0]["finished_at"] if runs and runs[0]["status"] in ("ok", "partial") else None
        )
        if observed is not None:
            self._ingestion_records[run_id] = runs[0]
        self._ingestions[run_id] = observed
        return observed

    def manifest(self):
        return {k: self.evidence[k] for k in sorted(self.evidence)}


def decode_json(value):
    return json.loads(value) if isinstance(value, str) else value
