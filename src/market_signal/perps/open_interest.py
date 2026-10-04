"""Perp open interest (OI): data collection only. Not a feature, not a signal.

Two venues, never merged:

* **Hyperliquid**: no OI history API. OI is captured *prospectively* in ``perp_snapshots``
  whenever Prism runs (``market update --only perps`` or ``market oi collect``). Capture
  times are irregular (a local PC, not an always-on collector) and a missed capture can
  never be recovered.
* **Binance USD-M**: ``GET /futures/data/openInterestHist`` serves OI statistics for only the
  latest ~30 days. Every run looks at stored coverage, re-fetches from the earliest gap (or a
  short overlap before the newest row) back to now, and upserts. Downtime shorter than ~30
  days is therefore recovered automatically by the next ordinary run; anything older is gone.

Rows keep exact provider timestamps, raw values (base units and the provider's own USD
value), the statistics period, provider symbol and the ingestion run (raw payloads archived).
``perp_oi_observations`` (a view) lists both venues side by side with ``venue`` explicit.
Future research must use observations by actual elapsed time, never by row count or an
assumed cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from market_signal.config import Settings
from market_signal.data.http import ProviderError
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.perps.binance import OI_PERIODS as PERIODS
from market_signal.perps.data import perp_config, snapshot_contexts

BINANCE = "binance"
HYPERLIQUID = "hyperliquid"
MARKET_TYPE = "usdm_perpetual"
NOTIONAL_METHOD = "provider:sumOpenInterestValue"
COLUMNS = ["coin", "dataset", "status", "received", "new_rows", "note"]


@dataclass(frozen=True)
class OiConfig:
    coins: list[str]
    enabled: bool
    period: str
    history_days: float
    overlap_periods: int
    symbols: dict[str, str]
    bn_stale_hours: float
    bn_recovery_warn_days: float
    hl_stale_hours: float
    hl_gap_hours: float

    @property
    def step(self) -> timedelta:
        return timedelta(seconds=PERIODS[self.period])

    @property
    def dataset(self) -> str:
        return f"perp_oi_{self.period}"


def oi_config(settings: Settings) -> OiConfig:
    cfg = perp_config(settings)
    oi = cfg.get("open_interest") or {}
    bn, hl = oi.get("binance") or {}, oi.get("hyperliquid") or {}
    period = str(bn.get("period", "1h"))
    if period not in PERIODS:
        raise ValueError(f"open_interest.binance.period {period!r} not one of {sorted(PERIODS)}")
    return OiConfig(
        coins=[str(c).upper() for c in cfg.get("coins") or []],
        enabled=bool(bn.get("enabled", False)),
        period=period,
        history_days=float(bn.get("history_days", 30)),
        overlap_periods=int(bn.get("overlap_periods", 3)),
        symbols={str(k).upper(): str(v) for k, v in (bn.get("symbols") or {}).items()},
        bn_stale_hours=float(bn.get("stale_hours", 12)),
        bn_recovery_warn_days=float(bn.get("recovery_warn_days", 20)),
        hl_stale_hours=float(hl.get("stale_hours", 20)),
        hl_gap_hours=float(hl.get("gap_hours", 20)),
    )


# --------------------------------------------------------------------------- planning


def plan_stop(stored: pd.Series, now: datetime, cfg: OiConfig) -> datetime:
    """How far back this run must reach. Never earlier than the API window: rows older
    than that are no longer served. Within the window: the earliest gap between stored
    rows (a period missing), else a short overlap before the newest row, so the latest
    (possibly provisional) points are re-read."""
    floor = now - timedelta(days=cfg.history_days)
    ts = pd.DatetimeIndex(pd.to_datetime(stored, utc=True)).sort_values()
    ts = ts[ts >= pd.Timestamp(floor)]
    if ts.empty:
        return floor
    stop = ts[-1].to_pydatetime() - cfg.overlap_periods * cfg.step
    gaps = ts[1:][(ts[1:] - ts[:-1]) > cfg.step * 1.5]
    if len(gaps):
        first_gap_after = ts[ts.get_loc(gaps[0]) - 1].to_pydatetime()
        stop = min(stop, first_gap_after)
    return max(floor, stop)


# --------------------------------------------------------------------------- writer


def upsert_oi_history(
    store: Store, coin: str, symbol: str, period: str, df: pd.DataFrame, source: str, run_id: str
) -> dict[str, int]:
    """Deterministic upsert keyed on (source, coin, period, observed_at). Unchanged rows are
    left alone (``ingested_at`` keeps the first-seen time); changed values are replaced and
    counted as revisions. All or nothing: a failure leaves the table as it was."""
    if df.empty:
        return {"inserted": 0, "revised": 0, "unchanged": 0}
    now = utcnow()
    stage = pd.DataFrame({
        "source": source, "coin": coin, "provider_symbol": symbol, "market_type": MARKET_TYPE,
        "period": period, "observed_at": df["observed_at"], "open_interest": df["open_interest"],
        "oi_notional": df["oi_notional"], "oi_notional_method": NOTIONAL_METHOD,
        "ingested_at": now, "updated_at": now, "ingest_run_id": run_id,
    })  # fmt: skip
    key = (
        "h.source=s.source AND h.coin=s.coin AND h.period=s.period AND h.observed_at=s.observed_at"
    )
    changed = "(h.open_interest IS DISTINCT FROM s.open_interest OR h.oi_notional IS DISTINCT FROM s.oi_notional)"
    store.con.register("_oi", stage)
    try:
        with store.transaction():
            revised = store.con.execute(
                f"SELECT count(*) FROM _oi s JOIN perp_oi_history h ON {key} WHERE {changed}"
            ).fetchone()[0]
            inserted = store.con.execute(
                f"SELECT count(*) FROM _oi s WHERE NOT EXISTS (SELECT 1 FROM perp_oi_history h WHERE {key})"
            ).fetchone()[0]
            store.con.execute(
                f"""UPDATE perp_oi_history h SET open_interest=s.open_interest, oi_notional=s.oi_notional,
                       provider_symbol=s.provider_symbol, updated_at=s.updated_at, ingest_run_id=s.ingest_run_id
                    FROM _oi s WHERE {key} AND {changed}"""
            )
            store.con.execute(
                f"""INSERT INTO perp_oi_history
                    SELECT source, coin, provider_symbol, market_type, period, observed_at, open_interest,
                           oi_notional, oi_notional_method, ingested_at, updated_at, ingest_run_id
                    FROM _oi s WHERE NOT EXISTS (SELECT 1 FROM perp_oi_history h WHERE {key})"""
            )
    finally:
        store.con.unregister("_oi")
    return {"inserted": int(inserted), "revised": int(revised),
            "unchanged": int(len(stage) - inserted - revised)}  # fmt: skip


def _stored_times(store: Store, coin: str, period: str, since: datetime) -> pd.Series:
    df = store.query(
        "SELECT observed_at FROM perp_oi_history WHERE source=? AND coin=? AND period=? "
        "AND observed_at >= ? ORDER BY observed_at",
        [BINANCE, coin, period, since],
    )
    return df["observed_at"] if not df.empty else pd.Series(dtype="datetime64[us, UTC]")


# --------------------------------------------------------------------------- updaters


def update_binance_oi(
    settings: Settings, store: Store, provider: Any | None = None, now: datetime | None = None
) -> pd.DataFrame:
    """Rolling backfill of Binance OI for every configured Prism perp coin. Safe to run
    any number of times, at any interval. One coin's failure never affects another's."""
    cfg = oi_config(settings)
    if not cfg.enabled:
        return pd.DataFrame(columns=COLUMNS)
    if provider is None:
        from market_signal.data.registry import ProviderRegistry
        from market_signal.perps.binance import BinanceFuturesProvider

        provider = BinanceFuturesProvider(ProviderRegistry(settings).http("binance_futures"))
    out: list[tuple] = []
    for coin in cfg.coins:
        symbol = cfg.symbols.get(coin)
        if symbol is None:
            out.append((coin, cfg.dataset, "unsupported", 0, 0, "no Binance symbol mapped"))
            continue
        t_now = (now or utcnow()).astimezone(UTC)
        floor = t_now - timedelta(days=cfg.history_days)
        stop = plan_stop(_stored_times(store, coin, cfg.period, floor), t_now, cfg)
        params = {
            "symbol": symbol,
            "period": cfg.period,
            "stop_at": stop.isoformat(),
            "end": t_now.isoformat(),
        }
        run_id = store.start_run(BINANCE, cfg.dataset, coin, params)
        try:
            df = provider.open_interest_history(symbol, cfg.period, stop, t_now)
            archived = store.archive_raw(provider.drain_raw(), run_id)
            if df.empty:
                store.finish_run(run_id, status="ok", archived=archived)
                out.append(
                    (coin, cfg.dataset, "unavailable", 0, 0, f"{symbol}: Binance returned no OI")
                )
                continue
            counts = upsert_oi_history(store, coin, symbol, cfg.period, df, BINANCE, run_id)
            store.finish_run(run_id, status="ok", rows_received=len(df),
                             rows_written=counts["inserted"] + counts["revised"], archived=archived)  # fmt: skip
            first, last = df["observed_at"].iloc[0], df["observed_at"].iloc[-1]
            out.append((coin, cfg.dataset, "ok", len(df), counts["inserted"],
                        f"{symbol} {first:%m-%d %H:%M}→{last:%m-%d %H:%M}"
                        + (f"; {counts['revised']} revised" if counts["revised"] else "")))  # fmt: skip
        except ProviderError as exc:
            store.finish_run(run_id, status="failed", archived=store.archive_raw(provider.drain_raw(), run_id),
                             error=str(exc)[:500])  # fmt: skip
            out.append((coin, cfg.dataset, "failed", 0, 0, str(exc)[:80]))
    return pd.DataFrame(out, columns=COLUMNS)


def collect_hl_snapshot(
    settings: Settings, store: Store, provider: Any | None = None
) -> pd.DataFrame:
    """Just the Hyperliquid snapshot (no candles/funding): the cheap, frequent part."""
    coins = oi_config(settings).coins
    if not coins:
        return pd.DataFrame(columns=COLUMNS)
    if provider is None:
        from market_signal.data.registry import ProviderRegistry

        provider = ProviderRegistry(settings).market(HYPERLIQUID)
    run_id = store.start_run(HYPERLIQUID, "perp_snapshot", "ALL", {})
    try:
        received, written, note = snapshot_contexts(settings, store, provider, coins, run_id)
        store.finish_run(run_id, status="ok", rows_received=received, rows_written=written,
                         archived=store.archive_raw(provider.drain_raw(), run_id))  # fmt: skip
        return pd.DataFrame(
            [("ALL", "perp_snapshot", "ok", received, written, note)], columns=COLUMNS
        )
    except ProviderError as exc:
        store.finish_run(run_id, status="failed", archived=store.archive_raw(provider.drain_raw(), run_id),
                         error=str(exc)[:500])  # fmt: skip
        return pd.DataFrame(
            [("ALL", "perp_snapshot", "failed", 0, 0, str(exc)[:80])], columns=COLUMNS
        )


# --------------------------------------------------------------------------- coverage


def load_oi(store: Store, coin: str, venue: str) -> pd.DataFrame:
    """One venue's OI observations for one coin, by exact timestamp. Never mixes venues."""
    try:
        return store.query(
            "SELECT * FROM perp_oi_observations WHERE venue=? AND coin=? ORDER BY observed_at",
            [venue, coin],
        )
    except Exception:
        return pd.DataFrame()


