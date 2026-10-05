"""The frozen Phase 20 historical lifecycle methodology study (``edge_lifecycle_v1``).

A ``LifecycleStudyManifest`` (strict YAML) declares the venues, coins, data windows, the data
cutoff, the first evaluation date per venue, the horizon and the catalogue families. Nothing
in it is a tunable lifecycle parameter: those live in the frozen ``LifecyclePolicy``, whose
full content and ID are embedded in the definition. Registration (through the Phase 17
study adapter, ``research.lab.structure_study``) adds the frozen perp costs, the retained Lab
dataset IDs, every strategy definition of the named catalogue families and the semantic
versions, producing a ``LifecycleStudyDefinition`` whose content hash is the ``study_id``.

This is methodology validation on existing catalogue members used as fixtures. It searches
for no winner, promotes nothing, and no result feeds a forward tracker, co-pilot or paper
account. Data stop at the cutoff (2026-10-01 in v1), so the Phase 9 validation window
[2026-10-01, 2027-10-01) is never read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from market_signal.models.domain import Timeframe
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)
from market_signal.research.lab.datasets import SeriesSelection
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lifecycle import policy as pol_mod
from market_signal.research.structure.study.spec import CostRef, DatasetRef

STUDY_VERSION = "edge_lifecycle_v1"
AVAILABILITY_STATEMENT = (
    "Historical daily perp bars and funding were backfilled. A bar is assumed available at "
    "its close and funding at its recorded available_at; no historical lifecycle evaluation "
    "was observed live."
)
EXPLORATORY_STATEMENT = (
    "Phase 20 results are EXPLORATORY methodology validation: existing catalogue strategies "
    "are fixtures for testing a time-varying evidence framework, not candidates. Nothing is "
    "promoted, no lifecycle parameter was tuned on these results, and no result feeds a "
    "forward tracker, co-pilot or paper account."
)

Venue = Literal["binance", "hyperliquid"]


class FamilyRef(LabModel):
    family: Name
    version: PositiveInt


class VenueSpec(LabModel):
    venue: Venue
    role: Literal["primary", "secondary"]
    coins: Annotated[tuple[Symbol, ...], Field(min_length=3)]
    bars_start: UTCDateTime
    funding_start: UTCDateTime
    first_evaluation: UTCDateTime


class LifecycleStudyManifest(LabModel):
    name: Name
    description: Text
    policy_version: PositiveInt
    cutoff: UTCDateTime
    horizon_bars: PositiveInt
    reference_coin: Symbol = "BTC"
    assumed_latency_s: float = 0.0
    venues: Annotated[tuple[VenueSpec, ...], Field(min_length=1)]
    families: Annotated[tuple[FamilyRef, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        names = [v.venue for v in self.venues]
        if len(names) != len(set(names)) or sum(v.role == "primary" for v in self.venues) != 1:
            raise ValueError("exactly one primary venue, each venue once")
        for v in self.venues:
            if self.reference_coin not in v.coins:
                raise ValueError(f"{v.venue} must include the reference coin")
            if not (v.bars_start < v.first_evaluation < self.cutoff):
                raise ValueError(f"{v.venue}: bars_start < first_evaluation < cutoff required")
            if v.funding_start >= self.cutoff:
                raise ValueError(f"{v.venue}: funding_start must precede the cutoff")
        pol_mod.policy(self.policy_version).floor.floor(self.horizon_bars)  # frozen floor exists
        fams = [(f.family, f.version) for f in self.families]
        if len(fams) != len(set(fams)):
            raise ValueError("duplicate family")
        return self

    def venue(self, name: str) -> VenueSpec:
        return next(v for v in self.venues if v.venue == name)

    def venue_coins(self) -> list[tuple[str, str]]:
        return [(v.venue, c) for v in self.venues for c in v.coins]

    def selections(self, venue: str, coin: str) -> tuple[SeriesSelection, ...]:
        """Daily perp bars and funding of one (venue, coin), ending at the cutoff."""
        v = self.venue(venue)
        return (
            SeriesSelection(kind="perp_bars", symbol=coin, source=venue, timeframe=Timeframe.D1,
                            start=v.bars_start, end=self.cutoff),
            SeriesSelection(kind="perp_funding", symbol=coin, source=venue,
                            start=v.funding_start, end=self.cutoff),
        )  # fmt: skip


class StrategyRef(LabModel):
    name: Name
    strategy_id: Text
    family: Name
    version: PositiveInt
    side: Literal["long", "short"]
    params: dict
    definition: dict


def load_manifest(path) -> LifecycleStudyManifest:
    from market_signal.research.lab.spec import _UniqueKeyLoader

    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return LifecycleStudyManifest.model_validate(raw)


def catalogue_strategies(manifest: LifecycleStudyManifest, catalogue_dir: Path) -> tuple[StrategyRef, ...]:  # fmt: skip
    """Every perp variant of the named families, in catalogue order (family, version, side,
    grid). Membership is fixed by the family files, whose IDs are pinned by tests."""
    from market_signal.research.lab.families import generate, load_catalogue

    cat = load_catalogue(catalogue_dir)
    out = []
    for ref in manifest.families:
        fam = cat[(ref.family, ref.version)]
        for v in generate(fam, "perp"):
            out.append(StrategyRef(name=v.name, strategy_id=v.strategy_id, family=v.family,
                                   version=v.version, side=v.side, params=dict(v.params),
                                   definition=v.hypothesis.definition.model_dump(mode="json")))  # fmt: skip
    return tuple(out)


class LifecycleStudyDefinition(LabModel):
    """The statistical identity of the study. ``study_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    study_version: Literal["edge_lifecycle_v1"] = STUDY_VERSION
    evidence_class: Literal["EXPLORATORY"] = "EXPLORATORY"
    availability_mode: Literal["assumed"] = "assumed"
    availability_statement: Literal[AVAILABILITY_STATEMENT] = AVAILABILITY_STATEMENT  # type: ignore[valid-type]
    validated_reachable: Literal[False] = False
    consumers: Literal["none"] = "none"
    manifest: LifecycleStudyManifest
    policy: dict
    policy_id: Text
    strategies: Annotated[tuple[StrategyRef, ...], Field(min_length=1)]
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
        frozen = pol_mod.LifecyclePolicy.model_validate(self.policy)
        if frozen.policy_id != self.policy_id or frozen.version != self.manifest.policy_version:
            raise ValueError("embedded lifecycle policy does not match its ID or version")
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            raise ValueError("duplicate strategy")
        for s in self.strategies:
            d = StrategyDefinition.model_validate(s.definition)
            if d.market != "perp" or d.side != s.side:
                raise ValueError(f"{s.name}: not a perp {s.side} definition")
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        data["costs"] = sorted(data["costs"], key=canonical_json)
        data["datasets"] = sorted(data["datasets"], key=canonical_json)
        return canonical_json(data)

    @property
    def study_id(self) -> str:
        return content_id("sstudy_", json.loads(self.canonical_json()))

    def lifecycle_policy(self) -> pol_mod.LifecyclePolicy:
        return pol_mod.LifecyclePolicy.model_validate(self.policy)

    def cost(self, venue: str, coin: str) -> CostRef:
        return next(c for c in self.costs if c.venue == venue and c.coin == coin)

    def dataset(self, venue: str, coin: str) -> str:
        return next(d.dataset_id for d in self.datasets if d.venue == venue and d.coin == coin)


