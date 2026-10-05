"""The frozen Phase 20 lifecycle policy: every window, gate, weight, floor and rule.

Everything that could otherwise be chosen after seeing results lives here, is versioned and
is content-addressed (``policy_id``). A study freezes the policy ID in its definition, and
every edge profile cites it. A test pins the v1 ID, so v1 can never be edited silently: a
change is ``LifecyclePolicy(version=2, ...)`` beside it, and v1 results keep their meaning.

v1 was written before any lifecycle computation touched real data. Its activation threshold
was then examined on synthetic data only (``synthetic.calibrate``), never tuned on the
historical experiment.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.research.lab.common import LabModel, Name, Number, PositiveInt, content_id

METHODOLOGY_VERSION = "edge_lifecycle_v1"
BASELINE_VERSION = "causal_trailing_baseline_v1"
INFERENCE_VERSION = "time_block_cluster_robust_v1"
MARKET_STATE_VERSION = "market_state_v1"
STRESS_VERSION = "market_stress_v1"
CUSUM_VERSION = "lifecycle_cusum_v1"
SEGMENTATION_VERSION = "retrospective_binseg_v1"
EDGE_STATE_VERSION = "edge_state_v1"
MACHINE_VERSION = "lifecycle_machine_v1"

Fraction = Annotated[Number, Field(ge=0, le=1)]


class CalendarWindow(LabModel):
    label: Name
    days: PositiveInt


class EventWindow(LabModel):
    """The latest ``events`` independent resolved outcomes, if they span at most
    ``max_span_days`` (so a 50-event window cannot silently reach back five years)."""

    label: Name
    events: PositiveInt
    max_span_days: PositiveInt


class WindowGates(LabModel):
    """A window is adequate only if all hold. Recency never substitutes for sample size."""

    min_events: PositiveInt = 20
    min_assets: PositiveInt = 3
    min_evaluable_share: Fraction = 0.8
    min_ess: PositiveInt = 20  # recency-weighted estimates (Kish effective sample size)


class LifetimeGates(LabModel):
    """The Phase 7 sample standard (``lab_evidence_policy``): 30 independent events, 3 assets."""

    min_events: PositiveInt = 30
    min_assets: PositiveInt = 3


class Weighting(LabModel):
    """Exponential recency weights w = 0.5 ** (age / half_life), age = days since the outcome
    resolved. Fixed half-lives; never optimised."""

    half_lives_days: tuple[PositiveInt, ...] = (30, 90, 180, 365)
    age_buckets_days: tuple[PositiveInt, ...] = (30, 90, 180, 365, 730)


class Inference(LabModel):
    """Uncertainty of a (weighted) mean. Co-timed cross-asset outcomes share market shocks, so
    an iid standard error is anti-conservative (Phase 18). v1 uses a cluster-robust (sandwich)
    variance with clusters = calendar blocks of the signal time, and never reports less than
    the weighted iid variance."""

    method: Literal["time_block_cluster_robust_v1"] = INFERENCE_VERSION
    block_days: PositiveInt = 7
    conservative: Literal["max(cluster, iid)"] = "max(cluster, iid)"


class Baseline(LabModel):
    """Causal excess: net minus the mean net of every eligible bar of the same asset and side
    whose outcome had resolved by the signal time, over a trailing window. Phase 4's
    full-period baseline would leak later bars into an earlier decision."""

    method: Literal["causal_trailing_baseline_v1"] = BASELINE_VERSION
    lookback_days: PositiveInt = 365
    min_bars: PositiveInt = 60


class EconomicFloor(LabModel):
    """Minimum after-cost per-event effect (fraction of notional) by holding horizon in daily
    bars. About 1.5-2x a typical round-trip fee + slippage + funding drag, so a tiny but
    statistically clean effect is not an edge."""

    by_horizon_bars: tuple[tuple[PositiveInt, Number], ...] = (
        (1, 0.0010),
        (5, 0.0025),
        (10, 0.0040),
        (20, 0.0060),
    )

    def floor(self, horizon_bars: int) -> float:
        table = dict(self.by_horizon_bars)
        if horizon_bars not in table:
            raise ValueError(f"no frozen economic floor for a {horizon_bars}-bar horizon")
        return float(table[horizon_bars])


class Classes(LabModel):
    """Per-window sign class: POS (mean >= floor and t >= pos_min_t), NEG (t <= neg_max_t),
    FLAT otherwise, NA when the window's gates fail."""

    pos_min_t: Number = 1.0
    neg_max_t: Number = -1.0
    # recent and lifetime "agree" when |recent - lifetime| <= max(floor, share x |lifetime|)
    agreement_share: Fraction = 0.5


