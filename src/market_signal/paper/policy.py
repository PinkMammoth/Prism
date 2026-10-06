"""Frozen, versioned policies of the paper auto-trader.

Five separate identities, each a content hash over every field that affects a decision:

- ``AutotraderPolicy`` (promotion): may this strategy generate *paper* trading intent?
  Stricter than ``copilot_policy`` and independent of it. It never says anything about
  real-money trading; there is no live/approval outcome anywhere in this package.
- ``RiskPolicy``: account, sizing, margin, exposure, conflict and kill-switch rules.
- ``ExecutionModel``: reference prices, slippage, fees, entry window, funding semantics.
- ``ExitPolicy``: how a paper position closes.
- ``PaperMaturityPolicy``: how much paper evidence exists (never whether it is good).

Changing any value is a new version and a new ID, hence a new paper run (``continues``
links the lineage). Never edit a released entry; add a new one.

All numeric risk values are arbitrary conservative research defaults chosen before any
paper result existed. They are not optimised and must not be tuned on paper outcomes.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    Symbol,
    content_id,
)

Fraction = Annotated[Number, Field(gt=0, le=1)]
Bps = Annotated[Number, Field(ge=0, le=500)]

PROMOTION_DECISIONS = ("PAPER_ELIGIBLE", "PAPER_INELIGIBLE")


# --------------------------------------------------------------------------- promotion


class AutotraderPolicy(LabModel):
    """``autotrader_policy``: paper-trading eligibility of a fired, enrolled strategy signal.

    Blocking rules (every one must pass). Compared with ``copilot_policy`` v1 it additionally
    requires: full research run and CONSISTENT/MIXED, a Phase 9 validation registration that
    is not adverse or errored, an active Phase 8 forward tracking, cross-venue corroboration
    not adverse, and forward evidence not adverse once DEVELOPING (not only MATURE).
    FDR survival, supportive validation and corroboration are NOT required: paper trading is
    itself evidence gathering. They are recorded as caveats.
    """

    name: Name = "autotrader_policy"
    version: PositiveInt = 1
    mode: Literal["paper"] = "paper"
    require_enrollment: Literal[True] = True  # only the run's frozen, manually chosen cohort
    eligible_tiers: tuple[str, ...] = ("EXPLORATORY", "RESEARCH_SUPPORTED")
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    min_excess: Number = 0.0  # primary-horizon expected-direction net excess must exceed this
    max_asset_event_share: Probability = 0.5
    block_dominated_by_one_asset: bool = True
    block_isolated_spike: bool = True
    require_full_research: bool = True
    allowed_full_research: tuple[str, ...] = ("FULL_RESEARCH_CONSISTENT", "FULL_RESEARCH_MIXED")
    require_validation_registration: bool = True
    blocked_validation: tuple[str, ...] = ("VALIDATION_ADVERSE", "VALIDATION_ERROR")
    # Cross-venue corroboration (Phase 11): ADVERSE blocks; MIXED/INSUFFICIENT/not run are
    # caveats. Frozen choice: the data is historically exposed, so it can only veto.
    blocked_corroboration: tuple[str, ...] = ("CROSS_VENUE_ADVERSE",)
    require_active_forward_tracking: bool = True
    adverse_forward_levels: tuple[str, ...] = ("DEVELOPING", "MATURE")
    fdr_caveat_q: Probability = 0.10

    @property
    def policy_id(self) -> str:
        return content_id("appolicy_", self.model_dump(mode="python"))


PROMOTION_POLICIES = {1: AutotraderPolicy()}


def _forward_adverse(fwd: dict | None) -> bool:
    if not fwd or fwd.get("excess_mean") is None:
        return False
    return fwd["excess_mean"] <= 0 or fwd.get("direction_vs_historical") == "opposite"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def evaluate_promotion(policy: AutotraderPolicy, evidence: dict | None, context: dict) -> dict:
    """Pure eligibility decision for one strategy at one moment.

    ``evidence``: ``engine.evidence_view`` (None if unusable). ``context``: ``enrolled``,
    ``semantics_ok``/``semantics_detail``, ``tracking_active``.
    Returns ``decision`` (PAPER_ELIGIBLE / PAPER_INELIGIBLE), ``checks``, ``blocked_by``,
    ``caveats``. Evidence is only read; the tier is never changed.
    """
    checks: list[dict] = []

    def check(rule: str, passed: bool, detail: str) -> None:
        checks.append({"rule": rule, "passed": bool(passed), "detail": detail})

    check("strategy_enrolled", context.get("enrolled", False),
          "in the paper run's frozen cohort" if context.get("enrolled")
          else "not in the paper run's cohort")  # fmt: skip
    check("versions_compatible", context.get("semantics_ok", False),
          context.get("semantics_detail") or "compiler/vocabulary versions match")  # fmt: skip
    check("forward_tracking_active",
          not policy.require_active_forward_tracking or context.get("tracking_active", False),
          "an active Phase 8 forward tracking observes the same signals"
          if context.get("tracking_active") else "no active Phase 8 forward tracking")  # fmt: skip
    caveats: list[str] = []
    if evidence is None:
        check("evidence_available", False, "no usable evidence profile")
        return _result(checks, caveats)
    check("evidence_available", True, f"profile {evidence['profile_id']}")
    tier = evidence["tier"]
    check("tier_eligible", tier in policy.eligible_tiers, f"evidence tier {tier}")
    n = evidence["sample"].get("independent_events") or 0
    assets = evidence["assets"].get("assets_with_events") or 0
    check("sample_adequate",
          n >= policy.min_independent_events and assets >= policy.min_assets_with_events,
          f"{n} independent events on {assets} assets (min {policy.min_independent_events} on "
          f"{policy.min_assets_with_events})")  # fmt: skip
    ex = evidence["effect"].get("excess_mean")
    check("effect_positive", ex is not None and ex > policy.min_excess,
          f"{evidence.get('primary_horizon')} expected-direction net excess {_pct(ex)}")  # fmt: skip
    a = evidence["assets"]
    share = a.get("max_asset_event_share")
    dominated = policy.block_dominated_by_one_asset and a.get("dominated_by_one_asset")
    check("breadth_ok", not dominated and (share is None or share <= policy.max_asset_event_share),
          f"largest asset share {'n/a' if share is None else f'{share:.0%}'}"
          + ("; pooled sign flips without the top asset" if a.get("dominated_by_one_asset") else ""))  # fmt: skip
    nb = evidence["neighbourhood"]
    check("not_isolated_spike", not (policy.block_isolated_spike and nb.get("isolated_spike")),
          f"parameter neighbourhood {nb.get('label') or 'n/a'}")  # fmt: skip
    fs = evidence.get("full_research_status")
    check("full_research_adequate",
          fs in policy.allowed_full_research or (fs is None and not policy.require_full_research),
          f"full research {fs or 'not run'} (needs {' or '.join(policy.allowed_full_research)})")  # fmt: skip
    vs = evidence.get("validation_status")
    registered = vs is not None
    check("validation_registered", registered or not policy.require_validation_registration,
          "Phase 9 validation registered" if registered else "no Phase 9 validation registered")  # fmt: skip
    check("validation_not_adverse", vs not in policy.blocked_validation,
          f"validation {vs or 'not run'}")  # fmt: skip
    cs = evidence.get("corroboration_status")
    check("corroboration_not_adverse", cs not in policy.blocked_corroboration,
          f"cross-venue corroboration {cs or 'not run'}")  # fmt: skip
    fwd = evidence.get("forward")
    level = fwd["maturity"] if fwd else None
    adverse_fwd = _forward_adverse(fwd)
    check("forward_not_adverse", not (level in policy.adverse_forward_levels and adverse_fwd),
          f"forward {level or 'not tracked'}"
          + (" and pointing against the tested direction" if adverse_fwd else ""))  # fmt: skip

    st = evidence.get("statistics") or {}
    if not st.get("fdr_survivor"):
        q = st.get("q")
        caveats.append(
            "did not survive family (BH) correction" + (f": q {q:.2f}" if q is not None else "")
        )
    if fs and fs != "FULL_RESEARCH_CONSISTENT":
        caveats.append(f"full research {fs.removeprefix('FULL_RESEARCH_').lower()}")
    if vs in ("VALIDATION_INSUFFICIENT", "VALIDATION_MIXED"):
        caveats.append(f"validation {vs.removeprefix('VALIDATION_').lower()}")
    if cs is None:
        caveats.append("cross-venue corroboration not run")
    elif cs != "CROSS_VENUE_CORROBORATIVE":
        caveats.append(f"cross-venue corroboration {cs.removeprefix('CROSS_VENUE_').lower()}")
    if level in (None, "TOO_EARLY", "EARLY"):
        caveats.append("forward evidence too early")
    if nb.get("label") != "plateau":
        caveats.append(f"parameter neighbourhood {nb.get('label') or 'n/a'}")
    return _result(checks, caveats)


def _result(checks: list[dict], caveats: list[str]) -> dict:
    blocked = [c["rule"] for c in checks if not c["passed"]]
    return {
        "decision": "PAPER_INELIGIBLE" if blocked else "PAPER_ELIGIBLE",
        "checks": checks,
        "blocked_by": blocked,
        "caveats": caveats,
    }


# --------------------------------------------------------------------------- risk


class AssetMargin(LabModel):
    symbol: Symbol
    maintenance_rate: Annotated[Number, Field(gt=0, lt=0.5)]


# Hyperliquid's published rule: maintenance = 1 / (2 x venue max leverage). The venue
# maxima are config/perps.yaml's documented defaults, frozen here as constants so the margin
# model is point-in-time safe: it never reads the latest perp_snapshots max leverage.
V1_MAINTENANCE = (
    AssetMargin(symbol="AAVE", maintenance_rate=0.05),
    AssetMargin(symbol="BTC", maintenance_rate=0.0125),
    AssetMargin(symbol="ETH", maintenance_rate=0.02),
    AssetMargin(symbol="HYPE", maintenance_rate=0.05),
    AssetMargin(symbol="LINK", maintenance_rate=0.05),
    AssetMargin(symbol="SOL", maintenance_rate=0.025),
)


class RiskPolicy(LabModel):
    """``paper_risk_policy``: the simulated account and every pre-trade rule.

    Sizing is a fixed fraction of current equity (``fixed_equity_fraction_v1``): the
    cohort's strategies carry no stop that their evidence evaluated, so stop-distance risk
    sizing would rest on an untested stop. No Kelly, no volatility targeting.
    """

    name: Name = "paper_risk_policy"
    version: PositiveInt = 1
    mode: Literal["paper"] = "paper"
    currency: Literal["USDC"] = "USDC"
    starting_equity: Annotated[Number, Field(gt=0)] = 10_000.0
    sizing: Literal["fixed_equity_fraction_v1"] = "fixed_equity_fraction_v1"
    position_notional_fraction: Fraction = 0.20  # notional per new position / equity
    min_position_notional_fraction: Fraction = 0.05  # smaller after scaling -> rejected
    margin_mode: Literal["isolated"] = "isolated"
    leverage: Annotated[Number, Field(ge=1, le=3)] = 2.0  # every position, fixed
    max_leverage: Annotated[Number, Field(ge=1, le=3)] = 2.0  # hard cap
    max_open_positions: PositiveInt = 3  # open positions + unfilled entry orders
    max_gross_notional_fraction: Fraction = 0.60
    max_asset_notional_fraction: Fraction = 0.20
    max_strategy_notional_fraction: Fraction = 0.40
    min_free_cash_fraction: Fraction = 0.25  # cash buffer kept after margin + fees
    daily_loss_halt_fraction: Fraction = 0.03  # bar-to-bar equity loss -> no entries that bar
    max_drawdown_kill_fraction: Fraction = 0.15  # from peak marked equity -> KILLED
    max_consecutive_error_cycles: PositiveInt = 3  # -> automatic PAUSE
    max_bar_interval_hours: Number = 24.0  # a larger gap in the signal lookback blocks entry
    maintenance: tuple[AssetMargin, ...] = V1_MAINTENANCE
    liquidation_model: Literal["isolated_full_margin_loss_v1"] = "isolated_full_margin_loss_v1"
    conflict_rule: Literal["one_position_per_asset_no_hedge_no_add_v1"] = (
        "one_position_per_asset_no_hedge_no_add_v1"
    )
    allocation_rule: Literal["same_snapshot_hash_lottery_proportional_v1"] = (
        "same_snapshot_hash_lottery_proportional_v1"
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.leverage > self.max_leverage:
            raise ValueError("leverage above the policy cap")
        if self.max_asset_notional_fraction < self.position_notional_fraction:
            raise ValueError("a single position would breach the per-asset cap")
        syms = [m.symbol for m in self.maintenance]
        if len(set(syms)) != len(syms):
            raise ValueError("duplicate maintenance entries")
        return self

    def maintenance_rate(self, symbol: str) -> float | None:
        return next((m.maintenance_rate for m in self.maintenance if m.symbol == symbol), None)

    @property
    def policy_id(self) -> str:
        return content_id("riskpolicy_", self.model_dump(mode="python"))


RISK_POLICIES = {1: RiskPolicy()}


# --------------------------------------------------------------------------- execution


class AssetCosts(LabModel):
    symbol: Symbol
    fee_bps: Bps
    slippage_bps: Bps


class ExecutionModel(LabModel):
    """``paper_execution_model``: how simulated orders fill. Pure; no venue connection.

    Entry: a market order for the open of bar T+1, recorded while the signal is live (at
    most ``max_entry_latency_hours`` after bar T closes; otherwise the entry is skipped).
    It fills at that bar's stored open +- slippage once the bar is stored. Never at T's
    close, never at a high/low. Exit: at the scheduled bar's close -+ slippage. Full
    immediate fills (liquid majors, small size); no partial fills or queue model.
    Costs are frozen per asset from the cohort's research plan (taker fee + slippage).
    """

    name: Name = "paper_execution_model"
    version: PositiveInt = 1
    mode: Literal["paper"] = "paper"
    venue: Literal["hyperliquid"] = "hyperliquid"
    order_type: Literal["market"] = "market"
    entry_reference: Literal["next_bar_open"] = "next_bar_open"
    exit_reference: Literal["exit_bar_close"] = "exit_bar_close"
    fill_model: Literal["full_immediate_v1"] = "full_immediate_v1"
    max_entry_latency_hours: Annotated[Number, Field(gt=0, le=24)] = 12.0
    funding: Literal["settled_rates_at_bar_close_price_v1"] = "settled_rates_at_bar_close_price_v1"
    funding_interval_hours: Annotated[Number, Field(gt=0, le=24)] = 1.0
    costs: Annotated[tuple[AssetCosts, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        syms = [c.symbol for c in self.costs]
        if len(set(syms)) != len(syms) or list(syms) != sorted(syms):
            raise ValueError("costs must be unique and sorted by symbol")
        return self

    def cost(self, symbol: str) -> AssetCosts:
        for c in self.costs:
            if c.symbol == symbol:
                return c
        raise ValueError(f"no frozen execution cost for {symbol}")

    @property
    def settlements_per_bar(self) -> int:
        return round(24 / self.funding_interval_hours)

    @property
    def policy_id(self) -> str:
        return content_id("execmodel_", self.model_dump(mode="python"))


# --------------------------------------------------------------------------- exit


class ExitPolicy(LabModel):
    """``paper_exit_policy``: fixed holding horizon equal to the primary research horizon.

    Entry at the open of T+1 and exit at the close of T+h reproduce the evidence's return
    window exactly. The definition's family-template ExitIntent (ATR stop, max hold) was
    never evaluated by any Lab evidence, so v1 uses no stop, no target and no
    opposite-signal exit. Liquidation (risk policy) is the only other way a position ends.
    """

    name: Name = "paper_exit_policy"
    version: PositiveInt = 1
    rule: Literal["fixed_horizon_v1"] = "fixed_horizon_v1"
    horizon_source: Literal["primary_research_horizon"] = "primary_research_horizon"
    horizon_bars: PositiveInt = 10
    protective_stop: None = None
    take_profit: None = None
    opposite_signal_exit: Literal[False] = False
    uses_strategy_exit_intent: Literal[False] = False

    @property
    def policy_id(self) -> str:
        return content_id("exitpolicy_", self.model_dump(mode="python"))


EXIT_POLICIES = {1: ExitPolicy()}


# --------------------------------------------------------------------------- maturity


class PaperMaturityPolicy(LabModel):
    """How much paper evidence exists: closed trades AND observed days. Never quality."""

    name: Name = "paper_maturity"
    version: PositiveInt = 1
    early_trades: PositiveInt = 10
    early_days: PositiveInt = 30
    developing_trades: PositiveInt = 30
    developing_days: PositiveInt = 90
    mature_trades: PositiveInt = 100
    mature_days: PositiveInt = 365

    def level(self, closed_trades: int, observed_days: int) -> str:
        if closed_trades >= self.mature_trades and observed_days >= self.mature_days:
            return "MATURE"
        if closed_trades >= self.developing_trades and observed_days >= self.developing_days:
            return "DEVELOPING"
        if closed_trades >= self.early_trades and observed_days >= self.early_days:
            return "EARLY"
        return "WARMUP"

    @property
    def policy_id(self) -> str:
        return content_id("papermaturity_", self.model_dump(mode="python"))


MATURITY_POLICIES = {1: PaperMaturityPolicy()}


def _get(registry: dict, version: int | None, what: str):
    v = version or max(registry)
    if v not in registry:
        raise ValueError(f"unknown {what} version {v}; known: {sorted(registry)}")
    return registry[v]


def promotion_policy(version: int | None = None) -> AutotraderPolicy:
    return _get(PROMOTION_POLICIES, version, "autotrader policy")


def risk_policy(version: int | None = None) -> RiskPolicy:
    return _get(RISK_POLICIES, version, "paper risk policy")


def exit_policy(version: int | None = None) -> ExitPolicy:
    return _get(EXIT_POLICIES, version, "paper exit policy")


def maturity_policy(version: int | None = None) -> PaperMaturityPolicy:
    return _get(MATURITY_POLICIES, version, "paper maturity policy")