def semantics() -> dict[str, str]:
    from market_signal.research.lab.compiler import COMPILER_VERSION
    from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

    return {"study_version": STUDY_VERSION, "methodology": pol_mod.METHODOLOGY_VERSION,
            "baseline": pol_mod.BASELINE_VERSION, "inference": pol_mod.INFERENCE_VERSION,
            "market_state": pol_mod.MARKET_STATE_VERSION, "stress": pol_mod.STRESS_VERSION,
            "cusum": pol_mod.CUSUM_VERSION, "segmentation": pol_mod.SEGMENTATION_VERSION,
            "edge_state": pol_mod.EDGE_STATE_VERSION, "machine": pol_mod.MACHINE_VERSION,
            "compiler": COMPILER_VERSION, "vocabulary": VOCABULARY_VERSION,
            "returns": "perps.backtest.side_forward_returns",
            "independence": "backtest.events.decluster"}  # fmt: skip


def definition(manifest: LifecycleStudyManifest, *, costs, datasets,
               catalogue_dir: Path) -> LifecycleStudyDefinition:  # fmt: skip
    p = pol_mod.policy(manifest.policy_version)
    return LifecycleStudyDefinition(manifest=manifest, policy=p.model_dump(mode="json"),
                                    policy_id=p.policy_id,
                                    strategies=catalogue_strategies(manifest, catalogue_dir),
                                    costs=tuple(costs), datasets=tuple(datasets),
                                    semantics=semantics())  # fmt: skip