class Recent(LabModel):
    """Which estimates are "recent" and "short". A calendar window is used when adequate;
    otherwise the event-count fallback (low-frequency strategies)."""

    recent: Name = "d180"
    recent_fallback: Name = "e50"
    short: Name = "d90"
    short_fallback: Name = "e20"
    weighted_half_life_days: PositiveInt = 90
    contemporary: Name = "d730"


class Activation(LabModel):
    """All must hold, at ``confirm_evaluations`` consecutive evaluations."""

    min_t: Number = 2.0
    min_mean: Literal["floor"] = "floor"
    weighted_min_mean: Number = 0.0  # HL-90 weighted net strictly above this
    weighted_min_ess: PositiveInt = 10
    min_positive_asset_share: Fraction = 0.5
    lifetime_min_t: Number = -2.0  # not significantly negative over all history
    min_excess: Literal["-floor"] = "-floor"  # not materially worse than random timing
    confirm_evaluations: PositiveInt = 2


class Continuation(LabModel):
    """Weaker than activation (hysteresis). Failing puts a participating strategy in DEGRADED;
    ``deactivate_after`` consecutive failures make it DORMANT. A hard failure is immediate."""

    min_mean: Number = 0.0
    min_t: Number = 0.5
    weighted_min_mean: Literal["-floor"] = "-floor"
    deactivate_after: PositiveInt = 4
    hard_fail_t: Number = -2.0


class Cusum(LabModel):
    """One-sided CUSUM on standardised **resolution-week block means** (co-timed outcomes
    share market shocks, so per-event increments are not independent). A block is processed
    once it has fully elapsed; its mean is standardised by the mean and standard deviation of
    all earlier block means (causal). While participating, the reference is the one-standard-
    error lower bound of the recent mean at activation, floored at the economic floor
    (activation selects a lucky-high window, so its point estimate overstates what to
    expect). k = 0.5, h = 5 give an in-control run length of roughly 900 blocks (about 18
    years) for Gaussian increments: the CUSUM targets material breaks; gradual decay to zero
    is left to the window rules."""

    unit: Literal["resolution_week_mean"] = "resolution_week_mean"
    block_days: PositiveInt = 7
    k: Number = 0.5
    h: Number = 5.0
    min_scale_blocks: PositiveInt = 20
    alarm_memory_days: PositiveInt = 90


class Retirement(LabModel):
    dormant_days: PositiveInt = 730
    lifetime_class: Literal["NEG"] = "NEG"


class MarketState(LabModel):
    """Coarse, causal labels at each daily close (``market_state_v1``)."""

    trend_sma: PositiveInt = 100
    trend_band: Number = 0.03
    vol_days: PositiveInt = 30
    vol_rank_lookback_days: PositiveInt = 730
    vol_rank_min_obs: PositiveInt = 365
    vol_low: Fraction = 1 / 3
    vol_high: Fraction = 2 / 3
    breadth_sma: PositiveInt = 50
    breadth_on: Fraction = 2 / 3
    breadth_off: Fraction = 1 / 3
    breadth_min_coins: PositiveInt = 3
    funding_mean_days: PositiveInt = 7
    funding_rank_lookback_days: PositiveInt = 365
    funding_rank_min_obs: PositiveInt = 180
    funding_low: Fraction = 1 / 3
    funding_high: Fraction = 2 / 3
    # current-regime similarity: exact match on these coarse dimensions
    similarity: tuple[Literal["trend", "vol", "breadth", "funding"], ...] = ("trend", "vol")
    # the regime-local lifecycle keeps one track per label of this dimension
    lifecycle_key: Literal["trend"] = "trend"
    regime_recent: Name = "d365"
    regime_recent_fallback: Name = "e50"


