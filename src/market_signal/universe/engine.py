"""Pure causal ranking and tier transition machine. No DB, network, equity or PnL."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from market_signal.universe.policy import CORE, POLICY, Policy


@dataclass(frozen=True)
class Market:
    asset: str
    observed_at: int
    first_seen: int
    price: float
    volume_usd: float
    oi_usd: float
    funding: float = 0
    active: bool = True
    supported: bool = True
    venue_available: bool = True
    spread_bps: float | None = None
    depth_usd: float | None = None  # lesser side, within 10 bps of executable top
    impact_bps: float | None = None
    trades_per_minute: float | None = None
    rv_bps: float | None = None  # RMS 10m returns scaled to one hour
    range_bps: float | None = None  # observed one-hour range
    return_bps: float | None = None
    volume_acceleration: float | None = None  # rolling 24h volume / prior capture
    oi_change: float | None = None  # absolute log change, 1h
    healthy: bool = True
    warmup: dict = field(default_factory=dict)
    quote_at: int | None = None
    size_decimals: int | None = None
    max_leverage: int | None = None


@dataclass
class State:
    tier: int = 0
    entered_at: int = 0
    promoted_at: int | None = None
    good: int = 0
    bad: int = 0
    last_evaluation: int = 0
    demotion_pending: bool = False
    rich_since: int | None = None
    pins: list[str] = field(default_factory=list)


def cost(m: Market, p: Policy = POLICY) -> dict:
    """Round trip taker cost, no midpoint fill. Depth is intentionally nearby only."""
    if m.spread_bps is None or m.depth_usd is None or m.depth_usd <= 0:
        return {"version": p.cost_version, "slippage_bps": None, "round_trip_bps": None}
    depth_impact = 10 * p.reference_notional_usd / m.depth_usd
    slip = max(p.slippage_floor_bps, 2 * depth_impact, m.impact_bps or 0, 0.05 * (m.rv_bps or 0))
    return {
        "version": p.cost_version,
        "slippage_bps": slip,
        "round_trip_bps": m.spread_bps + 2 * (p.fee_bps + slip),
        "reference_notional_usd": p.reference_notional_usd,
    }


def gates(m: Market, now: int, p: Policy = POLICY, *, execution: bool = False) -> list[str]:
    reasons = []
    if not m.active:
        reasons.append("MARKET_INACTIVE")
    if not m.supported or (m.max_leverage is not None and m.max_leverage < 2):
        reasons.append("UNSUPPORTED_MARKET")
    if not m.venue_available:
        reasons.append("VENUE_UNAVAILABLE")
    numbers = (m.price, m.volume_usd, m.oi_usd, m.funding)
    optional = (
        m.spread_bps,
        m.depth_usd,
        m.impact_bps,
        m.trades_per_minute,
        m.rv_bps,
        m.range_bps,
        m.return_bps,
        m.volume_acceleration,
        m.oi_change,
    )
    if (
        not m.healthy
        or any(not math.isfinite(x) for x in numbers)
        or any(x is not None and not math.isfinite(x) for x in optional)
        or m.price <= 0
    ):
        reasons.append("DATA_DEGRADED")
    if not 0 <= now - m.observed_at <= p.stale_seconds:
        reasons.append("DATA_STALE")
    if m.volume_usd < p.volume_usd_min:
        reasons.append("LOW_VOLUME")
    if m.oi_usd < p.oi_usd_min:
        reasons.append("LOW_OI")
    if m.first_seen > now or now - m.first_seen < p.listing_days * 86400:
        reasons.append("NEW_LISTING_WARMUP")
    # Missing cheap quotes do not imply good execution; allow radar warming, never admission.
    for value, reject, fails in (
        (m.spread_bps, "WIDE_SPREAD", lambda x: x < 0 or x > p.spread_bps_max),
        (m.depth_usd, "THIN_DEPTH", lambda x: x < p.depth_usd_min),
        (m.trades_per_minute, "LOW_TRADE_FREQUENCY", lambda x: x < p.trades_per_minute_min),
    ):
        if value is None:
            if execution:
                reasons.append("DATA_DEGRADED")
        elif fails(value):
            reasons.append(reject)
    if execution:
        if m.quote_at is None or not 0 <= now - m.quote_at <= p.stale_seconds:
            reasons.append("DATA_STALE")
        if not m.warmup.get("lightweight", False):
            reasons.append("LIGHTWEIGHT_WARMUP")
        c = cost(m, p)["round_trip_bps"]
        if c is None or m.rv_bps is None or m.rv_bps < p.movement_cost_multiple * c:
            reasons.append("UNECONOMIC_EXECUTION")
    return sorted(set(reasons))


def _percentiles(markets: list[Market], getter) -> dict[str, float]:
    """Midrank percentiles of finite values; ties share percentile, missing = zero."""
    values = [(getter(m), m.asset) for m in markets]
    valid = sorted((x, a) for x, a in values if x is not None and math.isfinite(x))
    out = {m.asset: 0.0 for m in markets}
    i = 0
    while i < len(valid):
        j = i + 1
        while j < len(valid) and valid[j][0] == valid[i][0]:
            j += 1
        pct = (i + j) / (2 * len(valid))
        for _, asset in valid[i:j]:
            out[asset] = pct
        i = j
    return out


def rank(markets: list[Market], now: int, p: Policy = POLICY) -> list[dict]:
    if len({m.asset for m in markets}) != len(markets):
        raise ValueError("duplicate canonical market")
    eligible = [m for m in markets if not (set(gates(m, now, p)) - {"NEW_LISTING_WARMUP"})]
    inputs = {
        "rv": lambda m: m.rv_bps,
        "range": lambda m: m.range_bps,
        "return": lambda m: None if m.return_bps is None else abs(m.return_bps),
        "volume": lambda m: math.log1p(max(m.volume_usd, 0)),
        "volume_acceleration": lambda m: m.volume_acceleration,
        "frequency": lambda m: m.trades_per_minute,
        "oi": lambda m: math.log1p(max(m.oi_usd, 0)),
        "oi_change": lambda m: m.oi_change,
        "funding": lambda m: abs(m.funding),
    }
    normalized = {k: _percentiles(eligible, getter) for k, getter in inputs.items()}
    rows = []
    for m in sorted(markets, key=lambda m: m.asset):
        rejected = gates(m, now, p)
        n = {k: v.get(m.asset, 0) for k, v in normalized.items()}
        movement = sum(
            w * n[k] for w, k in zip(p.movement_weights, ("rv", "range", "return"), strict=True)
        )
        participation = sum(
            w * n[k]
            for w, k in zip(
                p.participation_weights, ("volume", "volume_acceleration", "frequency"), strict=True
            )
        )
        positioning = sum(
            w * n[k]
            for w, k in zip(p.positioning_weights, ("oi", "oi_change", "funding"), strict=True)
        )
        execution = (
            0
            if m.spread_bps is None or m.depth_usd is None
            else max(0, 1 - max(m.spread_bps, 0) / (2 * p.spread_bps_max))
            * min(1, max(m.depth_usd, 0) / (4 * p.depth_usd_min))
        )
        data = (
            sum(
                x is not None
                for x in (
                    m.rv_bps,
                    m.range_bps,
                    m.return_bps,
                    m.volume_acceleration,
                    m.oi_change,
                    m.trades_per_minute,
                )
            )
            / 6
        )
        # Geometric product prevents a single high dimension dominating weak execution.
        blend = (
            movement ** p.blend_weights[0]
            * participation ** p.blend_weights[1]
            * positioning ** p.blend_weights[2]
        )
        score = 0 if set(rejected) - {"NEW_LISTING_WARMUP"} else blend * execution * data
        rows.append(
            {
                "asset": m.asset,
                "cohort": "CORE" if m.asset in CORE else "SATELLITE",
                "rank": None,
                "satellite_rank": None,
                "score": round(score, 12),
                "components": {
                    "movement": movement,
                    "participation": participation,
                    "positioning": positioning,
                    "execution": execution,
                    "data": data,
                    "normalized": n,
                },
                "reasons": rejected,
                "execution_reasons": gates(m, now, p, execution=True),
                "market": asdict(m),
                "cost": cost(m, p),
            }
        )
    rows.sort(key=lambda r: (-r["score"], -r["components"]["participation"], r["asset"]))
    satellite_rank = 0
    position = 0
    for r in rows:
        if not r["reasons"]:
            position += 1
            r["rank"] = position
            if r["cohort"] == "SATELLITE":
                satellite_rank += 1
                r["satellite_rank"] = satellite_rank
    return rows


def evaluate(
    markets: list[Market],
    states: dict[str, State],
    now: int,
    pins: dict[str, list[str]] | None = None,
    p: Policy = POLICY,
    *,
    warming: bool = False,
) -> dict:
    """Tier 2 selection != hypothesis availability. Risk pins override all removals/caps."""
    pins = pins or {}
    rows = rank(markets, now, p)
    by_asset = {r["asset"]: r for r in rows}
    prior = {a: State(**asdict(s)) for a, s in states.items()}
    new = {a: State(**asdict(s)) for a, s in states.items()}
    candidates = [r["asset"] for r in rows if not (set(r["reasons"]) - {"NEW_LISTING_WARMUP"})][
        : p.candidate_cap
    ]
    changes = []
    # Removed markets remain explicit in the ledger; pins may refer to absent venue assets.
    for a in sorted(set(new) | set(by_asset) | set(CORE) | set(pins)):
        st = new.setdefault(a, State())
        if now <= st.last_evaluation and st.last_evaluation:
            raise ValueError("universe clock must advance")
        r = by_asset.get(a)
        safety = r is None or bool(r["execution_reasons"])
        old = st.tier
        sr = r["satellite_rank"] if r else None
        consecutive = not st.last_evaluation or now - st.last_evaluation <= p.light_max_gap_seconds
        st.good = (
            (st.good if consecutive else 0) + 1
            if r
            and not safety
            and sr is not None
            and sr <= p.promotion_rank
            and r["score"] >= p.score_min
            else 0
        )
        poor = sr is None or sr > p.demotion_rank or (r and r["score"] < p.score_min)
        st.bad = (st.bad if consecutive else 0) + 1 if poor else 0
        st.pins = sorted(set(pins.get(a, [])))
        demote = safety or (
            poor
            and st.bad >= p.demotion_evaluations
            and now - (st.promoted_at or st.entered_at) >= p.minimum_dwell_seconds
        )
        if st.pins:
            st.tier = 3
            st.demotion_pending = a not in CORE and demote
        elif a in CORE:
            st.tier = 2  # permanent research; execution safety remains separately gated
            st.demotion_pending = False
        elif old in (2, 3) and not demote:
            st.tier = 2
            st.demotion_pending = False
        else:
            st.tier = 1 if a in candidates else 0
            st.promoted_at = None
            st.demotion_pending = False
        st.last_evaluation = now
        if st.tier != old:
            st.entered_at = now
    active = sum(s.tier in (2, 3) for a, s in new.items() if a not in CORE)
    for r in rows:
        a, st = r["asset"], new[r["asset"]]
        if st.tier == 1 and st.good >= p.promotion_evaluations:
            if active < p.satellite_cap:
                st.tier, st.entered_at, st.promoted_at = 2, now, now
                active += 1
            else:
                r["execution_reasons"].append("TIER_CAPACITY")
        elif st.tier == 1 and r["satellite_rank"] and r["satellite_rank"] > p.promotion_rank:
            r["execution_reasons"].append("BELOW_RANK_CUTOFF")
    desired = set(CORE) | set(pins)
    if warming:
        desired |= {a for a, s in new.items() if s.tier == 2}
        remaining = max(0, p.rich_satellite_cap - len(desired - set(CORE)))
        desired |= set([a for a in candidates if a not in desired][:remaining])
    for a, st in sorted(new.items()):
        prev = prior.get(a, State())
        if a in desired and prev.rich_since is None:
            st.rich_since = now
        elif a not in desired:
            st.rich_since = None
        if prev.tier != st.tier or prev.rich_since != st.rich_since or prev.pins != st.pins:
            r = by_asset.get(a, {})
            changes.append(
                {
                    "asset": a,
                    "previous_tier": prev.tier,
                    "new_tier": st.tier,
                    "effective_at": now,
                    "rank": r.get("rank"),
                    "score": r.get("score"),
                    "components": r.get("components"),
                    "reasons": r.get("execution_reasons", ["MARKET_INACTIVE"]),
                    "pins": st.pins,
                    "rich_since": st.rich_since,
                }
            )
        if a in by_asset:
            by_asset[a].update(
                tier=st.tier,
                state=asdict(st),
                warmed=a in desired,
                entry_eligible=st.tier == 2 and not by_asset[a]["execution_reasons"],
            )
    return {
        "policy_id": p.identity,
        "policy_version": p.version,
        "at": now,
        "stage": "RANKING + WARMING" if warming else "RANKING ONLY",
        "rows": rows,
        "states": {a: asdict(s) for a, s in sorted(new.items())},
        "candidates": candidates,
        "desired_subscriptions": sorted(desired),
        "changes": changes,
        "pins": pins,
        "tier_counts": {str(t): sum(s.tier == t for s in new.values()) for t in range(4)},
    }
