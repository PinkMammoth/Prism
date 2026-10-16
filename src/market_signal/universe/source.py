"""Public free Hyperliquid discovery; bounded books/candles, never full-market micro."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import pairwise

from market_signal.universe import cadence, ledger
from market_signal.universe.engine import Market, evaluate
from market_signal.universe.policy import CORE, POLICY


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite source value")
    return number


def decode(meta_ctx, now, first_seen):
    if not isinstance(meta_ctx, list) or len(meta_ctx) != 2:
        raise ValueError("invalid metaAndAssetCtxs response")
    meta, ctx = meta_ctx
    if len(meta["universe"]) != len(ctx):
        raise ValueError("meta/context alignment mismatch")
    markets = []
    for u, c in zip(meta["universe"], ctx, strict=True):
        a = u["name"]
        try:
            price = _finite(c.get("midPx") or c["markPx"])
            if price <= 0:
                raise ValueError("nonpositive source price")
            imp = c.get("impactPxs") or []
            impact = max(abs(_finite(x) / price - 1) * 10000 for x in imp) if imp else None
            markets.append(
                Market(
                    a,
                    now,
                    first_seen.get(a, now),
                    price,
                    _finite(c["dayNtlVlm"]),
                    _finite(_finite(c["openInterest"]) * price),
                    _finite(c["funding"]),
                    active=not u.get("isDelisted", False),
                    supported=bool(u.get("maxLeverage", 0))
                    and ":" not in a
                    and 0 <= int(u["szDecimals"]) <= POLICY.size_decimals_max,
                    impact_bps=impact,
                    size_decimals=int(u["szDecimals"]),
                    max_leverage=int(u["maxLeverage"]),
                )
            )
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            markets.append(Market(a, now, first_seen.get(a, now), 0, 0, 0, healthy=False))
    return markets


def book_metrics(book, now):
    bids, asks = book["levels"]
    if not bids or not asks:
        raise ValueError("empty executable book")
    for side, reverse in ((bids, True), (asks, False)):
        prices = [_finite(x["px"]) for x in side]
        if any(px <= 0 for px in prices) or any(_finite(x["sz"]) < 0 for x in side):
            raise ValueError("invalid executable levels")
        if prices != sorted(prices, reverse=reverse):
            raise ValueError("unordered executable levels")
    bid, ask = _finite(bids[0]["px"]), _finite(asks[0]["px"])
    if not 0 < bid <= ask:
        raise ValueError("crossed/invalid executable quote")
    mid = (bid + ask) / 2
    depth = min(
        sum(float(x["px"]) * float(x["sz"]) for x in bids if float(x["px"]) >= bid * 0.999),
        sum(float(x["px"]) * float(x["sz"]) for x in asks if float(x["px"]) <= ask * 1.001),
    )
    depth = _finite(depth)
    at = int(book["time"]) // 1000
    if not 0 <= now - at <= POLICY.stale_seconds:
        raise ValueError("stale executable quote")
    return {"spread_bps": (ask - bid) / mid * 10000, "depth_usd": depth, "quote_at": at}


def enrich_history(markets, history, now):
    """Only previously persisted observations; gaps break light warmup."""
    out = []
    for m in markets:
        prior = [
            x
            for x in history.get(m.asset, [])
            if now - 4500 <= x["observed_at"] < now and x["price"] > 0
        ]
        prior = prior[-(POLICY.light_observations - 1) :]
        values = [*prior, asdict(m)]
        continuous = (
            len(values) >= POLICY.light_observations
            and all(x.get("healthy", True) for x in values)
            and all(
                0 < b["observed_at"] - a["observed_at"] <= POLICY.light_max_gap_seconds
                for a, b in pairwise(values)
            )
        )
        returns = (
            [math.log(b["price"] / a["price"]) for a, b in pairwise(values)] if m.price > 0 else []
        )
        rv = (
            math.sqrt(
                sum(x * x for x in returns) * 3600 / (m.observed_at - prior[0]["observed_at"])
            )
            * 10000
            if continuous
            else None
        )
        prices = [x["price"] for x in values if x["price"] > 0]
        out.append(
            replace(
                m,
                rv_bps=rv,
                range_bps=(max(prices) / min(prices) - 1) * 10000 if continuous else None,
                return_bps=(m.price / prior[0]["price"] - 1) * 10000 if continuous else None,
                oi_change=abs(
                    math.log((m.oi_usd / m.price) / (prior[0]["oi_usd"] / prior[0]["price"]))
                )
                if continuous and m.oi_usd > 0 and prior[0]["oi_usd"] > 0
                else None,
                volume_acceleration=m.volume_usd / prior[-1]["volume_usd"]
                if prior and prior[-1]["volume_usd"] > 0
                else None,
                warmup={
                    "lightweight": continuous,
                    "technical": False,
                    "positioning": False,
                    "microstructure": False,
                    "large_print": False,
                },
            )
        )
    return out


def prepare(store, now):
    import gzip
    import json

    previous = ledger.latest(store)
    history = {}
    for (blob,) in store.con.execute(
        "SELECT payload FROM universe_evaluations WHERE policy_id=? AND evaluated_at>=? AND evaluated_at<? ORDER BY evaluated_at",
        [POLICY.identity, now - 4500, now],
    ).fetchall():
        for r in json.loads(gzip.decompress(blob))["rows"]:
            history.setdefault(r["asset"], []).append(r["market"])
    return {
        "previous": previous,
        "history": history,
        "pins": ledger.risk_pins(store, now),
        "states": ledger.state(store),
    }


def fetch(http, prepared, now, *, warming=False, clock=None):
    clock = clock or (lambda: int(time.time()))
    """Network runs outside writer/runtime locks. One meta batch + bounded cheap requests."""
    previous = prepared["previous"]
    first = dict(previous.get("listing_first_seen", {})) if previous else {}
    dexes = http.post_json("/info", {"type": "perpDexs"})
    names = ["", *sorted(x["name"] for x in dexes if x is not None)]
    markets = []
    discovery_errors = {}
    for dex in names:
        try:
            payload = http.post_json("/info", {"type": "metaAndAssetCtxs", "dex": dex})
            markets.extend(decode(payload, clock(), first))
        except Exception as exc:
            if not dex:
                raise  # never replace the native venue with an empty successful discovery
            discovery_errors[dex] = f"{type(exc).__name__}: {str(exc)[:160]}"
            for r in (previous or {}).get("rows", []):
                if r["asset"].startswith(dex + ":"):
                    markets.append(
                        replace(Market(**r["market"]), venue_available=False, healthy=False)
                    )
    for m in markets:
        first.setdefault(m.asset, m.first_seen)
    markets = enrich_history(markets, prepared["history"], now)
    cheap = sorted(
        (
            m
            for m in markets
            if m.active
            and m.supported
            and m.healthy
            and m.volume_usd >= POLICY.volume_usd_min
            and m.oi_usd >= POLICY.oi_usd_min
        ),
        key=lambda m: (-(m.rv_bps or 1) * math.log1p(m.volume_usd), m.asset),
    )
    mandatory = (
        set(CORE)
        | set(prepared["pins"])
        | {a for a, s in prepared["states"].items() if s.tier in (2, 3)}
    )
    light = set(mandatory)
    light |= set(
        [m.asset for m in cheap if m.asset not in mandatory][
            : max(0, POLICY.candidate_cap - len(mandatory))
        ]
    )
    errors = dict(discovery_errors)
    enriched = []
    for m in markets:
        if m.asset not in light or not m.active:
            enriched.append(m)
            continue
        try:
            metrics = book_metrics(
                http.post_json("/info", {"type": "l2Book", "coin": m.asset}), clock()
            )
            candles = http.post_json(
                "/info",
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": m.asset,
                        "interval": "5m",
                        "startTime": (now - 1200) * 1000,
                        "endTime": now * 1000,
                    },
                },
            )
            candles = sorted(
                (x for x in candles if int(x["T"]) < now * 1000), key=lambda x: int(x["t"])
            )
            recent = candles[-3:]
            if (
                len(recent) != 3
                or any(int(b["t"]) - int(a["t"]) != 300000 for a, b in pairwise(recent))
                or now * 1000 - int(recent[-1]["T"]) > 300000
            ):
                raise ValueError("missing recent trade frequency candles")
            enriched.append(
                replace(m, **metrics, trades_per_minute=sum(int(x["n"]) for x in recent) / 15)
            )
        except Exception as exc:
            errors[m.asset] = f"{type(exc).__name__}: {str(exc)[:160]}"
            enriched.append(replace(m, healthy=False))
    rich_data = {}
    if warming:
        from market_signal.universe import rich

        prospective = evaluate(
            enriched, prepared["states"], clock(), prepared["pins"], warming=True
        )
        initialized = [
            a
            for a, st in prepared["states"].items()
            if st.rich_since is not None and a in (previous or {}).get("rich_initialized", [])
        ]
        hour_due = previous is None or now // 3600 != previous["at"] // 3600
        rich_data = rich.fetch(
            http,
            prospective["desired_subscriptions"],
            now,
            initialized=initialized,
            hour_due=hour_due,
        )
    return {
        "rich_data": rich_data,
        "markets": enriched,
        "listing_first_seen": first,
        "errors": errors,
        "light": light,
        "prepared_at": now,
    }


def commit(store, fetched, now, *, warming=False, telemetry=None):
    from market_signal.universe.portability import availability

    previous = ledger.latest(store)
    refresh_started = fetched.get("prepared_at", now)
    if not cadence.due(previous, refresh_started):
        return {"status": "NOT_DUE", "last_evaluation": previous["at"]}
    if warming:
        from market_signal.universe.rich import commit as commit_rich

        commit_rich(store, fetched.get("rich_data", {}), now)
    # Re-read current risk at commit; network latency cannot evict a newly opened position.
    decision = evaluate(
        fetched["markets"], ledger.state(store), now, ledger.risk_pins(store, now), warming=warming
    )
    if telemetry is not None:
        telemetry["ranking_computed_at"] = time.time()
    decision["schedule"] = {"version": cadence.VERSION, "slot": cadence.slot(refresh_started)}
    decision["listing_first_seen"] = fetched["listing_first_seen"]
    decision["source_errors"] = {
        **fetched["errors"],
        **{a: r["errors"] for a, r in fetched.get("rich_data", {}).items() if r["errors"]},
    }
    decision["discovered_count"] = len(fetched["markets"])
    decision["light_collection"] = sorted(fetched["light"])
    initialized = set((previous or {}).get("rich_initialized", []))
    initialized.update(a for a, r in fetched.get("rich_data", {}).items() if r["bars"])
    decision["rich_initialized"] = sorted(initialized & set(decision["desired_subscriptions"]))
    for r in decision["rows"]:
        if (
            warming
            and r["asset"] in decision["light_collection"]
            and r["asset"] not in CORE
            and r["market"]["healthy"]
            and r["market"]["active"]
        ):
            m = r["market"]
            at = datetime.fromtimestamp(now, UTC)
            store.con.execute(
                "INSERT INTO context_hl_oi_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    r["asset"],
                    at.replace(minute=0, second=0, microsecond=0),
                    at,
                    m["oi_usd"] / m["price"] if m["price"] > 0 else None,
                    m["oi_usd"],
                    m["price"],
                    None,
                    m["price"],
                    m["funding"],
                    None,
                    None,
                    None,
                    m["volume_usd"],
                    "dynamic_universe_prospective_v1",
                    "dynamic_universe_warming_v1",
                ],
            )
        r["hypotheses"] = (
            availability(store, r["asset"], now, r["state"]["rich_since"])
            if r["asset"] in decision["light_collection"]
            else {"warmup": {}, "hypotheses": []}
        )
        r["market"]["warmup"].update(r["hypotheses"]["warmup"])
    if telemetry is not None:
        telemetry["ranking_db_commit_started_at"] = time.time()
    decision["evaluation_id"] = ledger.append(
        store, decision, [asdict(m) for m in fetched["markets"]]
    )
    if telemetry is not None:
        telemetry["ranking_db_commit_completed_at"] = time.time()
    # Batch prospective evidence writes: per-row autocommit would delay quote/exit writers.
    if telemetry is not None:
        telemetry["outcomes_db_commit_started_at"] = time.time()
    with store.transaction():
        decision["outcomes_recorded"] = ledger.collect_outcomes(store, now)
    if telemetry is not None:
        telemetry["outcomes_db_commit_completed_at"] = time.time()
    return decision
