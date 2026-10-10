"""Specialist episode linking. AI reports cannot replace deterministic official state."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from market_signal.context.work_monitors import PROVIDER_IDS
from market_signal.research.lab.common import canonical_json, content_id


def prepare(store, obs, payload):
    pid, xid = obs.source_id, payload["external_event_id"]
    prior = store.con.execute(
        "SELECT event_id,payload FROM context_work_observations WHERE provider=? "
        "AND external_event_id=? ORDER BY observed_at DESC,receipt_id DESC LIMIT 1",
        [pid, xid],
    ).fetchone()
    # Exact source URLs permit corroborating pre-existing deterministic episodes. Do not use
    # the broad entity-window heuristic: two BTC stories can be unrelated.
    exact = (
        store.con.execute(
            "SELECT event_id FROM (SELECT event_id,first_seen_at AS t FROM context_events "
            "WHERE source_ref=? UNION ALL SELECT event_id,observed_at AS t "
            "FROM context_event_updates WHERE source_ref=?) ORDER BY t,event_id LIMIT 1",
            [obs.source_ref, obs.source_ref],
        ).fetchone()
        if urlsplit(obs.source_ref).path.rstrip("/")
        else None
    )
    eid = prior[0] if prior else exact[0] if exact else None
    key = content_id("work:", {"provider": pid, "external_event_id": xid})
    protected = False
    if eid:
        key, first = store.con.execute(
            "SELECT dedup_key,source_id FROM context_events WHERE event_id=?", [eid]
        ).fetchone()
        protected = first not in PROVIDER_IDS
        protected = (
            protected
            or store.con.execute(
                "SELECT 1 FROM context_event_updates u JOIN context_sources s USING(source_id) "
                "WHERE u.event_id=? AND s.tier<=2 LIMIT 1",
                [eid],
            ).fetchone()
            is not None
        )
    duplicate = False
    if prior:
        previous, current = json.loads(prior[1])["item"], payload["item"]
        # Title, prose and discovery/send time are not new facts. New source URL only
        # qualifies as corroboration when explicitly asserted by the sensor.
        keys = ("factual_claims", "attributes", "assets", "entities", "market_wide")
        duplicate = all(previous.get(k) == current.get(k) for k in keys) and (
            previous["url"] == current["url"] or payload["monitor"]["novelty"] != "corroboration"
        )
    corroboration = payload["monitor"]["novelty"] == "corroboration"
    return (
        obs.model_copy(
            update={"dedup_key": key, "update_kind": "corroboration" if corroboration else None}
        ),
        protected or corroboration,
        duplicate,
        eid,
    )


def record(store, rec, eid):
    p = rec["payload"]
    store.con.execute(
        "INSERT INTO context_work_observations VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        [
            rec["receipt_id"],
            rec["observation"]["source_id"],
            p["external_event_id"],
            eid,
            rec["gateway_received_at"],
            rec["auth_identity"],
            canonical_json(p),
        ],
    )
