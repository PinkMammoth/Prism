"""Append-only, compressed evaluations with frozen content and bounded prospective outcomes."""

from __future__ import annotations

import gzip
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise

from market_signal.research.lab.common import canonical_json, content_id
from market_signal.universe.engine import State
from market_signal.universe.policy import POLICY


def latest(store, *, at=None):
    if not store.con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='universe_evaluations'"
    ).fetchone()[0]:
        return None
    # Sorting with the BLOB projected can materialize historical payloads even
    # though only one evaluation is needed. Keep the ordering query scalar-only.
    sql = "SELECT evaluation_id FROM universe_evaluations WHERE policy_id=?"
    params = [POLICY.identity]
    if at is not None:
        sql += " AND evaluated_at<=?"
        params.append(at)
    row = store.con.execute(sql + " ORDER BY evaluated_at DESC LIMIT 1", params).fetchone()
    if not row:
        return None
    payload = store.con.execute(
        "SELECT payload FROM universe_evaluations WHERE evaluation_id=?", [row[0]]
    ).fetchone()[0]
    result = json.loads(gzip.decompress(payload))
    result["evaluation_id"] = row[0]
    return result


def state(store):
    last = latest(store)
    return {a: State(**s) for a, s in last["states"].items()} if last else {}


def append(store, result, source):
    definition = canonical_json(asdict(POLICY))
    row = store.con.execute(
        "SELECT definition FROM universe_policies WHERE policy_id=?", [POLICY.identity]
    ).fetchone()
    if row and canonical_json(json.loads(row[0])) != definition:
        raise ValueError("universe policy content mismatch")
    source_sha = content_id("", source)
    eid = content_id(
        "universeeval_",
        {"policy": POLICY.identity, "at": result["at"], "source": source_sha, "decision": result},
    )
    old = store.con.execute(
        "SELECT evaluation_id FROM universe_evaluations WHERE policy_id=? AND evaluated_at=?",
        [POLICY.identity, result["at"]],
    ).fetchone()
    if old:
        if old[0] != eid:
            raise ValueError("universe evaluation conflict at same causal clock")
        return eid
    with store.transaction():
        store.con.execute(
            "INSERT INTO universe_policies VALUES (?,?) ON CONFLICT DO NOTHING",
            [POLICY.identity, definition],
        )
        store.con.execute(
            "INSERT INTO universe_evaluations VALUES (?,?,?,?,?)",
            [
                eid,
                POLICY.identity,
                result["at"],
                source_sha,
                gzip.compress(canonical_json(result).encode(), mtime=0),
            ],
        )
    return eid


