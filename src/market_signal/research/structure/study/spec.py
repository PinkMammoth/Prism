"""The frozen Phase 17 study definition: everything decided BEFORE any outcome is computed.

A ``StudyManifest`` (strict YAML) declares the hypotheses' ingredients: venues and their
data/event windows, architectures (timeframes, horizons, central chain parameters,
one-at-a-time sensitivity axes), the regime/volatility vocabulary, the statistics, gates,
correction families and verdict policy. Registration adds the frozen cost assumptions
(from Prism's perp cost machinery), the retained Lab dataset IDs and the semantic
versions, producing a ``StudyDefinition`` whose content hash is the ``study_id``.

Every bounded choice is a ``Literal`` or a validated subset, so a definition cannot drift
into an unbounded search. Results are EXPLORATORY by construction (``evidence_class`` and
``validated_reachable`` are literals), and nothing here names a consumer.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)
from market_signal.research.structure.registry import (
    HORIZONS,
    REGISTRY_VERSION,
    BreachParams,
    BreakoutParams,
    ChainSpec,
    ClusterParams,
    PriorExtremeParams,
    RejectionParams,
    RetestParams,
    StructureShiftParams,
    SwingLevelParams,
    SwingParams,
    Tolerance,
)

STUDY_VERSION = "structure_falsification_v1"
EVIDENCE_CLASS = "EXPLORATORY"
AVAILABILITY_STATEMENT = (
    "Historical intraday availability is reconstructed under an explicit latency assumption "
    "rather than observed in real time."
)
EXPLORATORY_STATEMENT = (
    "Phase 17 results are EXPLORATORY: backfilled intraday history, assumed-latency "
    "availability, and data that is not untouched validation. Nothing here is validated, "
    "and no result feeds a forward tracker, co-pilot or paper account."
)

Venue = Literal["hyperliquid", "binance"]
TF = Literal["15m", "1h", "4h"]
LevelKind = Literal["swing", "prior_extreme", "cluster"]
LEVEL_KINDS: tuple[LevelKind, ...] = ("swing", "prior_extreme", "cluster")
Direction = Literal["reversal", "continuation"]
DIRECTIONS: tuple[Direction, ...] = ("reversal", "continuation")

# The ladder: each rung is a restriction of the previous one, entered only once it is known.
LADDER = ("A_breach", "B_failed", "C_rejection", "D_shift", "E_retest")
HELD = "H_held"  # first-class comparator: the same breaches whose breakout held
STRETCH = "M_stretch"  # simple mean-reversion/momentum control without any level narrative
RUNGS = (*LADDER, HELD, STRETCH)
# Rungs with an objective ex-ante invalidation for the REVERSAL direction (the swept
# extreme; for A the breach bar's own extreme, the only part known at the breach).
R_RUNGS = (*LADDER, HELD)
SUBGROUP_RUNGS = ("A_breach", "B_failed", HELD)
SUBGROUP_CELLS = ("range", "with_trend", "counter_trend", "vol_low", "vol_mid", "vol_high")

# --------------------------------------------------------------------------- axes

AxisName = Literal[
    "level_swing",  # swing levels: left = right bars of the structure-timeframe pivot
    "prior_n",  # prior-extreme levels: n structure bars
    "cluster_tol_atr",  # cluster levels: tolerance in structure ATR
    "overshoot_atr",  # breach: minimum overshoot in event-timeframe prior ATR
    "failure_window",  # breakout outcome window (event bars)
    "rejection_wick",  # rejection: min wick / range on the swept side
    "shift_window",  # structure shift: max confirmation bars
    "shift_swing",  # structure shift: left = right bars of the confirmation swings
    "retest_tol_atr",  # retest tolerance in confirmation ATR
    "retest_max_delay",  # retest: max confirmation bars
]
# The kinds an axis applies to, and the first rung whose events it can change.
AXIS_KINDS: dict[str, tuple[str, ...]] = {
    "level_swing": ("swing",),
    "prior_n": ("prior_extreme",),
    "cluster_tol_atr": ("cluster",),
}
AXIS_RUNG: dict[str, str] = {
    "level_swing": "A_breach", "prior_n": "A_breach", "cluster_tol_atr": "A_breach",
    "overshoot_atr": "A_breach", "failure_window": "B_failed", "rejection_wick": "C_rejection",
    "shift_window": "D_shift", "shift_swing": "D_shift", "retest_tol_atr": "E_retest",
    "retest_max_delay": "E_retest",
}  # fmt: skip


def axes_for(kind: str, rung: str) -> list[str]:
    """Axes that can change the events of ``rung`` for level ``kind`` (H follows B's)."""
    order = {r: i for i, r in enumerate(LADDER)}
    target = order["B_failed" if rung == HELD else rung]
    return [
        a for a, r in AXIS_RUNG.items()
        if order[r] <= target and kind in AXIS_KINDS.get(a, (kind,))
    ]  # fmt: skip


class CentralChain(LabModel):
    """The central (preregistered default) chain parameters for every level kind."""

    swing: SwingLevelParams = SwingLevelParams()
    prior_extreme: PriorExtremeParams = PriorExtremeParams()
    cluster: ClusterParams = ClusterParams()
    min_excursion_bps: Literal[0.0, 5.0, 10.0, 25.0] = 0.0
    min_excursion_atr: Literal[0.0, 0.1, 0.25, 0.5] = 0.1
    failure_window: Literal[0, 1, 2, 3, 5] = 2
    rejection: RejectionParams = RejectionParams()
    structure_shift: StructureShiftParams = StructureShiftParams()
    retest: RetestParams = RetestParams()

    @model_validator(mode="after")
    def same_bar_rejection(self) -> Self:
        # within_bars = 0 means rejection is decided on bars up to the failure bar, so C is
        # known exactly when B is: C and B share an executable entry, which is what makes
        # the "rejection vs no rejection" contrast a clean, disjoint, same-entry comparison.
        if self.rejection.within_bars != 0:
            raise ValueError("Phase 17 v1 freezes rejection within_bars = 0")
        if self.cluster.tolerance.method != "atr" or self.retest.tolerance.method != "atr":
            raise ValueError("cluster and retest tolerances are ATR-based in v1")
        return self

    def value(self, axis: str) -> float:
        return {
            "level_swing": self.swing.swing.left, "prior_n": self.prior_extreme.n,
            "cluster_tol_atr": self.cluster.tolerance.value,
            "overshoot_atr": self.min_excursion_atr, "failure_window": self.failure_window,
            "rejection_wick": self.rejection.min_wick_range,
            "shift_window": self.structure_shift.window,
            "shift_swing": self.structure_shift.swing.left,
            "retest_tol_atr": self.retest.tolerance.value,
            "retest_max_delay": self.retest.max_delay,
        }[axis]  # fmt: skip

    def with_value(self, axis: str, v: float) -> CentralChain:
        """A copy with one axis moved (validated through the registry's bounded params)."""
        d = self.model_dump(mode="python")
        iv = int(v) if float(v).is_integer() else v
        if axis == "level_swing":
            d["swing"]["swing"] = {"left": iv, "right": iv}
        elif axis == "prior_n":
            d["prior_extreme"]["n"] = iv
        elif axis == "cluster_tol_atr":
            d["cluster"]["tolerance"] = {"method": "atr", "value": float(v)}
        elif axis == "overshoot_atr":
            d["min_excursion_atr"] = float(v)
        elif axis == "failure_window":
            d["failure_window"] = iv
        elif axis == "rejection_wick":
            d["rejection"]["min_wick_range"] = float(v)
        elif axis == "shift_window":
            d["structure_shift"]["window"] = iv
        elif axis == "shift_swing":
            d["structure_shift"]["swing"] = {"left": iv, "right": iv}
        elif axis == "retest_tol_atr":
            d["retest"]["tolerance"] = {"method": "atr", "value": float(v)}
        elif axis == "retest_max_delay":
            d["retest"]["max_delay"] = iv
        else:  # pragma: no cover - AxisName is a Literal
            raise ValueError(axis)
        return CentralChain.model_validate(d)

    def level(self, kind: str):
        return {"swing": self.swing, "prior_extreme": self.prior_extreme,
                "cluster": self.cluster}[kind]  # fmt: skip


class Axis(LabModel):
    """One sensitivity axis: three ordered values with the central value in the middle, so
    each neighbour is exactly one step from the centre (one-at-a-time, never a grid)."""

    name: AxisName
    values: Annotated[tuple[Number, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if list(self.values) != sorted(set(self.values)):
            raise ValueError(f"axis {self.name}: values must be distinct and ascending")
        return self


# --------------------------------------------------------------------------- vocabulary


class RegimeSpec(LabModel):
    """Frozen regime/volatility vocabulary, measured on the STRUCTURE series and read at the
    entry instant from the newest structure bar already available (no classifier).

    - trend direction: sign of close - EMA(``trend_ema``);
    - range: efficiency ratio ER(``er_n``) below its random-walk expectation 1/sqrt(n)
      (a window no more directional than noise); takes precedence over trend labels;
    - with_trend / counter_trend: the trade direction equals / opposes the trend direction;
    - volatility: ATR(14)/close ranked within the trailing ``vol_rank_days`` of structure
      bars (causal percentile), split into terciles low / mid / high.
    """

    trend_ema: Literal[50] = 50
    er_n: Literal[20] = 20
    range_rule: Literal["er_below_random_walk_expectation_v1"] = (
        "er_below_random_walk_expectation_v1"
    )
    vol_measure: Literal["atr14_over_close"] = "atr14_over_close"
    vol_rank_days: Literal[30] = 30
    vol_buckets: Literal["terciles"] = "terciles"


class StretchControl(LabModel):
    """``M_stretch``: the first event bar whose ``lookback``-bar close change reaches
    ``threshold_atr`` x prior ATR (down = low side, up = high side). A plain price-extension
    control with no level, failure or structure narrative."""

    lookback_bars: Literal[4] = 4
    threshold_atr: Literal[2.0] = 2.0


class VenueWindow(LabModel):
    venue: Venue
    event_start: UTCDateTime
    event_end: UTCDateTime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.event_start >= self.event_end:
            raise ValueError("event window start must precede its end")
        return self


class Architecture(LabModel):
    """One timeframe architecture. ``primary`` carries the confirmatory families and the
    sensitivity axes; ``secondary`` is a separate, smaller exploratory family."""

    name: Literal["primary", "secondary"]
    structure_tf: TF
    event_tf: TF
    confirm_tf: TF
    resolve_tf: Literal["15m"] | None = None
    horizons: Annotated[tuple[PositiveInt, ...], Field(min_length=1, max_length=4)]
    primary_horizon: PositiveInt
    windows: Annotated[tuple[VenueWindow, ...], Field(min_length=1, max_length=2)]
    central: CentralChain = CentralChain()
    axes: tuple[Axis, ...] = ()
    stretch_control: StretchControl | None = None
    subgroups: bool = False
    regime: RegimeSpec = RegimeSpec()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        ChainSpec(venue="v", structure_tf=self.structure_tf, event_tf=self.event_tf,
                  confirm_tf=self.confirm_tf)  # timeframe order  # fmt: skip
        if any(h not in HORIZONS for h in self.horizons) or len(set(self.horizons)) != len(
            self.horizons
        ):
            raise ValueError(f"horizons must be distinct values from {HORIZONS}")
        if self.primary_horizon not in self.horizons:
            raise ValueError("the primary horizon must be one of the horizons")
        if self.resolve_tf is not None and self.resolve_tf == self.event_tf:
            raise ValueError("ambiguity resolution needs a faster timeframe than the events")
        names = [a.name for a in self.axes]
        if len(names) != len(set(names)):
            raise ValueError("duplicate sensitivity axis")
        for a in self.axes:
            c = self.central.value(a.name)
            if a.values[1] != c:
                raise ValueError(f"axis {a.name}: the central value {c} must be the middle value")
            for v in a.values:
                self.central.with_value(a.name, v)  # every value must be expressible
        if len({w.venue for w in self.windows}) != len(self.windows):
            raise ValueError("one event window per venue")
        return self

    def window(self, venue: str) -> VenueWindow:
        return next(w for w in self.windows if w.venue == venue)

    def variants(self, kind: str) -> list[tuple[str, CentralChain, dict]]:
        """(variant key, chain parameters, {axis: value} moved) for one level kind:
        the central chain plus one-at-a-time neighbours on the axes that apply to it."""
        out = [("central", self.central, {})]
        for a in self.axes:
            if kind not in AXIS_KINDS.get(a.name, (kind,)):
                continue
            for v in a.values:
                if v != a.values[1]:
                    out.append((f"{a.name}={v:g}", self.central.with_value(a.name, v),
                                {a.name: v}))  # fmt: skip
        return out

    def chain_spec(self, venue: str, kind: str, chain: CentralChain) -> ChainSpec:
        return ChainSpec(
            venue=venue, structure_tf=self.structure_tf, event_tf=self.event_tf,
            confirm_tf=self.confirm_tf,
            breakout=BreakoutParams(
                breach=BreachParams(level=chain.level(kind),
                                    min_excursion_bps=chain.min_excursion_bps,
                                    min_excursion_atr=chain.min_excursion_atr),
                failure_window=chain.failure_window),
            rejection=chain.rejection, structure_shift=chain.structure_shift,
            retest=chain.retest,
        )  # fmt: skip


# --------------------------------------------------------------------------- data windows


class VenueData(LabModel):
    """Retained-snapshot windows for one venue: each series selects ``[start, end)`` on
    close time (funding: availability). Starts precede the event windows (warmup); every
    series ends at ``end``, so no bar after it is ever read."""

    venue: Venue
    start_4h: UTCDateTime
    start_1h: UTCDateTime
    start_15m: UTCDateTime
    start_funding: UTCDateTime
    end: UTCDateTime

    def start(self, tf: str) -> object:
        return {"4h": self.start_4h, "1h": self.start_1h, "15m": self.start_15m,
                "funding": self.start_funding}[tf]  # fmt: skip


# --------------------------------------------------------------------------- statistics


class StatisticsSpec(LabModel):
    """Tests, gates and correction. One-sided tests use the matched random-entry null;
    contrasts use label permutation; intervals use the percentile bootstrap."""

    metric: Literal["net_excess_primary_horizon_v1"] = "net_excess_primary_horizon_v1"
    baseline: Literal["same_asset_direction_vol_tercile_eligible_v1"] = (
        "same_asset_direction_vol_tercile_eligible_v1"
    )
    test: Literal["matched_random_entry_mean_excess_v1"] = "matched_random_entry_mean_excess_v1"
    contrast_test: Literal["label_permutation_mean_difference_v1"] = (
        "label_permutation_mean_difference_v1"
    )
    independent_events: Literal["greedy_gap_horizon_per_asset_direction_v1"] = (
        "greedy_gap_horizon_per_asset_direction_v1"
    )
    draws: Literal[2000] = 2000
    permutations: Literal[4000] = 4000
    bootstrap: Literal[2000] = 2000
    seed: Literal[12345] = 12345
    correction: Literal["benjamini_hochberg"] = "benjamini_hochberg"
    q: Literal[0.1] = 0.1
    min_independent_events: Literal[30] = 30
    min_assets_with_events: Literal[3] = 3
    min_evaluable_fraction: Literal[0.8] = 0.8
    economic_floor: Literal[0.001] = 0.001  # 10 bps net excess at the primary horizon
    min_positive_asset_share: Probability = 0.6
    cross_venue_p: Literal[0.05] = 0.05


class FamiliesSpec(LabModel):
    """What one BH correction family is. Families never span venues or architectures."""

    primary: Literal["per_venue_architecture_rung_x_level_x_direction_plus_controls_v1"] = (
        "per_venue_architecture_rung_x_level_x_direction_plus_controls_v1"
    )
    contrasts: Literal["per_venue_failed_vs_held_and_rejection_split_two_sided_v1"] = (
        "per_venue_failed_vs_held_and_rejection_split_two_sided_v1"
    )
    subgroups: Literal["per_venue_regime_volatility_cells_A_B_H_reversal_two_sided_v1"] = (
        "per_venue_regime_volatility_cells_A_B_H_reversal_two_sided_v1"
    )
    sensitivity: Literal["descriptive_only_never_tested"] = "descriptive_only_never_tested"


class StudyManifest(LabModel):
    """Declarative, strict YAML. Everything the run may do is fixed here."""

    name: Name
    description: Text
    coins: Annotated[tuple[Symbol, ...], Field(min_length=1, max_length=12)]
    assumed_latency_s: Literal[60.0] = 60.0
    venues: Annotated[tuple[VenueData, ...], Field(min_length=1, max_length=2)]
    architectures: Annotated[tuple[Architecture, ...], Field(min_length=1, max_length=2)]
    statistics: StatisticsSpec = StatisticsSpec()
    families: FamiliesSpec = FamiliesSpec()
    verdict_policy: Literal["phase17_verdicts_v1"] = "phase17_verdicts_v1"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.coins)) != len(self.coins):
            raise ValueError("duplicate coins")
        venues = {v.venue: v for v in self.venues}
        if len(venues) != len(self.venues):
            raise ValueError("one data block per venue")
        names = [a.name for a in self.architectures]
        if len(set(names)) != len(names) or "primary" not in names:
            raise ValueError("exactly one primary architecture (and at most one secondary)")
        for arch in self.architectures:
            if arch.name == "secondary" and (arch.axes or arch.subgroups):
                raise ValueError("the secondary architecture is central-only, no subgroups")
            for w in arch.windows:
                if w.venue not in venues:
                    raise ValueError(f"no data block for venue {w.venue}")
                d = venues[w.venue]
                for tf in {arch.structure_tf, arch.event_tf, arch.confirm_tf}:
                    if not d.start(tf) < w.event_start:
                        raise ValueError(f"{w.venue} {tf} data must start before the event window")
                if w.event_end != d.end:
                    raise ValueError("event windows end where the retained data ends")
        # venues are never pooled; Phase 11's rule keeps their outcome hours disjoint
        prim = next(a for a in self.architectures if a.name == "primary")
        if len(prim.windows) == 2:
            a, b = sorted(prim.windows, key=lambda w: w.event_start)
            if a.event_end > b.event_start:
                raise ValueError("venue event windows must not overlap (no shared outcome hour)")
        return self

    def architecture(self, name: str) -> Architecture:
        return next(a for a in self.architectures if a.name == name)


def load_manifest(path) -> StudyManifest:
    from pathlib import Path

    from market_signal.research.lab.spec import _UniqueKeyLoader

    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return StudyManifest.model_validate(raw)


# --------------------------------------------------------------------------- definition


class CostRef(LabModel):
    venue: Venue
    coin: Symbol
    fee_bps: Annotated[Number, Field(ge=0, lt=1000)]
    slippage_bps: Annotated[Number, Field(ge=0, lt=1000)]

    @property
    def per_side(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 1e4


class DatasetRef(LabModel):
    venue: Venue
    coin: Symbol
    dataset_id: Annotated[str, Field(pattern=r"^dataset_[0-9a-f]{64}$")]


class StudyDefinition(LabModel):
    """The statistical identity of the study. ``study_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    study_version: Literal["structure_falsification_v1"] = STUDY_VERSION
    evidence_class: Literal["EXPLORATORY"] = EVIDENCE_CLASS
    availability_mode: Literal["assumed"] = "assumed"
    availability_statement: Literal[AVAILABILITY_STATEMENT] = AVAILABILITY_STATEMENT  # type: ignore[valid-type]
    validated_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    manifest: StudyManifest
    costs: tuple[CostRef, ...]
    datasets: tuple[DatasetRef, ...]
    semantics: dict[str, str]

    @model_validator(mode="after")
    def complete(self) -> Self:
        need = {(v.venue, c) for v in self.manifest.venues for c in self.manifest.coins}
        for label, refs in (("cost", self.costs), ("dataset", self.datasets)):
            keys = [(r.venue, r.coin) for r in refs]
            if len(keys) != len(set(keys)) or set(keys) != need:
                raise ValueError(f"exactly one {label} entry per venue and coin")
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        data["costs"] = sorted(data["costs"], key=canonical_json)
        data["datasets"] = sorted(data["datasets"], key=canonical_json)
        return canonical_json(data)

    @property
    def study_id(self) -> str:
        return content_id("sstudy_", json.loads(self.canonical_json()))

    def cost(self, venue: str, coin: str) -> CostRef:
        return next(c for c in self.costs if c.venue == venue and c.coin == coin)

    def dataset(self, venue: str, coin: str) -> str:
        return next(d.dataset_id for d in self.datasets if d.venue == venue and d.coin == coin)


def semantics() -> dict[str, str]:
    """Versions whose change makes results incomparable (frozen into every definition)."""
    from market_signal.research.structure.manifest import BUILDER

    return {"study_version": STUDY_VERSION, "structure_registry": REGISTRY_VERSION,
            "structure_builder": BUILDER, "trade_path": "trade_path_v1"}  # fmt: skip


def family_sizes(arch: Architecture, n_kinds: int = len(LEVEL_KINDS)) -> dict[str, int]:
    """Preregistered family sizes m (before any sample gate removes untestable members)."""
    primary = n_kinds * (len(LADDER) + 1) * len(DIRECTIONS)
    if arch.stretch_control is not None:
        primary += len(DIRECTIONS)
    out = {"primary": primary}
    if arch.name == "primary":
        out["contrasts"] = n_kinds * 2
    if arch.subgroups:
        out["subgroups"] = n_kinds * len(SUBGROUP_RUNGS) * len(SUBGROUP_CELLS)
    return out


def swing(left: int) -> SwingParams:
    return SwingParams(left=left, right=left)


def atr_tol(v: float) -> Tolerance:
    return Tolerance(method="atr", value=v)


__all__ = [
    "AVAILABILITY_STATEMENT",
    "EXPLORATORY_STATEMENT",
    "LADDER",
    "LEVEL_KINDS",
    "RUNGS",
    "Architecture",
    "Axis",
    "CentralChain",
    "CostRef",
    "DatasetRef",
    "StudyDefinition",
    "StudyManifest",
    "VenueData",
    "family_sizes",
    "load_manifest",
    "semantics",
]  # fmt: skip
