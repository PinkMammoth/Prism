"""Canonical market-context records (``context_observation_v1`` / ``context_event_v1``).

An **observation** is one report of something, from one source, as normalised by a provider.
The ledger (``context.ledger``) turns observations into **events** (one per logical event)
plus append-only **updates**.

The rule that matters: **an event exists for research only from ``first_seen_at``, the moment
Prism itself first observed it.** ``published_at``, ``provider_time`` and an external
monitor's own ``reported_first_seen_at`` are provenance and latency measurements, never
availability. A provider never supplies ``observed_at``: the ledger stamps it from Prism's
clock on receipt. Historical records (``observation_mode='historical'``) are stored with the
truthful import time as ``first_seen_at``; their publication time is kept separately and they
are flagged so no confirmatory claim relies on an unknown Prism latency.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

import pandas as pd
from pydantic import Field, field_validator, model_validator

from market_signal.context.taxonomy import (
    SUBCATEGORIES,
    TAXONOMY_VERSION,
    Category,
    Confidence,
    SourceType,
    cap_confidence,
)
from market_signal.research.lab.common import (
    Digest,
    LabModel,
    Name,
    Symbol,
    UTCDateTime,
    canonical_json,
    content_id,
)

OBSERVATION_SCHEMA = "context_observation_v1"
EVENT_SCHEMA = "context_event_v1"
UPDATE_KINDS = ("observed", "corroboration", "revision", "confidence_change", "status_change",
                "denial", "resolution", "value_release", "consensus", "relevance_override",
                "schedule_change")  # fmt: skip
Short = Annotated[str, Field(strict=True, min_length=1, max_length=500)]
Summary = Annotated[str, Field(strict=True, max_length=4000)]
Scalar = str | int | float | bool | None


# --------------------------------------------------------------------------- attributes


class MacroAttrs(LabModel):
    """Scheduled macro release / decision. Values are as Prism observed them (never later
    revisions retro-applied). ``unit`` describes ``previous``/``consensus``/``actual``."""

    kind: Literal["macro"] = "macro"
    series_key: Name  # e.g. us_cpi_mom
    period: Short | None = None  # e.g. 2026-09
    unit: Short | None = None  # e.g. pct_mom, k_jobs, pct, bp
    previous: float | None = None
    consensus: float | None = None
    actual: float | None = None
    revision_of_previous: float | None = None
    importance: Literal[1, 2, 3] = 1  # 1 = tier-1
    markets: tuple[Short, ...] = ()  # affected markets/currencies, e.g. ("USD", "rates")
    timezone: Short | None = None  # source timezone of the scheduled time
    time_precision: Literal["exact", "approximate", "date_only"] = "exact"
    schedule_source: Short | None = None


class SecurityAttrs(LabModel):
    kind: Literal["security"] = "security"
    protocol: Short | None = None
    chain: Short | None = None
    exploit_type: Short | None = None
    status: Literal["suspected", "confirmed", "denied", "resolved", "unknown"] = "unknown"
    loss_usd_estimate: float | None = Field(default=None, ge=0)
    target_kind: Literal["protocol", "bridge", "exchange", "wallet", "other", "unknown"] = "unknown"
    deposits_paused: bool | None = None
    withdrawals_paused: bool | None = None
    recovery_status: Literal["none", "partial", "full", "unknown"] = "unknown"


class ListingAttrs(LabModel):
    """Announcement and trading commencement are different instants and stay separate."""

    kind: Literal["listing"] = "listing"
    exchange: Short
    action: Literal["listing", "delisting"]
    market_type: Short | None = None  # spot | perp
    announced_at: UTCDateTime | None = None
    trading_open_at: UTCDateTime | None = None
    deposit_open_at: UTCDateTime | None = None
    trading_close_at: UTCDateTime | None = None
    pairs: tuple[Short, ...] = ()


class UnlockAttrs(LabModel):
    kind: Literal["unlock"] = "unlock"
    unlock_time: UTCDateTime
    amount: float | None = Field(default=None, ge=0)
    pct_circulating: float | None = Field(default=None, ge=0, le=100)
    recipient_category: Short | None = None


class GenericAttrs(LabModel):
    """Flat scalar facts for everything else (no nested free-form structures)."""

    kind: Literal["generic"] = "generic"
    facts: dict[Name, Scalar] = Field(default_factory=dict)


Attributes = Annotated[MacroAttrs | SecurityAttrs | ListingAttrs | UnlockAttrs | GenericAttrs,
                       Field(discriminator="kind")]  # fmt: skip


# --------------------------------------------------------------------------- observation


class Observation(LabModel):
    """One normalised report. Built by a provider or the validated external importer."""

    schema_version: Literal["context_observation_v1"] = OBSERVATION_SCHEMA
    taxonomy_version: Literal["context_taxonomy_v1"] = TAXONOMY_VERSION
    source_id: Name
    source_type: SourceType
    source_ref: Short | None = None  # URL or the source's own identifier
    subcategory: Name
    title: Short
    summary: Summary = ""
    scheduled: bool = False
    event_time: UTCDateTime | None = None  # when it happens/happened (descriptive)
    published_at: UTCDateTime | None = None  # source publication time (provenance)
    provider_time: UTCDateTime | None = None  # the provider record's own timestamp
    reported_first_seen_at: UTCDateTime | None = None  # an EXTERNAL monitor's first sighting
    confidence: Confidence
    entities: tuple[Name, ...] = ()
    assets: tuple[Symbol, ...] = ()  # structured tickers supplied by the source (direct links)
    market_wide: bool = False
    scope: Literal["asset", "exchange", "systemic"] = "asset"
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    region: Short | None = None
    relevance_end: UTCDateTime | None = None  # provider/manual override of the default window
    dedup_key: Annotated[str, Field(pattern=r"^[a-z0-9_]+:[A-Za-z0-9_.:/@=-]{1,300}$")] | None = (
        None
    )
    update_kind: Literal[UPDATE_KINDS] | None = None  # type: ignore[valid-type]
    attributes: Attributes = Field(default_factory=GenericAttrs)
    observation_mode: Literal["live", "historical"] = "live"
    raw_sha256: Digest | None = None  # hash of the raw payload this was normalised from

    @field_validator("subcategory")
    @classmethod
    def known_subcategory(cls, v: str) -> str:
        if v not in SUBCATEGORIES:
            raise ValueError(f"unknown subcategory {v!r} (taxonomy {TAXONOMY_VERSION})")
        return v

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.scheduled and self.event_time is None:
            raise ValueError("a scheduled event needs event_time")
        if self.category == Category.MACRO and self.attributes.kind not in ("macro", "generic"):
            raise ValueError("macro events carry macro (or generic) attributes")
        if self.attributes.kind == "listing" and self.subcategory not in ("listing", "delisting"):
            raise ValueError("listing attributes only on listing/delisting events")
        if len(set(self.assets)) != len(self.assets) or len(set(self.entities)) != len(
            self.entities
        ):
            raise ValueError("duplicate asset/entity")
        return self

    @property
    def category(self) -> Category:
        return SUBCATEGORIES[self.subcategory]

    def capped(self) -> Observation:
        """The observation with its confidence capped by its source tier (a social/AI first
        report can never arrive as CONFIRMED/OFFICIAL)."""
        c = cap_confidence(self.confidence, self.source_type)
        return self if c == self.confidence else self.model_copy(update={"confidence": c})

    def provenance_hash(self) -> str:
        """Content identity of the report (independent of when Prism received it), so a
        re-poll of an unchanged item is recognised as a duplicate. The raw-payload hash is
        excluded: a feed's other items changing must not make this item look new."""
        return content_id("", self.model_dump(mode="json", exclude={"raw_sha256"}))[:64]


