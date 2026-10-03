"""Binance USD-M perpetuals: daily candles and funding history (public endpoints, no key).

Second venue, used for HISTORY: Binance perps go back to 2019, years before Hyperliquid's
data. Stored with ``source = "binance"`` in the same perp tables; loaders never mix
venues. Funding settles every 8 hours (some symbols switch to 4h at times); the daily
aggregation handles either.

Binance blocks some locations (notably the US) with HTTP 451. That is reported clearly
and nothing is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from market_signal.config import Settings
from market_signal.data.calendars import close_times
from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.data.providers.base import HttpProvider, normalise_bars
from market_signal.data.store import Store
from market_signal.data.validation import BatchRejected, validate_batch
from market_signal.models.domain import Calendar, Timeframe, utcnow
from market_signal.perps.data import perp_config, upsert_funding, upsert_perp_bars

SOURCE = "binance"
DAY_MS = 86_400_000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _explain(exc: ProviderError) -> ProviderError:
    msg = str(exc)
    if "HTTP 451" in msg or "HTTP 403" in msg:
        return ProviderError(
            "Binance refused the request from this location (HTTP 451/403: restricted region). "
            "Its futures market data isn't available here; Binance history can't be used."
        )
    return exc


class BinanceFuturesProvider(HttpProvider):
    name = "binance_futures"

    def __init__(
        self,
        http: HttpClient,
        kline_limit: int = 1500,
        funding_limit: int = 1000,
        max_pages: int = 400,
    ):
        super().__init__(http)
        self.kline_limit, self.funding_limit, self.max_pages = kline_limit, funding_limit, max_pages

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        rows: list = []
        cursor, end_ms = _ms(start), _ms(end)
        for _ in range(self.max_pages):
            try:
                page = self.http.get_json("/fapi/v1/klines", params={
                    "symbol": symbol, "interval": "1d", "startTime": cursor, "endTime": end_ms, "limit": self.kline_limit})  # fmt: skip
            except ProviderError as exc:
                raise _explain(exc) from None
            if not isinstance(page, list) or any(
                not isinstance(r, list) or len(r) < 7 for r in page
            ):
                raise SchemaError(
                    "binance klines: expected a list of arrays [openTime, o, h, l, c, v, closeTime, …]"
                )
            rows += page
            if len(page) < self.kline_limit:
                break
            nxt = int(page[-1][0]) + DAY_MS
            if nxt <= cursor or nxt > end_ms:
                break
            cursor = nxt
        if not rows:
            return normalise_bars(
                pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
            )
        df = pd.DataFrame(
            [r[:6] for r in rows], columns=["t", "open", "high", "low", "close", "volume"]
        )
        df["ts"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
        return normalise_bars(df.drop_duplicates("t"))

    def funding_history(
        self, symbol: str, start: datetime, end: datetime | None = None
    ) -> pd.DataFrame:
        rows: list = []
        cursor, end_ms = _ms(start), _ms(end or utcnow())
        for _ in range(self.max_pages):
            try:
                page = self.http.get_json("/fapi/v1/fundingRate", params={
                    "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": self.funding_limit})  # fmt: skip
            except ProviderError as exc:
                raise _explain(exc) from None
            if not isinstance(page, list):
                raise SchemaError(f"binance fundingRate: expected list, got {type(page).__name__}")
            for r in page:
                if not isinstance(r, dict) or not {"fundingTime", "fundingRate"} <= r.keys():
                    raise SchemaError("binance fundingRate row missing fundingTime/fundingRate")
                if r.get("symbol", symbol) != symbol:
                    raise SchemaError(
                        f"binance fundingRate: asked for {symbol}, got {r.get('symbol')}"
                    )
            rows += page
            if len(page) < self.funding_limit:
                break
            nxt = int(page[-1]["fundingTime"]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
        if not rows:
            return pd.DataFrame({"time": pd.Series(dtype="datetime64[us, UTC]"), "funding_rate": pd.Series(dtype=float),
                                 "premium": pd.Series(dtype=float)})  # fmt: skip
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "time": pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True).astype("datetime64[us, UTC]"),
            "funding_rate": pd.to_numeric(df["fundingRate"], errors="coerce"),
            "premium": float("nan"),
        })  # fmt: skip
        if out["funding_rate"].isna().any():
            raise SchemaError("binance fundingRate: non-numeric fundingRate")
        return out.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def update_binance_perps(
    settings: Settings, store: Store, provider: Any | None = None
) -> pd.DataFrame:
    """Incremental, idempotent; first run backfills from ``history_start``."""
    v = ((perp_config(settings).get("venues") or {}).get("binance")) or {}
    out: list[tuple] = []
    if not v.get("enabled", False):
        return pd.DataFrame(columns=["coin", "dataset", "status", "received", "new_rows", "note"])
    if provider is None:
        from market_signal.data.registry import ProviderRegistry

        provider = BinanceFuturesProvider(ProviderRegistry(settings).http("binance_futures"))
    start0 = datetime.fromisoformat(str(v.get("history_start", "2019-09-01"))).replace(tzinfo=UTC)

    def last(sql: str, coin: str) -> datetime | None:
        row = store.con.execute(sql, [coin, SOURCE]).fetchone()
        return None if not row or row[0] is None else pd.Timestamp(row[0]).to_pydatetime()

    def run(dataset: str, coin: str, params: dict, fn) -> None:
        run_id = store.start_run(SOURCE, dataset, coin, params)
        try:
            received, written, note = fn(run_id)
            store.finish_run(run_id, status="ok", rows_received=received, rows_written=written,
                             archived=store.archive_raw(provider.drain_raw(), run_id))  # fmt: skip
            out.append((coin, dataset, "ok", received, written, note))
        except (ProviderError, BatchRejected) as exc:
            store.finish_run(run_id, status="failed", archived=store.archive_raw(provider.drain_raw(), run_id),
                             error=str(exc)[:500])  # fmt: skip
            out.append((coin, dataset, "failed", 0, 0, str(exc)[:80]))

    for coin, symbol in (v.get("symbols") or {}).items():
        lb = last("SELECT max(ts) FROM perp_bars WHERE coin=? AND source=?", coin)
        start = start0 if lb is None else lb - timedelta(days=3)

        def bars_fn(
            run_id: str, coin: str = coin, symbol: str = symbol, start: datetime = start
        ) -> tuple:
            fetched = pd.Timestamp(utcnow())
            bars = provider.daily_bars(symbol, start, fetched.to_pydatetime())
            if bars.empty:
                return 0, 0, "no candles"
            bars["close_time"] = close_times(bars["ts"], Calendar.CRYPTO_24_7, Timeframe.D1)
            bars = bars[bars["close_time"] <= fetched].reset_index(drop=True)  # closed bars only
            vr = validate_batch(bars, symbol=f"{coin}-PERP@binance", timeframe=Timeframe.D1, source=SOURCE,
                                calendar=Calendar.CRYPTO_24_7, run_id=run_id)  # fmt: skip
            store.log_issues(vr.issues)
            return (
                len(bars),
                upsert_perp_bars(store, coin, vr.clean, SOURCE, run_id),
                f"{symbol} daily candles",
            )

        run("perp_bars_1d", coin, {"symbol": symbol, "start": str(start)}, bars_fn)
        lf = last("SELECT max(time) FROM perp_funding WHERE coin=? AND source=?", coin)
        f_start = start0 if lf is None else lf + timedelta(milliseconds=1)

        def funding_fn(
            run_id: str, coin: str = coin, symbol: str = symbol, f_start: datetime = f_start
        ) -> tuple:
            df = provider.funding_history(symbol, f_start)
            return (
                len(df),
                upsert_funding(store, coin, df, SOURCE, run_id),
                f"{symbol} funding (8h)",
            )

        run("perp_funding", coin, {"symbol": symbol, "start": str(f_start)}, funding_fn)
    return pd.DataFrame(out, columns=["coin", "dataset", "status", "received", "new_rows", "note"])


def hyperliquid_start(store: Store) -> pd.Timestamp | None:
    """When the Hyperliquid perp history used by the main research begins (earliest bar)."""
    try:
        t = store.con.execute(
            "SELECT min(ts) FROM perp_bars WHERE source IN ('hyperliquid', 'synthetic')"
        ).fetchone()[0]
    except Exception:
        return None
    return None if t is None else pd.Timestamp(t).tz_convert("UTC")
