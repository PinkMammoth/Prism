"""Explicit access clocks; publication/processing timestamps never imply commit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd


class ClockBasis(StrEnum):
    ORIGIN_OBSERVED = "origin-observed"
    ORIGIN_COMMITTED = "origin-committed"
    CONSUMER_AVAILABLE = "consumer-available"


def utc(value):
    t = pd.Timestamp(value)
    if pd.isna(t) or t.tzinfo is None:
        raise ValueError("a finite timezone-aware timestamp is required")
    return t.tz_convert("UTC")


@dataclass(frozen=True)
class AccessClock:
    """Caller-supplied, content-bound receipt evidence, not a runtime authority claim.

    Committed/available may be conservative upper bounds measured after the operation.
    A missing clock stays unknown. Local and replicated consumers use the same contract.
    """

    origin_id: str
    origin_observed_at: str
    origin_committed_at: str | None = None
    consumer_id: str | None = None
    received_at_here: str | None = None
    available_at_here: str | None = None

    def __post_init__(self):
        if not self.origin_id:
            raise ValueError("origin identity required")
        observed = utc(self.origin_observed_at)
        committed = utc(self.origin_committed_at) if self.origin_committed_at else None
        received = utc(self.received_at_here) if self.received_at_here else None
        available = utc(self.available_at_here) if self.available_at_here else None
        if committed is not None and committed < observed:
            raise ValueError("commit precedes observation")
        if received is not None and (committed is None or received < committed):
            raise ValueError("receipt requires a preceding commit bound")
        if available is not None and (
            received is None or available < received or not self.consumer_id
        ):
            raise ValueError("availability requires consumer identity and preceding receipt")

    def time(self, basis: ClockBasis, consumer_id: str):
        if basis == ClockBasis.ORIGIN_OBSERVED:
            return utc(self.origin_observed_at)
        if basis == ClockBasis.ORIGIN_COMMITTED:
            return utc(self.origin_committed_at) if self.origin_committed_at else None
        if self.consumer_id != consumer_id:
            return None
        return utc(self.available_at_here) if self.available_at_here else None

    def as_dict(self):
        return {
            "origin_id": self.origin_id,
            "origin_observed_at": utc(self.origin_observed_at).isoformat(),
            "origin_committed_at": (
                utc(self.origin_committed_at).isoformat() if self.origin_committed_at else None
            ),
            "consumer_id": self.consumer_id,
            "received_at_here": utc(self.received_at_here).isoformat()
            if self.received_at_here
            else None,
            "available_at_here": (
                utc(self.available_at_here).isoformat() if self.available_at_here else None
            ),
        }
