"""Single-item extension of context_import_v1. Never an execution instruction."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field

from market_signal.context.entities import EntityMap
from market_signal.context.model import GenericAttrs, Observation, Short
from market_signal.context.providers.importer import URL, ImportItem, _forbidden, validate_item
from market_signal.context.taxonomy import SUBCATEGORIES
from market_signal.research.lab.common import (
    LabModel,
    Name,
    UTCDateTime,
    canonical_json,
    content_id,
)

ID = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


class GatewayItem(ImportItem):
    first_seen_at: UTCDateTime = Field(
        description="Sender discovery time only. Prism independently stamps authoritative knowledge time."
    )
    subcategory: Name = Field(json_schema_extra={"enum": sorted(SUBCATEGORIES)})
    source_type: Literal["ai_monitor"] = "ai_monitor"
    confidence: Literal["UNCONFIRMED", "REPORTED"]
    assets: Annotated[tuple[Short, ...], Field(max_length=16)] = ()
    entities: Annotated[tuple[Short, ...], Field(max_length=16)] = ()
    attributes: GenericAttrs = Field(default_factory=GenericAttrs)
    update_kind: None = None
    dedup_key: None = None
    relevance_end: None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    region: Short | None = None
    factual_claims: Annotated[tuple[Short, ...], Field(max_length=16)] = ()
    provenance: Annotated[tuple[Short, ...], Field(max_length=8)] = ()
    corroborating_urls: Annotated[tuple[URL, ...], Field(max_length=8)] = ()


class Submission(LabModel):
    schema_version: Literal["context_import_v1"] = Field(alias="schema")
    submission_id: ID
    external_event_id: ID
    sent_at: UTCDateTime = Field(description="Actual UTC send time; preserve unchanged on retries.")
    sender_version: ID
    test: bool = Field(default=False, description="True excludes this receipt from active context.")
    item: GatewayItem


def action_schema() -> dict:
    schema = Submission.model_json_schema(by_alias=True)
    # JSON Schema $refs are rooted at the complete tool schema, not its payload property.
    definitions = schema.pop("$defs", {})
    return {
        "type": "object",
        "$defs": definitions,
        "properties": {"payload": schema},
        "required": ["payload"],
        "additionalProperties": False,
    }


def public_url(value: str) -> None:
    """Syntax containment, not a claim that a URL or its report is true. No fetching."""
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
        raise ValueError("source URLs must be public HTTPS URLs without credentials/fragments")
    if u.port not in (None, 443) or "." not in u.hostname or u.hostname.endswith(".local"):
        raise ValueError("invalid public source host")
    try:
        address = ipaddress.ip_address(u.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("private source address")


def validate(raw: dict, received: datetime, em: EntityMap) -> tuple[Submission, Observation]:
    def forbidden_extra(obj):
        if isinstance(obj, dict):
            return any(
                k
                in {
                    "position_size",
                    "order_parameters",
                    "executable_code",
                    "code",
                    "script",
                    "shell",
                    "risk_change",
                    "strategy_registration",
                    "materiality",
                    "interpretation",
                    "market_interpretation",
                    "hypothesis",
                    "command",
                    "arbitrary_code",
                }
                or forbidden_extra(v)
                for k, v in obj.items()
            )
        return any(forbidden_extra(v) for v in obj) if isinstance(obj, list) else False

    if not isinstance(raw, dict) or not isinstance(raw.get("item"), dict):
        raise ValueError("submission/item must be objects")
    for field in ("sent_at",):
        if not isinstance(raw.get(field), str):
            raise ValueError("timestamps must be timezone-aware ISO strings")
    for field in ("first_seen_at", "published_at", "event_time", "relevance_end"):
        if raw["item"].get(field) is not None and not isinstance(raw["item"][field], str):
            raise ValueError("timestamps must be timezone-aware ISO strings")
    for obj, fields in ((raw, ("test",)), (raw["item"], ("market_wide", "scheduled"))):
        if any(field in obj and type(obj[field]) is not bool for field in fields):
            raise ValueError("flags must be JSON booleans")
    if _forbidden(raw) or forbidden_extra(raw):
        raise ValueError("AI boundary: forbidden trade/sentiment field")
    sub = Submission.model_validate(raw)
    it = sub.item
    if it.update_kind is not None or it.dedup_key is not None or it.relevance_end is not None:
        raise ValueError("external updates, dedup keys and relevance overrides are not accepted")
    if sub.sent_at > received or it.first_seen_at > sub.sent_at:
        raise ValueError("future/inconsistent sender timestamp")
    if it.event_time and it.event_time > received and not it.scheduled:
        raise ValueError("future event_time requires a scheduled event")
    for url in (it.url, *it.corroborating_urls):
        public_url(url)
    if len(it.attributes.facts) > 32 or len(canonical_json(it.attributes.facts)) > 8000:
        raise ValueError("facts exceed bounds")
    known_assets = set(em.market_wide_assets)
    for entity in em.entities.values():
        known_assets.update(entity.assets)
        known_assets.update(entity.ecosystem)
    assets = []
    for alias in it.assets:
        a = alias.upper()
        if a not in known_assets:
            matches = [
                e
                for e in em.entities.values()
                if alias.casefold() in {x.casefold() for x in (*e.aliases, *e.ticker_aliases)}
                and len(e.assets) == 1
            ]
            if len(matches) != 1:
                raise ValueError("unknown/ambiguous asset")
            a = matches[0].assets[0]
        assets.append(a)
    entities = []
    for alias in it.entities:
        matches = [
            e.entity_id
            for e in em.entities.values()
            if alias.casefold()
            in {x.casefold() for x in (e.entity_id, *e.aliases, *e.ticker_aliases)}
        ]
        if len(matches) != 1:
            raise ValueError("unknown/ambiguous entity")
        entities.append(matches[0])
    extra = {
        "source_type",
        "country",
        "region",
        "factual_claims",
        "provenance",
        "corroborating_urls",
    }
    item = it.model_dump(mode="json", exclude=extra)
    item.update(assets=assets, entities=entities)
    obs = validate_item(item, "chatgpt_work", received, em)
    if not assets and not entities and not it.market_wide:
        raise ValueError("an event needs a known asset/entity or market_wide=true")
    return sub, obs.model_copy(
        update={
            "source_id": "chatgpt_work_v1",
            "country": it.country,
            "region": it.region,
            "raw_sha256": content_id("", raw),
        }
    )
