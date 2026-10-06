"""Official RSS/Atom feeds with a deterministic, versioned classifier (``rss_classifier_v1``).

Quality over quantity: an item is kept only if a rule classifies it AND it is either
exchange/system-wide in scope or names an entity in the explicit entity map. Everything else
is counted as ``filtered`` and never stored. No sentiment, no direction, no LLM.

Feed kinds:
  statuspage    exchange status pages (one item per incident; the incident URL is stable, so
                later re-polls with new status text become updates of the same event)
  central_bank  e.g. Fed press releases: FOMC statements (linked to the scheduled decision),
                and crypto/stablecoin/digital-asset items
  regulator     SEC/CFTC press: only items naming a mapped entity or a crypto keyword
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import pandas as pd

from market_signal.context.entities import EntityMap
from market_signal.context.model import GenericAttrs, Observation
from market_signal.context.providers.base import FetchResult, sha256
from market_signal.context.providers.calendar import dedup_key as macro_key
from market_signal.context.taxonomy import SourceType
from market_signal.data.http import HttpClient, SchemaError

CLASSIFIER_VERSION = "rss_classifier_v1"
ATOM = "{http://www.w3.org/2005/Atom}"

CRYPTO_WORDS = re.compile(r"\b(crypto(?:currenc(?:y|ies)|-?assets?)?|digital[- ]assets?|bitcoin|"
                          r"stablecoins?|blockchain|tokeni[sz]ed|DeFi|virtual currenc(?:y|ies))\b",
                          re.I)  # fmt: skip
OUTAGE = re.compile(r"\b(outage|degraded|unavailable|down|disruption|elevated error|trading "
                    r"(?:halt|halted|paused|suspended)|connectivity issues?|investigating)\b", re.I)  # fmt: skip
TRANSFERS = re.compile(
    r"\b(delayed sends|sends|receives|deposits?|withdrawals?|transfers?|funding)\b", re.I
)
# an outage counts only if it touches trading/market access (not cards, payments, support...)
TRADING_SCOPE = re.compile(r"\b(trading|trades?|orders?|order placement|API|futures|perps?|"
                           r"perpetuals?|market data|matching|spot|exchange|login|sign[- ]?in|"
                           r"website|site|web/mobile|platform)\b", re.I)  # fmt: skip
NON_MARKET = re.compile(r"\b(card|payments?|customer support|support response|prediction "
                        r"markets?|NFT|learn|earn|staking rewards|tax)\b", re.I)  # fmt: skip
PRODUCT_STATUS = re.compile(r"^([A-Z0-9]{2,10})[-/](?:USD|USDC|USDT|EUR|GBP|BTC)\b.*\b(limit only|"
                            r"cancel only|post only|auction|delist|halt)", re.I)  # fmt: skip
MAINT = re.compile(r"scheduled|maintenance|THIS IS A SCHEDULED EVENT", re.I)
STATUS = re.compile(r"<strong>\s*(Resolved|Completed|Monitoring|Identified|Investigating|Update|"
                    r"Scheduled|In progress|Verifying)\s*</strong>", re.I)  # fmt: skip
TICKER_SUFFIX = re.compile(r"\s[-–]\s*\(?([A-Z0-9]{2,10})\)?\s*$")
FOMC_STATEMENT = re.compile(r"\bFOMC statement\b", re.I)
CHARGES = re.compile(r"\b(charges?|charged|sues|lawsuit|complaint|settle(?:s|ment)?)\b", re.I)


def _text(x: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()


def _time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = pd.Timestamp(s).to_pydatetime()
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=pd.Timestamp(0, tz="UTC").tzinfo)


def parse_feed(body: bytes) -> list[dict]:
    """RSS 2.0 ``item`` or Atom ``entry`` → dicts (title, link, guid, description, pubDate)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SchemaError(f"feed is not XML: {exc}") from exc
    out = []
    for it in root.iter("item"):
        out.append({"title": (it.findtext("title") or "").strip(), "link": (it.findtext("link") or "").strip(),
                    "guid": (it.findtext("guid") or "").strip(), "description": it.findtext("description") or "",
                    "published": it.findtext("pubDate"), "category": it.findtext("category") or ""})  # fmt: skip
    for it in root.iter(f"{ATOM}entry"):
        link = it.find(f"{ATOM}link")
        out.append({"title": (it.findtext(f"{ATOM}title") or "").strip(),
                    "link": link.get("href", "") if link is not None else "",
                    "guid": it.findtext(f"{ATOM}id") or "",
                    "description": it.findtext(f"{ATOM}summary") or it.findtext(f"{ATOM}content") or "",
                    "published": it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated"),
                    "category": ""})  # fmt: skip
    return out


