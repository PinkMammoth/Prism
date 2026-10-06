"""Frozen Phase 21 incubation policies: who may generate prospective *paper* observations.

These policies answer "should this candidate be allowed to generate prospective paper
observations?" — never "should real capital trade this?". Each is versioned and
content-addressed; tests pin the IDs, so a change is a new version beside the old one.

| Profile | What it is |
|---|---|
| CONSERVATIVE | the unchanged Phase 20 lifecycle (``lab_edge_lifecycle_policy`` v1, weekly cadence, two confirmations) used as the benchmark; its PAPER_ACTIVE/DEGRADED map to EXPLORATORY_PAPER |
| BALANCED | one frozen threshold on a short calendar window, evaluated at every trigger-bar close |
| AGGRESSIVE | the same small rule set with a lower bar and a shorter window; tolerates substantial false activity |

The admission rule set is deliberately small (``evidence.admission``): an adequate recent
sample, after-cost mean at least the economic floor, a standardised effect threshold, no
single-asset dominance, and not worse than random timing. No cross-venue replication, no
lifetime profitability, no long lifetime consistency. A credibly negative lifetime raises
the bar; it never bans a strategy.

BALANCED/AGGRESSIVE thresholds were chosen on synthetic data only, by selection rules
written down before the grid was run (``synthetic.DESIGN_RULES``); the historical replay is
diagnostics and never fed a threshold.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.research.lab.common import LabModel, Name, Number, PositiveInt, content_id
from market_signal.research.lifecycle import policy as lifecycle_policy
from market_signal.research.lifecycle.policy import EconomicFloor

METHODOLOGY_VERSION = "candidate_incubation_v1"
MACHINE_VERSION = "incubation_machine_v1"
EVIDENCE_VERSION = "incubation_evidence_v1"
GRADUATION_VERSION = "incubation_graduation_v1"
OPPORTUNITY_VERSION = "incubation_opportunity_v1"

Fraction = Annotated[Number, Field(ge=0, le=1)]
Profile = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
PROFILES: tuple[str, ...] = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")

# Candidate levels. INSUFFICIENT/NEUTRAL/WATCH/DORMANT never generate shadow intents.
LEVELS = ("INSUFFICIENT", "NEUTRAL", "WATCH", "EXPLORATORY_PAPER", "CONFIRMED_PAPER",
          "DORMANT", "RETIRED")  # fmt: skip
ADMITTED = ("EXPLORATORY_PAPER", "CONFIRMED_PAPER")


class Window(LabModel):
    """The admission window: outcomes resolved in (T - days, T]. Gated by sample size and
    breadth so a 2-trade window can never admit, however large its mean."""

    days: PositiveInt
    min_events: PositiveInt
    min_assets: PositiveInt = 2


class Watch(LabModel):
    """WATCH = interesting recent behaviour, not enough for paper: at least ``min_events``
    in the admission window with a positive after-cost mean."""

    min_events: PositiveInt = 3


class Admission(LabModel):
    """All must hold at ONE evaluation (no multi-week confirmation)."""

    min_t: Number
    min_mean: Literal["floor"] = "floor"
    # leave-best-asset-out mean must be at least this (multiples of the floor): the edge is
    # not one asset's luck. 0 = still positive without the best asset; -1 = not below -floor.
    loo_min_floors: Number
    min_excess: Literal["-floor"] = "-floor"  # not materially worse than random timing
    hostile_lifetime_t: Number = -2.0  # an adequate lifetime this negative raises the bar ...
    hostile_lifetime_extra_t: Number = 0.5  # ... by this much (never a ban)
    readmit_new_outcomes: PositiveInt = 1  # re-admission needs new evidence since dormancy


class Deactivation(LabModel):
    """Any one makes an admitted candidate DORMANT (fast: a zero-cost experiment needs no
    months of evidence to stop)."""

    exit_max_t: Number  # recent evidence reversed: window t at or below this
    lapse_min_events: PositiveInt  # signal conditions gone: window sample below this
    episode_min_outcomes: PositiveInt = 5  # prospective/episode outcomes strongly adverse:
    episode_max_t: Number = -1.5  # ... at least 5 and their t at or below this
    regime_min_events: PositiveInt = 3  # regime changed and the window's outcomes in the
    # current regime (at least 3) have a negative mean


class Graduation(LabModel):
    """EXPLORATORY_PAPER -> CONFIRMED_PAPER, from forward (episode) evidence ONLY. Designed,
    frozen and reported; connected to nothing. There is no LIVE state."""

    version: Literal["incubation_graduation_v1"] = GRADUATION_VERSION
    min_outcomes: PositiveInt = 20
    min_assets: PositiveInt = 2
    min_mean: Literal["floor"] = "floor"
    min_t: Number = 1.5
    loo_positive: Literal[True] = True  # not dominated by one asset
    agree_with_research: Literal[True] = True  # forward and admission means share a sign
    max_unavailable_share: Fraction = 0.2  # execution behaviour: missed/unavailable intents
    demote_below_mean: Number = 0.0  # CONFIRMED -> EXPLORATORY if the forward mean falls below


class Cadence(LabModel):
    """Evaluate at every close of the strategy's trigger bar (daily strategies: daily), and
    only when new information arrived: an evaluation whose window, episode and regime are
    unchanged cannot change the decision and is skipped. No weekly wait."""

    rule: Literal["each_trigger_bar_close"] = "each_trigger_bar_close"
    skip_unchanged: Literal[True] = True


class IncubationPolicy(LabModel):
    name: Literal["lab_incubation_policy"] = "lab_incubation_policy"
    version: PositiveInt = 1
    profile: Literal["BALANCED", "AGGRESSIVE"]
    purpose: Literal["exploratory_paper_admission"] = "exploratory_paper_admission"
    grants_live: Literal[False] = False
    methodology_version: Literal["candidate_incubation_v1"] = METHODOLOGY_VERSION
    machine_version: Literal["incubation_machine_v1"] = MACHINE_VERSION
    window: Window
    watch: Watch = Watch()
    admission: Admission
    deactivation: Deactivation
    graduation: Graduation = Graduation()
    cadence: Cadence = Cadence()
    floor: EconomicFloor = EconomicFloor()  # the Phase 20 economic floor, unchanged

    @model_validator(mode="after")
    def hysteretic(self) -> Self:
        if self.deactivation.exit_max_t >= self.admission.min_t:
            raise ValueError("deactivation must be weaker than admission (hysteresis)")
        if self.deactivation.lapse_min_events > self.window.min_events:
            raise ValueError("the lapse gate must not exceed the admission sample gate")
        if self.watch.min_events > self.window.min_events:
            raise ValueError("WATCH must need no more evidence than admission")
        return self

    @property
    def policy_id(self) -> str:
        return content_id("incpolicy_", self.model_dump(mode="json"))


class ConservativeBenchmark(LabModel):
    """The Phase 20 lifecycle, unchanged and unweakened, as the CONSERVATIVE benchmark.
    Its own (weekly, two-confirmation) machine decides; levels are mapped, nothing else."""

    name: Literal["lab_incubation_conservative_benchmark"] = "lab_incubation_conservative_benchmark"
    version: PositiveInt = 1
    profile: Literal["CONSERVATIVE"] = "CONSERVATIVE"
    purpose: Literal["benchmark"] = "benchmark"
    grants_live: Literal[False] = False
    lifecycle_policy_version: PositiveInt = 1
    lifecycle_policy_id: Name
    mode: Literal["recent"] = "recent"
    mapping: tuple[tuple[str, str], ...] = (
        ("DISCOVERED", "INSUFFICIENT"), ("WATCH", "NEUTRAL"), ("ACTIVE_CANDIDATE", "WATCH"),
        ("PAPER_ACTIVE", "EXPLORATORY_PAPER"), ("DEGRADED", "EXPLORATORY_PAPER"),
        ("DORMANT", "DORMANT"), ("RETIRED", "RETIRED"),
    )  # fmt: skip
    graduation: Graduation = Graduation()  # the same forward overlay as every profile
    floor: EconomicFloor = EconomicFloor()

    @model_validator(mode="after")
    def same_lifecycle(self) -> Self:
        if (
            lifecycle_policy.policy(self.lifecycle_policy_version).policy_id
            != self.lifecycle_policy_id
        ):
            raise ValueError("the benchmark must cite the frozen Phase 20 lifecycle policy")
        return self

    def lifecycle(self) -> lifecycle_policy.LifecyclePolicy:
        return lifecycle_policy.policy(self.lifecycle_policy_version)

    def level(self, lifecycle_state: str) -> str:
        return dict(self.mapping)[lifecycle_state]

    @property
    def policy_id(self) -> str:
        return content_id("incpolicy_", self.model_dump(mode="json"))


AnyPolicy = IncubationPolicy | ConservativeBenchmark

# Frozen v1 profiles. Thresholds chosen on synthetic data only (synthetic.DESIGN_RULES).
CONSERVATIVE = ConservativeBenchmark(lifecycle_policy_id=lifecycle_policy.policy(1).policy_id)
# Selected by the pre-declared design rules (synthetic.DESIGN_RULES) from the grid members
# ``w21_t2`` (BALANCED) and ``w21_t1`` (AGGRESSIVE): the same 21-day window, one threshold
# apart. A test checks the frozen values equal those grid members.
BALANCED = IncubationPolicy(
    profile="BALANCED",
    window=Window(days=21, min_events=4),
    admission=Admission(min_t=2.0, loo_min_floors=0.0),
    deactivation=Deactivation(exit_max_t=1.0, lapse_min_events=2),
)
AGGRESSIVE = IncubationPolicy(
    profile="AGGRESSIVE",
    window=Window(days=21, min_events=4),
    admission=Admission(min_t=1.0, loo_min_floors=0.0),
    deactivation=Deactivation(exit_max_t=0.0, lapse_min_events=2),
)

POLICIES: dict[str, AnyPolicy] = {"CONSERVATIVE": CONSERVATIVE, "BALANCED": BALANCED,
                                  "AGGRESSIVE": AGGRESSIVE}  # fmt: skip


def get(profile: str) -> AnyPolicy:
    key = profile.upper()
    if key not in POLICIES:
        raise ValueError(f"unknown incubation policy {profile!r}; one of {', '.join(PROFILES)}")
    return POLICIES[key]


class ShadowExecution(LabModel):
    """Frozen shadow-paper execution assumptions (``shadow_execution_v1``). Analytical, no
    venue, no account: a standardised fixed notional per intent so policies are compared
    on candidate quality, not on sizing. Sizing is separate from the research signal and is
    never optimised per strategy."""

    name: Literal["shadow_execution_v1"] = "shadow_execution_v1"
    mode: Literal["shadow"] = "shadow"
    venue: Literal["hyperliquid"] = "hyperliquid"
    entry_reference: Literal["next_bar_open"] = "next_bar_open"
    exit_rule: Literal["close_of_bar_T_plus_h"] = "close_of_bar_T_plus_h"
    stop: Literal["none"] = "none"  # the research effect unit has no stop; neither does this
    horizon_bars: PositiveInt = 10
    sizing: Literal["fixed_notional_v1"] = "fixed_notional_v1"
    notional_usd: Annotated[Number, Field(gt=0)] = 1_000.0
    leverage: Literal[1] = 1
    compounding: Literal[False] = False
    costs: Literal["frozen_taker_fee_plus_slippage_per_side"] = (
        "frozen_taker_fee_plus_slippage_per_side"
    )
    funding: Literal["causal_daily_funding_paid_T+1..T+h"] = "causal_daily_funding_paid_T+1..T+h"
    max_entry_latency_hours: Annotated[Number, Field(gt=0, le=24)] = 12.0
    outcome_wait_days: PositiveInt = 7  # missing exit data/funding -> 'unavailable' after this
    independence: Literal["one_open_intent_per_policy_strategy_asset"] = (
        "one_open_intent_per_policy_strategy_asset")  # fmt: skip
