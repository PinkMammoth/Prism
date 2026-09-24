"""DuckDB storage.

Schema is created by ordered, idempotent migrations (``MIGRATIONS``). All writes are
upserts keyed on natural primary keys, so re-running an update is a no-op. When a
provider *changes* a previously stored bar, the old values are copied to
``bar_revisions`` before being replaced, so revisions are visible, never silent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from market_signal.data.http import RawPayload
from market_signal.models.domain import Asset, Timeframe, utcnow

MIGRATIONS: list[str] = [
    # 1 — core market data + provenance
    """
    CREATE TABLE IF NOT EXISTS assets (
        symbol VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        asset_class VARCHAR NOT NULL,
        currency VARCHAR NOT NULL,
        calendar VARCHAR NOT NULL,
        active BOOLEAN NOT NULL,
        provider_ids JSON,
        series JSON,
        updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bars (
        symbol VARCHAR NOT NULL,
        timeframe VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        close_time TIMESTAMPTZ NOT NULL,
        open DOUBLE NOT NULL,
        high DOUBLE NOT NULL,
        low DOUBLE NOT NULL,
        close DOUBLE NOT NULL,
        volume DOUBLE,
        ingest_run_id VARCHAR NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (symbol, timeframe, source, ts)
    );
    CREATE TABLE IF NOT EXISTS bar_revisions (
        symbol VARCHAR, timeframe VARCHAR, source VARCHAR, ts TIMESTAMPTZ,
        old_open DOUBLE, old_high DOUBLE, old_low DOUBLE, old_close DOUBLE, old_volume DOUBLE,
        new_open DOUBLE, new_high DOUBLE, new_low DOUBLE, new_close DOUBLE, new_volume DOUBLE,
        old_ingest_run_id VARCHAR, new_ingest_run_id VARCHAR, revised_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS corporate_actions (
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        date DATE NOT NULL,
        split_factor DOUBLE NOT NULL,
        dividend DOUBLE NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, source, date)
    );
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        run_id VARCHAR PRIMARY KEY,
        provider VARCHAR NOT NULL,
        dataset VARCHAR NOT NULL,
        entity VARCHAR,
        params JSON,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        rows_received INTEGER,
        rows_written INTEGER,
        rows_rejected INTEGER,
        raw_paths JSON,
        raw_sha256 JSON,
        schema_fingerprint VARCHAR,
        error VARCHAR
    );
    CREATE TABLE IF NOT EXISTS series_changes (
        symbol VARCHAR, timeframe VARCHAR, old_source VARCHAR, new_source VARCHAR,
        detected_at TIMESTAMPTZ, note VARCHAR
    );
    CREATE TABLE IF NOT EXISTS data_quality_issues (
        run_id VARCHAR,
        symbol VARCHAR,
        timeframe VARCHAR,
        source VARCHAR,
        check_name VARCHAR,
        severity VARCHAR,
        ts TIMESTAMPTZ,
        detail VARCHAR,
        detected_at TIMESTAMPTZ
    );
    """,
    # 2 — point-in-time macro + fundamentals
    """
    CREATE TABLE IF NOT EXISTS macro_observations (
        series_id VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        obs_date DATE NOT NULL,
        realtime_start DATE NOT NULL,
        realtime_end DATE,
        value DOUBLE,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (series_id, source, obs_date, realtime_start)
    );
    CREATE TABLE IF NOT EXISTS fundamental_facts (
        fact_key VARCHAR PRIMARY KEY,
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        concept VARCHAR NOT NULL,
        unit VARCHAR NOT NULL,
        period_start DATE,
        period_end DATE NOT NULL,
        value DOUBLE NOT NULL,
        form VARCHAR,
        fy INTEGER,
        fp VARCHAR,
        filed DATE NOT NULL,
        accn VARCHAR,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        ingest_run_id VARCHAR NOT NULL
    );
    CREATE TABLE IF NOT EXISTS crypto_metrics (
        symbol VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        metric VARCHAR NOT NULL,
        obs_date DATE NOT NULL,
        value DOUBLE,
        available_at TIMESTAMPTZ NOT NULL,
        pit_method VARCHAR NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL,
        ingest_run_id VARCHAR NOT NULL,
        PRIMARY KEY (symbol, source, metric, obs_date, pit_method)
    );
    """,
    # 3 — scans, research, portfolio, journal, alerts
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        scan_id VARCHAR PRIMARY KEY,
        as_of TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        config_hash VARCHAR,
        data_fingerprint VARCHAR,
        regime JSON,
        summary JSON
    );
    CREATE TABLE IF NOT EXISTS scan_results (
        scan_id VARCHAR NOT NULL,
        symbol VARCHAR NOT NULL,
        setup VARCHAR NOT NULL,
        score DOUBLE,
        coverage DOUBLE,
        status VARCHAR,
        price DOUBLE,
        payload JSON,
        PRIMARY KEY (scan_id, symbol, setup)
    );
    CREATE TABLE IF NOT EXISTS research_runs (
        run_id VARCHAR PRIMARY KEY,
        name VARCHAR,
        kind VARCHAR,
        created_at TIMESTAMPTZ,
        config JSON,
        config_hash VARCHAR,
        data_fingerprint JSON,
        code_version VARCHAR,
        git_commit VARCHAR,
        summary JSON,
        report_path VARCHAR
    );
    CREATE TABLE IF NOT EXISTS positions (
        position_id VARCHAR PRIMARY KEY,
        book VARCHAR NOT NULL,          -- 'paper' | 'real'
        kind VARCHAR NOT NULL,          -- TRADE | INVESTMENT
        symbol VARCHAR NOT NULL,
        setup VARCHAR,
        entry_date DATE NOT NULL,
        entry_price DOUBLE NOT NULL,
        quantity DOUBLE NOT NULL,
        fees DOUBLE DEFAULT 0,
        thesis VARCHAR,
        evidence VARCHAR,
        horizon VARCHAR,
        invalidation_price DOUBLE,
        invalidation_note VARCHAR,
        score_at_entry DOUBLE,
        regime_at_entry VARCHAR,
        risk_at_entry DOUBLE,           -- portfolio fraction at risk to invalidation
        followed_system BOOLEAN,
        exit_date DATE,
        exit_price DOUBLE,
        exit_reason VARCHAR,
        realised_return DOUBLE,
        mae DOUBLE,
        mfe DOUBLE,
        outcome VARCHAR,
        lessons VARCHAR,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS journal_entries (
        entry_id VARCHAR PRIMARY KEY,
        position_id VARCHAR,
        created_at TIMESTAMPTZ NOT NULL,
        kind VARCHAR,                   -- entry | review | exit | note
        text VARCHAR,
        followed_system BOOLEAN,
        tags JSON
    );
    CREATE TABLE IF NOT EXISTS alert_rules (
        rule_id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        symbol VARCHAR,
        params JSON,
        active BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS alert_events (
        event_id VARCHAR PRIMARY KEY,
        rule_id VARCHAR,
        fired_at TIMESTAMPTZ,
        symbol VARCHAR,
        message VARCHAR,
        payload JSON,
        acknowledged BOOLEAN DEFAULT FALSE
    );
    """,
]


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


