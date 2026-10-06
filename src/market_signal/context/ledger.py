"""Append-only context ledger: observations → logical events + updates (migration 22).

Pipeline for each observation (``ingest``):

1. **validate** (pydantic ``Observation``) and **cap** its confidence by source tier;
2. **stamp** ``observed_at`` from Prism's clock (a provider can never backdate it);
3. **deduplicate / link** deterministically, in this order:
   a. the provider's stable ``dedup_key`` (e.g. ``macro:us_cpi:2026-10-14``) — when a key is
      given it is authoritative: no key match means a new event (many scheduled events
      share one calendar URL, so URL matching never applies to keyed observations);
   b. keyless only: an identical ``source_ref`` (URL / source id) already stored;
   c. unscheduled only: an event of the same category first seen within ``LINK_WINDOW``
      *before* this observation that shares a primary entity (or a direct asset);
   d. otherwise a new event;
4. an identical report (same provenance hash) already on that event is a **duplicate**;
5. **store**: a new event row is the immutable first observation; anything later is a
   ``context_event_updates`` row with its own ``observed_at``. Nothing is ever UPDATEd or
   DELETEd, so ``state_asof(t)`` reconstructs exactly what Prism knew at ``t``.

Confidence evolution (``corroboration_v1``): an update never silently lowers confidence
except an explicit denial (→ DENIED) or an explicit ``confidence_change``; two distinct
tier-1/2 sources at REPORTED or better make an event at least CONFIRMED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from market_signal.context.entities import EntityMap, load_entities
from market_signal.context.model import Observation, changed_fields, observation_json
from market_signal.context.taxonomy import (
    CONFIDENCE_RANK,
    LINK_WINDOW,
    RELEVANCE,
    SOURCE_TIER,
    SUBCATEGORIES,
    Confidence,
    SourceType,
)
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.research.lab.common import content_id

CORROBORATION_VERSION = "corroboration_v1"
# host clock back-steps (~0.6 s on WSL) must not split one event in two when linking
CLOCK_TOLERANCE = timedelta(seconds=5)
# Events that happen ON a venue: the venue entity does not link its own token (a Hyperliquid
# listing of FOO is not a HYPE event). Outages/insolvency are about the venue, so they do.
VENUE_SCOPED = frozenset(
    {"listing", "delisting", "exchange_maintenance", "exchange_deposit_withdrawal_disruption"}
)


@dataclass
class IngestResult:
    new_events: list[str] = field(default_factory=list)
    new_updates: list[str] = field(default_factory=list)
    duplicates: int = 0
    rejected: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # update id -> how it was linked

    def as_dict(self) -> dict:
        return {"new_events": len(self.new_events), "new_updates": len(self.new_updates),
                "duplicates": self.duplicates, "rejected": len(self.rejected),
                "errors": self.rejected[:10]}  # fmt: skip


def _ts(x) -> pd.Timestamp | None:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NaT:
        return None
    t = pd.Timestamp(x)
    if pd.isna(t):
        return None
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _max_conf(a: str, b: str) -> str:
    return a if CONFIDENCE_RANK[Confidence(a)] >= CONFIDENCE_RANK[Confidence(b)] else b


# --------------------------------------------------------------------------- sources


def register_source(store: Store, source_id: str, source_type: SourceType,
                    payload: dict | None = None, now: datetime | None = None) -> None:  # fmt: skip
    """Idempotent: the first registration of a source id is kept."""
    store.con.execute(
        "INSERT INTO context_sources VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        [source_id, source_type.value, SOURCE_TIER[source_type],
         json.dumps(payload or {}, sort_keys=True, default=str), now or utcnow()],
    )  # fmt: skip


# --------------------------------------------------------------------------- linking


def _event_rows(store: Store, where: str, args: list) -> pd.DataFrame:
    return store.con.execute(f"SELECT * FROM context_events WHERE {where}", args).df()


def _find_event(store: Store, obs: Observation, observed_at: datetime) -> tuple[str | None, str]:
    """(event_id, how) for the logical event this observation belongs to."""
    if obs.dedup_key:
        r = store.con.execute("SELECT event_id FROM context_events WHERE dedup_key=?",
                              [obs.dedup_key]).fetchone()  # fmt: skip
        if r:
            return r[0], "dedup_key"
        return None, "new"  # a provider's stable key is authoritative: no fuzzier matching
    if obs.source_ref:
        r = store.con.execute(
            "SELECT event_id FROM (SELECT event_id, first_seen_at AS t FROM context_events "
            "WHERE source_ref=? UNION ALL SELECT event_id, observed_at FROM context_event_updates "
            "WHERE source_ref=?) ORDER BY t, event_id LIMIT 1", [obs.source_ref, obs.source_ref]
        ).fetchone()  # fmt: skip
        if r:
            return r[0], "source_ref"
    if obs.scheduled:
        return None, "new"
    window = LINK_WINDOW[obs.category]
    cand = _event_rows(store, "category=? AND NOT scheduled AND first_seen_at <= ? AND first_seen_at >= ?",
                       [obs.category.value, observed_at + CLOCK_TOLERANCE, observed_at - window])  # fmt: skip
    if cand.empty:
        return None, "new"
    mine_e = set(obs.entities)
    mine_a = set(obs.assets)
    direct: dict[str, set] = {}
    if mine_a:  # one query for every candidate's direct links
        ids = list(cand["event_id"])
        dl = store.con.execute(
            f"SELECT event_id, asset FROM context_asset_links WHERE link_type='direct' AND "
            f"event_id IN ({','.join('?' * len(ids))})", ids).df()  # fmt: skip
        for e, a in dl.itertuples(index=False):
            direct.setdefault(e, set()).add(a)
    hits = []
    for r in cand.itertuples():
        ents = set(json.loads(r.entities) or [])
        if (mine_e and mine_e & ents) or (mine_a and mine_a & direct.get(r.event_id, set())):
            hits.append((_ts(r.first_seen_at), r.event_id))
    if not hits:
        return None, "new"
    return sorted(hits)[0][1], "entity_window"


# --------------------------------------------------------------------------- state


def _base_state(row: Any) -> dict:
    g = row.__getitem__ if isinstance(row, dict) else lambda k: getattr(row, k)
    return {
        "event_id": g("event_id"), "dedup_key": g("dedup_key"), "category": g("category"),
        "subcategory": g("subcategory"), "title": g("title"), "summary": g("summary"),
        "scheduled": bool(g("scheduled")), "event_time": _iso(g("event_time")),
        "published_at": _iso(g("published_at")), "provider_time": _iso(g("provider_time")),
        "reported_first_seen_at": _iso(g("reported_first_seen_at")),
        "first_seen_at": _iso(g("first_seen_at")), "processed_at": _iso(g("processed_at")),
        "first_source": g("source_id"), "first_source_ref": g("source_ref"),
        "confidence": g("confidence"), "scope": g("scope"), "country": g("country"),
        "region": g("region"), "relevance_end": _iso(g("relevance_end")),
        "observation_mode": g("observation_mode"),
        "attributes": json.loads(g("attributes")), "entities": json.loads(g("entities")),
        "provenance_hash": g("provenance_hash"),
        "sources": [g("source_id")],
        "history": [{"seq": 0, "kind": "observed", "observed_at": _iso(g("first_seen_at")),
                     "source": g("source_id"), "confidence": g("confidence")}],
        "last_updated_at": _iso(g("first_seen_at")),
        # attribute -> when Prism first held its current value (e.g. consensus vs actual)
        "attr_seen": {k: _iso(g("first_seen_at")) for k, v in json.loads(g("attributes")).items()
                      if v not in (None, [], {}) and k != "kind"},
    }  # fmt: skip


def _iso(x) -> str | None:
    t = _ts(x)
    return None if t is None else t.isoformat()


def _apply(state: dict, u: Any) -> dict:
    ch = json.loads(u.changes) if isinstance(u.changes, str) else u.changes
    for k in ("subcategory", "title", "summary", "event_time", "relevance_end"):
        if k in ch:
            state[k] = ch[k]
    if "attributes" in ch:
        a = dict(state["attributes"])
        delta = ch["attributes"]
        if "facts" in delta:
            a["facts"] = {**(a.get("facts") or {}), **delta["facts"]}
            delta = {k: v for k, v in delta.items() if k != "facts"}
        a.update(delta)
        state["attributes"] = a
        for k in [*delta, *(["facts"] if "facts" in ch["attributes"] else [])]:
            state["attr_seen"][k] = _iso(u.observed_at)
    if "entities" in ch:
        state["entities"] = sorted(set(state["entities"]) | set(ch["entities"]))
    state["confidence"] = u.confidence
    if u.source_id not in state["sources"]:
        state["sources"].append(u.source_id)
    state["history"].append({"seq": int(u.seq), "kind": u.kind, "observed_at": _iso(u.observed_at),
                             "source": u.source_id, "confidence": u.confidence})  # fmt: skip
    state["last_updated_at"] = _iso(u.observed_at)
    if SUBCATEGORIES.get(state["subcategory"]):
        state["category"] = SUBCATEGORIES[state["subcategory"]].value
    return state


def state_asof(store: Store, event_id: str, as_of: datetime | None = None) -> dict | None:
    """The event as Prism knew it at ``as_of`` (None if Prism had not seen it yet)."""
    row = store.con.execute("SELECT * FROM context_events WHERE event_id=?", [event_id]).df()
    if row.empty:
        return None
    r = row.iloc[0]
    if as_of is not None and _ts(r["first_seen_at"]) > _ts(as_of):
        return None
    st = _base_state(r.to_dict())
    sql = "SELECT * FROM context_event_updates WHERE event_id=?"
    args: list = [event_id]
    if as_of is not None:
        sql += " AND observed_at <= ?"
        args.append(as_of)
    ups = store.con.execute(sql + " ORDER BY seq", args).df()
    for u in ups.itertuples():
        st = _apply(st, u)
    return st


def states_asof(store: Store, as_of: datetime, *, event_ids: list[str] | None = None,
                since: datetime | None = None, until: datetime | None = None) -> list[dict]:  # fmt: skip
    """Every event Prism knew at ``as_of`` (optionally restricted), folded to that instant.
    ``since``/``until`` bound the anchor time (event_time for scheduled, else first_seen)."""
    where, args = ["first_seen_at <= ?"], [as_of]
    if event_ids is not None:
        if not event_ids:
            return []
        where.append(f"event_id IN ({','.join('?' * len(event_ids))})")
        args += list(event_ids)
    if since is not None:
        where.append("coalesce(event_time, first_seen_at) >= ?")
        args.append(since)
    if until is not None:
        where.append("coalesce(event_time, first_seen_at) <= ?")
        args.append(until)
    ev = store.con.execute(f"SELECT * FROM context_events WHERE {' AND '.join(where)}", args).df()
    if ev.empty:
        return []
    ids = list(ev["event_id"])
    ups = store.con.execute(
        f"SELECT * FROM context_event_updates WHERE observed_at <= ? AND event_id IN "
        f"({','.join('?' * len(ids))}) ORDER BY event_id, seq", [as_of, *ids]).df()  # fmt: skip
    by = {k: g for k, g in ups.groupby("event_id")} if not ups.empty else {}
    out = []
    for r in ev.to_dict("records"):
        st = _base_state(r)
        for u in (by.get(r["event_id"]) if r["event_id"] in by else pd.DataFrame()).itertuples():
            st = _apply(st, u)
        out.append(st)
    return sorted(out, key=lambda s: (s["first_seen_at"], s["event_id"]))


# --------------------------------------------------------------------------- relevance


def relevance_window(state: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[start, end) of the event's active horizon. Never starts before Prism saw it."""
    w = RELEVANCE[state["subcategory"]]
    seen = _ts(state["first_seen_at"])
    attrs = state.get("attributes") or {}
    anchor = None
    if attrs.get("kind") == "listing":
        anchor = _ts(attrs.get("trading_close_at") if state["subcategory"] == "delisting"
                     else attrs.get("trading_open_at"))  # fmt: skip
    if anchor is None and attrs.get("kind") == "unlock":
        anchor = _ts(attrs.get("unlock_time"))
    if anchor is None and state["scheduled"]:
        anchor = _ts(state["event_time"])
    if anchor is None:
        anchor = seen
    start = max(seen, anchor - w.pre)
    end = _ts(state["relevance_end"]) or anchor + w.post
    return start, max(end, start)


