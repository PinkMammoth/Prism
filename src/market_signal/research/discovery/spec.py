"""The frozen Phase 22 discovery definition: everything decided BEFORE any real outcome.

``DiscoveryManifest`` (strict YAML) fixes the venues and their roles, data/event windows,
the contemporary / recent windows (chosen from the calendar and data coverage, never from
an equity curve), the statistics, the economic floor, the verdict criteria, the frequency
gates, clustering, shortlist and ensemble rules. Registration adds the frozen catalogue
(``catalogue.strategies()`` and ``vol_tests()``: every strategy ID), the frozen perp costs,
the retained Lab dataset IDs and the semantic versions, producing an
``IntradayDiscoveryDefinition`` whose content hash is the ``study_id``. It is governed by the
Phase 17 study adapter (``research.lab.structure_study``): same tables, same lifecycle.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from market_signal.research.discovery import STATEMENT
from market_signal.research.discovery import catalogue as cat
from market_signal.research.discovery.primitives import FEATURES_VERSION
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
from market_signal.research.structure.study.spec import CostRef, DatasetRef

STUDY_VERSION = "intraday_discovery_v1"
EVIDENCE_CLASS = "EXPLORATORY"
AVAILABILITY_STATEMENT = (
    "Historical intraday availability is reconstructed under an explicit latency assumption "
    "rather than observed in real time."
)
VERDICTS = ("REJECTED", "NO_EVIDENCE", "INTERESTING", "INCUBATION_CANDIDATE",
            "STRONG_INCUBATION_CANDIDATE")  # fmt: skip
CANDIDATE_VERDICTS = ("INCUBATION_CANDIDATE", "STRONG_INCUBATION_CANDIDATE")

Venue = Literal["hyperliquid", "binance"]
TFS = ("15m", "1h", "4h")


class TfStarts(LabModel):
    m15: UTCDateTime = Field(alias="15m")
    h1: UTCDateTime = Field(alias="1h")
    h4: UTCDateTime = Field(alias="4h")

    model_config = {**LabModel.model_config, "populate_by_name": True}

    def get(self, tf: str):
        return {"15m": self.m15, "1h": self.h1, "4h": self.h4}[tf]


class LongContext(LabModel):
    """Context only (never a verdict input): 1H strategies on the venue's older 1h history,
    executed on the 1h grid (the 15m execution grid does not exist there). The coarser grid
    delays entry by one hour; differences are labelled, not interpreted."""

    event_start: UTCDateTime
    event_end: UTCDateTime
    execution_timeframe: Literal["1h"] = "1h"


class VenueSpec(LabModel):
    venue: Venue
    role: Literal["discovery", "replication"]
    coins: Annotated[tuple[Symbol, ...], Field(min_length=2, max_length=12)]
    data_start: TfStarts
    funding_start: UTCDateTime
    event_start: UTCDateTime
    event_end: UTCDateTime
    long_context: LongContext | None = None
    notes: Text | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if "BTC" not in self.coins:
            raise ValueError("BTC is the market reference and must be in every venue")
        if not self.event_start < self.event_end:
            raise ValueError("event_start < event_end")
        for tf in TFS:
            if self.data_start.get(tf) >= self.event_start:
                raise ValueError(f"{tf} data must start before the event window (warm-up)")
        if self.funding_start > min(self.data_start.get(tf) for tf in TFS):
            raise ValueError("funding must cover every data window")
        lc = self.long_context
        if lc is not None:
            if lc.event_end > self.event_start:
                raise ValueError("long context must end where the discovery window begins")
            if self.data_start.h1 >= lc.event_start or self.data_start.h4 >= lc.event_start:
                raise ValueError("1h/4h data must start before the long-context window")
        return self

    def data_bounds(self, tf: str):
        return self.data_start.get(tf), self.event_end


class Windows(LabModel):
    """Evaluation windows on SIGNAL time, frozen from the calendar before outcomes."""

    contemporary_start: UTCDateTime
    recent_180_start: UTCDateTime
    recent_90_start: UTCDateTime
    recent_30_start: UTCDateTime
    end: UTCDateTime
    recency_half_life_days: PositiveInt = 90


class Statistics(LabModel):
    """``intraday_discovery_inference_v1``."""

    metric: Literal["net_after_cost_and_funding_per_event_v1"] = (
        "net_after_cost_and_funding_per_event_v1"
    )
    test: Literal["one_sided_daily_block_clustered_t_adjacent_v1"] = (
        "one_sided_daily_block_clustered_t_adjacent_v1"
    )
    independence: Literal["greedy_gap_primary_horizon_per_coin_v1"] = (
        "greedy_gap_primary_horizon_per_coin_v1"
    )
    baseline: Literal["every_bar_same_coin_side_horizon_vol_tercile_v1"] = (
        "every_bar_same_coin_side_horizon_vol_tercile_v1"
    )
    correction: Literal["benjamini_hochberg_within_bh_family"] = (
        "benjamini_hochberg_within_bh_family"
    )
    q: Probability = 0.10
    untestable_p: Literal[1.0] = 1.0  # members without enough evidence keep m fixed
    min_events: PositiveInt = 30
    min_assets: PositiveInt = 3
    min_clusters: PositiveInt = 20
    # economic floor: minimum NET (after cost + funding) mean per event by horizon minutes
    floor_by_minutes: tuple[tuple[PositiveInt, Number], ...] = (
        (15, 0.0004), (30, 0.0005), (60, 0.0006), (120, 0.0008), (240, 0.0010),
        (480, 0.0015), (720, 0.0018), (1440, 0.0025))  # fmt: skip

    def floor(self, minutes: int) -> float:
        t = dict(self.floor_by_minutes)
        if minutes not in t:
            raise ValueError(f"no frozen economic floor for {minutes} minutes")
        return float(t[minutes])


class Frequency(LabModel):
    """Independent signals per day across the venue universe (the perp sleeve's view)."""

    sparse_per_day: Number = 1 / 7  # < 1 signal / week: flagged sparse
    too_sparse_per_day: Number = 1 / 30  # < 1 signal / month: too sparse ...
    extraordinary_t: Number = 3.0  # ... unless the contemporary effect is extraordinary


class Criteria(LabModel):
    """``phase22_verdicts_v1`` (first match):

    1. NO_EVIDENCE: fewer than ``min_events`` independent events in every evaluation window.
    2. REJECTED (too sparse): independent signals/day < 1/30 and contemporary t < 3.
    3. INCUBATION_CANDIDATE: the first route that holds, AND every guard on it.
       Routes (window: net mean >= floor, t >= bar, >= ``min_events``):
       contemporary (t >= ``contemporary_t``), recent-180 and recent-90 (t >= ``recent_t``),
       recent-30 (t >= ``recent30_t``). Old null history never vetoes a recent route: an
       emerging or temporary edge is a legitimate candidate.
       Guards (on the route's window): >= min assets; the NEXT more recent window is not
       credibly dead (t > ``recent_min_t`` when it holds >= ``recent_min_events``; for the
       contemporary route also recent-180 net > 0) — a strong past never rescues a dead
       recent edge; leave-best-asset-out mean > 0; the five best events < ``max_top5_share``
       of the net sum; positive-asset share >= 0.5; gross mean > 0; excess over random
       same-side entries > 0 (not a timing artefact); nearby-parameter support (a one-step
       neighbour with mean > 0 and t >= 1 in the same window) or, without neighbours, t >=
       ``lone_t``; independent signals/day >= 1/week.
       STRONG_INCUBATION_CANDIDATE: additionally BH q <= ``q`` in its family (contemporary
       test) and the most recent window's mean > 0.
    4. INTERESTING: contemporary, recent-180 or recent-90 mean > 0 with t >= 1.
    5. REJECTED: contemporary t <= -1, or an edge only before costs (gross t >= 1, net <= 0).
    6. NO_EVIDENCE otherwise.

    Disclosure: the recent-90 and recent-30 routes and the "next more recent window" guard
    were added after the planted-edge calibration (synthetic only, before any real outcome)
    showed that 30-60-day edges are diluted below detection in the 180-day window.
    """

    version: Literal["phase22_verdicts_v1"] = "phase22_verdicts_v1"
    contemporary_t: Number = 1.5
    recent_t: Number = 2.0
    recent30_t: Number = 2.5
    recent_min_t: Number = -1.0
    recent_min_events: PositiveInt = 10
    max_top5_share: Probability = 0.5
    min_positive_asset_share: Probability = 0.5
    lone_t: Number = 2.0
    interesting_t: Number = 1.0
    rejected_t: Number = -1.0


class Clustering(LabModel):
    """Duplicate detection: two strategies' independent events match when they are on the
    same coin, same side and their entries are within ``match_minutes``; overlap = matches /
    (|A| + |B| - matches). Single-linkage clusters at ``threshold``; representative = lowest
    complexity score, then highest contemporary t, then key."""

    match_minutes: PositiveInt = 120
    threshold: Probability = 0.5


class Shortlist(LabModel):
    eligible: tuple[str, ...] = CANDIDATE_VERDICTS
    representatives_only: Literal[True] = True
    max_per_bh_family: PositiveInt = 3


class Ensemble(LabModel):
    """Aggregate opportunity: dedup by (coin, side) within ``dedup_minutes``."""

    dedup_minutes: PositiveInt = 240
    target_per_day: Number = 5.0  # a reference line, never a quota


class DiscoveryManifest(LabModel):
    name: Name
    description: Text
    assumed_latency_s: Literal[60.0] = 60.0
    reference: Literal["BTC"] = "BTC"
    venues: Annotated[tuple[VenueSpec, ...], Field(min_length=1, max_length=2)]
    windows: Windows
    statistics: Statistics = Statistics()
    frequency: Frequency = Frequency()
    criteria: Criteria = Criteria()
    clustering: Clustering = Clustering()
    shortlist: Shortlist = Shortlist()
    ensemble: Ensemble = Ensemble()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        roles = [v.role for v in self.venues]
        if roles.count("discovery") != 1:
            raise ValueError("exactly one discovery venue")
        if len({v.venue for v in self.venues}) != len(self.venues):
            raise ValueError("one entry per venue")
        d = self.discovery
        w = self.windows
        if not (d.event_start <= w.contemporary_start < w.recent_180_start < w.recent_90_start
                < w.recent_30_start < w.end <= d.event_end):  # fmt: skip
            raise ValueError("windows must nest inside the discovery event window, in order")
        for s in cat.strategies():  # every horizon must have a frozen floor
            for h in s.horizons:
                self.statistics.floor(s.horizon_minutes(h))
        return self

    @property
    def discovery(self) -> VenueSpec:
        return next(v for v in self.venues if v.role == "discovery")

    def venue(self, name: str) -> VenueSpec:
        return next(v for v in self.venues if v.venue == name)

    def venue_coins(self) -> list[tuple[str, str]]:
        return [(v.venue, c) for v in self.venues for c in v.coins]

    def selections(self, venue: str, coin: str) -> tuple[SeriesSelection, ...]:
        from market_signal.models.domain import Timeframe

        v = self.venue(venue)
        out = [SeriesSelection(kind="perp_intraday_bars", symbol=coin, source=venue,
                               timeframe=Timeframe(tf), start=v.data_start.get(tf), end=v.event_end)
               for tf in TFS]  # fmt: skip
        out.append(SeriesSelection(kind="perp_funding", symbol=coin, source=venue,
                                   start=v.funding_start, end=v.event_end))  # fmt: skip
        return tuple(out)


def load_manifest(path) -> DiscoveryManifest:
    from pathlib import Path

    from market_signal.research.lab.spec import _UniqueKeyLoader

    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return DiscoveryManifest.model_validate(raw)


def catalogue_spec() -> dict:
    """The catalogue frozen into every definition (and its hash)."""
    return {"catalogue_id": cat.catalogue_id(), "version": cat.CATALOGUE_VERSION,
            "strategies": [{"key": s.key, "strategy_id": s.strategy_id} for s in cat.strategies()],
            "vol_tests": [{"key": v.key, "test_id": v.test_id} for v in cat.vol_tests()]}  # fmt: skip


def semantics() -> dict[str, str]:
    return {"study_version": STUDY_VERSION, "catalogue": cat.CATALOGUE_VERSION,
            "features": FEATURES_VERSION, "entry": cat.ENTRY_RULE, "exit": cat.EXIT_RULE,
            "inference": "intraday_discovery_inference_v1", "verdicts": "phase22_verdicts_v1",
            "lifecycle": "edge_lifecycle_v1 (Phase 20 classify, read-only)",
            "stress": "market_stress_v1 (Phase 20)",
            "incubation_replay": "incubation_machine_v1 (Phase 21, read-only, diagnostics)"}  # fmt: skip


class IntradayDiscoveryDefinition(LabModel):
    schema_version: Literal["1"] = "1"
    study_version: Literal["intraday_discovery_v1"] = STUDY_VERSION
    evidence_class: Literal["EXPLORATORY"] = EVIDENCE_CLASS
    availability_mode: Literal["assumed"] = "assumed"
    availability_statement: Literal[AVAILABILITY_STATEMENT] = AVAILABILITY_STATEMENT  # type: ignore[valid-type]
    statement: Literal[STATEMENT] = STATEMENT  # type: ignore[valid-type]
    validated_reachable: Literal[False] = False
    live_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    manifest: DiscoveryManifest
    catalogue: dict
    costs: tuple[CostRef, ...]
    datasets: tuple[DatasetRef, ...]
    semantics: dict[str, str]

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.catalogue != catalogue_spec():
            raise ValueError("catalogue differs from the frozen intraday_catalogue_v1")
        need = set(self.manifest.venue_coins())
        for label, refs in (("cost", self.costs), ("dataset", self.datasets)):
            keys = [(r.venue, r.coin) for r in refs]
            if len(keys) != len(set(keys)) or set(keys) != need:
                raise ValueError(f"exactly one {label} entry per venue and coin")
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python", by_alias=True)
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


__all__ = ["CANDIDATE_VERDICTS", "STUDY_VERSION", "VERDICTS", "DiscoveryManifest",
           "IntradayDiscoveryDefinition", "catalogue_spec", "load_manifest", "semantics"]  # fmt: skip
