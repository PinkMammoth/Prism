"""Primitive registry: versioned definitions with small, bounded parameter sets.

Every primitive is identified by ``(name, version)``. A version pins the exact definition in
docs/STRUCTURE.md; changing a definition means adding a new version, never editing an old
one. Parameters are pydantic models whose fields accept only an explicit allowed set
(``Literal``), so a spec cannot drift into an unbounded search space and two spellings of
one parameterisation cannot exist. Phase 17 preregisters which combinations it tests; this
module only bounds what is expressible.

No pandas here: specs can be validated and hashed without importing calculation code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator

from market_signal.research.lab.common import LabModel, canonical_json, content_id

REGISTRY_VERSION = "structure_primitives_v1"

# The ATR used to normalise distances: Wilder ATR(14) of the bar series the measurement is
# made on, taken from the bar BEFORE the measured bar (so a bar's own range never inflates
# the unit it is measured in). Part of every v1 definition below.
NORMALISER = "atr_wilder_14_prior"
ATR_N = 14
# rel_volume in event records: bar volume / mean volume of the 20 bars before it.
REL_VOLUME_N = 20

Side = Literal["high", "low"]

BPS_TOLERANCES = (5.0, 10.0, 20.0, 30.0)
ATR_TOLERANCES = (0.1, 0.2, 0.3, 0.5)


class Tolerance(LabModel):
    """A price tolerance: ``value`` bps of the level price, or ``value`` x prior ATR."""

    method: Literal["bps", "atr"]
    value: float

    @model_validator(mode="after")
    def bounded(self) -> Self:
        allowed = BPS_TOLERANCES if self.method == "bps" else ATR_TOLERANCES
        if self.value not in allowed:
            raise ValueError(f"{self.method} tolerance must be one of {allowed}")
        return self


# --------------------------------------------------------------------------- state layer


class SwingParams(LabModel):
    """``swing_v1``: N bars left (strict), N bars right (non-strict), confirmed after right."""

    left: Literal[1, 2, 3, 5] = 2
    right: Literal[1, 2, 3, 5] = 2


class SwingLevelParams(LabModel):
    """A confirmed swing used as a reference level until breached or ``max_age_bars`` old."""

    kind: Literal["swing"] = "swing"
    swing: SwingParams = SwingParams()
    max_age_bars: Literal[50, 100, 200, 500] = 200


class PriorExtremeParams(LabModel):
    """``prior_extreme_v1``: the max high / min low of the ``n`` bars before the current bar
    (the Lab's ``donchian_high_n`` / ``donchian_low_n``), as a level with an origin bar."""

    kind: Literal["prior_extreme"] = "prior_extreme"
    n: Literal[10, 20, 50, 100, 200] = 20


class ClusterParams(LabModel):
    """``level_cluster_v1``: confirmed swings within ``tolerance`` of each other with no
    higher high (lower low) between them; versioned snapshots, never regrouped."""

    kind: Literal["cluster"] = "cluster"
    swing: SwingParams = SwingParams()
    tolerance: Tolerance = Tolerance(method="atr", value=0.2)
    max_age_bars: Literal[50, 100, 200, 500] = 200


LevelParams = Annotated[
    SwingLevelParams | PriorExtremeParams | ClusterParams, Field(discriminator="kind")
]


class TouchParams(LabModel):
    """A touch: the bar's extreme comes within ``tolerance`` of an intact level. Consecutive
    bars inside the zone are ONE touch; a new touch needs a bar fully outside the zone."""

    tolerance: Tolerance = Tolerance(method="atr", value=0.1)


# --------------------------------------------------------------------------- event layer


class BreachParams(LabModel):
    """``level_breach_v1``: the first bar whose high exceeds an eligible level (low side
    mirrored). It is an event only if the overshoot is at least BOTH minimums."""

    level: LevelParams = SwingLevelParams()
    min_excursion_bps: Literal[0.0, 5.0, 10.0, 25.0] = 0.0
    min_excursion_atr: Literal[0.0, 0.1, 0.25, 0.5] = 0.1


class BreakoutParams(LabModel):
    """``breakout_outcome_v1`` / ``failed_breakout_v1``: classify a breach by whether price
    closed back inside the level within ``failure_window`` bars (0 = same bar only)."""

    breach: BreachParams = BreachParams()
    failure_window: Literal[0, 1, 2, 3, 5] = 2


class RejectionParams(LabModel):
    """``rejection_v1`` (thresholded event over continuous metrics): the first bar from the
    breach bar to ``within_bars`` after the failure bar whose wick on the swept side is at
    least ``min_wick_range`` of its range."""

    min_wick_range: Literal[0.33, 0.5, 0.66] = 0.5
    within_bars: Literal[0, 1, 2] = 0


class StructureShiftParams(LabModel):
    """``structure_shift_v1``: after a failed breakout, a break of the latest intact
    opposite swing that was already confirmed when the failure was known."""

    swing: SwingParams = SwingParams(left=2, right=2)
    window: Literal[5, 10, 20, 50] = 20
    break_on: Literal["close", "wick"] = "close"
    min_excursion_atr: Literal[0.0, 0.5, 1.0] = 0.0


class RetestParams(LabModel):
    """``retest_v1``: after a level is broken, the first return to within ``tolerance``."""

    tolerance: Tolerance = Tolerance(method="atr", value=0.2)
    max_delay: Literal[5, 10, 20] = 10


# --------------------------------------------------------------------------- path layer

HORIZONS = (1, 2, 4, 8, 16, 24, 48, 96)
PCT_LEVELS = (0.5, 1.0, 2.0, 3.0, 5.0)
ATR_LEVELS = (0.5, 1.0, 2.0, 3.0)
R_TARGETS = (1.0, 2.0, 3.0, 5.0)


def _subset(values: tuple, allowed: tuple, what: str) -> tuple:
    if not values or len(set(values)) != len(values) or any(v not in allowed for v in values):
        raise ValueError(f"{what} must be distinct values from {allowed}")
    return tuple(sorted(values))


class TradePathParams(LabModel):
    """``trade_path_v1``: excursions over fixed horizons from the next bar open after an
    event is available. Threshold levels are descriptive, never optimised."""

    horizons: tuple[int, ...] = (4, 16, 48)
    pct_levels: tuple[float, ...] = (1.0, 2.0)
    atr_levels: tuple[float, ...] = (1.0, 2.0)
    r_targets: tuple[float, ...] = R_TARGETS

    @field_validator("horizons", "pct_levels", "atr_levels", "r_targets")
    @classmethod
    def bounded(cls, value: tuple, info: ValidationInfo) -> tuple:
        allowed = {"horizons": HORIZONS, "pct_levels": PCT_LEVELS, "atr_levels": ATR_LEVELS,
                   "r_targets": R_TARGETS}[info.field_name]  # fmt: skip
        return _subset(value, allowed, info.field_name)


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Primitive:
    name: str
    version: str
    layer: Literal["state", "event", "path"]
    params: type[LabModel] | None
    meaning: str


PRIMITIVES: dict[str, Primitive] = {
    p.version: p
    for p in (
        Primitive("swing", "swing_v1", "state", SwingParams,
                  "pivot high/low: strict left, non-strict right; known after the right bars"),
        Primitive("prior_extreme", "prior_extreme_v1", "state", PriorExtremeParams,
                  "max high / min low of the n bars before the current bar, with origin bar"),
        Primitive("level_cluster", "level_cluster_v1", "state", ClusterParams,
                  "versioned snapshots of confirmed swings within a tolerance (equal highs/lows)"),
        Primitive("level_touch", "level_touch_v1", "state", TouchParams,
                  "separated touches of an intact level; consecutive in-zone bars are one touch"),
        Primitive("rejection_metrics", "rejection_metrics_v1", "state", None,
                  "continuous per-bar wick/body/close-location/range/volume measurements"),
        Primitive("efficiency_ratio", "efficiency_ratio_v1", "state", None,
                  "|close_t - close_t-n| / sum of |close changes| over the same n bars"),
        Primitive("level_breach", "level_breach_v1", "event", BreachParams,
                  "first bar exceeding an eligible level by the minimum overshoot"),
        Primitive("breakout_outcome", "breakout_outcome_v1", "event", BreakoutParams,
                  "FAILED / HELD / UNRESOLVED / GAP within the failure window"),
        Primitive("failed_breakout", "failed_breakout_v1", "event", BreakoutParams,
                  "a breach whose close returned inside the level within the window ('sweep')"),
        Primitive("rejection", "rejection_v1", "event", RejectionParams,
                  "wick-on-the-swept-side threshold on a failed breakout"),
        Primitive("structure_shift", "structure_shift_v1", "event", StructureShiftParams,
                  "break of the latest intact opposite swing known when the failure was known"),
        Primitive("retest", "retest_v1", "event", RetestParams,
                  "first return to within tolerance of a broken level"),
        Primitive("trade_path", "trade_path_v1", "path", TradePathParams,
                  "MFE/MAE (close and intrabar), thresholds, R, explicit OHLC ambiguity"),
    )
}  # fmt: skip


def primitive(version: str) -> Primitive:
    if version not in PRIMITIVES:
        raise ValueError(f"unknown primitive version {version!r}; registry {REGISTRY_VERSION}")
    return PRIMITIVES[version]


def params_key(params: LabModel | None) -> dict:
    """Canonical, JSON-able parameters (defaults resolved) for identities and manifests."""
    return {} if params is None else params.model_dump(mode="json")


def spec_id(version: str, params: LabModel | None) -> str:
    """Identity of one primitive parameterisation."""
    p = primitive(version)
    if p.params is not None and not isinstance(params, p.params):
        raise ValueError(f"{version} takes {p.params.__name__}")
    return content_id("sprim_", {"primitive": version, "params": params_key(params)})


# --------------------------------------------------------------------------- chains


class ChainSpec(LabModel):
    """A declarative confirmation chain over shared detectors (no bespoke strategy code).

    Stages are optional and cumulative in the fixed order breach -> failed breakout ->
    rejection -> structure shift -> retest; each stage keeps its own event identity and
    references its causal predecessor. ``ablation`` names the deepest stage present, so
    Phase 17's A..E ladder is a list of ChainSpecs differing only by truncation.

    Timeframes: ``structure_tf`` forms the levels, ``event_tf`` detects the breach and its
    outcome, ``confirm_tf`` the shift and retest. Each must be at least as slow as the next.
    """

    venue: Annotated[str, Field(min_length=1, max_length=40)]
    structure_tf: Literal["15m", "1h", "4h", "1d"]
    event_tf: Literal["15m", "1h", "4h", "1d"]
    confirm_tf: Literal["15m", "1h", "4h", "1d"]
    breakout: BreakoutParams = BreakoutParams()
    rejection: RejectionParams | None = None
    structure_shift: StructureShiftParams | None = None
    retest: RetestParams | None = None
    failed_only: bool = True  # False keeps stage A (any qualifying breach)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        secs = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        if not secs[self.structure_tf] >= secs[self.event_tf] >= secs[self.confirm_tf]:
            raise ValueError("timeframes must satisfy structure >= event >= confirmation")
        if not self.failed_only and (self.rejection or self.structure_shift or self.retest):
            raise ValueError("later stages require the failed-breakout stage")
        if self.retest is not None and self.structure_shift is None:
            raise ValueError("retest_v1 in a chain retests the structure-shift level")
        return self

    @property
    def ablation(self) -> str:
        if not self.failed_only:
            return "A_breach"
        if self.retest is not None:
            return "E_retest" if self.rejection else "E_retest_no_rejection"
        if self.structure_shift is not None:
            return "D_structure_shift" if self.rejection else "D_shift_no_rejection"
        return "C_rejection" if self.rejection else "B_failed_breakout"

    @property
    def chain_id(self) -> str:
        return content_id("schain_", {"registry": REGISTRY_VERSION,
                                      "spec": self.model_dump(mode="json")})  # fmt: skip


def catalog() -> list[dict]:
    """Data-only description of the registry (versions, layers, parameter schemas)."""
    return [
        {
            "primitive": p.name,
            "version": p.version,
            "layer": p.layer,
            "meaning": p.meaning,
            "params": None if p.params is None else p.params.model_json_schema(),
        }
        for p in PRIMITIVES.values()
    ]


def canonical(params: LabModel) -> str:
    return canonical_json(params.model_dump(mode="json"))