def is_active(state: dict, t: datetime) -> bool:
    if state["confidence"] == Confidence.DENIED.value:
        return False
    start, end = relevance_window(state)
    return start <= _ts(t) < end


# --------------------------------------------------------------------------- ingest


def _auto_key(obs: Observation, observed_at: datetime, ph: str) -> str:
    who = ".".join(obs.entities or obs.assets or ("none",))
    return f"auto:{obs.category.value}:{who}:{pd.Timestamp(observed_at):%Y%m%dT%H%M%S}:{ph[:12]}"


def _write_links(store: Store, event_id: str, obs: Observation, em: EntityMap,
                 at: datetime) -> None:  # fmt: skip
    text = f"{obs.title}\n{obs.summary}"
    ents = set(obs.entities) | set(em.entities_in(text))
    if obs.subcategory in VENUE_SCOPED:  # a listing ON a venue is not news ABOUT its token
        a = obs.attributes.model_dump()
        ents -= {a.get("exchange"), (a.get("facts") or {}).get("exchange")}
    ents = sorted(e for e in ents if e)
    for ln in em.links(entities=ents, assets=list(obs.assets), market_wide=obs.market_wide):
        store.con.execute(
            "INSERT INTO context_asset_links VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [event_id, ln.asset, ln.link_type, ln.entity, em.version, at],
        )


