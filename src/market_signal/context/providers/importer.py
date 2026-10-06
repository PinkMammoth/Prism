"""External event import (``context_import_v1``): ChatGPT Work monitoring output, manual entries.

This is one ingestion route among several, never the only one. Every item passes
deterministic validation before it can be stored:

* strict JSON (duplicate keys / non-finite numbers rejected), unknown fields rejected;
* the **AI boundary**: any field that expresses a trade (direction, side, size, order,
  leverage, target, stop, signal, recommendation, sentiment score...) rejects the item. An
  external monitor reports facts; Prism research decides what, if anything, they mean;
* the subcategory must be in the versioned taxonomy and match the category;
* entities must exist in the explicit entity map (an unknown entity is rejected, never guessed);
* the source URL must be http(s);
* the monitor's own ``first_seen_at`` must not be in the future relative to Prism's receipt.

Availability: an imported event exists for Prism from **Prism's receipt time** (the ledger
stamps it). The monitor's ``first_seen_at`` is kept as ``reported_first_seen_at`` for latency
analysis only. AI-monitor reports are tier 3, so their confidence is capped at REPORTED until
an independent tier-1/2 source corroborates them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field, ValidationError

from market_signal.context.entities import EntityMap
from market_signal.context.model import Attributes, GenericAttrs, Observation, Short, Summary
from market_signal.context.taxonomy import SUBCATEGORIES, Confidence, SourceType
from market_signal.research.lab.common import LabModel, Name, Symbol, UTCDateTime, strict_json

IMPORT_SCHEMA = "context_import_v1"
PRODUCERS = {"chatgpt_work": SourceType.AI_MONITOR, "ai_monitor": SourceType.AI_MONITOR,
             "manual": SourceType.MANUAL}  # fmt: skip
FORBIDDEN_KEYS = frozenset({
    "direction", "side", "trade", "order", "orders", "size", "position", "leverage", "target",
    "target_price", "stop", "stop_loss", "take_profit", "entry", "exit", "signal", "buy", "sell",
    "long", "short", "recommendation", "action_to_take", "sentiment", "sentiment_score",
    "conviction", "expected_return", "price_target",
})  # fmt: skip
URL = Annotated[str, Field(strict=True, pattern=r"^https?://[^\s]{3,2000}$")]


class ImportItem(LabModel):
    event_time: UTCDateTime | None = None
    published_at: UTCDateTime | None = None
    first_seen_at: UTCDateTime  # the MONITOR's first sighting (kept as reported latency only)
    source: Short  # who published it, e.g. "Hyperliquid status", "Reuters"
    url: URL
    assets: tuple[Symbol, ...] = ()
    entities: tuple[Name, ...] = ()
    market_wide: bool = False
    category: Literal["macro", "market_structure", "protocol", "security"]
    subcategory: Name
    title: Short
    summary: Summary = ""
    confidence: Confidence
    scheduled: bool = False
    update_kind: Literal["corroboration", "revision", "denial", "resolution", "consensus",
                         "value_release", "confidence_change"] | None = None  # fmt: skip
    dedup_key: str | None = None
    relevance_end: UTCDateTime | None = None
    attributes: Attributes = Field(default_factory=GenericAttrs)


class ImportBatch(LabModel):
    schema_version: Literal["context_import_v1"] = Field(alias="schema")
    producer: Literal["chatgpt_work", "ai_monitor", "manual"]
    session: Short | None = None
    items: Annotated[tuple[dict[str, Any], ...], Field(min_length=1, max_length=500)]


def _forbidden(obj: Any, path: str = "") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                hits.append(f"{path}{k}")
            hits += _forbidden(v, f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _forbidden(v, f"{path}{i}.")
    return hits


def validate_item(raw: dict, producer: str, received_at: datetime, em: EntityMap) -> Observation:
    """One import item → Observation, or raise ValueError with the reason."""
    bad = _forbidden(raw)
    if bad:
        raise ValueError(f"AI boundary: trade/sentiment fields are not accepted ({', '.join(bad)})")
    try:
        it = ImportItem.model_validate(raw)
    except ValidationError as exc:
        e = exc.errors()[0]
        raise ValueError(f"invalid field {'.'.join(map(str, e['loc']))}: {e['msg']}") from None
    if it.subcategory not in SUBCATEGORIES:
        raise ValueError(f"unknown subcategory {it.subcategory!r}")
    if SUBCATEGORIES[it.subcategory].value != it.category:
        raise ValueError(f"subcategory {it.subcategory!r} is not in category {it.category!r}")
    unknown = [e for e in it.entities if e not in em.entities]
    if unknown:
        raise ValueError(f"unknown entities {unknown} (add them to config/context/entities.yaml)")
    if it.first_seen_at > pd.Timestamp(received_at).to_pydatetime():
        raise ValueError("first_seen_at is after Prism received the item")
    if it.published_at and it.published_at > pd.Timestamp(received_at).to_pydatetime():
        raise ValueError("published_at is after Prism received the item")
    key = it.dedup_key
    try:
        return Observation(
            source_id=f"import_{producer}", source_type=PRODUCERS[producer], source_ref=it.url,
            subcategory=it.subcategory, title=it.title, summary=it.summary,
            scheduled=it.scheduled, event_time=it.event_time, published_at=it.published_at,
            reported_first_seen_at=it.first_seen_at, confidence=it.confidence,
            entities=it.entities, assets=it.assets, market_wide=it.market_wide,
            relevance_end=it.relevance_end, dedup_key=key, update_kind=it.update_kind,
            attributes=it.attributes.model_copy(update={"facts": {
                **getattr(it.attributes, "facts", {}), "reported_source": it.source}})
            if isinstance(it.attributes, GenericAttrs) else it.attributes,
        )  # fmt: skip
    except ValidationError as exc:
        e = exc.errors()[0]
        raise ValueError(
            f"invalid observation {'.'.join(map(str, e['loc']))}: {e['msg']}"
        ) from None


def parse_batch(
    text: str, received_at: datetime, em: EntityMap
) -> tuple[list[Observation], list[dict]]:
    """(valid observations, [{index, error}]) for one import file. A malformed envelope raises."""
    data = strict_json(text)
    bad = (
        _forbidden({k: v for k, v in data.items() if k != "items"})
        if isinstance(data, dict)
        else []
    )
    if bad:
        raise ValueError(f"AI boundary: forbidden envelope fields {bad}")
    batch = ImportBatch.model_validate(data)
    good, errors = [], []
    for i, raw in enumerate(batch.items):
        try:
            good.append(validate_item(raw, batch.producer, received_at, em))
        except ValueError as exc:
            errors.append({"index": i, "error": str(exc)[:300]})
    return good, errors


def import_events(
    store, text: str, *, now: datetime | None = None, em: EntityMap | None = None
) -> dict:
    """Validate + ingest one batch; records a provider run (``import_<producer>``)."""
    from market_signal.context import ledger
    from market_signal.context.entities import load_entities
    from market_signal.models.domain import utcnow

    em = em or load_entities()
    received = pd.Timestamp(now or utcnow()).tz_convert("UTC").to_pydatetime()
    data = strict_json(text)
    producer = str((data or {}).get("producer", "unknown")) if isinstance(data, dict) else "unknown"
    good, errors = parse_batch(text, received, em)
    run_id = ledger.new_run_id(f"import_{producer}")
    res = ledger.ingest(store, good, run_id=run_id, now=received, entity_map=em)
    ledger.record_run(store, f"import_{producer}", received,
                      status="partial" if errors or res.rejected else "ok",
                      received=len(good) + len(errors), result=res, run_id=run_id,
                      payload={"validation_errors": errors})  # fmt: skip
    return {"producer": producer, "received_at": received.isoformat(), "valid": len(good),
            "invalid": errors, **res.as_dict()}  # fmt: skip
