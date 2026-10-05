"""Intraday ingestion: provider adapters, backfill, incremental update, archives, disk guard.

Venues (``config/perps.yaml`` → ``intraday``):

- **Hyperliquid** (live, canonical for the paper/co-pilot venue): ``POST /info``
  ``candleSnapshot`` with ``interval`` 15m/1h/4h. Provider-native bars. ``t`` is the bar open
  (ms), ``T = t + interval - 1 ms``; ``endTime`` is inclusive of a bar's ``t``; the newest
  candle returned is still forming. Only the newest 5,000 candles per interval are served
  (15m ≈ 52 days, 1h ≈ 208 days, 4h ≈ 833 days): older history is not obtainable here.
- **Binance USD-M** (history only, never live): ``GET /fapi/v1/klines``; same open-time
  semantics (``closeTime = openTime + interval - 1 ms``), years of history, 1,500 per page.

Closed-bar guarantee at ingestion: a bar is kept only if ``close_time + settle`` is at or
before the instant its request was SENT. A still-forming candle is never stored.
Availability: ``first_observed_at`` is when the response was held, never the theoretical close.

Each call is one ``ingestion_runs`` row per venue covering all requested coins/timeframes, with
every provider response archived in ONE gzip JSON-lines file (sha256 per response).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from market_signal.config import Settings
from market_signal.data.http import ProviderError, RawPayload, SchemaError
from market_signal.data.store import ArchivedPayload, Store
from market_signal.intraday.bars import (
    INGEST_VERSION,
    SeriesPolicy,
    add_coverage,
    upsert_bars,
)
from market_signal.intraday.grid import (
    INTRADAY,
    interval,
    is_aligned,
    latest_closed_open,
    parse_timeframe,
    utc,
)
from market_signal.models.domain import Timeframe, utcnow

DATASET = "perp_intraday_bars"
PROVIDER_INTERVAL = {Timeframe.M15: "15m", Timeframe.H1: "1h", Timeframe.H4: "4h"}
RESULT_COLUMNS = ["venue", "coin", "timeframe", "start", "end", "received", "inserted", "revised",
                  "unchanged", "forming", "invalid", "status", "note"]  # fmt: skip


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class VenueConfig:
    name: str
    live: bool
    symbols: dict[str, str]  # Prism coin -> provider symbol
    timeframes: tuple[Timeframe, ...]
    retention_bars: int | None
    page_bars: int
    overlap_bars: dict[str, int]
    history_start: dict[str, pd.Timestamp | None]  # None: as far as the provider serves


@dataclass(frozen=True)
class IntradayConfig:
    venues: dict[str, VenueConfig]
    settle: pd.Timedelta
    stale_after: dict[str, pd.Timedelta]
    db_bytes_per_row: float
    raw_bytes_per_row: float

    def policy(self, venue: str) -> SeriesPolicy:
        v = self.venues[venue]
        return SeriesPolicy(
            live=v.live, retention_bars=v.retention_bars, stale_after=self.stale_after
        )

    def series(self, venue: str | None = None, coins=None, tfs=None, live_only: bool = False):
        out = []
        for v in self.venues.values():
            if (venue and v.name != venue) or (live_only and not v.live):
                continue
            for coin in v.symbols:
                if coins and coin not in coins:
                    continue
                for tf in v.timeframes:
                    if tfs and tf not in tfs:
                        continue
                    out.append((v.name, coin, tf, self.policy(v.name)))
        return out


def intraday_config(settings: Settings) -> IntradayConfig:
    from market_signal.perps.data import perp_config

    pc = perp_config(settings)
    c = pc.get("intraday") or {}
    tfs = tuple(parse_timeframe(t) for t in c.get("timeframes", [t.value for t in INTRADAY]))
    venues = {}
    for name, v in (c.get("venues") or {}).items():
        syms = v.get("symbols") or {str(x).upper(): str(x).upper() for x in pc.get("coins") or []}
        hist = {}
        for tf in tfs:
            h = (v.get("history_start") or {}).get(tf.value)
            hist[tf.value] = None if h in (None, "max") else utc(pd.Timestamp(str(h), tz="UTC"))
        venues[name] = VenueConfig(
            name=name, live=bool(v.get("live", False)), symbols={str(k).upper(): str(s) for k, s in syms.items()},
            timeframes=tuple(parse_timeframe(t) for t in v.get("timeframes", [t.value for t in tfs])),
            retention_bars=None if v.get("retention_bars") is None else int(v["retention_bars"]),
            page_bars=int(v.get("page_bars", 1000)),
            overlap_bars={tf.value: int((v.get("overlap_bars") or {}).get(tf.value, 3)) for tf in tfs},
            history_start=hist,
        )  # fmt: skip
    stale = {
        k: pd.Timedelta(minutes=float(m)) for k, m in (c.get("stale_after_minutes") or {}).items()
    }
    d = c.get("disk") or {}
    return IntradayConfig(venues=venues, settle=pd.Timedelta(seconds=float(c.get("settle_seconds", 5))),
                          stale_after=stale, db_bytes_per_row=float(d.get("db_bytes_per_row", 120)),
                          raw_bytes_per_row=float(d.get("raw_bytes_per_row", 60)))  # fmt: skip


# --------------------------------------------------------------------------- provider adapters


def _bars_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["open_time", "open", "high", "low", "close", "volume", "trades"]
    )
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce").astype("Int64")
    return df


class HyperliquidFetcher:
    """Hyperliquid perp candles via the existing provider's HTTP client (raw capture)."""

    source = raw_provider = "hyperliquid"

    def __init__(self, provider: Any):
        self.provider = provider

    def drain_raw(self) -> list[RawPayload]:
        return self.provider.drain_raw()

    def page(
        self, symbol: str, tf: Timeframe, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        ms = int(interval(tf).total_seconds() * 1000)
        payload = self.provider.info({"type": "candleSnapshot", "req": {
            "coin": symbol, "interval": PROVIDER_INTERVAL[tf],
            "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000) - 1}})  # fmt: skip
        if not isinstance(payload, list):
            raise SchemaError(
                f"hyperliquid candleSnapshot: expected list, got {type(payload).__name__}"
            )
        rows = []
        for c in payload:
            if not isinstance(c, dict) or not {"t", "T", "o", "h", "l", "c", "v"} <= c.keys():
                raise SchemaError(f"hyperliquid candle missing fields: {c!r}"[:200])
            if (
                c.get("s", symbol) != symbol
                or c.get("i", PROVIDER_INTERVAL[tf]) != PROVIDER_INTERVAL[tf]
            ):
                raise SchemaError(
                    f"hyperliquid: asked {symbol}/{PROVIDER_INTERVAL[tf]}, got {c.get('s')}/{c.get('i')}"
                )
            if int(c["T"]) != int(c["t"]) + ms - 1:
                raise SchemaError(f"hyperliquid candle T != t + interval - 1ms: {c!r}"[:200])
            rows.append({"open_time": int(c["t"]), "open": c["o"], "high": c["h"], "low": c["l"],
                         "close": c["c"], "volume": c["v"], "trades": c.get("n")})  # fmt: skip
        return _bars_frame(rows)


class BinanceFetcher:
    """Binance USD-M klines (history venue)."""

    source, raw_provider = "binance", "binance_futures"

    def __init__(self, provider: Any):
        self.provider = provider

    def drain_raw(self) -> list[RawPayload]:
        return self.provider.drain_raw()

    def page(
        self, symbol: str, tf: Timeframe, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        from market_signal.perps.binance import _explain

        ms = int(interval(tf).total_seconds() * 1000)
        limit = int((end - start) / interval(tf)) + 1
        try:
            page = self.provider.http.get_json("/fapi/v1/klines", params={
                "symbol": symbol, "interval": PROVIDER_INTERVAL[tf], "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000) - 1, "limit": min(limit, 1500)})  # fmt: skip
        except ProviderError as exc:
            raise _explain(exc) from None
        if not isinstance(page, list) or any(not isinstance(r, list) or len(r) < 9 for r in page):
            raise SchemaError(
                "binance klines: expected arrays [openTime, o, h, l, c, v, closeTime, qv, n, …]"
            )
        rows = []
        for r in page:
            if int(r[6]) != int(r[0]) + ms - 1:
                raise SchemaError(f"binance kline closeTime != openTime + interval - 1ms: {r[:7]}")
            rows.append({"open_time": int(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4],
                         "volume": r[5], "trades": r[8]})  # fmt: skip
        return _bars_frame(rows)


def make_fetcher(settings: Settings, venue: str):
    from market_signal.data.registry import ProviderRegistry

    reg = ProviderRegistry(settings)
    if venue == "hyperliquid":
        return HyperliquidFetcher(reg.market("hyperliquid"))
    if venue == "binance":
        from market_signal.perps.binance import BinanceFuturesProvider

        return BinanceFetcher(BinanceFuturesProvider(reg.http("binance_futures")))
    raise ValueError(f"no intraday adapter for venue {venue!r}")


# --------------------------------------------------------------------------- planning


@dataclass
class Plan:
    """Fetch every bar opening in ``[start, end)`` for one coin/timeframe."""

    coin: str
    symbol: str
    tf: Timeframe
    start: pd.Timestamp
    end: pd.Timestamp
    notes: list[str] = field(default_factory=list)

    @property
    def bars(self) -> int:
        return max(int((self.end - self.start) / interval(self.tf)), 0)


def _retention_floor(v: VenueConfig, tf: Timeframe, asof: pd.Timestamp) -> pd.Timestamp | None:
    if v.retention_bars is None:
        return None
    return latest_closed_open(asof, tf) - (v.retention_bars - 1) * interval(tf)


def _clamp(v: VenueConfig, p: Plan, asof: pd.Timestamp) -> Plan:
    floor = _retention_floor(v, p.tf, asof)
    if floor is not None and p.start < floor:
        if p.start == pd.Timestamp(0, tz="UTC"):  # "max": as far back as the provider serves
            p.notes.append(f"provider retention: newest {v.retention_bars} bars only")
        else:
            p.notes.append(f"{p.start:%Y-%m-%d %H:%M}→{floor:%Y-%m-%d %H:%M} NOT SERVED "
                           f"(provider keeps only the newest {v.retention_bars} bars)")  # fmt: skip
        p.start = floor
    return p


def plan_update(store: Store, v: VenueConfig, coin: str, tf: Timeframe, asof) -> Plan | None:
    """Incremental: nothing while no new bar has closed since the newest stored one; else from
    ``overlap`` bars before the newest stored bar (the revision window; it also re-covers any
    missed fetches since) to the newest closed bar. A series with no rows is seeded from its
    configured history start (live venues only)."""
    asof = utc(asof)
    end = latest_closed_open(asof, tf) + interval(tf)
    last = store.con.execute(
        "SELECT max(open_time) FROM perp_intraday_bars WHERE source=? AND coin=? AND timeframe=?",
        [v.name, coin, tf.value],
    ).fetchone()[0]
    if last is None:
        if not v.live:
            return None  # history venues are filled by an explicit backfill only
        start = v.history_start.get(tf.value) or pd.Timestamp(0, tz="UTC")
    elif utc(last) >= latest_closed_open(asof, tf):
        return None  # current: no new bar has closed (the overlap is re-read with the next one)
    else:
        start = utc(last) - v.overlap_bars.get(tf.value, 3) * interval(tf)
    p = _clamp(v, Plan(coin, v.symbols[coin], tf, start, end), asof)
    return p if p.start < p.end else None


def plan_backfill(v: VenueConfig, coin: str, tf: Timeframe, start, end, asof) -> Plan:
    """Explicit ``[start, end)``; ``end`` is capped at the newest closed bar; start may be
    clamped by provider retention (reported in the plan notes, never silent)."""
    asof = utc(asof)
    s = utc(start).ceil(interval(tf))
    e = min(utc(end).floor(interval(tf)), latest_closed_open(asof, tf) + interval(tf))
    return _clamp(v, Plan(coin, v.symbols[coin], tf, s, e), asof)


# --------------------------------------------------------------------------- disk guard


class DiskUnsafe(RuntimeError):
    """A write would push the volume below the runtime's warning threshold. Nothing written."""


def estimate(plans: list[Plan], cfg: IntradayConfig) -> dict[str, float]:
    rows = sum(p.bars for p in plans)
    return {
        "rows": rows,
        "db_bytes": rows * cfg.db_bytes_per_row,
        "raw_bytes": rows * cfg.raw_bytes_per_row,
    }


def check_disk(db: Path, est: dict[str, float]) -> dict[str, Any]:
    """Refuse (before any write) when free space after the write, the database's growth
    multiplied across every retained backup copy, and the raw archive would leave less than the
    runtime's WARNING free fraction. Deletes nothing."""
    from market_signal.ops.runtime import BACKUP_KEEP, DISK_WARN, disk_state

    if str(db) == ":memory:":
        return {"checked": False}
    d = disk_state(db)
    copies = sum(BACKUP_KEEP.values())
    need = est["db_bytes"] * (1 + copies) + est["raw_bytes"]
    floor = DISK_WARN * d.total
    out = {"checked": True, "free": d.free, "total": d.total, "projected_bytes": need,
           "backup_copies": copies, "free_after": d.free - need, "floor": floor}  # fmt: skip
    if d.free - need < floor:
        raise DiskUnsafe(
            f"intraday write refused before writing: ~{est['rows']:,.0f} bars ≈ "
            f"{est['db_bytes'] / 2**20:.1f} MiB in the database (× {1 + copies} with retained backups) + "
            f"{est['raw_bytes'] / 2**20:.1f} MiB raw = {need / 2**20:.0f} MiB; {d.describe()}. "
            f"Required capacity: at least {(need + floor - d.free) / 2**20:.0f} MiB more free space. "
            "Nothing was deleted."
        )
    return out


# --------------------------------------------------------------------------- archives


class ArchiveBundle:
    """All responses of one run in one gzip JSON-lines file (sha256 per response body),
    written as they arrive so a long backfill never holds every response in memory.

    One file per run (not per response) keeps the archive's block overhead bounded at a
    15-minute cadence. Line i: ``{"envelope": {...}, "body": "<response text>"}``; the run's
    ``raw_paths`` entries are ``<file>#<i>``. The file appears under its final name only once
    complete (written as ``.part`` and renamed)."""

    def __init__(self, store: Store, provider: str, run_id: str):
        self.target = (
            store.raw_dir
            / f"{provider}_intraday"
            / utcnow().strftime("%Y%m%d")
            / f"{run_id}.jsonl.gz"
        )
        self.tmp = self.target.with_name(self.target.name + ".part")
        self.fh = None
        self.archived: list[ArchivedPayload] = []

    def write(self, payloads: list[RawPayload]) -> None:
        if not payloads:
            return
        if self.fh is None:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.fh = gzip.open(self.tmp, "wb")  # noqa: SIM115 (open across write() calls; closed in close())
        for p in payloads:
            digest = hashlib.sha256(p.body).hexdigest()
            env = {"provider": p.provider, "method": p.method, "url": p.url, "params": p.params,
                   "status": p.status, "fetched_at": p.fetched_at.isoformat(), "sha256": digest}  # fmt: skip
            self.fh.write(
                json.dumps({"envelope": env, "body": p.body.decode("utf-8", "replace")}).encode()
                + b"\n"
            )
            self.archived.append(ArchivedPayload(f"{self.target}#{len(self.archived)}", digest))

    def close(self) -> list[ArchivedPayload]:
        if self.fh is not None:
            self.fh.close()
            self.tmp.replace(self.target)
            self.fh = None
        return self.archived


# --------------------------------------------------------------------------- running plans


def _validate(df: pd.DataFrame, tf: Timeframe, symbol: str) -> tuple[pd.DataFrame, list[str]]:
    """Grid alignment is a schema fact (error); impossible OHLC rows are dropped and reported."""
    if not df["open_time"].map(lambda t: is_aligned(t, tf)).all():
        raise SchemaError(f"{symbol} {tf}: bar open times off the UTC {tf} grid")
    dup = df[df["open_time"].duplicated(keep=False)]
    if not dup.empty:
        if dup.drop_duplicates().duplicated("open_time").any():
            raise SchemaError(f"{symbol} {tf}: conflicting duplicate bars in one response")
        df = df.drop_duplicates("open_time")
    px = df[["open", "high", "low", "close"]]
    bad = (
        px.isna().any(axis=1)
        | (px <= 0).any(axis=1)
        | (df["high"] < px.max(axis=1))
        | (df["low"] > px.min(axis=1))
        | (df["volume"] < 0)
    )
    issues = [f"{t:%Y-%m-%d %H:%M} impossible OHLCV dropped" for t in df.loc[bad, "open_time"]]
    return df[~bad].reset_index(drop=True), issues


def _plan_params(plans: list[Plan]) -> list[dict]:
    """Requested windows, grouped: one entry per (timeframe, start, end, notes) with its coins."""
    groups: dict[tuple, list[str]] = {}
    for p in plans:
        groups.setdefault(
            (p.tf.value, p.start.isoformat(), p.end.isoformat(), "; ".join(p.notes)), []
        ).append(p.coin)
    return [{"tf": tf, "start": s, "end": e, "coins": coins, **({"notes": n} if n else {})}
            for (tf, s, e, n), coins in groups.items()]  # fmt: skip


def run_plans(
    store: Store,
    cfg: IntradayConfig,
    venue: str,
    fetcher: Any,
    plans: list[Plan],
    *,
    mode: str,
    now: Callable[[], datetime] = utcnow,
) -> pd.DataFrame:
    """Fetch, keep closed bars only, upsert with revisions, record coverage. One ingestion run."""
    from market_signal.config import find_project_root
    from market_signal.ops.runtime import revision

    v = cfg.venues[venue]
    out: list[dict] = []
    if not plans:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    entity = (
        ",".join(sorted({p.tf.value for p in plans}))
        + ":"
        + ",".join(sorted({p.coin for p in plans}))
    )
    run_id = store.start_run(venue, DATASET, entity, {
        "mode": mode, "ingest_version": INGEST_VERSION, "git_commit": revision(find_project_root()),
        "settle_seconds": cfg.settle.total_seconds(),
        "plans": _plan_params(plans)})  # fmt: skip
    received = written = rejected = 0
    errors = []
    bundle = ArchiveBundle(store, getattr(fetcher, "raw_provider", venue), run_id)
    for p in plans:
        row = {"venue": venue, "coin": p.coin, "timeframe": p.tf.value, "start": p.start, "end": p.end,
               "received": 0, "inserted": 0, "revised": 0, "unchanged": 0, "forming": 0, "invalid": 0,
               "status": "ok", "note": "; ".join(p.notes)}  # fmt: skip
        try:
            frames, cursor, step = [], p.start, interval(p.tf) * v.page_bars
            sent = utc(now())
            while cursor < p.end:
                page_end = min(cursor + step, p.end)
                frames.append(fetcher.page(p.symbol, p.tf, cursor, page_end))
                cursor = page_end
            observed = utc(now())
            df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            row["received"] = len(df)
            if not df.empty:
                df = df[(df["open_time"] >= p.start) & (df["open_time"] < p.end)]
                df, issues = _validate(df, p.tf, p.symbol)
                row["invalid"] = len(issues)
                rejected += len(issues)
                if issues:
                    _log_issues(store, run_id, venue, p, issues, observed)
                # closed-bar guarantee: closed by the instant the FIRST request was sent
                cutoff = sent - cfg.settle
                closed = df["open_time"] + interval(p.tf) <= cutoff
                row["forming"] = int((~closed).sum())
                df = df[closed]
            with store.transaction():
                counts = upsert_bars(store, venue, p.coin, p.tf, df, observed_at=observed.to_pydatetime(),
                                     run_id=run_id, in_transaction=True)  # fmt: skip
                closed_end = min(
                    p.end, latest_closed_open(sent - cfg.settle, p.tf) + interval(p.tf)
                )
                add_coverage(
                    store, venue, p.coin, p.tf, p.start, closed_end, observed.to_pydatetime()
                )
            row.update({k: counts[k] for k in ("inserted", "revised", "unchanged")})
            received += row["received"]
            written += counts["inserted"] + counts["revised"]
        except (ProviderError, ValueError) as exc:
            row.update(status="failed", note=str(exc)[:200])
            errors.append(f"{p.coin}/{p.tf.value}: {str(exc)[:160]}")
        bundle.write(fetcher.drain_raw())
        out.append(row)
    archived = bundle.close()
    store.finish_run(run_id, status="failed" if errors else "ok", rows_received=received,
                     rows_written=written, rows_rejected=rejected, archived=archived,
                     error="; ".join(errors)[:500] or None)  # fmt: skip
    return pd.DataFrame(out, columns=RESULT_COLUMNS)


def _log_issues(store: Store, run_id: str, venue: str, p: Plan, issues: list[str], at) -> None:
    store.log_issues(pd.DataFrame({
        "run_id": run_id, "symbol": f"{p.coin}-PERP", "timeframe": p.tf.value, "source": venue,
        "check_name": "intraday_ohlc", "severity": "error", "ts": pd.NaT, "detail": issues,
        "detected_at": at}))  # fmt: skip


# --------------------------------------------------------------------------- entry points


def update(
    settings: Settings,
    store: Store,
    *,
    venue: str | None = None,
    coins: list[str] | None = None,
    tfs: list[Timeframe] | None = None,
    fetchers: dict[str, Any] | None = None,
    now: Callable[[], datetime] = utcnow,
    include_history_venues: bool = False,
) -> pd.DataFrame:
    """Incremental update (what the runtime schedules): live venues only unless a venue is
    named. Only fetches from the overlap window before the newest stored bar onward."""
    cfg = intraday_config(settings)
    frames = []
    asof = utc(now())
    for v in cfg.venues.values():
        if venue and v.name != venue:
            continue
        if not venue and not v.live and not include_history_venues:
            continue
        plans = [p for coin in v.symbols if not coins or coin in coins
                 for tf in v.timeframes if not tfs or tf in tfs
                 if (p := plan_update(store, v, coin, tf, asof)) is not None]  # fmt: skip
        if not plans:
            continue
        check_disk(store.path, estimate(plans, cfg))
        f = (fetchers or {}).get(v.name) or make_fetcher(settings, v.name)
        frames.append(run_plans(store, cfg, v.name, f, plans, mode="update", now=now))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=RESULT_COLUMNS)


def backfill(
    settings: Settings,
    store: Store,
    *,
    venue: str,
    start=None,
    end=None,
    coins: list[str] | None = None,
    tfs: list[Timeframe] | None = None,
    fetcher: Any | None = None,
    now: Callable[[], datetime] = utcnow,
    dry_run: bool = False,
) -> tuple[list[Plan], dict, pd.DataFrame]:
    """Deterministic backfill of ``[start, end)`` (defaults: the configured history start per
    timeframe, and the newest closed bar). Idempotent; disk-guarded before any write."""
    cfg = intraday_config(settings)
    v = cfg.venues[venue]
    asof = utc(now())
    plans = []
    for coin in v.symbols:
        if coins and coin not in coins:
            continue
        for tf in v.timeframes:
            if tfs and tf not in tfs:
                continue
            s = (
                start
                if start is not None
                else (v.history_start.get(tf.value) or pd.Timestamp(0, tz="UTC"))
            )
            e = end if end is not None else asof
            p = plan_backfill(v, coin, tf, s, e, asof)
            if p.start < p.end:
                plans.append(p)
    est = estimate(plans, cfg)
    if dry_run:
        return plans, {**est, "disk": None}, pd.DataFrame(columns=RESULT_COLUMNS)
    disk = check_disk(store.path, est)
    f = fetcher or make_fetcher(settings, venue)
    return (
        plans,
        {**est, "disk": disk},
        run_plans(store, cfg, venue, f, plans, mode="backfill", now=now),
    )