def resolved_entities(obs: Observation, em: EntityMap) -> list[str]:
    return sorted(set(obs.entities) | set(em.entities_in(f"{obs.title}\n{obs.summary}")))


def ingest(store: Store, observations: list[Observation | dict], *, run_id: str | None = None,
           now: datetime | None = None, entity_map: EntityMap | None = None) -> IngestResult:  # fmt: skip
    """Validate, deduplicate and append. ``now`` is Prism's receipt time (tests inject it);
    it becomes ``first_seen_at`` / ``observed_at``. Each observation commits atomically."""
    em = entity_map or load_entities()
    res = IngestResult()
    for raw in observations:
        try:
            obs = raw if isinstance(raw, Observation) else Observation.model_validate(raw)
        except Exception as exc:  # validation failure: rejected, never stored
            res.rejected.append(str(exc).splitlines()[0][:200])
            continue
        obs = obs.capped()
        observed_at = pd.Timestamp(now or utcnow()).tz_convert("UTC").to_pydatetime()
        # resolve aliases named in the text into the observation's entity list
        ents = resolved_entities(obs, em)
        if ents != list(obs.entities):
            obs = obs.model_copy(update={"entities": tuple(ents)})
        ph = obs.provenance_hash()
        with store.transaction():
            register_source(store, obs.source_id, obs.source_type, now=observed_at)
            event_id, how = _find_event(store, obs, observed_at)
            processed_at = max(utcnow(), observed_at)
            if event_id is None:
                key = obs.dedup_key or _auto_key(obs, observed_at, ph)
                event_id = content_id("ctxev_", {"dedup_key": key})
                d = obs.model_dump(mode="json")
                store.con.execute(
                    "INSERT INTO context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [event_id, key, obs.schema_version, obs.taxonomy_version, obs.category.value,
                     obs.subcategory, obs.title, obs.summary, obs.scheduled, obs.event_time,
                     obs.published_at, obs.provider_time, obs.reported_first_seen_at, observed_at,
                     processed_at, obs.source_id, obs.source_ref, obs.confidence.value, obs.scope,
                     obs.country, obs.region, obs.relevance_end, obs.observation_mode,
                     json.dumps(d["attributes"], sort_keys=True), json.dumps(list(obs.entities)),
                     ph, obs.raw_sha256, observation_json(obs), run_id],
                )  # fmt: skip
                _write_links(store, event_id, obs, em, observed_at)
                res.new_events.append(event_id)
                continue
            dup = store.con.execute(
                "SELECT 1 FROM context_events WHERE event_id=? AND provenance_hash=? UNION ALL "
                "SELECT 1 FROM context_event_updates WHERE event_id=? AND provenance_hash=?",
                [event_id, ph, event_id, ph]).fetchone()  # fmt: skip
            if dup:
                res.duplicates += 1
                continue
            st = state_asof(store, event_id, None)
            # monotonic per event: a host clock stepping back (seen on WSL) must never place an
            # update before the event's own first sighting or before an earlier update
            observed_at = max(observed_at, _ts(st["last_updated_at"]).to_pydatetime())
            processed_at = max(processed_at, observed_at)
            changes = changed_fields(st, obs)
            new_ents = sorted(set(obs.entities) - set(st["entities"]))
            if new_ents:
                changes["entities"] = new_ents
            same_source = obs.source_id in st["sources"]
            if not changes and same_source and obs.update_kind is None:
                res.duplicates += 1  # same source, nothing new (e.g. re-worded re-poll)
                continue
            conf = _next_confidence(store, st, obs)
            kind = obs.update_kind or (
                "confidence_change" if conf != st["confidence"] and not changes
                else "status_change" if "subcategory" in changes
                else "revision" if changes else "corroboration")  # fmt: skip
            seq = store.con.execute("SELECT coalesce(max(seq), 0) + 1 FROM context_event_updates "
                                    "WHERE event_id=?", [event_id]).fetchone()[0]  # fmt: skip
            uid = content_id("ctxup_", {"event_id": event_id, "seq": int(seq), "ph": ph})
            store.con.execute(
                "INSERT INTO context_event_updates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [uid, event_id, int(seq), kind, observed_at, processed_at, obs.published_at,
                 obs.source_id, obs.source_ref, conf, json.dumps(changes, sort_keys=True, default=str),
                 obs.observation_mode, ph, observation_json(obs), run_id],
            )  # fmt: skip
            _write_links(store, event_id, obs, em, observed_at)
            res.new_updates.append(uid)
            res.links[uid] = how
    return res


