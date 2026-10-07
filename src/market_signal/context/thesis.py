"""Research-only ``TradeThesis`` (``trade_thesis_v1``) with complete evidence provenance.

A thesis combines catalyst/context, positioning, activity, an OPTIONAL direction hypothesis,
an invalidation idea and an expiry. It is a research record: status is always
``research_only`` (CHECKed in the table). It carries no size, leverage, order or stop price
for execution, and nothing in Prism reads it to trade.

Construction is deterministic and cannot invent facts:
* the context/positioning/activity fields are DERIVED from a recorded context snapshot,
  never typed in;
* every cited event must be in that snapshot (known at ``as_of``), else the thesis is rejected;
* an AI may *propose* a thesis (``proposal_from_ai``), but only the fields of
  ``ThesisProposal`` are accepted, and the result goes through the same validation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

import pandas as pd
from pydantic import Field, ValidationError

from market_signal.context.providers.importer import _forbidden
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Symbol,
    UTCDateTime,
    canonical_json,
    content_id,
    strict_json,
)

THESIS_VERSION = "trade_thesis_v1"
Short = Annotated[str, Field(strict=True, min_length=1, max_length=500)]
MAX_EXPIRY = pd.Timedelta(days=30)


class Invalidation(LabModel):
    kind: Literal["price_level", "time", "event_denied", "positioning_normalised", "other"]
    level: float | None = None  # research reference only (e.g. a price that falsifies the idea)
    description: Short


class Evidence(LabModel):
    snapshot_id: Annotated[str, Field(pattern=r"^ctxsnap_[0-9a-f]{64}$")]
    event_ids: tuple[str, ...]
    oi_observations: tuple[str, ...]  # "venue:coin:open_interest@age_minutes"
    price_state: dict[str, str | float | bool | None]
    rule_versions: dict[Name, str]


class TradeThesis(LabModel):
    schema_version: Literal["trade_thesis_v1"] = THESIS_VERSION
    asset: Symbol
    as_of: UTCDateTime
    status: Literal["research_only"] = "research_only"
    origin: Literal["manual", "rule", "ai_proposal"]
    context: dict[Name, str | int | float | bool | tuple[str, ...] | None]
    positioning: dict[Name, str | None]
    activity: Literal["elevated", "normal", "quiet", "unknown"]
    direction_hypothesis: Literal["long", "short", "none"]
    hypothesis_rule: Short  # the rule/version or 'manual' that produced the hypothesis
    invalidation: Invalidation
    expires_at: UTCDateTime
    evidence: Evidence

    @property
    def thesis_id(self) -> str:
        return content_id("thesis_", self.model_dump(mode="json"))


class ThesisProposal(LabModel):
    """The only fields an external/AI proposer may supply."""

    asset: Symbol
    as_of: UTCDateTime
    cited_event_ids: tuple[str, ...] = ()
    direction_hypothesis: Literal["long", "short", "none"] = "none"
    hypothesis_rule: Short = "ai_proposal"
    invalidation: Invalidation
    expires_at: UTCDateTime


def load_snapshot(store: Store, snapshot_id: str) -> dict:
    r = store.con.execute("SELECT payload FROM context_snapshots WHERE snapshot_id=?",
                          [snapshot_id]).fetchone()  # fmt: skip
    if not r:
        raise ValueError(f"snapshot {snapshot_id} is not recorded")
    return {"snapshot_id": snapshot_id, **json.loads(r[0])}


def build_thesis(snapshot: dict, *, direction: str, hypothesis_rule: str, invalidation: dict,
                 expires_at: datetime, cited_event_ids: tuple[str, ...] = (),
                 origin: str = "manual") -> TradeThesis:  # fmt: skip
    as_of = pd.Timestamp(snapshot["as_of"])
    known = {e["event_id"]: e for e in snapshot["active_events"] + snapshot["recent_events"]
             + snapshot["upcoming_linked"]}  # fmt: skip
    unknown = [e for e in cited_event_ids if e not in known]
    if unknown:
        raise ValueError(f"cited events not in the snapshot (unknown at as_of): {unknown}")
    if pd.Timestamp(expires_at) <= as_of:
        raise ValueError("expires_at must be after as_of")
    if pd.Timestamp(expires_at) - as_of > MAX_EXPIRY:
        raise ValueError("a thesis may not stay open longer than 30 days")
    cited = [known[e] for e in cited_event_ids] or snapshot["active_events"][:5]
    pos = (snapshot.get("positioning") or {}).get("venues") or {}
    oi = tuple(f"{v}:{snapshot['asset']}:{b.get('open_interest')}@{b.get('age_minutes')}m"
               for v, b in sorted(pos.items()) if b.get("open_interest") is not None)  # fmt: skip
    act = snapshot["activity"]
    macro = snapshot["macro"]
    nxt = macro.get("next_tier1") or {}
    return TradeThesis(
        asset=snapshot["asset"], as_of=as_of.to_pydatetime(), origin=origin,
        context={
            "catalyst_subcategories": tuple(sorted({e["subcategory"] for e in cited})),
            "max_materiality": max((e["materiality"] for e in cited), default=0),
            "minutes_to_next_tier1": macro.get("minutes_to_next_tier1"),
            "next_tier1": nxt.get("subcategory"),
            "in_post_event_window": macro.get("in_post_event_window"),
        },
        positioning={f"{v}_{k}": b["crowding"][k] for v, b in sorted(pos.items())
                     for k in ("leverage", "skew")},
        activity=act["state"], direction_hypothesis=direction, hypothesis_rule=hypothesis_rule,
        invalidation=Invalidation.model_validate(invalidation),
        expires_at=pd.Timestamp(expires_at).to_pydatetime(),
        evidence=Evidence(
            snapshot_id=snapshot["snapshot_id"], event_ids=tuple(e["event_id"] for e in cited),
            oi_observations=oi,
            price_state={"source": act.get("source"), "bar_close": act.get("bar_close"),
                         "tr_atr": act.get("tr_atr"), "rel_volume": act.get("rel_volume"),
                         "rv_state": act.get("rv_state")},
            rule_versions={"snapshot": snapshot["version"], "macro_state": macro["version"],
                           "activity": act["version"], "thesis": THESIS_VERSION,
                           **{f"crowding_{v}": b["crowding"]["version"] for v, b in pos.items()}}),
    )  # fmt: skip


def record_thesis(store: Store, thesis: TradeThesis) -> str:
    tid = thesis.thesis_id
    store.con.execute(
        "INSERT INTO context_theses VALUES (?, ?, ?, ?, 'research_only', ?, ?) ON CONFLICT DO NOTHING",
        [tid, thesis.asset, thesis.as_of, max(utcnow(), thesis.as_of), thesis.evidence.snapshot_id,
         canonical_json(thesis.model_dump(mode="json"))],
    )  # fmt: skip
    return tid


def proposal_from_ai(store: Store, text: str) -> TradeThesis:
    """Validate an AI-proposed thesis: strict JSON, no trade fields, cited events must exist in
    a snapshot recorded for (asset, as_of). Rebuilt deterministically; nothing is trusted."""
    data = strict_json(text)
    bad = [k for k in _forbidden(data) if not k.startswith("direction")]
    if bad:
        raise ValueError(f"AI boundary: execution fields not accepted ({bad})")
    try:
        p = ThesisProposal.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid proposal: {exc.errors()[0]['msg']}") from None
    r = store.con.execute("SELECT snapshot_id FROM context_snapshots WHERE asset=? AND as_of=? "
                          "ORDER BY recorded_at LIMIT 1", [p.asset, p.as_of]).fetchone()  # fmt: skip
    if not r:
        raise ValueError("no recorded snapshot for that asset/as_of: record one first")
    return build_thesis(load_snapshot(store, r[0]), direction=p.direction_hypothesis,
                        hypothesis_rule=p.hypothesis_rule, invalidation=p.invalidation.model_dump(),
                        expires_at=p.expires_at, cited_event_ids=p.cited_event_ids,
                        origin="ai_proposal")  # fmt: skip