@dataclass
class ArchivedPayload:
    path: str
    sha256: str


class Store:
    """Thin repository over a DuckDB file. Not thread-safe; open one per process."""

    def __init__(
        self, path: Path | str, raw_dir: Path | str | None = None, read_only: bool = False
    ):
        self.path = Path(path)
        self.raw_dir = Path(raw_dir) if raw_dir else self.path.parent / "raw"
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path), read_only=read_only)
        self.con.execute("SET TimeZone='UTC'")
        if not read_only:
            self.migrate()

    # ------------------------------------------------------------------ schema
    def migrate(self) -> None:
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for i, ddl in enumerate(MIGRATIONS, start=1):
            if i > current:
                self.con.execute(ddl)
                self.con.execute("INSERT INTO schema_version VALUES (?)", [i])

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    # ------------------------------------------------------------------ assets
    def upsert_assets(self, assets: Iterable[Asset]) -> None:
        now = utcnow()
        for a in assets:
            series = {
                tf.value: {"provider": s.provider, "symbol": s.symbol} for tf, s in a.series.items()
            }
            self.con.execute(
                "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                [a.symbol, a.name, a.asset_class.value, a.currency, a.calendar.value, a.active,
                 json.dumps(a.provider_ids), json.dumps(series), now],
            )  # fmt: skip

    # ------------------------------------------------------------------ provenance
    def archive_raw(self, payloads: list[RawPayload], run_id: str) -> list[ArchivedPayload]:
        out = []
        for i, p in enumerate(payloads):
            digest = hashlib.sha256(p.body).hexdigest()
            day = p.fetched_at.strftime("%Y%m%d")
            target = self.raw_dir / p.provider / day / f"{run_id}_{i:04d}.json.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            envelope = {
                "provider": p.provider,
                "method": p.method,
                "url": p.url,
                "params": p.params,
                "status": p.status,
                "fetched_at": p.fetched_at.isoformat(),
                "sha256": digest,
            }
            with gzip.open(target, "wb") as fh:
                fh.write(json.dumps(envelope).encode() + b"\n")
                fh.write(p.body)
            out.append(ArchivedPayload(str(target), digest))
        return out

    def start_run(
        self, provider: str, dataset: str, entity: str | None, params: dict[str, Any]
    ) -> str:
        run_id = new_id("run_")
        self.con.execute(
            "INSERT INTO ingestion_runs (run_id, provider, dataset, entity, params, started_at, status) "
            "VALUES (?,?,?,?,?,?, 'running')",
            [run_id, provider, dataset, entity, json.dumps(params, default=str), utcnow()],
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        rows_received: int = 0,
        rows_written: int = 0,
        rows_rejected: int = 0,
        archived: list[ArchivedPayload] | None = None,
        schema_fingerprint: str = "",
        error: str | None = None,
    ) -> None:
        archived = archived or []
        self.con.execute(
            """UPDATE ingestion_runs SET finished_at=?, status=?, rows_received=?, rows_written=?,
               rows_rejected=?, raw_paths=?, raw_sha256=?, schema_fingerprint=?, error=?
               WHERE run_id=?""",
            [utcnow(), status, rows_received, rows_written, rows_rejected,
             json.dumps([a.path for a in archived]), json.dumps([a.sha256 for a in archived]),
             schema_fingerprint, error, run_id],
        )  # fmt: skip

    def last_schema_fingerprint(self, provider: str, dataset: str) -> str | None:
        row = self.con.execute(
            """SELECT schema_fingerprint FROM ingestion_runs
               WHERE provider=? AND dataset=? AND status='ok' AND schema_fingerprint <> ''
               ORDER BY finished_at DESC LIMIT 1""",
            [provider, dataset],
        ).fetchone()
        return row[0] if row else None

    def log_issues(self, issues: pd.DataFrame) -> None:
        if issues is None or issues.empty:
            return
        self.con.register("_issues", issues)
        self.con.execute(
            """INSERT INTO data_quality_issues
               SELECT run_id, symbol, timeframe, source, check_name, severity, ts, detail, detected_at
               FROM _issues"""
        )
        self.con.unregister("_issues")

    # ------------------------------------------------------------------ bars
    def upsert_bars(
        self, symbol: str, timeframe: Timeframe, source: str, bars: pd.DataFrame, run_id: str
    ) -> dict[str, int]:
        """Idempotent upsert. Returns counts of inserted / revised / unchanged rows."""
        if bars.empty:
            return {"inserted": 0, "revised": 0, "unchanged": 0}
        now = utcnow()
        stage = bars[["ts", "close_time", "open", "high", "low", "close", "volume"]].copy()
        stage["symbol"], stage["timeframe"], stage["source"] = symbol, timeframe.value, source
        self.con.register("_stage", stage)
        try:
            with self.transaction():
                key = "b.symbol=s.symbol AND b.timeframe=s.timeframe AND b.source=s.source AND b.ts=s.ts"
                changed_pred = (
                    "b.open IS DISTINCT FROM s.open OR b.high IS DISTINCT FROM s.high OR "
                    "b.low IS DISTINCT FROM s.low OR b.close IS DISTINCT FROM s.close OR "
                    "b.volume IS DISTINCT FROM s.volume"
                )
                revised = self.con.execute(
                    f"""INSERT INTO bar_revisions
                        SELECT b.symbol, b.timeframe, b.source, b.ts,
                               b.open, b.high, b.low, b.close, b.volume,
                               s.open, s.high, s.low, s.close, s.volume,
                               b.ingest_run_id, ?, ?
                        FROM bars b JOIN _stage s ON {key}
                        WHERE {changed_pred}""",
                    [run_id, now],
                ).fetchone()[0]
                self.con.execute(
                    f"""UPDATE bars b SET open=s.open, high=s.high, low=s.low, close=s.close,
                        volume=s.volume, close_time=s.close_time, ingest_run_id=?, ingested_at=?
                        FROM _stage s WHERE {key} AND ({changed_pred})""",
                    [run_id, now],
                )
                inserted = self.con.execute(
                    f"""INSERT INTO bars
                        SELECT s.symbol, s.timeframe, s.source, s.ts, s.close_time,
                               s.open, s.high, s.low, s.close, s.volume, ?, ?
                        FROM _stage s
                        WHERE NOT EXISTS (SELECT 1 FROM bars b WHERE {key})""",
                    [run_id, now],
                ).fetchone()[0]
        finally:
            self.con.unregister("_stage")
        return {
            "inserted": int(inserted),
            "revised": int(revised),
            "unchanged": len(stage) - int(inserted) - int(revised),
        }

    def upsert_actions(self, symbol: str, source: str, actions: pd.DataFrame, run_id: str) -> int:
        if actions is None or actions.empty:
            return 0
        stage = actions[["date", "split_factor", "dividend"]].copy()
        stage["symbol"], stage["source"], stage["run_id"] = symbol, source, run_id
        self.con.register("_act", stage)
        try:
            self.con.execute(
                """INSERT OR REPLACE INTO corporate_actions
                   SELECT symbol, source, date, split_factor, dividend, run_id FROM _act"""
            )
        finally:
            self.con.unregister("_act")
        return len(stage)

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        source: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Bars for ONE series. If ``source`` is None and several sources exist, raise:
        callers must pick explicitly (no silent stitching)."""
        if source is None:
            sources = self.sources_for(symbol, timeframe)
            if len(sources) > 1:
                raise ValueError(
                    f"{symbol} {timeframe}: multiple sources {sources}; pass source= explicitly"
                )
            if not sources:
                return pd.DataFrame(
                    columns=["ts", "close_time", "open", "high", "low", "close", "volume"]
                )
            source = sources[0]
        sql = """SELECT ts, close_time, open, high, low, close, volume FROM bars
                 WHERE symbol=? AND timeframe=? AND source=?"""
        params: list[Any] = [symbol, timeframe.value, source]
        if start is not None:
            sql += " AND ts >= ?"
            params.append(start)
        if end is not None:
            sql += " AND ts <= ?"
            params.append(end)
        df = self.con.execute(sql + " ORDER BY ts", params).df()
        for c in ("ts", "close_time"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def sources_for(self, symbol: str, timeframe: Timeframe) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT source FROM bars WHERE symbol=? AND timeframe=? ORDER BY source",
            [symbol, timeframe.value],
        ).fetchall()
        return [r[0] for r in rows]

    def get_actions(self, symbol: str, source: str) -> pd.DataFrame:
        df = self.con.execute(
            "SELECT date, split_factor, dividend FROM corporate_actions WHERE symbol=? AND source=? ORDER BY date",
            [symbol, source],
        ).df()
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def last_bar_ts(self, symbol: str, timeframe: Timeframe, source: str) -> pd.Timestamp | None:
        row = self.con.execute(
            "SELECT max(ts) FROM bars WHERE symbol=? AND timeframe=? AND source=?",
            [symbol, timeframe.value, source],
        ).fetchone()
        return pd.Timestamp(row[0]).tz_convert("UTC") if row and row[0] is not None else None

    def series_inventory(self) -> pd.DataFrame:
        return self.query(
            """SELECT symbol, timeframe, source, count(*) AS n_bars, min(ts) AS first_ts,
                      max(ts) AS last_ts, max(close_time) AS last_close, max(ingested_at) AS last_ingested
               FROM bars GROUP BY 1,2,3 ORDER BY 1,2,3"""
        )

    def record_series_change(
        self, symbol: str, timeframe: Timeframe, old: str, new: str, note: str
    ) -> None:
        exists = self.con.execute(
            "SELECT 1 FROM series_changes WHERE symbol=? AND timeframe=? AND old_source=? AND new_source=?",
            [symbol, timeframe.value, old, new],
        ).fetchone()
        if not exists:
            self.con.execute(
                "INSERT INTO series_changes VALUES (?,?,?,?,?,?)",
                [symbol, timeframe.value, old, new, utcnow(), note],
            )

    def series_fingerprint(self, symbol: str, timeframe: Timeframe, source: str) -> dict[str, Any]:
        """Content hash of a series (for research reproducibility)."""
        row = self.con.execute(
            """SELECT count(*), min(ts), max(ts),
                      sum(hash(ts, open, high, low, close, volume)) % 1000000007
               FROM bars WHERE symbol=? AND timeframe=? AND source=?""",
            [symbol, timeframe.value, source],
        ).fetchone()
        return {
            "symbol": symbol,
            "timeframe": timeframe.value,
            "source": source,
            "n": int(row[0]),
            "first": str(row[1]),
            "last": str(row[2]),
            "hash": str(row[3]),
        }