def _next_confidence(store: Store, st: dict, obs: Observation) -> str:
    cur, new = st["confidence"], obs.confidence.value
    if obs.update_kind == "denial":
        return Confidence.DENIED.value
    if obs.update_kind == "confidence_change":
        return new
    out = _max_conf(cur, new) if cur != Confidence.DENIED.value else new
    srcs = set(st["sources"]) | {obs.source_id}
    if len(srcs) >= 2 and CONFIDENCE_RANK[Confidence(out)] < CONFIDENCE_RANK[Confidence.CONFIRMED]:
        tiers = store.con.execute(
            f"SELECT count(*) FROM context_sources WHERE tier <= 2 AND source_id IN "
            f"({','.join('?' * len(srcs))})", sorted(srcs)).fetchone()[0]  # fmt: skip
        reported = CONFIDENCE_RANK[Confidence(new)] >= CONFIDENCE_RANK[Confidence.REPORTED] and \
            CONFIDENCE_RANK[Confidence(cur)] >= CONFIDENCE_RANK[Confidence.REPORTED]  # fmt: skip
        if tiers >= 2 and reported:
            out = Confidence.CONFIRMED.value
    return out


# --------------------------------------------------------------------------- provider runs


def new_run_id(provider: str) -> str:
    import uuid

    return f"ctxrun_{provider[:24]}_{uuid.uuid4().hex[:16]}"


