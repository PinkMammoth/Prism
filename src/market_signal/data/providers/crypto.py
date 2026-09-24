"""Crypto market-data providers: Coinbase Exchange (primary), Bitstamp (fallback),
Hyperliquid (HYPE).

All three are public, keyless REST APIs. See docs/DATA_SOURCES.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.data.providers.base import (
    BarsResult,
    HttpProvider,
    PriceQuote,
    empty_bars,
    normalise_bars,
    resample_bars,
    schema_fingerprint,
)
from market_signal.models.domain import Timeframe, utcnow

EPOCH_START = datetime(2010, 1, 1, tzinfo=UTC)


def _as_utc(dt: datetime | None, default: datetime) -> datetime:
    if dt is None:
        return default
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- Coinbase
class CoinbaseProvider(HttpProvider):
    """Coinbase Exchange public candles.

    Rows: ``[time, low, high, open, close, volume]`` newest-first, max 300 per request.
    No native 4h granularity: 4h bars are aggregated from 1h bars of this provider and
    labelled ``coinbase:agg1h``.
    """

    name = "coinbase"
    GRANULARITY = {Timeframe.H1: 3600, Timeframe.D1: 86400}

    def __init__(self, http: HttpClient, max_candles: int = 300):
        super().__init__(http)
        self.max_candles = max_candles

    def _parse(self, payload: Any) -> pd.DataFrame:
        if isinstance(payload, dict):
            raise ProviderError(f"coinbase error: {payload.get('message', payload)}")
        if not isinstance(payload, list):
            raise SchemaError("coinbase candles: expected a list")
        rows = []
        for row in payload:
            if not (isinstance(row, list) and len(row) == 6):
                raise SchemaError(f"coinbase candles: unexpected row shape {row!r}")
            t, low, high, open_, close, vol = row
            rows.append((t, open_, high, low, close, vol))
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["t"].astype("int64"), unit="s", utc=True)
        return normalise_bars(df)

    def _fetch(
        self, symbol: str, gran: int, start: datetime, end: datetime
    ) -> tuple[pd.DataFrame, str]:
        frames, fp = [], ""
        step = timedelta(seconds=gran * self.max_candles)
        cursor = start
        while cursor < end:
            window_end = min(cursor + step - timedelta(seconds=gran), end)
            payload = self.http.get_json(
                f"/products/{symbol}/candles",
                params={
                    "granularity": gran,
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                },
            )
            fp = fp or schema_fingerprint(payload)
            frames.append(self._parse(payload))
            cursor = window_end + timedelta(seconds=gran)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return empty_bars(), fp
        df = pd.concat(frames).drop_duplicates("ts", keep="last")
        return normalise_bars(df), fp

    def get_daily_bars(self, symbol, start, end) -> BarsResult:
        start = _as_utc(start, EPOCH_START)
        end = _as_utc(end, utcnow())
        bars, fp = self._fetch(symbol, 86400, start, end)
        return BarsResult(
            self.name, Timeframe.D1, bars, raw=self.drain_raw(), schema_fingerprint=fp
        )

    def get_intraday_bars(self, symbol, timeframe, start, end) -> BarsResult:
        start = _as_utc(start, utcnow() - timedelta(days=365))
        end = _as_utc(end, utcnow())
        if timeframe == Timeframe.H1:
            bars, fp = self._fetch(symbol, 3600, start, end)
            return BarsResult(
                self.name, timeframe, bars, raw=self.drain_raw(), schema_fingerprint=fp
            )
        if timeframe == Timeframe.H4:
            # align to 4h buckets so partial buckets are not fetched at the edges
            aligned = start.replace(minute=0, second=0, microsecond=0)
            aligned -= timedelta(hours=aligned.hour % 4)
            hourly, fp = self._fetch(symbol, 3600, aligned, end)
            bars = resample_bars(hourly, "4h", expected_parts=4)
            return BarsResult(
                f"{self.name}:agg1h", timeframe, bars, raw=self.drain_raw(), schema_fingerprint=fp
            )
        raise ProviderError(f"coinbase: unsupported timeframe {timeframe}")

    def get_latest_price(self, symbol: str) -> PriceQuote:
        payload = self.http.get_json(f"/products/{symbol}/ticker")
        self.drain_raw()
        try:
            return PriceQuote(
                symbol,
                float(payload["price"]),
                pd.Timestamp(payload["time"]).to_pydatetime(),
                self.name,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"coinbase ticker schema changed: {payload!r}") from exc


# --------------------------------------------------------------------------- Bitstamp
class BitstampProvider(HttpProvider):
    """Bitstamp public OHLC (native 4h). When both start and end are given the API
    honours ``end``, so we page backwards from ``end`` using ``limit``."""

    name = "bitstamp"
    STEP = {Timeframe.H1: 3600, Timeframe.H4: 14400, Timeframe.D1: 86400}

    def __init__(self, http: HttpClient, max_candles: int = 1000):
        super().__init__(http)
        self.max_candles = max_candles

    def _parse(self, payload: Any) -> pd.DataFrame:
        try:
            rows = payload["data"]["ohlc"]
        except (KeyError, TypeError) as exc:
            raise SchemaError(f"bitstamp ohlc schema changed: {str(payload)[:200]}") from exc
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows)
        need = {"timestamp", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            raise SchemaError(f"bitstamp ohlc missing {need - set(df.columns)}")
        df["ts"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="s", utc=True)
        return normalise_bars(df)

    def _fetch(
        self, symbol: str, tf: Timeframe, start: datetime, end: datetime
    ) -> tuple[pd.DataFrame, str]:
        step = self.STEP[tf]
        frames, fp = [], ""
        cursor_end = int(end.timestamp())
        start_ts = int(start.timestamp())
        while cursor_end > start_ts:
            payload = self.http.get_json(
                f"/ohlc/{symbol}/",
                params={"step": step, "limit": self.max_candles, "end": cursor_end},
            )
            fp = fp or schema_fingerprint(payload)
            df = self._parse(payload)
            if df.empty:
                break
            frames.append(df)
            earliest = int(df["ts"].min().timestamp())
            if earliest >= cursor_end:
                break
            cursor_end = earliest - step
        if not frames:
            return empty_bars(), fp
        df = pd.concat(frames).drop_duplicates("ts", keep="last")
        df = df[df["ts"] >= pd.Timestamp(start)]
        return normalise_bars(df), fp

    def get_daily_bars(self, symbol, start, end) -> BarsResult:
        bars, fp = self._fetch(
            symbol, Timeframe.D1, _as_utc(start, EPOCH_START), _as_utc(end, utcnow())
        )
        return BarsResult(
            self.name, Timeframe.D1, bars, raw=self.drain_raw(), schema_fingerprint=fp
        )

    def get_intraday_bars(self, symbol, timeframe, start, end) -> BarsResult:
        if timeframe not in self.STEP:
            raise ProviderError(f"bitstamp: unsupported timeframe {timeframe}")
        bars, fp = self._fetch(
            symbol,
            timeframe,
            _as_utc(start, utcnow() - timedelta(days=365)),
            _as_utc(end, utcnow()),
        )
        return BarsResult(self.name, timeframe, bars, raw=self.drain_raw(), schema_fingerprint=fp)

    def get_latest_price(self, symbol: str) -> PriceQuote:
        payload = self.http.get_json(f"/ticker/{symbol}/")
        self.drain_raw()
        try:
            return PriceQuote(
                symbol,
                float(payload["last"]),
                datetime.fromtimestamp(int(payload["timestamp"]), tz=UTC),
                self.name,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError("bitstamp ticker schema changed") from exc


# --------------------------------------------------------------------------- Hyperliquid
class HyperliquidProvider(HttpProvider):
    """Hyperliquid public info API. Only the latest 5,000 candles are served.

    ``symbol`` is the token name (e.g. ``HYPE``); the spot pair id (``@107``) is resolved
    from ``spotMeta`` and cached, with a configured fallback.
    """

    name = "hyperliquid"
    INTERVAL = {Timeframe.H1: "1h", Timeframe.H4: "4h", Timeframe.D1: "1d"}

    def __init__(
        self,
        http: HttpClient,
        pair_fallbacks: dict[str, str] | None = None,
        max_candles: int = 5000,
    ):
        super().__init__(http)
        self.pair_fallbacks = pair_fallbacks or {}
        self.max_candles = max_candles
        self._pair_cache: dict[str, str] = {}

    def info(self, payload: dict[str, Any]) -> Any:
        return self.http.post_json("/info", payload)

    def resolve_spot_pair(self, token: str) -> str:
        if token in self._pair_cache:
            return self._pair_cache[token]
        try:
            meta = self.info({"type": "spotMeta"})
            tokens = {t["index"]: t["name"] for t in meta["tokens"]}
            for pair in meta["universe"]:
                base_idx, quote_idx = pair["tokens"]
                if tokens.get(base_idx) == token and tokens.get(quote_idx) == "USDC":
                    self._pair_cache[token] = pair["name"]
                    return pair["name"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"hyperliquid spotMeta schema changed: {exc}") from exc
        if token in self.pair_fallbacks:
            return self.pair_fallbacks[token]
        raise ProviderError(f"hyperliquid: no USDC spot pair for {token}")

    def _parse(self, payload: Any) -> pd.DataFrame:
        if not isinstance(payload, list):
            raise SchemaError(
                f"hyperliquid candleSnapshot: expected list, got {type(payload).__name__}"
            )
        if not payload:
            return empty_bars()
        df = pd.DataFrame(payload)
        need = {"t", "o", "h", "l", "c", "v"}
        if not need.issubset(df.columns):
            raise SchemaError(f"hyperliquid candle missing {need - set(df.columns)}")
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df["ts"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
        return normalise_bars(df)

    def _candles(
        self, symbol: str, tf: Timeframe, start: datetime, end: datetime
    ) -> tuple[pd.DataFrame, str]:
        coin = self.resolve_spot_pair(symbol)
        span = timedelta(seconds=tf.seconds * self.max_candles)
        start = max(start, end - span)  # API serves only the latest 5,000 candles
        payload = self.info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": self.INTERVAL[tf],
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000),
                },
            }
        )
        return self._parse(payload), schema_fingerprint(payload)

    def get_daily_bars(self, symbol, start, end) -> BarsResult:
        bars, fp = self._candles(
            symbol, Timeframe.D1, _as_utc(start, EPOCH_START), _as_utc(end, utcnow())
        )
        return BarsResult(
            self.name, Timeframe.D1, bars, raw=self.drain_raw(), schema_fingerprint=fp
        )

    def get_intraday_bars(self, symbol, timeframe, start, end) -> BarsResult:
        if timeframe not in self.INTERVAL:
            raise ProviderError(f"hyperliquid: unsupported timeframe {timeframe}")
        bars, fp = self._candles(
            symbol,
            timeframe,
            _as_utc(start, utcnow() - timedelta(days=365)),
            _as_utc(end, utcnow()),
        )
        return BarsResult(self.name, timeframe, bars, raw=self.drain_raw(), schema_fingerprint=fp)

    def get_latest_price(self, symbol: str) -> PriceQuote:
        coin = self.resolve_spot_pair(symbol)
        mids = self.info({"type": "allMids"})
        self.drain_raw()
        if not isinstance(mids, dict) or coin not in mids:
            raise SchemaError(f"hyperliquid allMids missing {coin}")
        return PriceQuote(symbol, float(mids[coin]), utcnow(), self.name)
