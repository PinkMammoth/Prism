"""Neutral post-commit information publication; no account, DB or notice-file writes.

The caller must already have committed context. Consumer delivery failures propagate
to its existing durable receipt retry path. A publication is an immutable value; each
consumer owns persistence, idempotency and recovery. No global subscriber registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from market_signal.intelligence.clocks import AccessClock, utc
from market_signal.research.lab.common import canonical_json, content_id


@dataclass(frozen=True)
class CommittedInformation:
    payload_json: str

    @property
    def payload(self):
        return json.loads(self.payload_json)

    @property
    def publication_id(self):
        return content_id("intpublication_", self.payload)


def publish_context_commit(receipt, payload, *, consumers=(), origin_id="local"):
    if receipt.get("ingest_status") != "INGESTED" or not receipt.get("logical_event_id"):
        return None
    observed = utc(receipt["gateway_received_at"])
    committed = utc(receipt["context_available_at"])
    clock = AccessClock(origin_id, observed.isoformat(), committed.isoformat())
    body = {
        "version": "committed_information_v1",
        "source": "context",
        "idempotency_key": receipt["receipt_id"],
        "event_ids": [receipt["logical_event_id"]],
        "provider": receipt.get("provider"),
        "clocks": clock.as_dict(),
        "clock_quality": "origin commit upper bound measured after commit; consumer receipt unobserved",
        "sender_at": payload.get("sent_at"),
        "recovered_after_commit": bool(receipt.get("recovered_after_commit", False)),
    }
    publication = CommittedInformation(canonical_json(body))
    for consumer in consumers:
        consumer(publication)
    return publication


def access_catalog_from_publication(
    package, publication, *, consumer_id=None, received_at_here=None, available_at_here=None
):
    """Bind a neutral commit receipt to exact retained context evidence records.

    This is an explicit export/subscriber utility, never an automatic runtime write.
    Only reports/links for the published episode observed by that receipt, and their
    source registry records, receive the conservative commit bound. Market data does
    not inherit context clocks. Consumers supply their own durable receive/apply times.
    """
    from market_signal.intelligence.snapshot import validate_package

    package = validate_package(package)
    body = publication.payload
    if body["clocks"]["origin_id"] != package["origin_id"]:
        raise ValueError("publication and package origins differ")
    origin = AccessClock(**body["clocks"])
    if origin.origin_committed_at is None:
        raise ValueError("publication has no commit bound")
    reports, sources = {}, set()
    for eid, evidence in package["evidence"].items():
        row, table = evidence["record"], evidence["table"]
        if table not in ("context_events", "context_event_updates", "context_asset_links"):
            continue
        if row["event_id"] not in body["event_ids"]:
            continue
        observed = utc(evidence["clocks"]["origin_observed_at"])
        if observed > utc(origin.origin_observed_at):
            continue
        if row.get("processed_at") and utc(row["processed_at"]) > utc(origin.origin_committed_at):
            continue
        reports[eid] = observed
        if row.get("source_id"):
            sources.add(row["source_id"])
    for eid, evidence in package["evidence"].items():
        if evidence["table"] == "context_sources" and evidence["key"] in sources:
            observed = utc(evidence["clocks"]["origin_observed_at"])
            if observed <= utc(origin.origin_observed_at):
                reports[eid] = observed
    return {
        eid: AccessClock(
            origin.origin_id,
            observed.isoformat(),
            origin.origin_committed_at,
            consumer_id,
            received_at_here,
            available_at_here,
        )
        for eid, observed in reports.items()
    }
