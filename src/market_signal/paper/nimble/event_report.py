"""Prospective event results, separated by hypothesis and independent cohort."""

import json
from collections import Counter

from market_signal.models.domain import utcnow
from market_signal.paper.v2.data import clean, ts

from . import engine, event_spec, report


def summary(store, rid):
    run = engine.load(store, rid)
    if run["policy_id"] != event_spec.policy_id():
        return {"run_id": rid, "event_hypotheses": 0, "classification": "BASELINE"}
    records = [
        {
            "kind": k,
            "event_id": eid,
            "hypothesis_id": hid,
            "asset": a,
            "recorded_at": at,
            **json.loads(raw),
        }
        for k, eid, hid, a, at, raw in store.con.execute(
            "SELECT kind,event_id,hypothesis_id,asset,recorded_at,payload FROM paper_event_evidence "
            "WHERE run_id=? ORDER BY recorded_at,evidence_id",
            [rid],
        ).fetchall()
    ]
    ps = report.trades(store, rid)
    out = []
    for h in engine.hypotheses(store, rid):
        if h.source_phase != 30:
            continue
        rows = [r for r in records if r["hypothesis_id"] == h.hypothesis_id]
        enrolled = [r for r in rows if r["kind"] == "enrolled"]
        decisions = [r for r in rows if r["kind"] == "decision"]
        trades = [p for p in ps if p["hypothesis_id"] == h.hypothesis_id]
        closed = [p for p in trades if p["state"] == "CLOSED"]
        classified = [
            r for r in records if r["kind"] == "classification" and r.get("family") == h.source_key
        ]
        out.append(
            {
                "events": len({r["event_id"] for r in classified}),
                "classification_rejections": dict(
                    Counter(r["reason"] for r in classified if r.get("reason"))
                ),
                "hypothesis_id": h.hypothesis_id,
                "hypothesis": h.name,
                "variant": h.variant,
                "event_class": h.source_key,
                "eligible_events": len(enrolled),
                "decisions": len(decisions),
                "entries": sum(bool(p.get("entry_at")) for p in trades),
                "rejections": dict(Counter(r["reason"] for r in decisions if r.get("reason"))),
                "confirmation_missing_attempts": sum(r["kind"] == "waiting" for r in rows),
                "confirmation_delay_seconds": report._latencies(
                    [r.get("confirmation_delay_seconds") for r in decisions]
                ),
                "move_missed_before_fill_mean": report._mean(
                    [
                        p.get("PRE_ENTRY_REACTION_CAPTURED", {}).get("signed_mid_move_before_fill")
                        for p in trades
                    ]
                ),
                "event_age_seconds": report._latencies(
                    [p.get("event_age_at_entry_seconds") for p in trades]
                ),
                "mfe_mean": report._mean([p.get("mfe") for p in closed]),
                "mae_mean": report._mean([p.get("mae") for p in closed]),
                "false_positive_proxy_no_reaction": sum(
                    p.get("exit_reason") == "THESIS_EXPIRED" for p in closed
                ),
                **report.metrics(trades),
            }
        )
    return clean(
        {
            "version": event_spec.VERSION,
            "run_id": rid,
            "variant": json.loads(run["definition"])["event_variant"],
            "hypotheses": out,
            "events": len({r["event_id"] for r in records}),
            "classification_rejections": dict(
                Counter(
                    r["reason"]
                    for r in records
                    if r["kind"] == "classification" and r.get("reason")
                )
            ),
            "source_comparison": source_comparison(store, rid, records),
            "evidence_rows": len(records),
            "evidence": records,
            "graduation": "NONE_PROSPECTIVE_OBSERVATION_ONLY",
        }
    )