class Stress(LabModel):
    """Objective market-level stress (``market_stress_v1``) on the reference coin (BTC)."""

    abs_return_1d: Number = 0.10
    drawdown_7d: Number = 0.25
    vol_7d_multiple: Number = 3.0
    vol_baseline_days: PositiveInt = 365
    vol_baseline_min_obs: PositiveInt = 180
    episode_tail_days: PositiveInt = 7
    liquidation_proxy: Literal["unavailable"] = "unavailable"


class Evaluation(LabModel):
    cadence_days: PositiveInt = 7
    curve_step_days: PositiveInt = 30
    curve_window: Name = "d180"
    short_burst_days: PositiveInt = 56


class LifecyclePolicy(LabModel):
    name: Literal["lab_edge_lifecycle_policy"] = "lab_edge_lifecycle_policy"
    version: PositiveInt = 1
    methodology_version: Literal["edge_lifecycle_v1"] = METHODOLOGY_VERSION
    calendar_windows: tuple[CalendarWindow, ...] = (
        CalendarWindow(label="d30", days=30),
        CalendarWindow(label="d90", days=90),
        CalendarWindow(label="d180", days=180),
        CalendarWindow(label="d365", days=365),
        CalendarWindow(label="d730", days=730),
    )
    event_windows: tuple[EventWindow, ...] = (
        EventWindow(label="e20", events=20, max_span_days=365),
        EventWindow(label="e50", events=50, max_span_days=730),
        EventWindow(label="e100", events=100, max_span_days=1095),
    )
    gates: WindowGates = WindowGates()
    lifetime: LifetimeGates = LifetimeGates()
    weighting: Weighting = Weighting()
    inference: Inference = Inference()
    baseline: Baseline = Baseline()
    floor: EconomicFloor = EconomicFloor()
    classes: Classes = Classes()
    recent: Recent = Recent()
    activation: Activation = Activation()
    continuation: Continuation = Continuation()
    cusum: Cusum = Cusum()
    retirement: Retirement = Retirement()
    market_state: MarketState = MarketState()
    stress: Stress = Stress()
    evaluation: Evaluation = Evaluation()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        labels = [w.label for w in self.calendar_windows] + [w.label for w in self.event_windows]
        if len(labels) != len(set(labels)):
            raise ValueError("window labels must be unique")
        for ref in (self.recent.recent, self.recent.recent_fallback, self.recent.short,
                    self.recent.short_fallback, self.recent.contemporary,
                    self.market_state.regime_recent, self.market_state.regime_recent_fallback,
                    self.evaluation.curve_window):  # fmt: skip
            if ref not in labels:
                raise ValueError(f"unknown window {ref!r}")
        if self.recent.weighted_half_life_days not in self.weighting.half_lives_days:
            raise ValueError("the recent weighted half-life must be a frozen half-life")
        if self.activation.min_t <= self.classes.pos_min_t:
            raise ValueError("activation must be stricter than the POS class (hysteresis)")
        if (self.continuation.min_mean >= self.floor.floor(10)
                or self.continuation.min_t >= self.activation.min_t):  # fmt: skip
            raise ValueError("continuation must be weaker than activation (hysteresis)")
        return self

    @property
    def policy_id(self) -> str:
        return content_id("lcpolicy_", self.model_dump(mode="json"))

    def window(self, label: str) -> CalendarWindow | EventWindow:
        for w in (*self.calendar_windows, *self.event_windows):
            if w.label == label:
                return w
        raise KeyError(label)


POLICIES = {1: LifecyclePolicy()}


def policy(version: int | None = None) -> LifecyclePolicy:
    return POLICIES[version or max(POLICIES)]
