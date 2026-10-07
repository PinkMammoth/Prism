"""Hyperliquid perp universe diff: listings and delistings on the execution venue.

Source: ``POST /info {"type": "meta"}`` (official, structured, keyless). The first successful
run only records a baseline (the existing universe is not news). Later runs emit a
``listing`` for each coin that appeared and a ``delisting`` for each coin newly flagged
``isDelisted`` (or removed). Hyperliquid publishes no announcement time here, so
``trading_open_at`` is when Prism first saw the coin listed and ``announced_at`` stays unknown:
announcement and trading commencement are never conflated.
"""

from __future__ import annotations

import re
from datetime import datetime

from market_signal.context.model import ListingAttrs, Observation
from market_signal.context.providers.base import FetchResult, sha256
from market_signal.context.taxonomy import SourceType
from market_signal.data.http import HttpClient, SchemaError

SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._/-]{0,39}")


def universe(meta: dict) -> dict[str, bool]:
    """coin -> delisted flag."""
    try:
        return {str(u["name"]): bool(u.get("isDelisted", False)) for u in meta["universe"]}
    except (KeyError, TypeError) as exc:
        raise SchemaError("hyperliquid meta: unexpected schema") from exc


class HyperliquidUniverseProvider:
    name = "hyperliquid_universe"
    stale_hours = 3.0

    def __init__(self, http: HttpClient):
        self.http = http

    def fetch(self, now: datetime, last_state: dict) -> FetchResult:
        meta = self.http.post_json("/info", {"type": "meta"})
        raw = self.http.drain()
        cur = universe(meta)
        fr = FetchResult(received=len(cur), raw=raw, state={"universe": cur})
        prev: dict[str, bool] | None = last_state.get("universe")
        if prev is None:
            fr.notes.append(f"baseline recorded: {len(cur)} perps (no events)")
            return fr
        h = sha256(raw[-1].body) if raw else None
        for coin in sorted(cur):
            listed = coin not in prev
            delisted = cur[coin] and not prev.get(coin, False)
            if not (listed or delisted):
                continue
            action = "listing" if listed else "delisting"
            attrs = ListingAttrs(exchange="hyperliquid", action=action, market_type="perp",
                                 trading_open_at=now if listed else None,
                                 trading_close_at=now if delisted else None, pairs=(f"{coin}-USD",))  # fmt: skip
            fr.observations.append(Observation(
                source_id="hyperliquid_meta", source_type=SourceType.EXCHANGE_API,
                source_ref=f"hyperliquid:meta:{coin}:{action}", subcategory=action,
                title=f"Hyperliquid perp {action}: {coin}", confidence="OFFICIAL",
                assets=(coin.upper(),) if SYMBOL.fullmatch(coin.upper()) else (),
                scope="exchange", attributes=attrs,
                dedup_key=f"hl_universe:{action}:{coin}", raw_sha256=h))  # fmt: skip
        for coin in sorted(set(prev) - set(cur)):
            if prev[coin]:
                continue  # already delisted earlier
            fr.observations.append(Observation(
                source_id="hyperliquid_meta", source_type=SourceType.EXCHANGE_API,
                source_ref=f"hyperliquid:meta:{coin}:delisting", subcategory="delisting",
                title=f"Hyperliquid perp removed from universe: {coin}", confidence="OFFICIAL",
                scope="exchange", dedup_key=f"hl_universe:delisting:{coin}",
                attributes=ListingAttrs(exchange="hyperliquid", action="delisting",
                                        market_type="perp", trading_close_at=now)))  # fmt: skip
        return fr
