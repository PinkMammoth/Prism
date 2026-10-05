"""Future external-event stream: interface concept only (``external_event_v1``).

Phase 21 does NOT ingest, store or trade external events. This schema exists so a later
event-derived strategy (its own preregistered study) can plug into candidate incubation
cleanly. Possible future sources: a monitoring service or an AI agent watching protocol
hacks, exchange failures, regulatory actions, listings/delistings, governance votes,
protocol upgrades, ETF/treasury developments.

The one rule that matters: **an event is available to Prism only from the moment Prism first
observed it** (``first_seen_at``). Publication time is provenance, never availability: an
article published at 09:00 that Prism's monitor first saw at 15:00 can inform nothing
before 15:00. ``available_at`` is therefore derived and cannot be supplied.

No mapping from an event to a trade direction lives here (e.g. "hack -> short the victim
token" or "listing -> long" are hypotheses requiring their own study).
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.research.lab.common import LabModel, Name, Symbol, Text, UTCDateTime, content_id

EVENT_SCHEMA_VERSION = "external_event_v1"
Category = Literal["protocol_hack", "exchange_failure", "regulatory", "listing", "delisting",
                   "governance", "protocol_upgrade", "etf_or_treasury", "other"]  # fmt: skip


class Provenance(LabModel):
    source: Name  # e.g. "chatgpt_work_monitor", "manual", "rss:<feed>"
    source_ref: Text  # the source's own identifier / URL
    retrieved_by: Name  # the Prism component or agent that recorded it
    raw_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]  # hash of the raw payload


class ExternalEvent(LabModel):
    schema_version: Literal["external_event_v1"] = EVENT_SCHEMA_VERSION
    category: Category
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    headline: Text
    event_time: UTCDateTime | None = None  # when it happened, if known (descriptive)
    published_at: UTCDateTime | None = None  # when the source published it (provenance)
    first_seen_at: UTCDateTime  # when Prism first observed it: the ONLY availability time
    expires_at: UTCDateTime | None = None  # after this the event is no longer "current"
    confidence: Annotated[float, Field(ge=0, le=1)]
    provenance: Provenance

    @model_validator(mode="after")
    def causal(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.first_seen_at:
            raise ValueError("an event must expire after Prism first saw it")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("duplicate asset")
        return self

    @property
    def available_at(self):
        """Never earlier than Prism's own observation, whatever the publication time."""
        return self.first_seen_at

    def usable_at(self, decision_time) -> bool:
        """May a decision at ``decision_time`` read this event?"""
        if decision_time < self.first_seen_at:
            return False
        return self.expires_at is None or decision_time < self.expires_at

    @property
    def event_id(self) -> str:
        return content_id("extevent_", self.model_dump(mode="json"))