def classify(item: dict, feed_id: str, feed: dict, em: EntityMap) -> Observation | None:
    """One feed item → an observation, or None (noise / unmapped). Pure and deterministic."""
    title, desc = item["title"], _text(item["description"])
    text = f"{title}\n{desc}"
    ents = em.entities_in(text)
    kind = feed["kind"]
    st = SourceType(feed["source_type"])
    pub = _time(item.get("published"))
    ref = item.get("link") or item.get("guid") or None
    base = {"source_id": f"rss_{feed_id}", "source_type": st, "source_ref": ref, "title": title[:500],
            "summary": desc[:1500], "published_at": pub, "provider_time": pub,
            "country": feed.get("country"), "confidence": "OFFICIAL"}  # fmt: skip
    if kind == "statuspage":
        exch = feed["exchange"]
        m = STATUS.search(item["description"] or "")
        status = m.group(1).lower() if m else "unknown"
        tick = TICKER_SUFFIX.search(title)
        assets = ()
        if tick and em.asset_entity(tick.group(1)):
            assets = (tick.group(1),)
        prod = PRODUCT_STATUS.search(title)
        if prod:  # one product's trading mode: kept only for a mapped asset
            if not em.asset_entity(prod.group(1).upper()):
                return None
            assets = (prod.group(1).upper(),)
        mapped = bool(assets) or bool(set(ents) - {exch})
        if prod:
            sub, scope = "risk_parameter_change", "asset"
        elif MAINT.search(text) and not OUTAGE.search(title):
            if not mapped:
                return None
            sub, scope = "exchange_maintenance", "asset"
        elif TRANSFERS.search(title):
            if not mapped:
                return None  # a single unmapped coin's deposit delay is noise for Prism
            sub, scope = "exchange_deposit_withdrawal_disruption", "asset"
        elif OUTAGE.search(text) and TRADING_SCOPE.search(title) and not NON_MARKET.search(title):
            sub, scope = "exchange_outage", "exchange"
        else:
            return None
        iid = urlsplit(ref or "").path.rstrip("/").rsplit("/", 1)[-1] or sha256(title.encode())[:16]
        facts = {"exchange": exch, "status": status, "classifier": CLASSIFIER_VERSION}
        return Observation(**base, subcategory=sub, entities=tuple(sorted(set(ents) | {exch}
                           if exch in em.entities else set(ents))), assets=assets, scope=scope,
                           dedup_key=f"statuspage:{exch}:{iid}",
                           update_kind="resolution" if status in ("resolved", "completed") else None,
                           attributes=GenericAttrs(facts=facts))  # fmt: skip
    if kind == "central_bank":
        if FOMC_STATEMENT.search(title) and pub is not None:
            return Observation(**base, subcategory="fomc_decision", scheduled=True,
                               event_time=pub, market_wide=True, scope="systemic",
                               dedup_key=macro_key("fomc_decision", pd.Timestamp(pub).tz_convert(
                                   "America/New_York").date()), update_kind="value_release",
                               attributes=GenericAttrs(facts={"statement_url": ref or "",
                                                              "classifier": CLASSIFIER_VERSION}))  # fmt: skip
        if CRYPTO_WORDS.search(text) or ents:
            return Observation(**base, subcategory="regulatory_action", entities=tuple(ents),
                               market_wide=not ents, scope="systemic" if not ents else "asset",
                               attributes=GenericAttrs(facts={"classifier": CLASSIFIER_VERSION}))  # fmt: skip
        return None
    if kind == "regulator":
        if not (ents or CRYPTO_WORDS.search(text)):
            return None
        sub = "lawsuit" if CHARGES.search(title) and ents else "regulatory_action"
        return Observation(**base, subcategory=sub, entities=tuple(ents), market_wide=not ents,
                           scope="systemic" if not ents else "asset",
                           attributes=GenericAttrs(facts={"classifier": CLASSIFIER_VERSION}))  # fmt: skip
    return None


class RssProvider:
    name = "rss"
    stale_hours = 3.0

    def __init__(self, feeds: dict[str, dict], em: EntityMap, user_agent: str,
                 transport=None, max_items: int = 50):  # fmt: skip
        self.feeds, self.em, self.ua, self.transport = feeds, em, user_agent, transport
        self.max_items = max_items

    def fetch(self, now: datetime, last_state: dict) -> FetchResult:
        fr = FetchResult()
        errors = []
        for fid, feed in sorted(self.feeds.items()):
            u = urlsplit(feed["url"])
            http = HttpClient(provider=f"rss_{fid}", base_url=f"{u.scheme}://{u.netloc}",
                              requests_per_second=1, max_retries=2, backoff_seconds=1,
                              user_agent=self.ua, transport=self.transport)  # fmt: skip
            try:
                body = http.request("GET", u.path + (f"?{u.query}" if u.query else ""))
                items = parse_feed(body)[: self.max_items]
            except Exception as exc:  # one feed failing never blocks the others
                errors.append(f"{fid}: {str(exc)[:120]}")
                fr.raw += http.drain()
                continue
            fr.raw += http.drain()
            raw_hash = sha256(body)
            for it in items:
                fr.received += 1
                obs = classify(it, fid, feed, self.em)
                if obs is None:
                    fr.filtered += 1
                    continue
                fr.observations.append(obs.model_copy(update={"raw_sha256": raw_hash}))
            http.close()
        if errors:
            fr.notes += errors
            if len(errors) == len(self.feeds):
                raise SchemaError("all feeds failed: " + "; ".join(errors))
        return fr
