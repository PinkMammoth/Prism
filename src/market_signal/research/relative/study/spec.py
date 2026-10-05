"""The frozen Phase 18 study definition: everything decided BEFORE any outcome is computed.

A ``RelativeManifest`` (strict YAML) declares venues and per-timeframe data/event windows,
the per-venue coin universe (with explicit exclusions), the architectures (timeframe,
horizons, central parameters, one-at-a-time sensitivity axes), the frozen hypothesis
families, statistics, gates and verdict policy. Registration adds the frozen perp costs,
the retained Lab dataset IDs and the semantic versions, producing a
``RelativeStudyDefinition`` whose content hash is the ``study_id``. It is governed by the
Phase 17 study adapter (``research.lab.structure_study``): same tables, same lifecycle.

Every bounded choice is a ``Literal`` or a validated subset; the hypothesis families are
code constants (``FAMILIES``) whose membership is part of the definition, so a run cannot
add, drop or re-target a hypothesis. Results are EXPLORATORY by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from market_signal.research.lab.datasets import SeriesSelection
from market_signal.research.relative.primitives import PRIMITIVES_VERSION, REFERENCE
from market_signal.research.structure.study.spec import CostRef, DatasetRef, RegimeSpec

STUDY_VERSION = "relative_strength_v1"
EVIDENCE_CLASS = "EXPLORATORY"
AVAILABILITY_STATEMENT = (
    "Historical intraday availability is reconstructed under an explicit latency assumption "
    "rather than observed in real time."
)
EXPLORATORY_STATEMENT = (
    "Phase 18 results are EXPLORATORY: backfilled intraday history, assumed-latency "
    "availability, a six-asset universe, and data that is not untouched validation. Nothing "
    "here is validated, and no result feeds a forward tracker, co-pilot or paper account."
)

Venue = Literal["hyperliquid", "binance"]
TF = Literal["1h", "4h"]
Target = Literal["rel", "res", "usd"]
TARGETS: tuple[Target, ...] = ("rel", "res", "usd")
TARGET_MEANING = {
    "rel": "BTC-relative spread: long/short the alt against an equal notional of BTC",
    "res": "beta-neutral residual: the alt against beta(t) x notional of BTC",
    "usd": "USD-directional: the alt alone",
}
DIRECTIONS = ("continuation", "reversal")


# --------------------------------------------------------------------------- families


@dataclass(frozen=True)
class Member:
    """One preregistered hypothesis (a two-sided test; continuation and reversal are its two
    directions, read from the sign, never assumed).

    kind:
      - ``event``: edge-triggered events of ``pop`` (optionally under ``condition``); the
        statistic is the mean net excess of the continuation-oriented ``target`` over the
        matched baseline cell;
      - ``contrast``: the same population inside vs outside ``condition`` (difference of
        condition-matched excess);
      - ``ic``: mean per-timestamp Spearman of the ``pop`` ranking vs the forward ``target``;
      - ``bucket``: top/bottom stays in the top/bottom half (minus its null expectation);
      - ``spread``: long the top-ranked alt, short the bottom-ranked alt.
    """

    family: str
    name: str
    kind: Literal["event", "contrast", "ic", "bucket", "spread"]
    target: Target
    pop: str
    condition: str | None = None
    meaning: str = ""


def _ts(measure: str, target: Target, fam: str, what: str) -> tuple[Member, ...]:
    return (
        Member(fam, f"{measure}_strong", "event", target, f"{measure}+", None,
               f"{what} z >= +thr (edge) -> {TARGET_MEANING[target]}"),
        Member(fam, f"{measure}_weak", "event", target, f"{measure}-", None,
               f"{what} z <= -thr (edge) -> {TARGET_MEANING[target]}"),
    )  # fmt: skip


FAMILIES: dict[str, tuple[Member, ...]] = {
    # H1/H2 + raw-momentum and market-relative controls; target: forward BTC-relative.
    "relative_momentum": (
        *_ts("rel", "rel", "relative_momentum", "BTC-relative return"),
        *_ts("raw", "rel", "relative_momentum", "raw (unadjusted) return"),
        *_ts("mkt", "rel", "relative_momentum", "market-relative return"),
    ),
    # H3/H4 + the unadjusted BTC-relative control; target: forward beta-neutral residual.
    "residual": (
        *_ts("res", "res", "residual", "beta-adjusted residual"),
        *_ts("rel", "res", "residual", "BTC-relative return"),
        Member(
            "residual",
            "ic_res",
            "ic",
            "res",
            "res",
            meaning="Spearman(rank of residual(L), rank of forward residual): cross-sectional",
        ),
    ),
    # H5 / H6; target: forward beta-neutral residual.
    "dislocation": (
        Member(
            "dislocation",
            "breakdown",
            "event",
            "res",
            "bd",
            None,
            "corr_S(t) <= corr_W(t-S) - drop after corr_W(t-S) >= high (edge); oriented "
            "with the residual move over those S bars",
        ),
        Member(
            "dislocation",
            "tight_res_strong",
            "event",
            "res",
            "res+",
            "tight",
            "residual z >= +thr while corr_W(t-L) >= high",
        ),
        Member(
            "dislocation",
            "tight_res_weak",
            "event",
            "res",
            "res-",
            "tight",
            "residual z <= -thr while corr_W(t-L) >= high",
        ),
    ),
    # BTC consolidation and broad-pullback interactions; target: forward BTC-relative.
    "interactions": (
        Member(
            "interactions",
            "consol_rel_strong",
            "event",
            "rel",
            "rel+",
            "consolidation",
            "BTC-relative z >= +thr during BTC consolidation (vs consolidation entries)",
        ),
        Member(
            "interactions",
            "consol_rel_weak",
            "event",
            "rel",
            "rel-",
            "consolidation",
            "BTC-relative z <= -thr during BTC consolidation (vs consolidation entries)",
        ),
        Member(
            "interactions",
            "consol_vs_not_rel_strong",
            "contrast",
            "rel",
            "rel+",
            "consolidation",
            "does consolidation ADD information to rel_strong?",
        ),
        Member(
            "interactions",
            "consol_vs_not_rel_weak",
            "contrast",
            "rel",
            "rel-",
            "consolidation",
            "does consolidation ADD information to rel_weak?",
        ),
        Member(
            "interactions",
            "pullback_leader",
            "event",
            "rel",
            "top",
            "pullback",
            "alt becomes the cross-sectional leader during a broad pullback",
        ),
        Member(
            "interactions",
            "pullback_laggard",
            "event",
            "rel",
            "bottom",
            "pullback",
            "alt becomes the cross-sectional laggard during a broad pullback",
        ),
        Member(
            "interactions",
            "pullback_vs_not_leader",
            "contrast",
            "rel",
            "top",
            "pullback",
            "are leaders stronger in pullbacks than otherwise?",
        ),
        Member(
            "interactions",
            "pullback_vs_not_laggard",
            "contrast",
            "rel",
            "bottom",
            "pullback",
            "are laggards weaker in pullbacks than otherwise?",
        ),
    ),
    # Cross-sectional persistence; ranks by BTC-relative (== raw == market-relative) or
    # residual return.
    "cross_section": (
        Member(
            "cross_section",
            "ic_rel",
            "ic",
            "rel",
            "rel",
            meaning="Spearman(rank of R(L), rank of forward BTC-relative return)",
        ),
        Member(
            "cross_section",
            "top_persists",
            "bucket",
            "rel",
            "top",
            meaning="the leader finishes in the top half (minus its null expectation)",
        ),
        Member(
            "cross_section",
            "bottom_persists",
            "bucket",
            "rel",
            "bottom",
            meaning="the laggard finishes in the bottom half (minus its null expectation)",
        ),
        Member(
            "cross_section",
            "top_minus_bottom",
            "spread",
            "rel",
            "top",
            meaning="long the leader / short the laggard (4 legs of costs)",
        ),
    ),
    # Are the relative signals USD-directional trades? target: the alt alone.
    "absolute": (
        *_ts("rel", "usd", "absolute", "BTC-relative return"),
        *_ts("res", "usd", "absolute", "beta-adjusted residual"),
    ),
}
FAMILY_NAMES = tuple(FAMILIES)
POPULATIONS = ("raw+", "raw-", "rel+", "rel-", "mkt+", "mkt-", "res+", "res-", "top",
               "bottom", "bd")  # fmt: skip
CONDITIONS = ("consolidation", "pullback", "tight")


def members() -> list[Member]:
    return [m for fam in FAMILIES.values() for m in fam]


def families_spec() -> dict:
    """The family membership frozen into every definition (and its hash)."""
    return {f: [{"name": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
                 "condition": m.condition} for m in ms] for f, ms in FAMILIES.items()}  # fmt: skip


# --------------------------------------------------------------------------- parameters

AxisName = Literal[
    "lookback",  # L: relative-strength / residual lookback (bars)
    "beta_window",  # W: rolling beta / correlation window (bars)
    "z_threshold",  # thr: |z| threshold of the threshold events
    "corr_high",  # prior correlation threshold (dislocation)
    "corr_drop",  # correlation drop (dislocation)
    "corr_short",  # short correlation window S (dislocation)
]
# Axes that can change each population (sensitivity reads only these).
AXIS_POPS: dict[str, tuple[str, ...]] = {
    "lookback": ("raw+", "raw-", "rel+", "rel-", "mkt+", "mkt-", "res+", "res-", "top",
                 "bottom", "ic"),
    "beta_window": ("raw+", "raw-", "rel+", "rel-", "mkt+", "mkt-", "res+", "res-", "top",
                    "bottom", "bd", "ic"),
    "z_threshold": ("raw+", "raw-", "rel+", "rel-", "mkt+", "mkt-", "res+", "res-"),
    "corr_high": ("bd", "tight"),
    "corr_drop": ("bd",),
    "corr_short": ("bd",),
}  # fmt: skip
INT_AXES = ("lookback", "beta_window", "corr_short")


class Central(LabModel):
    """The central (preregistered default) parameters, in bars of the architecture's tf."""

    lookback: Literal[6, 18, 42] = 18
    beta_window: Literal[90, 180, 360] = 180
    z_threshold: Literal[1.5, 2.0, 2.5] = 2.0
    corr_high: Literal[0.6, 0.7, 0.8] = 0.7
    corr_drop: Literal[0.2, 0.3, 0.4] = 0.3
    corr_short: Literal[18, 42, 84] = 42
    confirm_within: Literal[18] = 18  # H6 -> breakdown confirmation window (decomposition)
    min_universe: Literal[4] = 4  # eligible alts needed for a cross-section

    def value(self, axis: str) -> float:
        return getattr(self, axis)

    def with_value(self, axis: str, v: float) -> Central:
        d = self.model_dump(mode="python")
        d[axis] = int(v) if axis in INT_AXES else float(v)
        return Central.model_validate(d)


