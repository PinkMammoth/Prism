"""Builds provider instances from configuration. The only place that knows concrete
provider classes; everything else asks the registry by name."""

from __future__ import annotations

from typing import Any

from market_signal.config import Settings
from market_signal.data.http import HttpClient
from market_signal.data.providers.base import MarketDataProvider
from market_signal.data.providers.crypto import (
    BitstampProvider,
    CoinbaseProvider,
    HyperliquidProvider,
)
from market_signal.data.providers.equities import StooqProvider, TiingoProvider
from market_signal.models.domain import Timeframe


def source_label(provider: str, timeframe: Timeframe) -> str:
    """The ``source`` value stored with bars for a configured (provider, timeframe)."""
    if provider == "coinbase" and timeframe == Timeframe.H4:
        return "coinbase:agg1h"
    return provider


class ProviderRegistry:
    def __init__(self, settings: Settings, transport: Any = None):
        self.settings = settings
        self.transport = transport  # httpx transport override (tests)
        self._cache: dict[str, Any] = {}

    def http(self, name: str, **headers: str) -> HttpClient:
        http_cfg = self.settings.providers.get("http") or {}
        cfg = self.settings.provider_cfg(name)
        return HttpClient(
            provider=name,
            base_url=cfg.get("base_url", ""),
            requests_per_second=float(cfg.get("requests_per_second", 1.0)),
            timeout_seconds=float(http_cfg.get("timeout_seconds", 30)),
            max_retries=int(http_cfg.get("max_retries", 4)),
            backoff_seconds=float(http_cfg.get("backoff_seconds", 2.0)),
            user_agent=headers.pop("user_agent", http_cfg.get("user_agent", "Prism/0.1")),
            headers=headers,
            transport=self.transport,
        )

    def market(self, name: str) -> MarketDataProvider:
        if name in self._cache:
            return self._cache[name]
        cfg = self.settings.provider_cfg(name)
        if name == "coinbase":
            p: Any = CoinbaseProvider(self.http(name), int(cfg.get("max_candles_per_request", 300)))
        elif name == "bitstamp":
            p = BitstampProvider(self.http(name), int(cfg.get("max_candles_per_request", 1000)))
        elif name == "hyperliquid":
            fallback = {"HYPE": cfg.get("hype_spot_pair_fallback", "@107")}
            p = HyperliquidProvider(self.http(name), fallback, int(cfg.get("max_candles", 5000)))
        elif name == "tiingo":
            p = TiingoProvider(
                self.http(name, **{"Content-Type": "application/json"}),
                self.settings.secret(cfg.get("env_key", "TIINGO_API_KEY")),
            )
        elif name == "stooq":
            p = StooqProvider(
                self.http(name), self.settings.secret(cfg.get("env_key", "STOOQ_API_KEY"))
            )
        else:
            raise KeyError(f"Unknown market data provider {name!r}")
        self._cache[name] = p
        return p