def observation_json(obs: Observation) -> str:
    return canonical_json(obs.model_dump(mode="json"))


def changed_fields(state: dict[str, Any], obs: Observation) -> dict[str, Any]:
    """Fields of ``obs`` that differ from an event's current ``state`` (title, summary, times,
    subcategory, attributes, relevance override). Attributes are compared field by field and
    only non-null new values count (a later report omitting a value does not erase it)."""
    out: dict[str, Any] = {}
    d = obs.model_dump(mode="json")
    for k in ("subcategory", "title", "summary"):
        if d[k] not in (None, "") and d[k] != state.get(k):
            out[k] = d[k]
    for k in ("event_time", "relevance_end"):  # compare instants, not string formats
        if d[k] is not None and (state.get(k) is None
                                 or pd.Timestamp(d[k]) != pd.Timestamp(state[k])):  # fmt: skip
            out[k] = d[k]
    old_attrs = state.get("attributes") or {}
    new_attrs = d["attributes"]
    old_kind, new_kind = old_attrs.get("kind"), new_attrs.get("kind")
    if old_kind == "generic" and new_kind not in (None, "generic"):
        # a later, structured report upgrades a generic first report (facts are kept)
        delta = {k: v for k, v in new_attrs.items() if v is not None and v != () and v != []}
        out["attributes"] = delta
        return out
    if new_kind == old_kind or not old_attrs:
        delta = {
            k: v
            for k, v in new_attrs.items()
            if v is not None and v != () and v != [] and old_attrs.get(k) != v
        }
        if new_kind == "generic":
            facts = {
                k: v
                for k, v in (new_attrs.get("facts") or {}).items()
                if (old_attrs.get("facts") or {}).get(k) != v
            }
            delta = {"facts": facts} if facts else {}
        if delta:
            out["attributes"] = delta
    return out
