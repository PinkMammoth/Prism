"""Perpetual-futures ingestion (Hyperliquid) and loaders.

Three datasets, each with provenance (ingestion run + archived raw payloads):
  perp_bars       daily perp candles (only closed bars are stored, like spot)
  perp_funding    settled funding rates, point-in-time: available at their settlement time
  perp_snapshots  daily snapshot of mark/oracle price, current funding, open interest and max
                  leverage. Hyperliquid serves no free OI *history*, so OI history exists
                  only from the first snapshot onward (prospective, like crypto fundamentals).
Perp series are never stitched to spot series.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from market_signal.config import Settings
from market_signal.data.calendars import close_times
from market_signal.data.http import ProviderError
from market_signal.data.store import Store
from market_signal.data.validation import BatchRejected, validate_batch
from market_signal.models.domain import Calendar, PitMethod, Timeframe, utcnow

SOURCE = "hyperliquid"


def perp_config(settings: Settings) -> dict[str, Any]:
    path = settings.paths.config / "perps.yaml"
    return settings.yaml("perps.yaml") if path.exists() else {"coins": []}


def _last(store: Store, sql: str, params: list) -> datetime | None:
    row = store.con.execute(sql, params).fetchone()
    return None if not row or row[0] is None else pd.Timestamp(row[0]).to_pydatetime()


# --------------------------------------------------------------------------- writers


def upsert_perp_bars(store: Store, coin: str, bars: pd.DataFrame, source: str, run_id: str) -> int:
    if bars.empty:
        return 0
    stage = bars[["ts", "open", "high", "low", "close", "volume", "close_time"]].copy()
    stage.insert(0, "coin", coin)
    stage.insert(1, "timeframe", Timeframe.D1.value)
    stage.insert(2, "source", source)
    stage["ingested_at"] = utcnow()
    stage["ingest_run_id"] = run_id
    store.con.register("_pb", stage)
    try:
        before = store.con.execute("SELECT count(*) FROM perp_bars").fetchone()[0]
        store.con.execute(
            """INSERT OR REPLACE INTO perp_bars
               SELECT coin, timeframe, source, ts, open, high, low, close, volume, close_time,
                      ingested_at, ingest_run_id FROM _pb"""
        )
        after = store.con.execute("SELECT count(*) FROM perp_bars").fetchone()[0]
    finally:
        store.con.unregister("_pb")
    return int(after - before)


def upsert_funding(store: Store, coin: str, df: pd.DataFrame, source: str, run_id: str) -> int:
    if df.empty:
        return 0
    stage = pd.DataFrame({
        "coin": coin, "source": source, "time": df["time"], "funding_rate": df["funding_rate"],
        "premium": df["premium"], "available_at": df["time"], "pit_method": PitMethod.MARKET_CLOSE.value,
        "ingest_run_id": run_id,
    })  # fmt: skip
    store.con.register("_pf", stage)
    try:
        before = store.con.execute("SELECT count(*) FROM perp_funding").fetchone()[0]
        store.con.execute(
            """INSERT OR REPLACE INTO perp_funding
               SELECT coin, source, time, funding_rate, premium, available_at, pit_method, ingest_run_id
               FROM _pf"""
        )
        after = store.con.execute("SELECT count(*) FROM perp_funding").fetchone()[0]
    finally:
        store.con.unregister("_pf")
    return int(after - before)


def insert_snapshots(
    store: Store, ctx: pd.DataFrame, source: str, at: datetime, run_id: str
) -> int:
    cols = ["mark_px", "oracle_px", "mid_px", "prev_day_px", "funding_rate", "premium",
            "open_interest", "oi_notional", "day_ntl_vlm", "max_leverage"]  # fmt: skip
    n = 0
    for _, r in ctx.iterrows():
        store.con.execute(
            f"INSERT OR REPLACE INTO perp_snapshots (coin, source, snapshot_at, {', '.join(cols)}, ingest_run_id) "
            f"VALUES (?,?,?,{','.join('?' * len(cols))},?)",
            [
                r["coin"],
                source,
                at,
                *[None if pd.isna(r[c]) else float(r[c]) for c in cols],
                run_id,
            ],
        )
        n += 1
    return n


# --------------------------------------------------------------------------- updater


def update_perps(settings: Settings, store: Store, provider: Any | None = None) -> pd.DataFrame:
    """Incremental, idempotent. One ingestion run per (coin, dataset)."""
    cfg = perp_config(settings)
    coins = [str(c).upper() for c in cfg.get("coins") or []]
    fcfg = cfg.get("funding") or {}
    if provider is None:
        from market_signal.data.registry import ProviderRegistry

        provider = ProviderRegistry(settings).market("hyperliquid")
    out: list[tuple] = []

    def run(dataset: str, coin: str, params: dict, fn) -> None:
        run_id = store.start_run(SOURCE, dataset, coin, params)
        try:
            received, written, note = fn(run_id)
            archived = store.archive_raw(provider.drain_raw(), run_id)
            store.finish_run(run_id, status="ok", rows_received=received, rows_written=written,
                             archived=archived)  # fmt: skip
            out.append((coin, dataset, "ok", received, written, note))
        except (ProviderError, BatchRejected) as exc:
            store.finish_run(run_id, status="failed", archived=store.archive_raw(provider.drain_raw(), run_id),
                             error=str(exc)[:500])  # fmt: skip
            out.append((coin, dataset, "failed", 0, 0, str(exc)[:80]))

    for coin in coins:
        last_bar = _last(
            store, "SELECT max(ts) FROM perp_bars WHERE coin=? AND source=?", [coin, SOURCE]
        )
        start = None if last_bar is None else last_bar - timedelta(days=3)

        def bars_fn(run_id: str, coin: str = coin, start: datetime | None = start) -> tuple:
            res = provider.perp_daily_bars(coin, start, None)
            fetched = pd.Timestamp(utcnow())
            bars = res.bars.copy()
            if bars.empty:
                return 0, 0, "no candles"
            bars["close_time"] = close_times(bars["ts"], Calendar.CRYPTO_24_7, Timeframe.D1)
            bars = bars[bars["close_time"] <= fetched].reset_index(drop=True)  # closed bars only
            vr = validate_batch(bars, symbol=f"{coin}-PERP", timeframe=Timeframe.D1, source=SOURCE,
                                calendar=Calendar.CRYPTO_24_7, run_id=run_id)  # fmt: skip
            store.log_issues(vr.issues)
            # validate_batch keeps extra columns, so close_time travels with the clean rows
            clean = vr.clean
            return (
                len(bars),
                upsert_perp_bars(store, coin, clean, SOURCE, run_id),
                "daily perp candles",
            )

        run("perp_bars_1d", coin, {"start": start}, bars_fn)

        last_f = _last(
            store, "SELECT max(time) FROM perp_funding WHERE coin=? AND source=?", [coin, SOURCE]
        )
        f_start = (
            utcnow() - timedelta(days=int(fcfg.get("backfill_days", 1095)))
            if last_f is None
            else last_f + timedelta(milliseconds=1)
        )

        def funding_fn(run_id: str, coin: str = coin, f_start: datetime = f_start) -> tuple:
            df = provider.funding_history(
                coin, f_start, None, page_size=int(fcfg.get("page_size", 500))
            )
            return len(df), upsert_funding(store, coin, df, SOURCE, run_id), "hourly funding"

        run("perp_funding", coin, {"start": str(f_start)}, funding_fn)

    def snap_fn(run_id: str) -> tuple:
        ctx = provider.perp_contexts()
        at = utcnow().astimezone(UTC)
        missing = [c for c in coins if c not in set(ctx["coin"])]
        rows = ctx[ctx["coin"].isin(coins)]
        note = "OI, mark, funding, max leverage" + (
            f"; not listed: {', '.join(missing)}" if missing else ""
        )
        return len(ctx), insert_snapshots(store, rows, SOURCE, at, run_id), note

    if coins:
        run("perp_snapshot", "ALL", {}, snap_fn)
    return pd.DataFrame(out, columns=["coin", "dataset", "status", "received", "new_rows", "note"])


# --------------------------------------------------------------------------- loaders
# One venue per coin today, so loaders read any source (the demo DB uses "synthetic").


def load_funding(store: Store, coin: str, since: datetime | None = None) -> pd.Series:
    """Settled funding rate per period, indexed by settlement time (UTC)."""
    sql = "SELECT time, funding_rate FROM perp_funding WHERE coin=?"
    params: list = [coin]
    if since is not None:
        sql += " AND time >= ?"
        params.append(since)
    try:
        df = store.query(sql + " ORDER BY time", params)
    except Exception:
        return pd.Series(dtype="float64")
    if df.empty:
        return pd.Series(dtype="float64")
    return pd.Series(
        df["funding_rate"].to_numpy(), index=pd.DatetimeIndex(pd.to_datetime(df["time"], utc=True))
    )


def load_snapshots(store: Store, coin: str | None = None) -> pd.DataFrame:
    try:
        if coin:
            return store.query(
                "SELECT * FROM perp_snapshots WHERE coin=? ORDER BY snapshot_at", [coin]
            )
        return store.query("SELECT * FROM perp_snapshots ORDER BY coin, snapshot_at")
    except Exception:
        return pd.DataFrame()


def load_perp_bars(store: Store, coin: str) -> pd.DataFrame:
    try:
        return store.query(
            "SELECT ts, open, high, low, close, volume, close_time FROM perp_bars "
            "WHERE coin=? AND timeframe='1d' ORDER BY ts",
            [coin],
        )
    except Exception:
        return pd.DataFrame()
