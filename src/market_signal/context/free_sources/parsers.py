"""Deterministic relevance before insertion; existing Phase 23 observations/materiality."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from market_signal.context.entities import load_entities
from market_signal.context.model import GenericAttrs, Observation
from market_signal.context.providers.base import sha256
from market_signal.context.providers.rss import CRYPTO_WORDS, _text, _time, classify, parse_feed

URGENT = re.compile(
    r"emergency|urgent|critical|consensus[- ]critical|security[- ](?:focused |hotfix|release|fix)|denial.of.service|\bDoS\b|chain.stability|chain.split|remote.code.execution|CVE-\d",
    re.I,
)
ACTION = re.compile(
    r"charges?|sues|lawsuit|settle|approv|ban|suspend|enforce|rule|custod|sanction|emergency|policy|liquidity|interest rate|FOMC statement",
    re.I,
)
NOISE = re.compile(
    r"sponsored|price prediction|price analysis|technical analysis|conference|webinar|roundtable|podcast",
    re.I,
)
TRADING = re.compile(
    r"trading|matching|orders?|liquidat|oracle|pricing|API|perpetual|futures|exchange|platform|login",
    re.I,
)
TRANSFER = re.compile(r"deposit|withdraw|wallet|mints?|burns?|redemption|issuance", re.I)
FAIL = re.compile(
    r"outage|degrad|delay|halt|suspend|unavailable|disrupt|error|investigat|down\b|pause|failure",
    re.I,
)
TECH = re.compile(r"mainnet|sequencer|block production|consensus|network|RPC|validator", re.I)


def _base(s, title, summary, url, published, event_time=None):
    return dict(
        source_id=s["id"],
        source_type=s["source_type"],
        source_ref=url,
        title=title[:500],
        summary=_text(summary)[:1500],
        published_at=_time(published),
        provider_time=_time(published),
        event_time=_time(event_time),
        confidence="OFFICIAL" if s["source_type"] not in ("rss", "news_api") else "REPORTED",
    )


def _status(s, item, em):
    iid, title, status = item["id"], item["name"], item["status"]
    updates = item["incident_updates"]
    if not isinstance(updates, list) or not updates:
        raise ValueError("incident updates missing")
    latest = updates[0]
    details = " ".join(u.get("body", "") for u in updates)
    components = " ".join(c["name"] for c in item.get("components", []))
    text = f"{title} {components} {details}"
    entity = s["entity"]
    if re.search(r"scheduled maintenance", title, re.I):
        return None
    if s["source_class"] == "exchange_status":
        if not (FAIL.search(text) and (TRADING.search(text) or TRANSFER.search(text))):
            return None
        if re.search(r"cards?|support|NFT|prediction markets|tax|staking", title, re.I):
            return None
        sub = (
            "exchange_deposit_withdrawal_disruption"
            if TRANSFER.search(title)
            else "exchange_outage"
        )
        scope = "exchange"
    elif entity == "circle":
        if not (
            TRANSFER.search(text)
            and re.search(r"USDC|mint|burn|redemption", text, re.I)
            and FAIL.search(text)
        ):
            return None
        sub, scope = "custody_disruption", "systemic"
    else:
        if not (TECH.search(text) and FAIL.search(text)):
            return None
        sub = (
            "chain_halt"
            if re.search(
                r"(?:chain|network|block production).*halt|halt.*block production", title, re.I
            )
            else "validator_issue"
        )
        scope = "asset"
    pub = item.get("created_at")
    url = s["url"].split("/api/")[0] + "/incidents/" + iid
    ents = tuple(sorted(set(em.entities_in(text)) | {entity}))
    base = _base(s, title, latest.get("body", ""), url, pub, item.get("started_at"))
    return Observation(
        **base,
        subcategory=sub,
        entities=ents,
        scope=scope,
        market_wide=scope in ("exchange", "systemic"),
        dedup_key=f"statuspage:{entity}:{iid}",
        update_kind="resolution" if status in ("resolved", "completed") else "status_change",
        attributes=GenericAttrs(
            facts={
                "status": status,
                "source_components": components[:1500],
                "source_event_id": iid,
                "source_revision_at": item["updated_at"],
                "information_time": latest.get("updated_at") or item["updated_at"],
            }
        ),
    )


def _github(s, item):
    advisory = s["parser"] == "github_advisories"
    title = item.get("summary") if advisory else item.get("name") or item["tag_name"]
    body = item.get("description", "") if advisory else item.get("body") or ""
    if advisory:
        if item["severity"] not in ("high", "critical") or item.get("withdrawn_at"):
            return None
    elif item.get("draft") or item.get("prerelease") or not URGENT.search(title + " " + body):
        return None
    xid = str(item["ghsa_id"] if advisory else item["id"])
    return Observation(
        **_base(s, title, body, item["html_url"], item["published_at"]),
        subcategory="critical_vulnerability",
        entities=(s["entity"],),
        dedup_key=f"github:{s['repository']}:{xid}",
        attributes=GenericAttrs(
            facts={
                "source_event_id": xid,
                "repository": s["repository"],
                "information_time": item.get("updated_at") or item["published_at"],
                "classifier": "free_github_material_v1",
            }
        ),
    )


def _rss(s, item, em):
    text = item["title"] + " " + _text(item["description"])
    if NOISE.search(text):
        return None
    kind = s["kind"]
    if kind in ("regulator", "central_bank"):
        # Fed identity alone is insufficient. Do not ingest every Fed/Treasury notice.
        if kind == "central_bank" and re.search(
            r"emergency|unscheduled|liquidity|extraordinary", text, re.I
        ):
            return Observation(
                **_base(s, item["title"], item["description"], item["link"], item["published"]),
                subcategory="central_bank_statement",
                market_wide=True,
                scope="systemic",
            )
        if not ACTION.search(text) or not (
            CRYPTO_WORDS.search(text)
            or set(em.entities_in(text)) - {"federal_reserve", "us_treasury"}
            or "FOMC statement" in item["title"]
        ):
            return None
        obs = classify(item, s["id"], s, em)
        return obs.model_copy(update={"source_id": s["id"]}) if obs else None
    if kind == "technical":
        if not URGENT.search(text):
            return None
        return Observation(
            **_base(s, item["title"], item["description"], item["link"], item["published"]),
            subcategory="critical_vulnerability",
            entities=(s["entity"],),
        )
    ents = em.entities_in(text)
    if not ents and not CRYPTO_WORDS.search(text):
        return None
    sub = None
    if re.search(r"exploit|hack|stolen", item["title"], re.I):
        sub = "exploit_suspected"
    elif re.search(r"depeg|loses.*peg", item["title"], re.I):
        sub = "stablecoin_depeg"
    elif FAIL.search(item["title"]) and TRADING.search(item["title"]):
        sub = "exchange_outage"
    elif ACTION.search(item["title"]):
        sub = "regulatory_action"
    elif URGENT.search(item["title"]):
        sub = "critical_vulnerability"
    if sub is None:
        return None
    return Observation(
        **_base(s, item["title"], item["description"], item["link"], item.get("published")),
        subcategory=sub,
        entities=tuple(ents),
        market_wide=not ents,
        scope="asset" if ents else "systemic",
    )


def parse(s, body, received):
    em = load_entities()
    if s["parser"] == "rss":
        try:
            root_tag = ET.fromstring(body).tag
        except ET.ParseError as exc:
            raise ValueError("malformed feed XML") from exc
        if root_tag not in (
            "rss",
            "{http://www.w3.org/2005/Atom}feed",
            "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF",
        ):
            raise ValueError("unexpected feed schema")
        items = parse_feed(body)
    else:
        data = json.loads(body)
        if s["parser"] == "statuspage":
            items = data["incidents"]
        elif s["parser"] == "gdelt":
            items = data["articles"]
        else:
            items = data
        if not isinstance(items, list):
            raise ValueError("source list schema changed")
    observations, rejected = [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for item in items[:100]:
        if s["parser"] == "statuspage":
            obs = _status(s, item, em)
        elif s["parser"].startswith("github"):
            obs = _github(s, item)
        elif s["parser"] == "gdelt":
            if urlsplit(item["url"]).hostname not in s["allowed_domains"]:
                reject("domain_quality")
                continue
            obs = _rss(
                s, dict(title=item["title"], description="", link=item["url"], published=None), em
            )
            if obs:
                obs = obs.model_copy(
                    update={
                        "attributes": GenericAttrs(
                            facts={
                                "information_time": _time(item["seendate"]).isoformat(),
                                "source_availability_at": _time(item["seendate"]).isoformat(),
                            }
                        )
                    }
                )
        else:
            obs = _rss(s, item, em)
        if obs is None:
            reject("irrelevant")
            continue
        info = (
            _time(getattr(obs.attributes, "facts", {}).get("information_time"))
            or obs.provider_time
            or obs.published_at
        )
        if info is None:
            reject("missing_information_time")
            continue
        age = (received - info).total_seconds()
        if age < -300:
            reject("clock_skew")
            continue
        if age > s["freshness_seconds"]:
            reject("stale")
            continue
        facts = dict(getattr(obs.attributes, "facts", {}))
        facts.update(
            source_event_id=str(
                item.get("id") or item.get("guid") or item.get("ghsa_id") or obs.source_ref
            ),
            information_time=info.isoformat(),
        )
        # Hash each item, not the whole feed: a different story must not revise this report.
        obs = obs.model_copy(
            update={
                "attributes": GenericAttrs(facts=facts),
                "raw_sha256": sha256(json.dumps(item, sort_keys=True).encode()),
            }
        )
        observations.append(obs.model_dump(mode="json"))
    return observations, {"detected": min(len(items), 100), "rejected": rejected}
