"""Dynamic paper admission hooks. Account limits and existing core fill semantics stay frozen."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from market_signal.paper.nimble.model import ExecutableQuote
from market_signal.paper.v2.data import ts
from market_signal.universe import ledger
from market_signal.universe.policy import CORE, POLICY


@dataclass(frozen=True)
class SatelliteQuote(ExecutableQuote):
    depth_usd: float = 0
    slippage_bps: float = 100


def quote(record, available_at, context=None):
    record = dict(record)
    depth = record.pop("depth_usd", 0)
    context = context or {}
    # When classifications are unavailable, 100bps conservative fallback is for exits only.
    impact = context.get("impact_bps")
    slip = max(
        POLICY.slippage_floor_bps,
        20 * POLICY.reference_notional_usd / depth if depth > 0 else 100,
        impact or 0,
        context.get("volatility_slippage_bps", 0),
    )
    return SatelliteQuote(**record, available_at=available_at, depth_usd=depth, slippage_bps=slip)


def active_assets(store, now):
    ev = ledger.latest(store, at=int(ts(now).timestamp()))
    if not ev or int(ts(now).timestamp()) - ev["at"] > 2 * POLICY.cadence_seconds:
        return set(CORE)
    return set(CORE) | {r["asset"] for r in ev["rows"] if r["tier"] == 2 and r["entry_eligible"]}


def admission(store, asset, hypothesis, signal_at, now, q=None):
    ev = ledger.latest(store, at=int(ts(now).timestamp()))
    if not ev or int(ts(now).timestamp()) - ev["at"] > 2 * POLICY.cadence_seconds:
        return (None, None) if asset in CORE else ("DATA_STALE", None)
    r = next((r for r in ev["rows"] if r["asset"] == asset), None)
    if asset in CORE:
        if not r:
            return None, None
        return None, {
            "evaluation_id": content_identity(ev),
            "policy_id": ev["policy_id"],
            "rank": r["rank"],
            "tier": r["tier"],
            "score": r["score"],
            "components": r["components"],
            "market": r["market"],
            "cost": r["cost"],
            "warmup": r.get("hypotheses", {}).get("warmup", {}),
            "time_since_promotion_seconds": int(ts(now).timestamp())
            - (r["state"].get("promoted_at") or r["state"]["entered_at"]),
        }
    if not r or r["tier"] not in (2, 3) or not r["entry_eligible"]:
        return "UNIVERSE_INELIGIBLE", r
    # Tier 3 pins are monitoring obligations, never permission for new entries.
    if r["tier"] == 3 or ts(signal_at).timestamp() < r["state"]["entered_at"]:
        return "UNIVERSE_INELIGIBLE", r
    h = next(
        (
            h
            for h in r.get("hypotheses", {}).get("hypotheses", [])
            if h["hypothesis_id"] == hypothesis.hypothesis_id
        ),
        None,
    )
    if not h or not h["available"]:
        return (h["reasons"][0] if h and h["reasons"] else "HYPOTHESIS_NOT_PORTABLE"), r
    if q is not None:
        spread = (q.ask - q.bid) / q.mid * 10000
        if spread > POLICY.spread_bps_max:
            return "WIDE_SPREAD", r
        if getattr(q, "depth_usd", 0) < POLICY.depth_usd_min:
            return "THIN_DEPTH", r
        if r["market"]["rv_bps"] < POLICY.movement_cost_multiple * (
            spread + 2 * (POLICY.fee_bps + getattr(q, "slippage_bps", 100))
        ):
            return "UNECONOMIC_EXECUTION", r
    context = {
        "evaluation_id": content_identity(ev),
        "policy_id": ev["policy_id"],
        "rank": r["rank"],
        "tier": r["tier"],
        "score": r["score"],
        "components": r["components"],
        "market": r["market"],
        "cost": r["cost"],
        "warmup": r["hypotheses"]["warmup"],
        "time_since_promotion_seconds": int(ts(now).timestamp())
        - (r["state"].get("promoted_at") or r["state"]["entered_at"]),
    }
    return None, context


def content_identity(ev):
    from market_signal.research.lab.common import content_id

    return ev.get("evaluation_id") or content_id("universesnapshot_", ev)


def size(equity, price, asset, *, decimals=None):
    from market_signal.paper.v2.engine import size as baseline_size

    if asset in CORE:
        return baseline_size(equity, price, asset)
    if (
        decimals is None
        or not 0 <= decimals <= POLICY.size_decimals_max
        or not math.isfinite(price)
        or price <= 0
    ):
        raise ValueError("unsupported satellite size configuration")
    step = Decimal(10) ** -decimals
    units = (Decimal(str(equity * 0.2)) / Decimal(str(price))).quantize(step, rounding=ROUND_DOWN)
    return float(units), float(units) * price


def pending_gate(store, position, now, q):
    """A risk pin preserves monitoring, not permission to fill an unsafe pending entry."""
    if position["asset"] in CORE:
        return None
    ev = ledger.latest(store, at=int(ts(now).timestamp()))
    if not ev or int(ts(now).timestamp()) - ev["at"] > 2 * POLICY.cadence_seconds:
        return "DATA_STALE"
    row = next((r for r in ev["rows"] if r["asset"] == position["asset"]), None)
    if not row:
        return "MARKET_INACTIVE"
    if row["execution_reasons"]:
        return row["execution_reasons"][0]
    if q and (q.ask - q.bid) / q.mid * 10000 > POLICY.spread_bps_max:
        return "WIDE_SPREAD"
    return None
