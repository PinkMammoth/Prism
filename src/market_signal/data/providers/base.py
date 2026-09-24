"""Provider interfaces.

Business logic depends on these protocols only. Implementations return *normalised*
frames and never write to the database themselves.

Bar frame contract (``BARS_COLUMNS``):
    ts      datetime64[UTC]  bar open time
    open, high, low, close, volume  float64 (volume may be NaN if unknown)
Corporate actions frame contract (``ACTIONS_COLUMNS``):
    date (datetime.date), split_factor (float, 1.0 = none), dividend (float, 0 = none)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from market_signal.data.http import HttpClient, RawPayload, SchemaError
from market_signal.models.domain import Timeframe

BARS_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
ACTIONS_COLUMNS = ["date", "split_factor", "dividend"]


@dataclass
class BarsResult:
    source: str  # label stored with each bar, e.g. "coinbase" or "coinbase:agg1h"
    timeframe: Timeframe
    bars: pd.DataFrame
    actions: pd.DataFrame | None = None
    raw: list[RawPayload] = field(default_factory=list)
    schema_fingerprint: str = ""


@dataclass
class PriceQuote:
    symbol: str
    price: float
    as_of: datetime
    source: str


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    def get_daily_bars(
        self, symbol: str, start: datetime | None, end: datetime | None
    ) -> BarsResult: ...

    def get_intraday_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime | None, end: datetime | None
    ) -> BarsResult: ...

    def get_latest_price(self, symbol: str) -> PriceQuote: ...


@runtime_checkable
class MacroDataProvider(Protocol):
    name: str

    def get_series(self, series_id: str, start: datetime | None = None) -> pd.DataFrame:
        """Return columns: series_id, obs_date, value, realtime_start, realtime_end,
        available_at, pit_method."""
        ...


@runtime_checkable
class FundamentalDataProvider(Protocol):
    name: str

    def get_fundamentals(self, entity: str) -> pd.DataFrame: ...


def empty_bars() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in BARS_COLUMNS})
    df["ts"] = pd.Series(dtype="datetime64[us, UTC]")
    return df[BARS_COLUMNS]


def normalise_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to the bar contract; sort; no filling of anything."""
    missing = [c for c in BARS_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaError(f"bars frame missing columns {missing}")
    out = df[BARS_COLUMNS].copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True).astype("datetime64[us, UTC]")
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    return out.sort_values("ts").reset_index(drop=True)


def schema_fingerprint(sample: object) -> str:
    """Describe the *shape* of a payload (keys/types), to detect provider schema drift."""
    import hashlib
    import json

    def shape(x: object, depth: int = 0) -> object:
        if depth > 4:
            return "…"
        if isinstance(x, dict):
            return {k: shape(v, depth + 1) for k, v in sorted(x.items())[:50]}
        if isinstance(x, list):
            return [shape(x[0], depth + 1)] if x else []
        return type(x).__name__

    return hashlib.sha256(json.dumps(shape(sample), sort_keys=True).encode()).hexdigest()[:12]


def resample_bars(bars: pd.DataFrame, rule: str, expected_parts: int) -> pd.DataFrame:
    """Aggregate finer bars into coarser ones (same provider only).

    A coarse bar is emitted only if *all* expected constituent bars exist. Incomplete
    buckets are dropped (missing stays missing) rather than built from partial data.
    """
    if bars.empty:
        return empty_bars()
    g = bars.set_index("ts").resample(rule, origin="epoch", label="left", closed="left")
    agg = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(min_count=1),
            "n": g["close"].count(),
        }
    )
    agg = agg[agg["n"] == expected_parts].drop(columns="n")
    return normalise_bars(agg.reset_index())


class HttpProvider:
    """Mixin holding an HttpClient; subclasses implement the protocol."""

    name: str = "base"

    def __init__(self, http: HttpClient):
        self.http = http

    def drain_raw(self) -> list[RawPayload]:
        return self.http.drain()
