"""Wiring: build context providers from config and run refresh groups (CLI + runtime).

Groups (each idempotent, safe to retry, and preserving ``first_seen_at``):
  macro        FRED release calendar, central-bank schedules, ALFRED first prints
  news         official RSS/status feeds, Hyperliquid perp universe diff
  positioning  fixed-hour Hyperliquid OI capture + Binance long/short ratio top-up
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from market_signal.config import Settings
from market_signal.context.providers.base import run_provider
from market_signal.data.store import Store

GROUPS = ("macro", "news", "positioning")


def sources_config(settings: Settings) -> dict:
    return settings.yaml("context/sources.yaml")


def stale_hours(settings: Settings) -> dict[str, float]:
    cfg = sources_config(settings)
    return {k: float(v.get("stale_hours", 24)) for k, v in (cfg.get("providers") or {}).items()
            if v.get("enabled", True)}  # fmt: skip


def _enabled(cfg: dict, name: str) -> bool:
    return bool(((cfg.get("providers") or {}).get(name) or {}).get("enabled", False))


def build(settings: Settings, store: Store, group: str, transport: Any = None) -> list:
    from market_signal.context.entities import load_entities
    from market_signal.context.providers.calendar import (
        CentralBankCalendarProvider,
        FredActualsProvider,
        FredCalendarProvider,
        load_calendar,
    )
    from market_signal.context.providers.hyperliquid import HyperliquidUniverseProvider
    from market_signal.context.providers.rss import RssProvider
    from market_signal.data.registry import ProviderRegistry

    cfg = sources_config(settings)
    reg = ProviderRegistry(settings, transport=transport)
    out: list = []
    if group == "macro":
        cal = load_calendar(settings)
        key = settings.secret("FRED_API_KEY")
        if _enabled(cfg, "fred_calendar"):
            out.append(FredCalendarProvider(reg.http("fred"), key, cal))
        if _enabled(cfg, "central_bank_calendar"):
            out.append(CentralBankCalendarProvider(cal))
        if _enabled(cfg, "fred_actuals"):
            out.append(FredActualsProvider(reg.http("fred"), key, cal, store))
    elif group == "news":
        if _enabled(cfg, "hyperliquid_universe"):
            out.append(HyperliquidUniverseProvider(reg.http("hyperliquid")))
        if _enabled(cfg, "rss"):
            out.append(RssProvider(cfg.get("feeds") or {}, load_entities(),
                                   cfg.get("user_agent", "Prism/0.1"), transport=transport))  # fmt: skip
    else:
        raise ValueError(f"unknown provider group {group!r}")
    for p in out:  # health thresholds come from config, not code
        p.stale_hours = float(
            ((cfg.get("providers") or {}).get(p.name) or {}).get("stale_hours", p.stale_hours)
        )
    return out


def refresh(settings: Settings, store: Store, groups: tuple[str, ...] = GROUPS,
            now: datetime | None = None, transport: Any = None) -> list[dict]:  # fmt: skip
    results = []
    for g in groups:
        if g == "positioning":
            results += refresh_positioning(settings, store, now=now, transport=transport)
            continue
        for p in build(settings, store, g, transport=transport):
            results.append(run_provider(store, p, now=now))
    return results


def refresh_positioning(settings: Settings, store: Store, now: datetime | None = None,
                        transport: Any = None) -> list[dict]:  # fmt: skip
    from market_signal.context.positioning import capture_hl_hourly, update_binance_ratios
    from market_signal.data.registry import ProviderRegistry
    from market_signal.perps.open_interest import oi_config

    reg = ProviderRegistry(settings, transport=transport)
    oc = oi_config(settings)
    out = [
        {
            "provider": "hl_oi_hourly",
            **capture_hl_hourly(store, oc.coins, reg.market("hyperliquid"), now=now),
        }
    ]
    rows = update_binance_ratios(store, oc.symbols, reg.http("binance_futures"), now=now)
    failed = [r for r in rows if r["status"] == "failed"]
    out.append({"provider": "binance_ls_ratios", "status": "partial" if failed else "ok",
                "series": len(rows), "new_rows": sum(r.get("new", 0) for r in rows),
                "failed": failed[:5]})  # fmt: skip
    return out