def record_run(store: Store, provider: str, started_at: datetime, *, status: str,
               received: int = 0, result: IngestResult | None = None, filtered: int = 0,
               latency_ms: float | None = None, error: str | None = None,
               payload: dict | None = None, finished_at: datetime | None = None,
               run_id: str | None = None) -> str:  # fmt: skip
    r = result or IngestResult()
    run_id = run_id or new_run_id(provider)
    fin = max(finished_at or utcnow(), started_at)
    store.con.execute(
        "INSERT INTO context_provider_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, provider, started_at, fin, status, received, len(r.new_events),
         len(r.new_updates), r.duplicates, len(r.rejected), filtered, latency_ms,
         error, json.dumps(payload or {}, sort_keys=True, default=str)],
    )  # fmt: skip
    return run_id


def links_for(store: Store, asset: str, as_of: datetime) -> pd.DataFrame:
    return store.con.execute(
        "SELECT event_id, link_type, entity FROM context_asset_links WHERE asset=? AND linked_at <= ?",
        [asset.upper(), as_of]).df()  # fmt: skip


def link_type_for(links: pd.DataFrame, event_id: str) -> str:
    lt = set(links.loc[links["event_id"] == event_id, "link_type"])
    for k in ("direct", "ecosystem", "market_wide"):
        if k in lt:
            return k
    return "none"


__all__ = [
    "CORROBORATION_VERSION",
    "IngestResult",
    "ingest",
    "is_active",
    "link_type_for",
    "links_for",
    "new_run_id",
    "record_run",
    "register_source",
    "relevance_window",
    "state_asof",
    "states_asof",
]  # fmt: skip