def oi_gaps(times: pd.Series, min_gap: timedelta, since: datetime | None = None) -> pd.DataFrame:
    """Intervals between consecutive observations longer than ``min_gap`` (by elapsed time)."""
    ts = pd.DatetimeIndex(pd.to_datetime(times, utc=True)).sort_values()
    if since is not None:
        ts = ts[ts >= pd.Timestamp(since)]
    if len(ts) < 2:
        return pd.DataFrame(columns=["after", "before", "hours"])
    d = ts[1:] - ts[:-1]
    m = d > min_gap
    return pd.DataFrame({"after": ts[:-1][m], "before": ts[1:][m],
                         "hours": d[m].total_seconds() / 3600}).reset_index(drop=True)  # fmt: skip


def oi_coverage(store: Store, settings: Settings, now: datetime | None = None) -> pd.DataFrame:
    """Per (venue, coin): row count, first/last observation, age, gaps in the last
    ``history_days``, and a status. Hyperliquid is judged as opportunistic sampling (only a
    long silence is flagged: it can't be recovered); Binance by whether its gaps can still
    be recovered from the ~30-day API window."""
    cfg = oi_config(settings)
    t_now = pd.Timestamp((now or utcnow()).astimezone(UTC))
    window = t_now - pd.Timedelta(days=cfg.history_days)
    try:
        obs = store.query(
            "SELECT venue, coin, period, observed_at FROM perp_oi_observations "
            "WHERE venue IN (?, ?) ORDER BY observed_at",
            [HYPERLIQUID, BINANCE],
        )
    except Exception:
        obs = pd.DataFrame(columns=["venue", "coin", "period", "observed_at"])
    rows = []

    def add(venue, coin, period, symbol, status, note="", g=None, s=None):
        rows.append({
            "venue": venue, "coin": coin, "period": period, "symbol": symbol,
            "rows": 0 if s is None else len(s),
            "first": None if s is None or s.empty else s.iloc[0],
            "last": None if s is None or s.empty else s.iloc[-1],
            "age_h": None if s is None or s.empty else (t_now - s.iloc[-1]).total_seconds() / 3600,
            "gaps_30d": 0 if g is None else len(g),
            "max_gap_h": None if g is None or g.empty else float(g["hours"].max()),
            "status": status, "note": note,
        })  # fmt: skip

    for coin in cfg.coins:
        s = pd.to_datetime(
            obs.loc[(obs["venue"] == HYPERLIQUID) & (obs["coin"] == coin), "observed_at"], utc=True
        )
        s = s.sort_values().reset_index(drop=True)
        if s.empty:
            add(
                HYPERLIQUID,
                coin,
                "snapshot",
                coin,
                "NO DATA",
                "prospective only: start collecting now",
            )
        else:
            g = oi_gaps(s, timedelta(hours=cfg.hl_gap_hours), window)
            age = (t_now - s.iloc[-1]).total_seconds() / 3600
            st = "ok" if age <= cfg.hl_stale_hours else "STALE"
            note = (
                "opportunistic snapshots"
                if st == "ok"
                else "not collecting; missed time is lost for good"
            )
            if len(g):
                note += f"; {len(g)} gap(s) >{cfg.hl_gap_hours:g}h (permanent)"
            add(HYPERLIQUID, coin, "snapshot", coin, st, note, g, s)
    if not cfg.enabled:
        return pd.DataFrame(rows)
    for coin in cfg.coins:
        symbol = cfg.symbols.get(coin)
        if symbol is None:
            add(BINANCE, coin, cfg.period, "-", "unsupported", "no Binance perp mapped")
            continue
        s = pd.to_datetime(obs.loc[(obs["venue"] == BINANCE) & (obs["coin"] == coin)
                                   & (obs["period"] == cfg.period), "observed_at"], utc=True)  # fmt: skip
        s = s.sort_values().reset_index(drop=True)
        if s.empty:
            add(BINANCE, coin, cfg.period, symbol, "NO DATA", "run `market oi collect`")
            continue
        g = oi_gaps(s, cfg.step * 1.5, window)
        age = (t_now - s.iloc[-1]).total_seconds() / 3600
        left = cfg.history_days - age / 24
        if age / 24 >= cfg.history_days:
            st, note = (
                "LOST",
                f"newest row {age / 24:.1f}d old: older than the API window, gap is permanent",
            )
        elif age / 24 >= cfg.bn_recovery_warn_days:
            st, note = "AT RISK", f"run within {left:.1f}d or the oldest missing hours are lost"
        elif age > cfg.bn_stale_hours:
            st, note = "stale", f"recoverable: next run backfills ({left:.1f}d left)"
        else:
            st, note = "ok", "backfillable (~30d window)"
        if len(g):
            note += f"; {len(g)} gap(s) in window (next run refetches)"
        add(BINANCE, coin, cfg.period, symbol, st, note, g, s)
    return pd.DataFrame(rows)