class Axis(LabModel):
    """Three ordered values with the central value in the middle (one-at-a-time)."""

    name: AxisName
    values: Annotated[tuple[Number, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if list(self.values) != sorted(set(self.values)):
            raise ValueError(f"axis {self.name}: values must be distinct and ascending")
        return self


class Window(LabModel):
    """One venue's data and event window for one architecture. Data ``[data_start,
    event_end)`` on close time; signals at bars closing in ``[event_start, event_end)``."""

    venue: Venue
    data_start: UTCDateTime
    event_start: UTCDateTime
    event_end: UTCDateTime
    coins: Annotated[tuple[Symbol, ...], Field(min_length=2, max_length=12)]
    excluded: dict[Symbol, Text] = {}

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.data_start < self.event_start < self.event_end:
            raise ValueError("data_start < event_start < event_end is required")
        if REFERENCE not in self.coins:
            raise ValueError(f"the reference {REFERENCE} must be in every window")
        if len(set(self.coins)) != len(self.coins):
            raise ValueError("duplicate coins")
        if set(self.excluded) & set(self.coins):
            raise ValueError("a coin cannot be both used and excluded")
        return self


class Architecture(LabModel):
    """One timeframe. ``primary`` carries the sensitivity axes; ``secondary`` is a separate,
    central-only exploratory replication on a faster timeframe."""

    name: Literal["primary", "secondary"]
    timeframe: TF
    horizons: Annotated[tuple[PositiveInt, ...], Field(min_length=1, max_length=4)]
    primary_horizon: PositiveInt
    windows: Annotated[tuple[Window, ...], Field(min_length=1, max_length=2)]
    central: Central = Central()
    axes: tuple[Axis, ...] = ()
    regime: RegimeSpec = RegimeSpec()
    pullback_rule: Literal["btc_and_alt_basket_negative_over_lookback_v1"] = (
        "btc_and_alt_basket_negative_over_lookback_v1"
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.horizons)) != len(self.horizons) or list(self.horizons) != sorted(
            self.horizons
        ):
            raise ValueError("horizons must be distinct and ascending")
        if self.primary_horizon not in self.horizons:
            raise ValueError("the primary horizon must be one of the horizons")
        names = [a.name for a in self.axes]
        if len(names) != len(set(names)):
            raise ValueError("duplicate sensitivity axis")
        for a in self.axes:
            c = self.central.value(a.name)
            if a.values[1] != c:
                raise ValueError(f"axis {a.name}: the central value {c} must be the middle value")
            for v in a.values:
                self.central.with_value(a.name, v)
        if len({w.venue for w in self.windows}) != len(self.windows):
            raise ValueError("one window per venue")
        if len(self.windows) == 2:
            a, b = sorted(self.windows, key=lambda w: w.event_start)
            if a.event_end > b.event_start:
                raise ValueError("venue event windows must not overlap (no shared outcome hour)")
        return self

    def window(self, venue: str) -> Window:
        return next(w for w in self.windows if w.venue == venue)

    def variants(self) -> list[tuple[str, Central, dict]]:
        """(key, parameters, {axis: value}): the centre plus one-at-a-time neighbours."""
        out = [("central", self.central, {})]
        for a in self.axes:
            for v in a.values:
                if v != a.values[1]:
                    out.append((f"{a.name}={v:g}", self.central.with_value(a.name, v),
                                {a.name: v}))  # fmt: skip
        return out


# --------------------------------------------------------------------------- statistics


class StatisticsSpec(LabModel):
    """Inference, gates and correction (``relative_inference_v1``).

    The primary test is a two-sided block-clustered t-test of the mean excess: events are
    grouped into calendar blocks of ``primary_horizon`` bars shared by ALL assets, and the
    variance adds the adjacent-block covariance (truncated, floored at the pure cluster
    variance). Relative signals fire on several alts at once (a BTC move shows up in every
    BTC-relative spread), so an independent-draw null understates the variance; the Phase 17
    matched random-entry p is reported beside it for comparison only."""

    metric: Literal["net_excess_primary_horizon_v1"] = "net_excess_primary_horizon_v1"
    baseline: Literal["same_asset_orientation_horizon_btc_vol_tercile_eligible_v1"] = (
        "same_asset_orientation_horizon_btc_vol_tercile_eligible_v1"
    )
    test: Literal["block_clustered_t_adjacent_blocks_two_sided_v1"] = (
        "block_clustered_t_adjacent_blocks_two_sided_v1"
    )
    naive_test: Literal["matched_random_entry_mean_excess_v1"] = (
        "matched_random_entry_mean_excess_v1"
    )
    independent_events: Literal["greedy_gap_horizon_per_asset_orientation_v1"] = (
        "greedy_gap_horizon_per_asset_orientation_v1"
    )
    ic_sampling: Literal["non_overlapping_every_primary_horizon_v1"] = (
        "non_overlapping_every_primary_horizon_v1"
    )
    draws: Literal[2000] = 2000
    seed: Literal[12345] = 12345
    correction: Literal["benjamini_hochberg"] = "benjamini_hochberg"
    q: Literal[0.1] = 0.1
    min_independent_events: Literal[30] = 30
    min_assets_with_events: Literal[3] = 3
    min_evaluable_fraction: Literal[0.8] = 0.8
    min_clusters: Literal[20] = 20
    min_timestamps: Literal[100] = 100
    economic_floor: Literal[0.001] = 0.001  # 10 bps net excess at the primary horizon
    ic_floor: Literal[0.03] = 0.03
    bucket_floor: Literal[0.03] = 0.03
    min_positive_asset_share: Probability = 0.6
    cross_venue_p: Literal[0.05] = 0.05


class FundingWindow(LabModel):
    venue: Venue
    start: UTCDateTime
    end: UTCDateTime


class StudyManifest(LabModel):
    """Declarative, strict YAML. Everything the run may do is fixed here."""

    name: Name
    description: Text
    reference: Literal["BTC"] = REFERENCE
    assumed_latency_s: Literal[60.0] = 60.0
    funding: Annotated[tuple[FundingWindow, ...], Field(min_length=1, max_length=2)]
    architectures: Annotated[tuple[Architecture, ...], Field(min_length=1, max_length=2)]
    statistics: StatisticsSpec = StatisticsSpec()
    verdict_policy: Literal["phase18_verdicts_v1"] = "phase18_verdicts_v1"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        names = [a.name for a in self.architectures]
        if len(set(names)) != len(names) or "primary" not in names:
            raise ValueError("exactly one primary architecture (and at most one secondary)")
        tfs = [a.timeframe for a in self.architectures]
        if len(set(tfs)) != len(tfs):
            raise ValueError("one architecture per timeframe")
        for a in self.architectures:
            if a.name == "secondary" and a.axes:
                raise ValueError("the secondary architecture is central-only")
        fv = {f.venue: f for f in self.funding}
        if len(fv) != len(self.funding):
            raise ValueError("one funding window per venue")
        for v in self.venues:
            if v not in fv:
                raise ValueError(f"no funding window for {v}")
            ws = [a.window(v) for a in self.architectures if v in {w.venue for w in a.windows}]
            if fv[v].start > min(w.data_start for w in ws) or fv[v].end < max(
                w.event_end for w in ws
            ):
                raise ValueError(f"{v} funding must cover every data window")
        return self

    @property
    def venues(self) -> tuple[str, ...]:
        seen: list[str] = []
        for a in self.architectures:
            for w in a.windows:
                if w.venue not in seen:
                    seen.append(w.venue)
        return tuple(seen)

    def architecture(self, name: str) -> Architecture:
        return next(a for a in self.architectures if a.name == name)

    def venue_coins(self) -> list[tuple[str, str]]:
        """Every (venue, coin) that needs a retained dataset, in manifest order."""
        out: list[tuple[str, str]] = []
        for a in self.architectures:
            for w in a.windows:
                for c in w.coins:
                    if (w.venue, c) not in out:
                        out.append((w.venue, c))
        return out

    def selections(self, venue: str, coin: str) -> tuple[SeriesSelection, ...]:
        """The exact retained series of one (venue, coin): each timeframe it is used in, and
        funding. Selections end where the event window ends: nothing later is read."""
        from market_signal.models.domain import Timeframe

        out = []
        for a in self.architectures:
            for w in a.windows:
                if w.venue == venue and coin in w.coins:
                    out.append(SeriesSelection(kind="perp_intraday_bars", symbol=coin, source=venue,
                                               timeframe=Timeframe(a.timeframe), start=w.data_start,
                                               end=w.event_end))  # fmt: skip
        f = next(x for x in self.funding if x.venue == venue)
        out.append(SeriesSelection(kind="perp_funding", symbol=coin, source=venue,
                                   start=f.start, end=f.end))  # fmt: skip
        return tuple(out)


def load_manifest(path) -> StudyManifest:
    from pathlib import Path

    from market_signal.research.lab.spec import _UniqueKeyLoader

    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return StudyManifest.model_validate(raw)


# --------------------------------------------------------------------------- definition


class RelativeStudyDefinition(LabModel):
    """The statistical identity of the study. ``study_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    study_version: Literal["relative_strength_v1"] = STUDY_VERSION
    evidence_class: Literal["EXPLORATORY"] = EVIDENCE_CLASS
    availability_mode: Literal["assumed"] = "assumed"
    availability_statement: Literal[AVAILABILITY_STATEMENT] = AVAILABILITY_STATEMENT  # type: ignore[valid-type]
    validated_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    manifest: StudyManifest
    families: dict[str, list[dict]]
    costs: tuple[CostRef, ...]
    datasets: tuple[DatasetRef, ...]
    semantics: dict[str, str]

    @model_validator(mode="after")
    def complete(self) -> Self:
        need = set(self.manifest.venue_coins())
        for label, refs in (("cost", self.costs), ("dataset", self.datasets)):
            keys = [(r.venue, r.coin) for r in refs]
            if len(keys) != len(set(keys)) or set(keys) != need:
                raise ValueError(f"exactly one {label} entry per venue and coin")
        if self.families != families_spec():
            raise ValueError("family membership differs from the frozen FAMILIES")
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
    return {"study_version": STUDY_VERSION, "primitives": PRIMITIVES_VERSION,
            "inference": "relative_inference_v1", "regime": "structure_regime_v1 (Phase 17)",
            "verdicts": "phase18_verdicts_v1"}  # fmt: skip


def family_sizes() -> dict[str, int]:
    """Preregistered family sizes m per (venue, architecture), before any sample gate."""
    return {f: len(ms) for f, ms in FAMILIES.items()}


__all__ = [
    "AVAILABILITY_STATEMENT",
    "DIRECTIONS",
    "EXPLORATORY_STATEMENT",
    "FAMILIES",
    "Architecture",
    "Axis",
    "Central",
    "Member",
    "RelativeStudyDefinition",
    "StatisticsSpec",
    "StudyManifest",
    "Window",
    "families_spec",
    "family_sizes",
    "load_manifest",
    "members",
    "semantics",
]
