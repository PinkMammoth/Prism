"""Equity/ETF daily providers: Tiingo (primary), Stooq (fallback), CSV import.

NYSE daily bars are stored with ``ts`` = session open (09:30 New York) in UTC.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from market_signal.data.calendars import nyse_open_utc
from market_signal.data.http import HttpClient, ProviderError, RawPayload, SchemaError
from market_signal.data.providers.base import (
    ACTIONS_COLUMNS,
    BarsResult,
    HttpProvider,
    PriceQuote,
    empty_bars,
    normalise_bars,
    schema_fingerprint,
)
from market_signal.models.domain import Calendar, Timeframe, utcnow


def _session_open_ts(dates: pd.Series) -> pd.Series:
    return pd.Series([pd.Timestamp(nyse_open_utc(d)) for d in dates], index=dates.index)


class MissingApiKey(ProviderError):
    pass


class TiingoProvider(HttpProvider):
    """Tiingo EOD prices. Stores RAW OHLCV; splits/dividends go to corporate actions."""

    name = "tiingo"

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key

    def _require_key(self) -> str:
        if not self.api_key:
            raise MissingApiKey("TIINGO_API_KEY not set (free key: https://www.tiingo.com)")
        return self.api_key

    @staticmethod
    def parse(payload: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
        if isinstance(payload, dict):
            raise ProviderError(f"tiingo error: {payload.get('detail', payload)}")
        if not isinstance(payload, list):
            raise SchemaError("tiingo prices: expected a list")
        if not payload:
            return empty_bars(), pd.DataFrame(columns=ACTIONS_COLUMNS)
        df = pd.DataFrame(payload)
        need = {"date", "open", "high", "low", "close", "volume", "divCash", "splitFactor"}
        if not need.issubset(df.columns):
            raise SchemaError(f"tiingo prices missing {sorted(need - set(df.columns))}")
        d = pd.to_datetime(df["date"], utc=True).dt.date
        df["ts"] = _session_open_ts(d)
        bars = normalise_bars(df)
        actions = pd.DataFrame(
            {
                "date": d,
                "split_factor": pd.to_numeric(df["splitFactor"], errors="coerce"),
                "dividend": pd.to_numeric(df["divCash"], errors="coerce"),
            }
        )
        actions = actions[(actions["split_factor"] != 1.0) | (actions["dividend"] != 0.0)]
        return bars, actions.reset_index(drop=True)

    def get_daily_bars(self, symbol, start, end) -> BarsResult:
        key = self._require_key()
        params = {
            "startDate": (start or datetime(1990, 1, 1)).date().isoformat(),
            "endDate": (end or utcnow()).date().isoformat(),
            "format": "json",
            "resampleFreq": "daily",
            "token": key,
        }
        payload = self.http.get_json(
            f"/tiingo/daily/{symbol}/prices", params=params, redact_params=("token",)
        )
        bars, actions = self.parse(payload)
        return BarsResult(
            self.name,
            Timeframe.D1,
            bars,
            actions,
            raw=self.drain_raw(),
            schema_fingerprint=schema_fingerprint(payload),
        )

    def get_intraday_bars(self, symbol, timeframe, start, end) -> BarsResult:
        raise ProviderError("tiingo: intraday equities are out of scope for V1")

    def get_latest_price(self, symbol: str) -> PriceQuote:
        res = self.get_daily_bars(symbol, utcnow() - pd.Timedelta(days=10), utcnow())
        if res.bars.empty:
            raise ProviderError(f"tiingo: no recent bars for {symbol}")
        last = res.bars.iloc[-1]
        return PriceQuote(symbol, float(last["close"]), last["ts"].to_pydatetime(), self.name)


class StooqProvider(HttpProvider):
    """Stooq daily CSV. Requires a (free, CAPTCHA-issued) API key since 2026.

    Stooq prices are provider-adjusted and carry no corporate actions, so this series'
    price basis differs from Tiingo's raw series. It is stored as a separate source.
    """

    name = "stooq"

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key

    @staticmethod
    def parse(body: bytes) -> pd.DataFrame:
        text = body.decode("utf-8", errors="replace").strip()
        if not text or text.lower().startswith(("no data", "exceeded", "<")):
            raise ProviderError(f"stooq: {text[:120]!r}")
        df = pd.read_csv(io.StringIO(text))
        cols = {c.lower(): c for c in df.columns}
        need = {"date", "open", "high", "low", "close"}
        if not need.issubset(cols):
            raise SchemaError(f"stooq csv columns changed: {list(df.columns)}")
        df = df.rename(columns={v: k for k, v in cols.items()})
        if "volume" not in df:
            df["volume"] = float("nan")
        d = pd.to_datetime(df["date"]).dt.date
        df["ts"] = _session_open_ts(d)
        return normalise_bars(df)

    def get_daily_bars(self, symbol, start, end) -> BarsResult:
        if not self.api_key:
            raise MissingApiKey("STOOQ_API_KEY not set (obtain via CAPTCHA at stooq.com)")
        params: dict[str, Any] = {"s": f"{symbol.lower()}.us", "i": "d", "apikey": self.api_key}
        if start:
            params["d1"] = start.strftime("%Y%m%d")
        if end:
            params["d2"] = end.strftime("%Y%m%d")
        body = self.http.request("GET", "/q/d/l/", params=params, redact_params=("apikey",))
        bars = self.parse(body)
        return BarsResult(
            self.name, Timeframe.D1, bars, raw=self.drain_raw(), schema_fingerprint="csv"
        )

    def get_intraday_bars(self, symbol, timeframe, start, end) -> BarsResult:
        raise ProviderError("stooq: intraday not supported")

    def get_latest_price(self, symbol: str) -> PriceQuote:
        res = self.get_daily_bars(symbol, None, None)
        last = res.bars.iloc[-1]
        return PriceQuote(symbol, float(last["close"]), last["ts"].to_pydatetime(), self.name)


def read_csv_bars(
    path: Path, calendar: Calendar, timeframe: Timeframe = Timeframe.D1
) -> tuple[pd.DataFrame, pd.DataFrame | None, RawPayload]:
    """Import user-supplied bars. Required columns: date|ts, open, high, low, close.
    Optional: volume, split_factor, dividend. Provenance = file SHA-256."""
    body = path.read_bytes()
    df = pd.read_csv(io.BytesIO(body))
    df.columns = [c.strip().lower() for c in df.columns]
    time_col = "ts" if "ts" in df.columns else "date" if "date" in df.columns else None
    if time_col is None:
        raise SchemaError("CSV needs a 'date' or 'ts' column")
    if "volume" not in df:
        df["volume"] = float("nan")
    if calendar == Calendar.NYSE and timeframe == Timeframe.D1:
        d = pd.to_datetime(df[time_col]).dt.date
        df["ts"] = _session_open_ts(d)
    else:
        df["ts"] = pd.to_datetime(df[time_col], utc=True)
    bars = normalise_bars(df)
    actions = None
    if {"split_factor", "dividend"} & set(df.columns):
        actions = pd.DataFrame(
            {
                "date": pd.to_datetime(df[time_col]).dt.date,
                "split_factor": pd.to_numeric(df.get("split_factor", 1.0), errors="coerce"),
                "dividend": pd.to_numeric(df.get("dividend", 0.0), errors="coerce"),
            }
        )
        actions = actions[(actions["split_factor"] != 1.0) | (actions["dividend"] != 0.0)]
    raw = RawPayload(
        provider="csv",
        method="FILE",
        url=str(path.resolve()),
        params={"sha256": hashlib.sha256(body).hexdigest()},
        status=200,
        body=body,
        fetched_at=datetime.now(UTC),
    )
    return bars, actions, raw
