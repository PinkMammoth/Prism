"""The frozen Phase 19 study definition: everything decided BEFORE any outcome is computed.

An ``OiStudyManifest`` (strict YAML) declares the primary venue's data/event window and
coins (with explicit exclusions), the deferred comparison venue and its coverage gate, the
availability assumption, the central parameters and one-at-a-time sensitivity axes, the
horizons (exactly one primary), statistics, gates and verdict policy. Registration adds the
frozen perp costs, the retained Lab dataset IDs and the semantic versions, producing an
``OiStudyDefinition`` whose content hash is the ``study_id``. It is governed by the Phase 17
study adapter (``research.lab.structure_study``): same tables, same lifecycle.

The hypothesis families are code constants (``FAMILIES``) whose membership is hashed into
the definition, so a run cannot add, drop or re-target a hypothesis. No member encodes a
folklore direction: each is ONE two-sided test whose sign is read afterwards
(continuation/reversal for price-oriented members, up/down for unconditional long-oriented
members, larger/smaller for the volatility family). Results are EXPLORATORY by construction.
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
from market_signal.research.oiprice.primitives import AVAILABILITY_VERSION, PRIMITIVES_VERSION
from market_signal.research.structure.study.spec import CostRef, DatasetRef, RegimeSpec

STUDY_VERSION = "oi_price_v1"
EVIDENCE_CLASS = "EXPLORATORY"
AVAILABILITY_STATEMENT = (
    "Historical OI availability is reconstructed under an explicit latency assumption rather "
    "than observed in real time: Binance OI statistics were backfilled, so their publication "
    "time is assumed (observed_at + latency), never observed."
)
EXPLORATORY_STATEMENT = (
    "Phase 19 results are EXPLORATORY: about four weeks of backfilled Binance OI in one market "
    "regime, assumed-latency availability, a six-asset universe, and no untouched validation "
    "data. Nothing here is validated, and no result feeds a forward tracker, co-pilot or paper "
    "account. OI contraction is never labelled liquidation: Prism holds no liquidation data."
)

Venue = Literal["binance", "hyperliquid"]
Target = Literal["dir", "absret"]
Orient = Literal["price", "long", "none"]
Baseline = Literal["uncond", "price", "fund", "vol"]
TARGET_MEANING = {
    "dir": "the coin alone vs USD, oriented by d, net of round-trip fee + slippage",
    "absret": "|log(exit / entry)| of the coin: movement magnitude, no direction, no costs",
}
BASELINE_MEANING = {
    "uncond": "same coin, orientation, horizon and own-volatility tercile (every eligible bar)",
    "price": "the price-only control: as uncond plus the same price direction and |pz| bucket",
    "fund": "the funding-only control: as uncond plus the same funding-percentile bucket",
    "vol": "the magnitude control: same coin, horizon, own-volatility tercile and |pz| bucket",
}
DIRECTION_LABELS: dict[str, tuple[str, str]] = {
    "price": ("continuation", "reversal"),
    "long": ("up", "down"),
    "none": ("larger", "smaller"),
}
SIGNS = ("positive", "negative")


# --------------------------------------------------------------------------- families


@dataclass(frozen=True)
class Member:
    """One preregistered two-sided hypothesis.

    kind:
      - ``event``: edge-triggered entries into state ``pop``; statistic = mean excess of the
        oriented ``target`` over the ``baseline`` cell, on independent events;
      - ``contrast``: entries into ``pop`` minus entries into ``pop_b`` (each against its own
        ``baseline`` cell, oriented the same way);
      - ``ic``: mean per-timestamp cross-sectional Spearman of signal ``pop`` vs the forward
        (gross, long) return, on non-overlapping samples.

    ``orient``: ``price`` = d is the sign of the price move over the lookback (positive =
    continuation); ``long`` = d = +1 (positive = the coin rises); ``none`` = no direction
    (the volatility target)."""

    family: str
    name: str
    kind: Literal["event", "contrast", "ic"]
    target: Target
    pop: str
    orient: Orient
    baseline: Baseline
    pop_b: str | None = None
    meaning: str = ""


def _ev(fam, name, pop, orient, baseline, meaning, target: Target = "dir") -> Member:
    return Member(fam, name, "event", target, pop, orient, baseline, None, meaning)


QUADRANTS = (("q_up_oi_up", "P+&O+"), ("q_up_oi_down", "P+&O-"), ("q_down_oi_up", "P-&O+"),
             ("q_down_oi_down", "P-&O-"))  # fmt: skip

# fmt: off
FAMILIES: dict[str, tuple[Member, ...]] = {
    # 1. The four price/OI quadrants and the price-only states, vs every bar of the coin.
    "quadrants": (
        *(_ev("quadrants", n, p, "price", "uncond", f"enters {p} (dead zones on both)")
          for n, p in QUADRANTS),
        _ev("quadrants", "price_up", "P+", "price", "uncond", "price-only control: enters P+"),
        _ev("quadrants", "price_down", "P-", "price", "uncond", "price-only control: enters P-"),
    ),
    # 2. Does OI add anything BEYOND price? The same quadrants vs the price-matched control.
    "oi_increment": tuple(
        _ev("oi_increment", n, p, "price", "price", f"{p} vs bars with the same price move")
        for n, p in QUADRANTS
    ),
    # 3. Is LARGE OI change different from ordinary OI change (given price)?
    "magnitude": (
        _ev("magnitude", "up_oi_large_up", "P+&OL+", "price", "price", "price up, OI s >= large"),
        _ev("magnitude", "up_oi_large_down", "P+&OL-", "price", "price", "price up, OI s <= -large"),
        _ev("magnitude", "down_oi_large_up", "P-&OL+", "price", "price", "price down, OI s >= large"),
        _ev("magnitude", "down_oi_large_down", "P-&OL-", "price", "price", "price down, OI s <= -large"),
        Member("magnitude", "expand_large_vs_ordinary", "contrast", "dir", "P&OL+", "price",
               "price", "P&OO+", "large vs ordinary OI expansion, price-matched each"),
        Member("magnitude", "contract_large_vs_ordinary", "contrast", "dir", "P&OL-", "price",
               "price", "P&OO-", "large vs ordinary OI contraction, price-matched each"),
    ),
    # 4. Positioning alone, ignoring price direction (coin units and the USD measure).
    "oi_only": (
        _ev("oi_only", "oi_large_up", "OL+", "long", "uncond", "coin OI s >= large"),
        _ev("oi_only", "oi_large_down", "OL-", "long", "uncond", "coin OI s <= -large"),
        _ev("oi_only", "usd_large_up", "UL+", "long", "uncond", "USD OI s >= large (moves with price)"),
        _ev("oi_only", "usd_large_down", "UL-", "long", "uncond", "USD OI s <= -large"),
    ),
    # 5. OI shocks (demeaned z vs the trailing distribution), then conditioned.
    "shocks": (
        _ev("shocks", "shock_pos", "SZ+", "long", "uncond", "OI z >= shock"),
        _ev("shocks", "shock_neg", "SZ-", "long", "uncond", "OI z <= -shock"),
        _ev("shocks", "shock_pos_price", "SZ+&P", "price", "price", "OI z >= shock with a price move"),
        _ev("shocks", "shock_neg_price", "SZ-&P", "price", "price", "OI z <= -shock with a price move"),
        _ev("shocks", "shock_pos_fund_high", "SZ+&FH", "long", "uncond", "OI z >= shock, funding pct high"),
        _ev("shocks", "shock_pos_fund_low", "SZ+&FL", "long", "uncond", "OI z >= shock, funding pct low"),
    ),
    # 6. Funding x OI ("crowding" defined objectively) and the funding-only controls.
    "funding_oi": (
        _ev("funding_oi", "crowd_long", "FH&CE+", "long", "uncond", "funding pct high + OI s >= crowd"),
        _ev("funding_oi", "crowd_short", "FL&CE+", "long", "uncond", "funding pct low + OI s >= crowd"),
        _ev("funding_oi", "crowd_long_price", "FH&CE+&P+", "long", "uncond", "crowd_long + price up"),
        _ev("funding_oi", "crowd_short_price", "FL&CE+&P-", "long", "uncond", "crowd_short + price down"),
        _ev("funding_oi", "unwind_high", "FH&CE-", "long", "uncond", "funding pct high + OI s <= -crowd"),
        _ev("funding_oi", "unwind_low", "FL&CE-", "long", "uncond", "funding pct low + OI s <= -crowd"),
        _ev("funding_oi", "fund_high", "FH", "long", "uncond", "funding-only control: pct high"),
        _ev("funding_oi", "fund_low", "FL", "long", "uncond", "funding-only control: pct low"),
        _ev("funding_oi", "oi_expand", "CE+", "long", "uncond", "OI-only control: OI s >= crowd"),
    ),
    # 7. Does OI add anything BEYOND funding? Crowding vs the funding-matched control.
    "crowding_increment": tuple(
        _ev("crowding_increment", n, p, "long", "fund", f"{p} vs bars in the same funding bucket")
        for n, p in (("crowd_long", "FH&CE+"), ("crowd_short", "FL&CE+"),
                     ("unwind_high", "FH&CE-"), ("unwind_low", "FL&CE-"))
    ),
    # 8. Divergence and price progress vs OI expansion.
    "divergence": (
        _ev("divergence", "flat_oi_surge", "F0&OL+", "long", "uncond", "|pz| < thr while OI s >= large"),
        _ev("divergence", "flat_oi_drop", "F0&OL-", "long", "uncond", "|pz| < thr while OI s <= -large"),
        _ev("divergence", "progress_low", "OL+&PS", "price", "uncond", "OI s >= large, |pz| < split"),
        _ev("divergence", "progress_high", "OL+&PB", "price", "uncond", "OI s >= large, |pz| >= split"),
        Member("divergence", "progress_low_vs_high", "contrast", "dir", "OL+&PS", "price", "uncond",
               "OL+&PB", "little vs much price progress for the same OI expansion"),
        _ev("divergence", "div_fund_extreme", "FX&DIV", "price", "price",
            "funding pct in either tail + price/OI divergence (P+O- or P-O+)"),
    ),
    # 9. Magnitude, not direction: does OI predict larger moves than its matched control?
    "volatility": (
        _ev("volatility", "vol_oi_large_up", "OL+", "none", "vol", "OI s >= large", "absret"),
        _ev("volatility", "vol_oi_large_down", "OL-", "none", "vol", "OI s <= -large", "absret"),
        _ev("volatility", "vol_flat_oi_surge", "F0&OL+", "none", "vol", "flat price + OI s >= large", "absret"),
        _ev("volatility", "vol_shock_pos", "SZ+", "none", "vol", "OI z >= shock", "absret"),
        _ev("volatility", "vol_shock_neg", "SZ-", "none", "vol", "OI z <= -shock", "absret"),
        _ev("volatility", "vol_crowd", "FX&CE+", "none", "vol", "funding tail + OI s >= crowd", "absret"),
    ),
    # 10. Lead/lag: does OI at t rank the NEXT returns across coins (beyond price)?
    "lead_lag": (
        Member("lead_lag", "ic_oi", "ic", "dir", "oi_s", "long", "uncond", meaning="coin OI s"),
        Member("lead_lag", "ic_oi_usd", "ic", "dir", "usd_s", "long", "uncond", meaning="USD OI s"),
        Member("lead_lag", "ic_price", "ic", "dir", "pz", "long", "uncond", meaning="price-only control"),
        Member("lead_lag", "ic_oi_beyond_price", "ic", "dir", "oi_s_resid", "long", "uncond",
               meaning="OI s residualised on pz across coins at t"),
    ),
    # 11. Cross-venue (Binance vs Hyperliquid OI). Gated on the comparison venue's coverage.
    "cross_venue": (
        _ev("cross_venue", "xv_agree", "XA", "price", "price", "price move; both venues' OI move the same way"),
        _ev("cross_venue", "xv_disagree", "XD", "price", "price", "price move; the venues' OI disagree"),
    ),
}
# fmt: on
FAMILY_NAMES = tuple(FAMILIES)


def members() -> list[Member]:
    return [m for fam in FAMILIES.values() for m in fam]


def families_spec() -> dict:
    """The family membership frozen into every definition (and its hash)."""
    return {f: [{"name": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
                 "orient": m.orient, "baseline": m.baseline, "pop_b": m.pop_b}
                for m in ms] for f, ms in FAMILIES.items()}  # fmt: skip


def family_sizes() -> dict[str, int]:
    """Preregistered family sizes m (one venue), before any sample gate."""
    return {f: len(ms) for f, ms in FAMILIES.items()}


# --------------------------------------------------------------------------- parameters

AxisName = Literal["lookback", "norm_window", "price_thr", "oi_thr", "oi_large", "shock_z",
                   "fund_tail", "crowd_oi"]  # fmt: skip
INT_AXES = ("lookback", "norm_window")
# State tokens each axis can change (sensitivity reads only these).
AXIS_TOKENS: dict[str, tuple[str, ...]] = {
    "lookback": ("*",),
    "norm_window": ("*",),
    "price_thr": ("P", "F0", "DIV", "XA", "XD"),
    "oi_thr": ("O", "OO", "DIV", "XA", "XD"),
    "oi_large": ("OL", "UL", "OO"),
    "shock_z": ("SZ",),
    "fund_tail": ("FH", "FL", "FX"),
    "crowd_oi": ("CE",),
}


class Central(LabModel):
    """The central (preregistered) parameters; lookbacks in hourly bars."""

    lookback: Literal[3, 6, 12] = 6
    norm_window: Literal[48, 72, 120] = 72
    price_thr: Literal[0.25, 0.5, 1.0] = 0.5  # |pz| dead zone (and the "flat" bound)
    oi_thr: Literal[0.25, 0.5, 1.0] = 0.5  # |oi_s| dead zone
    oi_large: Literal[1.5, 2.0, 2.5] = 2.0  # "large" |oi_s|; ordinary = [oi_thr, oi_large)
    shock_z: Literal[1.5, 2.0, 2.5] = 2.0
    fund_tail: Literal[0.85, 0.9, 0.95] = 0.9  # high tail pct >= this; low <= 1 - this
    crowd_oi: Literal[0.5, 1.0, 1.5] = 1.0  # OI s for crowding / unwind
    progress_split: Literal[1.0] = 1.0  # |pz| separating little / much price progress
    fund_window: Literal[720] = 720  # hourly fund_24h values a percentile is ranked against
    min_universe: Literal[4] = 4  # coins needed for a cross-sectional IC sample

    def value(self, axis: str) -> float:
        return getattr(self, axis)

    def with_value(self, axis: str, v: float) -> Central:
        d = self.model_dump(mode="python")
        d[axis] = int(v) if axis in INT_AXES else float(v)
        return Central.model_validate(d)


class Axis(LabModel):
    name: AxisName
    values: Annotated[tuple[Number, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if list(self.values) != sorted(set(self.values)):
            raise ValueError(f"axis {self.name}: values must be distinct and ascending")
        return self


class Window(LabModel):
    """The primary venue's window. Bars/OI ``[data_start, event_end)`` (bars on close time,
    OI on ``observed_at``); signals at bars closing in ``[event_start, event_end)``. Funding
    from ``funding_start`` (its percentile needs a month of history before the first OI)."""

    venue: Literal["binance"]
    data_start: UTCDateTime
    oi_start: UTCDateTime
    event_start: UTCDateTime
    event_end: UTCDateTime
    funding_start: UTCDateTime
    coins: Annotated[tuple[Symbol, ...], Field(min_length=4, max_length=12)]
    excluded: dict[Symbol, Text] = {}

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.data_start < self.oi_start < self.event_start < self.event_end:
            raise ValueError("data_start < oi_start < event_start < event_end is required")
        if self.funding_start > self.data_start:
            raise ValueError("funding must start no later than the bars")
        if "BTC" not in self.coins:
            raise ValueError("BTC (the regime reference) must be in the window")
        if len(set(self.coins)) != len(self.coins) or set(self.excluded) & set(self.coins):
            raise ValueError("duplicate or contradictory coins")
        return self


class ComparisonVenue(LabModel):
    """A second venue's OI used ONLY as a labelled comparison (never pooled, never a fill).

    ``min_aligned_hours``: the cross-venue family is tested only if at least this many
    in-window hours have a defined comparison change for at least ``min_assets`` coins;
    otherwise it is INSUFFICIENT (sparse prospective history is reported, not forced)."""

    venue: Literal["hyperliquid"]
    role: Literal["corroborative_prospective"] = "corroborative_prospective"
    coins: Annotated[tuple[Symbol, ...], Field(min_length=1, max_length=12)]
    max_age_s: Literal[7200.0] = 7200.0
    min_aligned_hours: Literal[336] = 336
    min_assets: Literal[3] = 3


class StatisticsSpec(LabModel):
    """Inference, gates and correction (``oi_inference_v1`` = Phase 18's
    ``relative_inference_v1`` block-clustered t-test: calendar blocks of ``primary_horizon``
    bars shared by ALL assets, adjacent-block covariance, Student t with G - 1 df). An
    event-level iid t-test is reported beside it for comparison only."""

    metric: Literal["excess_primary_horizon_v1"] = "excess_primary_horizon_v1"
    test: Literal["block_clustered_t_adjacent_blocks_two_sided_v1"] = (
        "block_clustered_t_adjacent_blocks_two_sided_v1"
    )
    naive_test: Literal["event_level_iid_t_v1"] = "event_level_iid_t_v1"
    independent_events: Literal["greedy_gap_horizon_per_asset_orientation_v1"] = (
        "greedy_gap_horizon_per_asset_orientation_v1"
    )
    ic_sampling: Literal["non_overlapping_every_primary_horizon_v1"] = (
        "non_overlapping_every_primary_horizon_v1"
    )
    correction: Literal["benjamini_hochberg"] = "benjamini_hochberg"
    q: Literal[0.1] = 0.1
    min_independent_events: Literal[30] = 30
    min_assets_with_events: Literal[3] = 3
    min_evaluable_fraction: Literal[0.8] = 0.8
    min_clusters: Literal[20] = 20
    # 552 event hours / 6 = 92 non-overlapping cross-sections exist; 60 is fixed from that
    # count before any outcome (Phase 18's 100 cannot be met by four weeks of data).
    min_timestamps: Literal[60] = 60
    economic_floor: Literal[0.001] = 0.001  # 10 bps (direction: net excess; volatility: |r|)
    ic_floor: Literal[0.03] = 0.03
    min_positive_asset_share: Probability = 0.6
    cross_venue_p: Literal[0.05] = 0.05


class OiStudyManifest(LabModel):
    """Declarative, strict YAML. Everything the run may do is fixed here."""

    name: Name
    description: Text
    timeframe: Literal["1h"] = "1h"
    assumed_bar_latency_s: Literal[60.0] = 60.0
    assumed_oi_latency_s: Literal[1800.0] = 1800.0
    oi_measure_primary: Literal["coin"] = "coin"
    window: Window
    comparison: ComparisonVenue
    horizons: Annotated[tuple[PositiveInt, ...], Field(min_length=1, max_length=4)]
    primary_horizon: PositiveInt
    central: Central = Central()
    axes: tuple[Axis, ...] = ()
    regime: RegimeSpec = RegimeSpec()
    statistics: StatisticsSpec = StatisticsSpec()
    verdict_policy: Literal["phase19_verdicts_v1"] = "phase19_verdicts_v1"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if list(self.horizons) != sorted(set(self.horizons)):
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
        return self

    @property
    def venues(self) -> tuple[str, ...]:
        return (self.window.venue, self.comparison.venue)

    def venue_coins(self) -> list[tuple[str, str]]:
        return [(self.window.venue, c) for c in self.window.coins] + [
            (self.comparison.venue, c) for c in self.comparison.coins
        ]

    def variants(self) -> list[tuple[str, Central, dict]]:
        out = [("central", self.central, {})]
        for a in self.axes:
            for v in a.values:
                if v != a.values[1]:
                    out.append((f"{a.name}={v:g}", self.central.with_value(a.name, v),
                                {a.name: v}))  # fmt: skip
        return out

    def selections(self, venue: str, coin: str) -> tuple[SeriesSelection, ...]:
        """Exact retained series of one (venue, coin). Everything ends at ``event_end``:
        nothing later is read."""
        from market_signal.models.domain import Timeframe

        w = self.window
        if venue == w.venue:
            return (
                SeriesSelection(kind="perp_intraday_bars", symbol=coin, source=venue,
                                timeframe=Timeframe.H1, start=w.data_start, end=w.event_end),
                SeriesSelection(kind="perp_funding", symbol=coin, source=venue,
                                start=w.funding_start, end=w.event_end),
                SeriesSelection(kind="perp_oi_history", symbol=coin, source=venue,
                                timeframe=Timeframe.H1, start=w.oi_start, end=w.event_end),
            )  # fmt: skip
        return (SeriesSelection(kind="perp_snapshots", symbol=coin, source=venue,
                                start=w.oi_start, end=w.event_end),)  # fmt: skip


def load_manifest(path) -> OiStudyManifest:
    from pathlib import Path

    from market_signal.research.lab.spec import _UniqueKeyLoader

    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return OiStudyManifest.model_validate(raw)


# --------------------------------------------------------------------------- definition


class OiStudyDefinition(LabModel):
    """The statistical identity of the study. ``study_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    study_version: Literal["oi_price_v1"] = STUDY_VERSION
    evidence_class: Literal["EXPLORATORY"] = EVIDENCE_CLASS
    availability_mode: Literal["assumed"] = "assumed"
    availability_statement: Literal[AVAILABILITY_STATEMENT] = AVAILABILITY_STATEMENT  # type: ignore[valid-type]
    validated_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    manifest: OiStudyManifest
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
    return {"study_version": STUDY_VERSION, "primitives": PRIMITIVES_VERSION,
            "availability": AVAILABILITY_VERSION,
            "inference": "oi_inference_v1 (= relative_inference_v1, Phase 18)",
            "regime": "structure_regime_v1 (Phase 17)", "verdicts": "phase19_verdicts_v1"}  # fmt: skip


__all__ = [
    "AVAILABILITY_STATEMENT",
    "DIRECTION_LABELS",
    "EXPLORATORY_STATEMENT",
    "FAMILIES",
    "SIGNS",
    "Central",
    "Member",
    "OiStudyDefinition",
    "OiStudyManifest",
    "StatisticsSpec",
    "families_spec",
    "family_sizes",
    "load_manifest",
    "members",
    "semantics",
]
