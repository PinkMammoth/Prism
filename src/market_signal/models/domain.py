"""Core domain types shared across the package.

These are deliberately small. Heavier numeric work happens on pandas frames whose
column contracts are documented in the modules that produce them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    ETF = "etf"
    COMMODITY = "commodity"  # commodity exposure via an ETP proxy


class Timeframe(StrEnum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"  # always derived from 1d on read; never stored

    @property
    def seconds(self) -> int:
        return {"1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}[self.value]


class Calendar(StrEnum):
    CRYPTO_24_7 = "crypto"
    NYSE = "nyse"


class Regime(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    UNKNOWN = "UNKNOWN"  # insufficient data — never silently mapped to NEUTRAL


class PitMethod(StrEnum):
    """How the availability time of a non-price datum was established.

    See docs/DATA_SOURCES.md. Only a subset is admissible in historical research.
    """

    VINTAGE = "vintage"
    FILING_DATE = "filing_date"
    RELEASE_RULE = "release_rule"
    MARKET_CLOSE = "market_close"
    SNAPSHOT = "snapshot"
    RECONSTRUCTED = "reconstructed"


BACKTEST_ADMISSIBLE_PIT = frozenset(
    {
        PitMethod.VINTAGE,
        PitMethod.FILING_DATE,
        PitMethod.RELEASE_RULE,
        PitMethod.MARKET_CLOSE,
        PitMethod.SNAPSHOT,
    }
)


class PositionKind(StrEnum):
    TRADE = "TRADE"  # technical invalidation, time stop
    INVESTMENT = "INVESTMENT"  # thesis-based exits, no tight technical stop


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class SeriesSpec:
    """One configured price series: exactly one provider per (asset, timeframe)."""

    timeframe: Timeframe
    provider: str
    symbol: str  # provider-specific identifier


@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    asset_class: AssetClass
    currency: str
    calendar: Calendar
    active: bool = True
    series: dict[Timeframe, SeriesSpec] = field(default_factory=dict)
    provider_ids: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    notes: str = ""

    def series_for(self, timeframe: Timeframe) -> SeriesSpec | None:
        return self.series.get(timeframe)

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == AssetClass.CRYPTO


def utcnow() -> datetime:
    return datetime.now(UTC)
