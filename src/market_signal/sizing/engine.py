"""Pure deterministic sizing: thesis geometry + account state + causal market state -> size.

``size`` never sees an outcome. It never increases risk after a loss, never uses a
confidence score, and treats leverage as collateral efficiency: the position is derived from
risk first; leverage only decides how much margin supports it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from .model import AccountSnapshot, FamilyEvidence, SizingRequest
from .policy import SizingPolicy

EMERGENCY_ADVERSE_MOVE = 0.10  # paper_nimble_v1 catastrophe guard (spec.EXECUTION)


@dataclass(frozen=True)
class SizingDecision:
    accepted: bool
    reason: str | None
    policy_id: str
    units: float = 0.0
    notional: float = 0.0
    leverage: float = 0.0
    margin: float = 0.0
    entry_fill: float = 0.0
    slippage_bps: float = 0.0
    risk_budget: float = 0.0
    intended_risk: float = 0.0
    risk_basis: str | None = None  # INVALIDATION | EMERGENCY_GUARD
    loss_fraction: float | None = None
    sizing_distance: float | None = None
    invalidation_distance: float | None = None
    volatility_distance: float | None = None
    cost_fraction: float | None = None
    liquidation_distance: float | None = None
    liquidation_price: float | None = None
    bound_by: tuple[str, ...] = ()
    explanation: tuple[tuple[str, object], ...] = field(default=())

    def explain(self) -> dict:
        return dict(self.explanation)


def _finite_pos(x) -> bool:
    return x is not None and isinstance(x, int | float) and math.isfinite(x) and x > 0


def floor_units(notional: float, price: float, decimals: int) -> float:
    step = Decimal(10) ** -decimals
    return float((Decimal(str(notional)) / Decimal(str(price))).quantize(step, rounding=ROUND_DOWN))


def liquidation_distance(leverage: float, venue_max_leverage: float) -> float:
    """Isolated-margin approximation: initial 1/L minus maintenance (half initial at max)."""
    return 1 / leverage - 1 / (2 * venue_max_leverage)


def drawdown_multiplier(policy: SizingPolicy, drawdown: float) -> float:
    m = 1.0
    for step in sorted(policy.drawdown_steps, key=lambda s: s.drawdown):
        if drawdown >= step.drawdown:
            m = min(m, step.multiplier)
    return m


def kelly_risk_fraction(policy: SizingPolicy, ev: FamilyEvidence | None) -> tuple[float, str]:
    """Fraction of a pessimistic Kelly estimate, capped. Insufficient evidence -> base risk."""
    base = policy.risk_fraction or 0.0
    if ev is None or ev.samples < policy.evidence_min_samples:
        return base, "INSUFFICIENT_EVIDENCE_FALLBACK"
    if ev.lower_r <= 0 or ev.second_moment_r <= 0:
        return 0.0, "PESSIMISTIC_EDGE_NON_POSITIVE"
    f = (policy.kelly_fraction or 0.0) * ev.lower_r / ev.second_moment_r
    cap = policy.evidence_max_risk_fraction or base
    return min(f, cap), "KELLY_LOWER_BOUND"


def evidence_multiplier(policy: SizingPolicy, ev: FamilyEvidence | None) -> tuple[float, str]:
    if ev is None or ev.samples < policy.evidence_min_samples:
        return 1.0, "INSUFFICIENT_EVIDENCE"
    if ev.lower_r > 0:
        return policy.evidence_ceiling_multiplier, "LOWER_BOUND_POSITIVE"
    if ev.mean_r <= 0:
        return policy.evidence_floor_multiplier, "MEAN_NON_POSITIVE"
    return 1.0, "UNCERTAIN"


def size(req: SizingRequest, account: AccountSnapshot, policy: SizingPolicy) -> SizingDecision:
    pid = policy.policy_id
    exp: list[tuple[str, object]] = []
    note = exp.append

    def reject(reason: str, **kw) -> SizingDecision:
        note(("decision", "REJECT:" + reason))
        return SizingDecision(False, reason, pid, explanation=tuple(exp), **kw)

    eq = account.equity
    note(("policy", policy.name))
    note(("equity", eq))
    if not (isinstance(eq, int | float) and math.isfinite(eq)) or eq <= 0:
        return reject("NO_EQUITY")
    if not (_finite_pos(req.bid) and _finite_pos(req.ask)) or req.bid > req.ask:
        return reject("INVALID_QUOTE")
    if req.side not in ("long", "short"):
        return reject("INVALID_SIDE")
    dd = account.drawdown
    note(("drawdown", dd))
    if account.halted or (policy.halt_drawdown is not None and dd >= policy.halt_drawdown):
        return reject("DRAWDOWN_HALT")
    # Count-based admission first: these never depend on size.
    if any(o.asset == req.asset for o in account.open):
        return reject("ASSET_ALREADY_OPEN")
    if len(account.open) >= policy.max_positions:
        return reject("MAX_POSITIONS")
    if req.cohort == "SATELLITE" and (
        sum(o.cohort == "SATELLITE" for o in account.open) >= policy.satellite_max_positions
    ):
        return reject("SATELLITE_CONCENTRATION")
    if policy.per_setup_max_positions is not None and (
        sum(o.hypothesis_id == req.hypothesis_id for o in account.open)
        >= policy.per_setup_max_positions
    ):
        return reject("SETUP_CONCENTRATION")

    s = req.sign
    side_px = req.ask if s > 0 else req.bid
    fee = policy.cost_model.fee_bps / 10000
    reserve = math.ceil(req.max_hold_minutes / 60) * max(0.0, s * (req.funding_rate_hourly or 0))
    has_inv = _finite_pos(req.invalidation_price)
    vol = req.causal_volatility if _finite_pos(req.causal_volatility) else None
    vol_d = policy.volatility_k * vol if vol is not None and policy.volatility_k > 0 else None
    fixed = policy.family == "fixed_allocation"

    # --- risk budget (fraction of equity), throttles only reduce -----------------------
    budget = 0.0
    mult = 1.0
    if not fixed:
        r = policy.risk_fraction or 0.0
        ev = account.evidence.get("*" if policy.evidence_scope == "pooled" else req.hypothesis_id)
        if policy.family == "fractional_kelly":
            r, why = kelly_risk_fraction(policy, ev)
            note(("kelly", why))
            if r <= 0:
                return reject("KELLY_NON_POSITIVE")
        elif policy.family == "evidence_weighted":
            m, why = evidence_multiplier(policy, ev)
            note(("evidence", f"{why} x{m:g}"))
            r = min(r * m, policy.evidence_max_risk_fraction or r * m)
        note(("base_risk_fraction", r))
        dm = drawdown_multiplier(policy, dd)
        note(("drawdown_multiplier", dm))
        rm = 1.0
        if policy.regime_ratio_threshold is not None:
            if req.regime_ratio is None or not math.isfinite(req.regime_ratio):
                rm = policy.regime_unavailable_multiplier
                note(("regime", "UNAVAILABLE"))
            elif req.regime_ratio >= policy.regime_ratio_threshold:
                rm = policy.regime_multiplier
            note(("regime_multiplier", rm))
        mult = dm * rm
        base_budget = eq * r
        budget = base_budget * mult
        note(("base_risk_budget", base_budget))
    else:
        base_budget = 0.0

    def geometry(slip_bps: float):
        fill = side_px * (1 + s * slip_bps / 10000)
        inv_d = s * (fill - req.invalidation_price) / fill if has_inv else None
        cost = slip_bps / 10000 + 2 * fee + reserve
        return fill, inv_d, cost

    def distance(inv_d):
        """(sizing distance, risk basis, fallback?)"""
        if policy.distance == "none":
            return None, None, True
        if policy.distance == "volatility" and vol_d is not None:
            return max(vol_d, policy.min_loss_distance), "VOLATILITY", False
        if inv_d is None:
            return None, None, True
        d = inv_d
        if policy.distance == "max_invalidation_volatility" and vol_d is not None:
            d = max(d, vol_d)
        return max(d, policy.min_loss_distance), "INVALIDATION", False

    slip = req.slippage_bps
    fill, inv_d, cost = geometry(slip)
    if has_inv and inv_d <= 0:
        return reject("INVALIDATION_BREACHED")
    note(("invalidation_distance", inv_d))
    note(("volatility_distance", vol_d))
    d, _, fallback = distance(inv_d)
    if fallback and not fixed:
        if policy.missing_invalidation == "reject":
            return reject("MISSING_INVALIDATION")
        note(("fallback", "FALLBACK_NOTIONAL_NO_CAUSAL_INVALIDATION"))
    bound: list[str] = []

    if not fixed and not fallback:
        ratio = cost / d
        note(("cost_to_distance", ratio))
        if policy.cost_risk_reject_above is not None and ratio > policy.cost_risk_reject_above:
            return reject("UNECONOMIC_COST_TO_RISK")
        if policy.cost_risk_reduce_above is not None and ratio > policy.cost_risk_reduce_above:
            mult *= 0.5
            budget *= 0.5
            note(("cost_multiplier", 0.5))
        note(("effective_risk_budget", budget))

    def target(fill_, d_, cost_):
        if fixed:
            return eq * policy.notional_fraction
        if fallback:
            return eq * policy.fallback_notional_fraction * mult
        return budget / (d_ + (cost_ if policy.include_costs_in_risk else 0.0))

    caps = {"MAX_NOTIONAL": eq * policy.max_notional_fraction}
    if req.depth_usd is not None and req.depth_usd > 0:
        caps["LIQUIDITY_CAP"] = req.depth_usd * policy.cost_model.depth_participation_max
    notional = target(fill, d, cost)
    # Size-dependent impact: two deterministic fixed-point passes (impact only worsens fills).
    for _ in range(2):
        capped = min(notional, *caps.values())
        slip = policy.cost_model.slippage_bps(req.slippage_bps, capped)
        fill, inv_d, cost = geometry(slip)
        if has_inv and inv_d <= 0:
            return reject("INVALIDATION_BREACHED_AFTER_IMPACT")
        d, _, fallback = distance(inv_d)
        notional = target(fill, d, cost)
    note(("sizing_distance", d))
    note(("cost_fraction", cost))
    note(("raw_notional", notional))
    for name, cap in caps.items():
        if notional > cap:
            notional = cap
            bound.append(name)
            note((name.lower(), cap))

    venue_cap = min(policy.leverage_cap, req.venue_max_leverage)
    per_margin = eq * policy.max_margin_fraction_per_position
    if policy.leverage_mode == "fixed":
        lev = venue_cap
    else:
        lev = min(venue_cap, max(1.0, math.ceil(notional / per_margin - 1e-12)))
    if notional / lev > per_margin + 1e-12:
        notional = per_margin * lev
        bound.append("MARGIN_CAP")
    note(("leverage", lev))

    # --- portfolio capacity ------------------------------------------------------------
    loss_frac = None if d is None else d + (cost if policy.include_costs_in_risk else 0.0)
    if fixed and has_inv:
        loss_frac = inv_d + cost
    # 1R is always the loss at the thesis invalidation when one exists.
    if has_inv:
        risk_basis, r_frac = "INVALIDATION", inv_d + cost
    elif loss_frac is not None:
        risk_basis, r_frac = "VOLATILITY", loss_frac
    else:
        risk_basis, r_frac = "EMERGENCY_GUARD", EMERGENCY_ADVERSE_MOVE + cost
    risk_frac_for_caps = r_frac
    limits = {
        "EXPOSURE_CAP": policy.gross_fraction * eq - sum(o.notional for o in account.open),
        "MARGIN_AVAILABLE": (
            policy.max_total_margin_fraction * eq - sum(o.margin for o in account.open)
        )
        * lev,
    }
    if policy.portfolio_risk_multiple is not None and base_budget > 0:
        limits["PORTFOLIO_RISK_CAP"] = (
            policy.portfolio_risk_multiple * base_budget - sum(o.risk for o in account.open)
        ) / risk_frac_for_caps
    if policy.cluster_risk_multiple is not None and base_budget > 0:
        beta = policy.satellite_beta if req.cohort == "SATELLITE" else 1.0
        used = sum(
            o.risk * (policy.satellite_beta if o.cohort == "SATELLITE" else 1.0)
            for o in account.open
            if o.side == req.side
        )
        limits["CORRELATED_CLUSTER_CAP"] = (policy.cluster_risk_multiple * base_budget - used) / (
            beta * risk_frac_for_caps
        )
    if policy.max_net_directional_fraction is not None:
        net = sum((1 if o.side == "long" else -1) * o.notional for o in account.open)
        limits["NET_DIRECTIONAL_CAP"] = policy.max_net_directional_fraction * eq - s * net
    for name, room in sorted(limits.items(), key=lambda kv: kv[1]):
        if notional > room + 1e-9:
            if policy.capacity_mode == "skip" or room <= 0:
                return reject(name)
            notional = room
            bound.append(name)
            note((name.lower(), room))

    units = floor_units(notional, fill, req.size_decimals)
    notional = units * fill
    note(("units", units))
    note(("notional", notional))
    if notional < policy.min_notional:
        return reject("MIN_NOTIONAL" if not bound else "CAPACITY_BELOW_MIN_NOTIONAL")
    margin = notional / lev
    liq_d = liquidation_distance(lev, req.venue_max_leverage)
    liq_px = fill * (1 - s * liq_d)
    note(("margin", margin))
    note(("liquidation_distance", liq_d))
    intended = notional * r_frac
    if policy.enforce_liquidation_safety:
        boundary = loss_frac if loss_frac is not None else EMERGENCY_ADVERSE_MOVE
        needed = max(policy.liquidation_buffer_multiple * boundary, policy.liquidation_min_distance)
        if liq_d < needed:
            return reject("LIQUIDATION_BEFORE_INVALIDATION")
    note(("final_intended_risk", intended))
    note(("decision", "ACCEPT"))
    return SizingDecision(
        True,
        None,
        pid,
        units=units,
        notional=notional,
        leverage=lev,
        margin=margin,
        entry_fill=fill,
        slippage_bps=slip,
        risk_budget=budget,
        intended_risk=intended,
        risk_basis=risk_basis,
        loss_fraction=loss_frac,
        sizing_distance=d,
        invalidation_distance=inv_d,
        volatility_distance=vol_d,
        cost_fraction=cost,
        liquidation_distance=liq_d,
        liquidation_price=liq_px,
        bound_by=tuple(bound),
        explanation=tuple(exp),
    )