def collect_outcomes(store, now):
    """Observed snapshot movement, with actual lag and gaps; never PnL or hidden interpolation."""
    evaluations = store.con.execute(
        "SELECT evaluation_id,evaluated_at,payload FROM universe_evaluations "
        "WHERE policy_id=? AND evaluated_at BETWEEN ? AND ? ORDER BY evaluated_at",
        [POLICY.identity, now - 5 * 3600, now],
    ).fetchall()
    decoded = []
    for eid, at, payload in evaluations:
        decision = json.loads(gzip.decompress(payload))
        decoded.append((eid, at, decision, {r["asset"]: r for r in decision["rows"]}))
    existing = set(
        store.con.execute(
            "SELECT evaluation_id,asset,horizon_minutes FROM universe_outcomes WHERE observed_at>=?",
            [now - 6 * 3600],
        ).fetchall()
    )
    count = 0
    for eid, start, ev, _ in decoded:
        for r in ev["rows"]:
            if r["asset"] not in ev["candidates"] or r["market"]["price"] <= 0:
                continue
            for horizon in POLICY.outcome_minutes:
                if (eid, r["asset"], horizon) in existing:
                    continue
                target = start + horizon * 60
                future = [
                    (at, by_asset.get(r["asset"]))
                    for _, at, _, by_asset in decoded
                    if start < at <= target + POLICY.cadence_seconds
                ]
                observed = next(
                    (
                        (at, x)
                        for at, x in future
                        if at >= target and x and x["market"]["healthy"] and x["market"]["active"]
                    ),
                    None,
                )
                if not observed:
                    continue
                at, last = observed
                prices = [
                    x["market"]["price"]
                    for t, x in future
                    if x and t <= at and x["market"]["price"] > 0
                ]
                prices.insert(0, r["market"]["price"])
                gaps = [t for t, x in future if t <= at and (not x or not x["market"]["healthy"])]
                times = [start, *[t for t, _ in future if t <= at]]
                gap = bool(gaps) or any(
                    b - a > POLICY.light_max_gap_seconds for a, b in pairwise(times)
                )
                payload = {
                    "target_at": target,
                    "observed_at": at,
                    "lag_seconds": at - target,
                    "return_bps": (last["market"]["price"] / prices[0] - 1) * 10000,
                    "sampled_range_bps": (max(prices) / min(prices) - 1) * 10000,
                    "sampled_rv_bps": math.sqrt(
                        sum(math.log(b / a) ** 2 for a, b in pairwise(prices))
                    )
                    * 10000,
                    "data_gap": gap,
                    "rank": r["rank"],
                    "score": r["score"],
                    "sampling": "10m_prospective_mark_snapshots_not_intraperiod_extremes",
                }
                store.con.execute(
                    "INSERT INTO universe_outcomes VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [eid, r["asset"], horizon, at, canonical_json(payload)],
                )
                count += 1
    return count


def risk_pins(store, now):
    """Every existing paper cohort's risk, unsettled close, event and required outcome."""
    pins = {}

    def add(asset, reason):
        if asset:
            pins.setdefault(asset, set()).add(reason)

    for state, payload in store.con.execute(
        "SELECT state,payload FROM paper_nimble_positions"
    ).fetchall():
        p = json.loads(payload)
        if state != "CLOSED":
            add(p.get("asset"), state)
        elif not p.get("accounting_final", False):
            add(p.get("asset"), "UNSETTLED_ACCOUNTING")
    for (payload,) in store.con.execute(
        "SELECT payload FROM paper_nimble_research_pending"
    ).fetchall():
        add(json.loads(payload).get("asset"), "RESEARCH_OUTCOME")
    for (asset,) in store.con.execute("SELECT DISTINCT asset FROM paper_event_pending").fetchall():
        add(asset, "EVENT_LIFECYCLE")
    # v2 is event sourced; retirement may leave frozen positions draining.
    from market_signal.paper.v2.engine import state as v2_state

    for (rid,) in store.con.execute("SELECT run_id FROM paper_v2_runs").fetchall():
        current = v2_state(store, rid)
        for kind in ("open", "pending"):
            for position in current.get(kind, {}).values():
                add(position.get("asset"), "V2_" + kind.upper())
    return {a: sorted(v) for a, v in sorted(pins.items())}


def status(store, now=None):
    now = now or int(datetime.now(UTC).timestamp())
    ev = latest(store)
    if not ev:
        return {
            "policy_id": POLICY.identity,
            "policy_version": POLICY.version,
            "stage": "NOT ACTIVATED",
            "rows": [],
            "tier_counts": {},
            "last_evaluation": None,
        }
    ev = dict(ev)
    ev["last_evaluation"] = ev["at"]
    ev["age_seconds"] = now - ev["at"]
    ev["fresh"] = 0 <= now - ev["at"] <= 2 * POLICY.cadence_seconds
    from market_signal.paper.nimble import dynamic_spec

    active = store.con.execute(
        "SELECT 1 FROM paper_nimble_runs r JOIN paper_nimble_accounts a USING(run_id) "
        "WHERE r.policy_id=? AND json_extract_string(a.payload,'$.status')='ACTIVE' LIMIT 1",
        [dynamic_spec.policy_id()],
    ).fetchone()
    ev["execution_active"] = bool(active)
    if active:
        ev["stage"] = "DYNAMIC PAPER EXECUTION ACTIVE"
    return ev
