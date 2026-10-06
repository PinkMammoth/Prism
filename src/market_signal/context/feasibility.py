"""Data feasibility audits (liquidations, microstructure, traditional markets), as data.

Verified 2026-10-06 against live endpoints and official documentation (``verified`` marks
what was checked live from this environment vs. read from docs). ``market context
feasibility --json`` prints this so an agent can read it without parsing prose.
Recommendations here are not implemented unless ``implemented`` says so.
"""

from __future__ import annotations

FEASIBILITY_VERSION = "context_feasibility_v1_2026-10-06"

LIQUIDATIONS = [
    {"venue": "binance_usdm", "feed": "WS <symbol>@forceOrder / !forceOrder@arr",
     "historical": "none: REST /fapi/v1/allForceOrders returns 404 (checked live)",
     "live": "yes, keyless WebSocket", "granularity": "per order: side, qty, price, avg price, trade time",
     "limits": "SNAPSHOT: only the latest liquidation per symbol per 1000 ms is pushed (docs) -> "
               "an undercounting sample in liquidation cascades",
     "timestamps": "E event time, o.T trade time (exchange clock)",
     "cost": "free", "reliable_for_totals": False,
     "recommendation": "optional always-on sampler later; label as lower bound; never sum as totals",
     "implemented": False, "verified": "docs + REST 404 live"},
    {"venue": "hyperliquid", "feed": "userEvents / userFills liquidation fields",
     "historical": "per user only (userFills by address)", "live": "per subscribed user only",
     "granularity": "liquidated user, mark px, method market|backstop",
     "limits": "no public all-market liquidation stream; public trades carry buyer/seller "
               "addresses but no liquidation flag (checked live)",
     "timestamps": "exchange ms", "cost": "free", "reliable_for_totals": False,
     "recommendation": "do not infer liquidations from trades or OI; revisit if Hyperliquid adds a "
                       "public feed", "implemented": False, "verified": "docs + live recentTrades"},
    {"venue": "aggregators (Coinglass etc.)", "feed": "paid API",
     "historical": "yes (modelled heatmaps are estimates, not prints)", "live": "yes",
     "limits": "paid; 'liquidation levels' are model output, not observed data",
     "cost": "paid", "reliable_for_totals": None,
     "recommendation": "out of scope (GBP 0 policy; modelled levels are not facts)",
     "implemented": False, "verified": "not checked"},
]  # fmt: skip

MICROSTRUCTURE = [
    {"item": "individual trades + aggressor side", "venue": "hyperliquid",
     "source": "REST recentTrades (last ~10 per call) / WS trades",
     "fields": "side (B/A = aggressor), px, sz, time ms, tid, users[buyer,seller]",
     "prospective": "yes via WS (needs an always-on process)", "history": "none beyond recent",
     "verified": "live"},
    {"item": "best bid/ask, spread", "venue": "hyperliquid", "source": "WS bbo; REST l2Book",
     "fields": "px, sz, n orders per level", "prospective": "yes", "history": "none",
     "verified": "docs + live l2Book"},
    {"item": "top-N depth / book imbalance", "venue": "hyperliquid",
     "source": "REST l2Book (20 levels/side), WS l2Book (fast=5, slow=20 levels)",
     "fields": "levels[bids, asks]", "prospective": "yes (periodic REST snapshots are cheap)",
     "history": "none", "verified": "live"},
    {"item": "impact prices (cost to trade size)", "venue": "hyperliquid",
     "source": "metaAndAssetCtxs impactPxs", "fields": "impact bid/ask px",
     "prospective": "IMPLEMENTED: captured hourly in context_hl_oi_hourly", "history": "none",
     "verified": "live"},
    {"item": "large prints / trade-flow imbalance / CVD", "venue": "hyperliquid",
     "source": "derived from WS trades", "fields": "aggregate signed volume by aggressor",
     "prospective": "yes with a WS collector", "history": "none", "verified": "derived"},
    {"item": "taker buy/sell volume ratio (hourly)", "venue": "binance_usdm",
     "source": "GET /futures/data/takerlongshortRatio (~30 days)",
     "fields": "buySellRatio, buyVol, sellVol", "prospective": "IMPLEMENTED: context_ls_ratios",
     "history": "~30 days rolling", "verified": "live"},
]  # fmt: skip

MICROSTRUCTURE_RECOMMENDATION = (
    "Next: a small always-on Hyperliquid WS collector for trades (aggressor-signed 1-minute "
    "volume, large prints) and bbo/l2Book top-5 snapshots every 10-60 s for the six perps, "
    "stored as minute aggregates (not raw ticks). It needs a long-lived process, which the "
    "current cron-style runtime is not; not built in Phase 23."
)

TRADITIONAL_MARKETS = [
    {"series": "US Treasury yields 2Y/10Y", "source": "FRED DGS2/DGS10 (already ingested)",
     "latency": "daily, available ~1-2 days later", "history": "decades", "licence": "public",
     "status": "ingested"},
    {"series": "VIX", "source": "FRED VIXCLS (already ingested)", "latency": "daily close, next day",
     "history": "1990-", "licence": "public via FRED", "status": "ingested"},
    {"series": "USD broad index (DXY proxy)", "source": "FRED DTWEXBGS (already ingested)",
     "latency": "daily, ~1 week behind", "history": "2006- (reconstructed pre-2019)",
     "licence": "public", "status": "ingested (proxy; DXY itself is ICE-licensed)"},
    {"series": "USDJPY", "source": "FRED DEXJPUS (H.10)", "latency": "daily noon NY rate, weekly-ish",
     "history": "1971-", "licence": "public", "status": "added to config/macro.yaml in Phase 23"},
    {"series": "S&P 500", "source": "FRED SP500", "latency": "daily close, next day",
     "history": "10 years (licence-limited)", "licence": "S&P licence via FRED, display/research",
     "status": "added to config/macro.yaml in Phase 23"},
    {"series": "WTI oil", "source": "FRED DCOILWTICO (EIA)", "latency": "daily, several days",
     "history": "1986-", "licence": "public", "status": "added to config/macro.yaml in Phase 23"},
    {"series": "gold", "source": "none free+official (LBMA prices left FRED in 2022)",
     "latency": "-", "history": "-", "licence": "LBMA licensed", "status": "not ingested"},
    {"series": "S&P/Nasdaq futures intraday", "source": "no free official intraday feed; "
     "Tiingo IEX gives ETF (SPY/QQQ) intraday on the free tier", "latency": "near real time (IEX)",
     "history": "limited", "licence": "Tiingo free tier terms", "status": "not ingested (later, if "
     "macro-response research shows value)"},
]  # fmt: skip


def report() -> dict:
    return {"version": FEASIBILITY_VERSION, "liquidations": LIQUIDATIONS,
            "microstructure": MICROSTRUCTURE,
            "microstructure_recommendation": MICROSTRUCTURE_RECOMMENDATION,
            "traditional_markets": TRADITIONAL_MARKETS}  # fmt: skip