def source_comparison(store, rid, records):
    results = []
    for eid in sorted({r["event_id"] for r in records}):
        rs = [r for r in records if r["event_id"] == eid]
        timings = next(
            (r.get("source_timings") for r in reversed(rs) if r.get("source_timings")), []
        )
        first = min(
            (ts(x.get("collector_observed_at") or x["gateway_received_at"]) for x in timings),
            default=None,
        )
        entry = next((r for r in rs if r["kind"] == "enrolled" and r.get("reference")), None)
        family = next((r.get("family") for r in rs if r.get("family")), None)
        providers = []
        viability = [r for r in rs if r["kind"] == "source_viability"]
        for t in timings:
            heard = ts(t.get("collector_observed_at") or t["gateway_received_at"])
            age = (
                (heard - ts(t["source_publication_at"])).total_seconds()
                if t.get("source_publication_at")
                else None
            )
            gate = next((r for r in reversed(viability) if r["provider"] == t["provider"]), None)
            providers.append(
                {
                    **t,
                    "later_than_first_seconds": (heard - first).total_seconds()
                    if first is not None
                    else None,
                    "publication_age_seconds": age,
                    "freshness_viable": age is not None
                    and family in event_spec.FAMILIES
                    and age <= event_spec.FAMILIES[family]["age_seconds"],
                    "remaining_execution_viable": (
                        gate["economic_at_evaluator"]
                        and gate["venue_available"]
                        and gate["confirmation_present"]
                        and not gate["classification_reason"]
                    )
                    if gate
                    else None,
                    "observed_gate_evidence": gate,
                    "viability_note": "freshness alone insufficient; no unobserved quote/confirmation counterfactual fill",
                }
            )
        results.append(
            {
                "logical_event_id": eid,
                "first_provider": timings[0]["provider"] if timings else None,
                "providers": providers,
                "reference": entry["reference"] if entry else None,
            }
        )
    return results


def comparison(store):
    runs = [r for r in engine.runs(store) if r["policy_id"] == event_spec.policy_id()]
    cohorts = [summary(store, r["run_id"]) for r in runs]
    return {
        "cohorts": cohorts,
        "paired_opportunities": paired(store, runs),
        "baseline_runs": [
            r["run_id"] for r in engine.runs(store) if r["policy_id"] != event_spec.policy_id()
        ],
        "as_of": utcnow().isoformat(),
        "note": "independent accounts; no pooling; zero/small samples do not establish edge",
    }


def paired(store, runs):
    trades = [
        p
        for r in runs
        for p in engine.positions(store, r["run_id"], terminal=True)
        if p.get("event_entry")
    ]
    groups = {}
    for p in trades:
        ev = p["event_entry"]
        groups.setdefault((ev["state"]["event_id"], p["asset"]), {})[ev["variant"]] = p
    out = []
    for (eid, asset), group in groups.items():
        immediate, confirmed = group.get("immediate"), group.get("confirmed")
        out.append(
            {
                "event_id": eid,
                "asset": asset,
                "immediate_trade_id": immediate["trade_id"] if immediate else None,
                "confirmed_trade_id": confirmed["trade_id"] if confirmed else None,
                "extra_entry_delay_seconds": (
                    ts(confirmed["entry_at"]) - ts(immediate["entry_at"])
                ).total_seconds()
                if immediate and confirmed
                else None,
                "extra_move_missed": confirmed["PRE_ENTRY_REACTION_CAPTURED"][
                    "signed_mid_move_before_fill"
                ]
                - immediate["PRE_ENTRY_REACTION_CAPTURED"]["signed_mid_move_before_fill"]
                if immediate and confirmed
                else None,
                "immediate_exit_before_confirmed_entry": bool(
                    immediate
                    and confirmed
                    and immediate.get("exit_at")
                    and ts(immediate["exit_at"]) < ts(confirmed["entry_at"])
                ),
                "immediate_mfe": immediate.get("mfe") if immediate else None,
                "confirmed_mfe": confirmed.get("mfe") if confirmed else None,
                "immediate_net_pnl": immediate.get("net_pnl") if immediate else None,
                "confirmed_net_pnl": confirmed.get("net_pnl") if confirmed else None,
            }
        )
    return out
