"""Causal event admission, independent cohort evidence and venue safety.

Only authoritative context can enroll an opportunity. Providers never submit orders.
Immutable eligibility attempts outlive the small restartable confirmation projection.
"""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

import pandas as pd

from market_signal.context import ledger
from market_signal.context.taxonomy import Confidence, SourceType, cap_confidence, materiality
from market_signal.microstructure.query import load_microstructure
from market_signal.paper.v2.data import Observation, clean, ts
from market_signal.research.lab.common import canonical_json, content_id

from . import engine
from . import event_spec as rules
from .model import StateUpdate, sign, volatility_definition

TERMINAL_STATUS = {"resolved", "completed", "denied", "monitoring"}


def evidence(store, rid, eid, hid, asset, kind, payload, now, *, key=None):
    p = clean(payload)
    identity = {
        "run": rid,
        "event": eid,
        "hypothesis": hid,
        "asset": asset,
        "kind": kind,
        "key": key or p,
    }
    store.con.execute(
        "INSERT INTO paper_event_evidence VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        [
            content_id("eventevidence_", identity),
            rid,
            eid,
            hid,
            asset,
            ts(now).to_pydatetime(),
            kind,
            canonical_json(p),
        ],
    )


def reports(store, state, now):
    """Use each report's own capped facts/provenance, never a mixed-source attribute bag."""
    eid = state["event_id"]
    sources = dict(
        store.con.execute("SELECT source_id,source_type FROM context_sources").fetchall()
    )
    r = engine.rows(store, "SELECT * FROM context_events WHERE event_id=?", [eid])[0]
    initial = json.loads(r["observation"])
    initial.update(source_type=sources[r["source_id"]], observed_at=r["first_seen_at"])
    out = [initial]
    for raw, observed in store.con.execute(
        "SELECT observation,observed_at FROM context_event_updates "
        "WHERE event_id=? AND observed_at<=? ORDER BY seq",
        [eid, ts(now).to_pydatetime()],
    ).fetchall():
        p = json.loads(raw)
        p["source_type"] = sources[p["source_id"]]
        out.append({**p, "observed_at": observed})
    prior = {}
    for report in out:
        attrs = report.get("attributes") or {}
        f = attrs.get("facts") or {}
        signature = (
            report["subcategory"],
            f.get("impact"),
            f.get("status", attrs.get("status")),
            f.get("component_states_json"),
            attrs.get("loss_usd_estimate", f.get("loss_usd_estimate")),
        )
        old = prior.get(report["source_id"])
        report["meaningful_transition"] = old is None or old != signature
        report["information_at"] = report.get("published_at")
        if (
            old is not None
            and old != signature
            and report["source_type"] in ("exchange_notice", "protocol_announcement")
        ):
            report["information_at"] = (
                f.get("information_time")
                or f.get("source_revision_at")
                or report.get("published_at")
            )
        prior[report["source_id"]] = signature
    return clean(out)


def classify(report, now, *, expectation=None):
    """(family, exact affected assets, rejection, normalized facts). No title is read."""
    attrs = report.get("attributes") or {}
    f = attrs.get("facts") or {}
    sub = report["subcategory"]
    security = attrs if attrs.get("kind") == "security" else f
    loss = security.get("loss_usd_estimate")
    loss = loss if isinstance(loss, int | float) and math.isfinite(loss) and loss >= 0 else None
    conf = cap_confidence(Confidence(report["confidence"]), SourceType(report["source_type"]))
    normalized = {
        "confidence": conf.value,
        "status": f.get("status", attrs.get("status")),
        "provider": report["source_id"],
        "subcategory": sub,
    }
    if report.get("observation_mode") != "live" or conf == Confidence.DENIED:
        return None, (), "DATA_DEGRADED", normalized
    if normalized["status"] in TERMINAL_STATUS:
        return None, (), "UNSUPPORTED_EVENT_CLASS", normalized
    official = rules.OFFICIAL_STATUS.get(report["source_id"])
    status_valid = bool(
        official
        and urlsplit(report.get("source_ref") or "").hostname == official[1]
        and report["source_type"] in ("exchange_notice", "protocol_announcement")
    )
    family, assets = None, ()
    if sub == "exchange_outage":
        family = "venue_disruption"
        if status_valid and official[0] == "hyperliquid":
            return family, ("BTC",), "VENUE_UNAVAILABLE", normalized
        if not status_valid or official[0] not in ("coinbase", "kraken"):
            return family, (), "UNSUPPORTED_EVENT_CLASS", normalized
        components = json.loads(f.get("component_states_json") or "[]")
        severe = f.get("impact") in ("major", "critical") and any(
            (
                c.get("id") in rules.TRADING_COMPONENT_IDS[official[0]]
                or (not c.get("id") and c.get("name") in rules.TRADING_COMPONENTS[official[0]])
            )
            and c.get("status") == "major_outage"
            for c in components
        )
        normalized["severity"] = (
            "critical" if f.get("impact") == "critical" else "major" if severe else "minor"
        )
        if not severe or normalized["status"] not in ("investigating", "identified"):
            return family, ("BTC",), "LOW_MATERIALITY", normalized
        assets = ("BTC",)
    elif sub in ("chain_halt", "exploit_confirmed", "emergency_pause"):
        family = "protocol_stress"
        entities = set(report.get("entities") or ())
        protocol = security.get("protocol")
        protocol = protocol.strip().lower() if isinstance(protocol, str) else None
        mapped = rules.PROTOCOL_ASSETS.get(protocol)
        assets = (
            (mapped,)
            if mapped and protocol in entities and mapped in (report.get("assets") or ())
            else ()
        )
        if sub == "chain_halt" and status_valid and official[0] == "solana":
            assets = ("SOL",)
        if not assets:
            return family, tuple(report.get("assets") or ()), "ASSET_OUTSIDE_UNIVERSE", normalized
        if sub == "chain_halt":
            components = json.loads(f.get("component_states_json") or "[]")
            severe = (
                status_valid
                and official[0] == "solana"
                and f.get("impact") in ("major", "critical")
                and any(
                    (
                        c.get("id") in rules.TRADING_COMPONENT_IDS["solana"]
                        or (not c.get("id") and c.get("name") in rules.TRADING_COMPONENTS["solana"])
                    )
                    and c.get("status") == "major_outage"
                    for c in components
                )
            )
            if not severe:
                return family, assets, "LOW_MATERIALITY", normalized
            assets = ("SOL",)
        elif not (
            security.get("status") == "confirmed"
            and security.get("target_kind") == "protocol"
            and (loss or 0) >= 10_000_000
            and report["source_type"]
            in ("ai_monitor", "protocol_announcement", "security_advisory")
        ):
            return family, assets, "LOW_MATERIALITY", normalized
    elif sub == "regulatory_action":
        family = "sec_spot_etf_approval_surprise"
        asset = f.get("underlying_asset")
        if not (
            report["source_id"] == "sec_rss_v1"
            and report["source_type"] == "regulator"
            and urlsplit(report.get("source_ref") or "").hostname in ("sec.gov", "www.sec.gov")
            and f.get("decision") == "approved"
            and f.get("product") == "spot_etf"
            and asset in ("BTC", "ETH")
            and f.get("docket_id")
            and expectation
            and expectation.get("docket_id") == f["docket_id"]
            and expectation.get("expected_decision") == "rejected"
            and ts(expectation["available_at"]) < ts(report["observed_at"])
        ):
            return family, (), "AMBIGUOUS_DIRECTION", normalized
        assets = (asset,)
    else:
        return None, tuple(report.get("assets") or ()), "UNSUPPORTED_EVENT_CLASS", normalized
    score = materiality(sub, conf, scope=report.get("scope", "asset"), loss_usd=loss)["score"]
    normalized["materiality"] = score
    minimum = rules.FAMILIES[family]["materiality"]
    if (
        conf not in (Confidence.REPORTED, Confidence.CONFIRMED, Confidence.OFFICIAL)
        or score < minimum
    ):
        return family, assets, "LOW_MATERIALITY", normalized
    published = report.get("information_at") or report.get("published_at")
    # Only a structured official transition may supply a new information clock.
    if (
        not published
        or ts(published) > ts(now)
        or ts(now) - ts(published) > pd.Timedelta(seconds=rules.FAMILIES[family]["age_seconds"])
    ):
        return family, assets, "STALE_EVENT", normalized
    normalized["publication_age_seconds"] = (ts(now) - ts(published)).total_seconds()
    return family, assets, None, normalized


def reference(source, asset, first_seen):
    """Freeze quote and volatility solely from minutes available before Prism first_seen."""
    df = load_microstructure(
        source.store,
        asset,
        ts(first_seen) - pd.Timedelta(minutes=20),
        first_seen,
        known_at=first_seen,
        availability="ingested",
        production_only=True,
        fill_missing=False,
    )
    if len(df) < 15:
        raise ValueError("DATA_DEGRADED")
    df = df.tail(15)
    if (
        not df["complete"].all()
        or not df["minute_close"].diff().dropna().eq(pd.Timedelta(minutes=1)).all()
    ):
        raise ValueError("DATA_DEGRADED")
    last = df.iloc[-1]
    at, available = ts(last["minute_close"]), ts(last["available_at"])
    if not at <= available < ts(first_seen) or ts(first_seen) - at > pd.Timedelta(seconds=120):
        raise ValueError("DATA_DEGRADED")
    bid, ask = last["bid_end"], last["ask_end"]
    mid = (bid + ask) / 2
    if not all(math.isfinite(x) and x > 0 for x in (bid, ask, mid)) or bid > ask:
        raise ValueError("DATA_DEGRADED")
    prev = df["mid_end"].shift(1)
    tr = (
        pd.concat(
            [
                df["high_px"] - df["low_px"],
                (df["high_px"] - prev).abs(),
                (df["low_px"] - prev).abs(),
            ],
            axis=1,
        )
        .max(axis=1)
        .mean()
    )
    vol = math.sqrt(15) * tr / mid
    if not math.isfinite(vol) or vol <= 0:
        raise ValueError("DATA_DEGRADED")
    return clean(
        {
            "price": mid,
            "bid": bid,
            "ask": ask,
            "quote_at": at,
            "available_at": available,
            "volatility_move": vol,
            "volatility_move_definition": volatility_definition(
                "sqrt(15) * mean_15_minutes(max(high-low, abs(high-prev_mid), abs(low-prev_mid))) / final_bid_ask_mid; first prev_mid unavailable",
                "causal_15_complete_ingested_minutes_before_prism_first_seen",
                df["available_at"].max(),
            ),
            "local_low": df["low_px"].min(),
            "local_high": df["high_px"].max(),
            "basis": "causal_pre_first_seen_minute_bid_ask",
            "microstructure": {
                "bid5": last["bid5_end"],
                "ask5": last["ask5_end"],
                "delta": last["delta_ntl"],
                "oi": last["oi_end"],
                "funding_rate": last["funding_end"],
                "observed_at": at,
                "available_at": available,
            },
        }
    )


def entry_microstructure(source, asset, now):
    df = load_microstructure(
        source.store,
        asset,
        ts(now) - pd.Timedelta(minutes=4),
        now,
        known_at=now,
        availability="ingested",
        production_only=True,
        fill_missing=False,
    )
    if df.empty:
        return {"status": "UNKNOWN"}
    r = df.iloc[-1]
    if not r["complete"] or ts(now) - ts(r["minute_close"]) > pd.Timedelta(seconds=90):
        return {"status": "UNKNOWN"}
    return clean(
        {
            "status": "KNOWN",
            "observed_at": r["minute_close"],
            "available_at": r["available_at"],
            "bid5": r["bid5_end"],
            "ask5": r["ask5_end"],
            "delta": r["delta_ntl"],
            "oi": r["oi_end"],
            "funding_rate": r["funding_end"],
        }
    )


def confirmation(source, asset, available, side, now):
    df = load_microstructure(
        source.store,
        asset,
        available,
        now,
        known_at=now,
        availability="ingested",
        production_only=True,
        fill_missing=False,
    )
    df = df[(df["minute_open"] >= ts(available)) & df["complete"]] if not df.empty else df
    for r in df.to_dict("records"):
        ntl = r["buy_ntl"] + r["sell_ntl"]
        ret = r.get("ret_mid")
        frac = (r["buy_ntl"] - r["sell_ntl"]) / ntl if ntl > 0 else None
        if (
            ret is not None
            and math.isfinite(ret)
            and frac is not None
            and sign(side) * ret > 0
            and sign(side) * frac >= 0.10
        ):
            if ts(now) - ts(r["minute_close"]) > pd.Timedelta(seconds=90):
                continue
            return clean(
                {
                    "observed_at": r["minute_close"],
                    "available_at": r["available_at"],
                    "signed_return": sign(side) * ret,
                    "signed_taker_fraction": sign(side) * frac,
                }
            )
    return None


def unavailable(store, now):
    ids = [
        r[0]
        for r in store.con.execute(
            "SELECT DISTINCT event_id FROM (SELECT event_id FROM context_events WHERE source_id='hyperliquid_status_v1' "
            "UNION ALL SELECT event_id FROM context_event_updates WHERE source_id='hyperliquid_status_v1')"
        ).fetchall()
    ]
    for eid in ids:
        st = ledger.state_asof(store, eid, ts(now).to_pydatetime())
        if not st:
            continue
        rs = [r for r in reports(store, st, now) if r["source_id"] == "hyperliquid_status_v1"]
        if not rs:
            continue
        r = rs[-1]
        f = r["attributes"].get("facts") or {}
        if urlsplit(r.get("source_ref") or "").hostname != "hyperliquid.statuspage.io":
            continue
        # Severe venue state persists until official monitoring/resolution, never a timeout.
        if (
            r["subcategory"] == "exchange_outage"
            and f.get("impact") in ("major", "critical")
            and f.get("status") not in TERMINAL_STATUS
        ):
            return eid
    return None


def source_timings(store, st, now):
    out = []
    notices = store.con.execute(
        "SELECT available_at,payload FROM paper_nimble_triggers WHERE source='context' AND available_at<=? AND json_contains(json_extract(payload,'$.event_ids'),?)",
        [ts(now).to_pydatetime(), json.dumps(st["event_id"])],
    ).fetchall()

    def context_time(received):
        return min(
            (
                at
                for at, raw in notices
                if ts(json.loads(raw)["metadata"].get("gateway_received_at")) == ts(received)
            ),
            default=None,
        )

    for provider, received, raw in store.con.execute(
        "SELECT provider,received_at,payload FROM context_free_observations WHERE event_id=? AND received_at<=? ORDER BY received_at",
        [st["event_id"], ts(now).to_pydatetime()],
    ).fetchall():
        r = json.loads(raw)
        out.append(
            {
                "provider": provider,
                "source_publication_at": r.get("published_at"),
                "source_revision_at": r.get("attributes", {})
                .get("facts", {})
                .get("source_revision_at"),
                "collector_observed_at": received,
                "context_available_at": context_time(received),
                "prism_first_seen_at": st["first_seen_at"],
            }
        )
    for provider, observed, raw in store.con.execute(
        "SELECT provider,observed_at,payload FROM context_work_observations WHERE event_id=? AND observed_at<=? ORDER BY observed_at",
        [st["event_id"], ts(now).to_pydatetime()],
    ).fetchall():
        r = json.loads(raw)
        out.append(
            {
                "provider": provider,
                "source_publication_at": r["item"].get("published_at"),
                "work_discovery_at": r["item"].get("first_seen_at"),
                "work_sent_at": r.get("sent_at"),
                "gateway_received_at": observed,
                "context_available_at": context_time(observed),
                "prism_first_seen_at": st["first_seen_at"],
            }
        )
    represented = {r["provider"] for r in out}
    for r in reports(store, st, now):
        if r["source_id"] in represented:
            continue
        received = r["observed_at"]
        metadata = next(
            (
                json.loads(raw)["metadata"]
                for at, raw in notices
                if ts(json.loads(raw)["metadata"].get("gateway_received_at")) == ts(received)
            ),
            {},
        )
        out.append(
            {
                "provider": r["source_id"],
                "source_publication_at": r.get("published_at"),
                "work_discovery_at": r.get("reported_first_seen_at"),
                "work_sent_at": metadata.get("sender_at"),
                "gateway_received_at": received,
                "context_available_at": context_time(received),
                "prism_first_seen_at": st["first_seen_at"],
            }
        )
        represented.add(r["source_id"])
    return clean(
        sorted(out, key=lambda r: ts(r.get("collector_observed_at") or r["gateway_received_at"]))
    )


def conflict(store, state, asset, side, now):
    opposite_categories = (
        ("regulatory_action",)
        if side == "short"
        else ("exchange_outage", "chain_halt", "exploit_confirmed", "emergency_pause")
    )
    ids = [
        r[0]
        for r in store.con.execute(
            "SELECT event_id FROM context_events WHERE first_seen_at>=? AND first_seen_at<=? "
            "AND subcategory IN (" + ",".join("?" for _ in opposite_categories) + ")",
            [
                (
                    ts(now) - pd.Timedelta(seconds=rules.definition()["conflict_seconds"])
                ).to_pydatetime(),
                ts(now).to_pydatetime(),
                *opposite_categories,
            ],
        ).fetchall()
    ]
    for st in ledger.states_asof(store, ts(now).to_pydatetime(), event_ids=ids):
        if st["event_id"] == state["event_id"]:
            continue
        for r in reports(store, st, now):
            family, assets, reason, _ = classify(
                r, now, expectation=expectation(store, r, st["first_seen_at"])
            )
            if not reason and asset in assets and rules.FAMILIES[family]["side"] != side:
                return st["event_id"]
    return None


def expectation(store, report, first_seen):
    docket = (report.get("attributes", {}).get("facts") or {}).get("docket_id")
    if not docket:
        return None
    # Per-report official facts only. Later secondary attributes cannot impersonate SEC state.
    states = ledger.states_asof(
        store,
        ts(first_seen).to_pydatetime(),
        since=(ts(first_seen) - pd.Timedelta(days=1)).to_pydatetime(),
    )
    for st in states:
        for old in reports(store, st, first_seen):
            f = old["attributes"].get("facts") or {}
            if (
                old["source_id"] == "sec_rss_v1"
                and old["source_type"] == "regulator"
                and urlsplit(old.get("source_ref") or "").hostname in ("sec.gov", "www.sec.gov")
                and old["confidence"] == "OFFICIAL"
                and f.get("docket_id") == docket
                and f.get("expected_decision") == "rejected"
            ):
                return {**f, "available_at": old["observed_at"]}
    return None


class EventSource:
    def __init__(self, source, geometry, *, clock=None):
        self.source, self.frozen_geometry = source, geometry
        self.intent_clock = clock

    def geometry(self, asset, observation):
        return self.frozen_geometry


def evaluate(
    store, rid, triggers, quotes, source, now, contexts=None, *, clock=None, quote_provider=None
):
    run = engine.load(store, rid)
    if run["policy_id"] != rules.policy_id():
        return {}
    now = ts(now)
    hs = {h.hypothesis_id: h for h in engine.hypotheses(store, rid) if h.source_phase == 30}
    totals = {}
    event_ids = sorted(
        {eid for t in triggers if t["source"] == "context" for eid in t["event_ids"]}
    )
    for eid in event_ids:
        if clock:
            now = ts(clock())
        st = ledger.state_asof(store, eid, now.to_pydatetime())
        if not st:
            continue
        eligible = []
        timings = source_timings(store, st, now)
        for r in reports(store, st, now):
            family, assets, reason, normalized = classify(
                r, now, expectation=expectation(store, r, st["first_seen_at"])
            )
            evidence(
                store,
                rid,
                eid,
                "classification",
                ",".join(assets),
                "classification",
                {
                    "family": family,
                    "assets": assets,
                    "reason": reason,
                    "normalized": normalized,
                    "source_timings": timings,
                    "first_seen_at": st["first_seen_at"],
                    "last_updated_at": st["last_updated_at"],
                },
                now,
                key=[
                    r["source_id"],
                    r["observed_at"],
                    family,
                    reason,
                    normalized.get("severity"),
                    normalized.get("confidence"),
                ],
            )
            if (
                not reason
                and ts(r["observed_at"]) > ts(run["activated_at"])
                and r.get("meaningful_transition", True)
            ):
                eligible.append((family, assets, r, normalized))
            for h in hs.values():
                if h.source_key != family:
                    continue
                for asset in assets:
                    frozen = store.con.execute(
                        "SELECT payload FROM paper_event_evidence WHERE run_id=? AND event_id=? AND hypothesis_id=? AND asset=? AND kind='enrolled' ORDER BY recorded_at LIMIT 1",
                        [rid, eid, h.hypothesis_id, asset],
                    ).fetchone()
                    if not frozen:
                        continue
                    frozen = json.loads(frozen[0])
                    ref = frozen.get("reference")
                    q = engine._quote(quotes, asset, now)
                    target = (
                        ref["price"]
                        * (
                            1
                            + sign(h.side)
                            * 0.5
                            * ref["volatility_move"]
                            * math.sqrt(rules.mapping(h)["expiry_minutes"] / 15)
                        )
                        if ref
                        else None
                    )
                    rate = (contexts or {}).get(asset, {}).get("funding_rate")
                    move = sign(h.side) * (target / q.mid - 1) if target and q else None
                    cost = (
                        (
                            (q.ask - q.bid) / q.mid
                            + 2 * engine.spec.EXECUTION["fee_bps"] / 10000
                            + 2 * engine.spec.EXECUTION["slippage_bps"][asset] / 10000
                            + max(0, sign(h.side) * rate)
                            * math.ceil(rules.mapping(h)["max_hold_minutes"] / 60)
                        )
                        if q and rate is not None
                        else None
                    )
                    economic = move is not None and cost is not None and move >= 2 * cost
                    evidence(
                        store,
                        rid,
                        eid,
                        h.hypothesis_id,
                        asset,
                        "source_viability",
                        {
                            "provider": r["source_id"],
                            "source_observed_at": r["observed_at"],
                            "evaluator_at": now,
                            "classification_reason": reason,
                            "remaining_target_move": move,
                            "estimated_cost": cost,
                            "economic_at_evaluator": economic,
                            "freshness_viable": reason != "STALE_EVENT",
                            "venue_available": unavailable(store, now) is None,
                            "confirmation_present": bool(
                                confirmation(source, asset, frozen["available_at"], h.side, now)
                            )
                            if h.variant == "confirmed"
                            else True,
                            "note": "observed gates at evaluator, not a counterfactual executed trade",
                        },
                        now,
                        key=[r["source_id"], r["observed_at"], reason],
                    )
        # Earliest eligible report preserves causal source selection across corroborators.
        for family, assets, r, normalized in eligible:
            for h in hs.values():
                if h.source_key != family:
                    continue
                for asset in assets:
                    key = [rid, eid, h.hypothesis_id, asset]
                    if store.con.execute(
                        "SELECT 1 FROM paper_event_pending WHERE run_id=? AND event_id=? AND hypothesis_id=? AND asset=?",
                        key,
                    ).fetchone():
                        continue
                    if store.con.execute(
                        "SELECT 1 FROM paper_event_evidence WHERE run_id=? AND event_id=? AND hypothesis_id=? AND asset=? AND kind='decision'",
                        key,
                    ).fetchone():
                        evidence(
                            store,
                            rid,
                            eid,
                            h.hypothesis_id,
                            asset,
                            "duplicate",
                            {"reason": "DUPLICATE_EPISODE", "update_at": st["last_updated_at"]},
                            now,
                        )
                        continue
                    # A batch can contain the initial minor report and a later eligible
                    # transition. Its reaction clock starts when THAT report is available.
                    relevant = [
                        t
                        for t in triggers
                        if eid in t["event_ids"] and ts(t["available_at"]) >= ts(r["observed_at"])
                    ]
                    matched = [
                        t
                        for t in relevant
                        if t["metadata"].get("gateway_received_at")
                        and ts(t["metadata"]["gateway_received_at"]) == ts(r["observed_at"])
                    ]
                    relevant = matched or [
                        t for t in relevant if not t["metadata"].get("gateway_received_at")
                    ]
                    if not relevant:
                        continue
                    available = min(ts(t["available_at"]) for t in relevant)
                    try:
                        ref = reference(source, asset, st["first_seen_at"])
                    except ValueError:
                        ref = None
                    q = engine._quote(quotes, asset, now)
                    availability_price = q.mid if q and ts(q.at) <= available else None
                    availability_quote_at = q.at if availability_price is not None else None
                    if availability_price is None:
                        try:
                            sampled = reference(source, asset, available)
                            availability_price, availability_quote_at = (
                                sampled["price"],
                                sampled["quote_at"],
                            )
                        except ValueError:
                            pass
                    p = clean(
                        {
                            "state": st,
                            "report": r,
                            "normalized": normalized,
                            "available_at": available,
                            "reference": ref,
                            "availability_price": availability_price,
                            "availability_quote_at": availability_quote_at,
                            "availability_price_basis": "latest_known_sample_before_context_availability_at_most_120s_old_or_missing",
                            "first_evaluator_notice_at": now,
                            "trigger_metadata": [t["metadata"] for t in relevant],
                        }
                    )
                    store.con.execute(
                        "INSERT INTO paper_event_pending VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                        [*key, canonical_json(p)],
                    )
                    evidence(store, rid, eid, h.hypothesis_id, asset, "enrolled", p, now)
    pending = engine.rows(
        store,
        "SELECT * FROM paper_event_pending WHERE run_id=? ORDER BY event_id,hypothesis_id,asset",
        [rid],
    )
    venue = unavailable(store, now) if pending else None
    for row in pending:
        if clock:
            now = ts(clock())
        if quote_provider:
            quotes, contexts = quote_provider(now)
        eid, hid, asset = row["event_id"], row["hypothesis_id"], row["asset"]
        h, p = hs[hid], json.loads(row["payload"])
        st = ledger.state_asof(store, eid, now.to_pydatetime())
        r, ref = p["report"], p["reference"]
        family, _, reason, normalized = classify(
            r, now, expectation=expectation(store, r, st["first_seen_at"])
        )
        if venue:
            reason = "VENUE_UNAVAILABLE"
        elif not ref:
            reason = "DATA_DEGRADED"
        elif resolved(store, st, now):
            reason = "UNSUPPORTED_EVENT_CLASS"
        elif conflict(store, st, asset, h.side, now):
            reason = "CONFLICTING_EVENT"
        elif now >= ts(p["available_at"]) + pd.Timedelta(
            minutes=rules.mapping(h)["expiry_minutes"]
        ):
            reason = "STALE_EVENT"
        confirmed = (
            confirmation(source, asset, p["available_at"], h.side, now)
            if h.variant == "confirmed" and not reason
            else None
        )
        if h.variant == "confirmed" and not reason and not confirmed:
            evidence(
                store,
                rid,
                eid,
                hid,
                asset,
                "waiting",
                {
                    "reason": "CONFIRMATION_MISSING",
                    "available_at": p["available_at"],
                    "wait_seconds": (now - ts(p["available_at"])).total_seconds(),
                },
                now,
                key=now.floor("min").isoformat(),
            )
            totals["CONFIRMATION_MISSING"] = totals.get("CONFIRMATION_MISSING", 0) + 1
            continue
        decision = {
            **p,
            "variant": h.variant,
            "family": family,
            "reason": reason,
            "decision_at": now.isoformat(),
            "confirmation": confirmed,
            "confirmation_delay_seconds": (
                ts(confirmed["available_at"]) - ts(p["available_at"])
            ).total_seconds()
            if confirmed
            else 0,
            "source_timings": source_timings(store, st, now),
        }
        if not reason:
            window = rules.mapping(h)["expiry_minutes"]
            confluence = engine.rows(
                store,
                "SELECT hypothesis_id,payload FROM paper_nimble_opportunities WHERE run_id=? AND asset=? AND fired "
                "AND evaluated_at>=? AND evaluated_at<=?",
                [rid, asset, (now - pd.Timedelta(minutes=15)).to_pydatetime(), now.to_pydatetime()],
            )
            aligned = [
                json.loads(x["payload"])
                for x in confluence
                if json.loads(x["payload"]).get("side") == h.side
            ]
            families = sorted(
                {x["hypothesis"]["family"].split(":")[0] for x in aligned if x.get("hypothesis")}
            )
            decision["confluence"] = {
                "label": "event_only"
                if not families
                else "event+" + (families[0] if len(families) == 1 else "multiple"),
                "families": families,
                "hypothesis_ids": [x["hypothesis_id"] for x in aligned],
                "exposure_change": 0,
            }
            geometry = {
                **ref,
                "pre_event_price": ref["price"],
                "event_reaction_move": 0.5 * ref["volatility_move"] * math.sqrt(window / 15),
                "microstructure": entry_microstructure(source, asset, now),
            }
            episode = content_id("eventepisode_", {"event": eid, "asset": asset})
            obs = Observation(
                hid,
                asset,
                p["available_at"],
                p["available_at"],
                True,
                intended_side=h.side,
                context={**source.context(asset), "event_entry": decision},
                evidence={
                    "episode_id": episode,
                    "event": decision,
                    "source_latency": {
                        "prism_first_seen_at": st["first_seen_at"],
                        "context_available_at": p["available_at"],
                        "first_evaluator_notice_at": p["first_evaluator_notice_at"],
                        "providers": decision["source_timings"],
                    },
                },
            )
            if clock:
                now = ts(clock())
                decision["decision_at"] = now.isoformat()
            if quote_provider:
                quotes, contexts = quote_provider(now)
            result = engine.admit(
                store,
                rid,
                [obs],
                quotes,
                EventSource(source, geometry, clock=clock),
                contexts,
                now=now,
            )
            opportunity = store.con.execute(
                "SELECT payload FROM paper_nimble_opportunities WHERE run_id=? AND hypothesis_id=? AND asset=? AND signal_at=?",
                [rid, hid, asset, ts(obs.signal_at).to_pydatetime()],
            ).fetchone()
            if opportunity:
                op = json.loads(opportunity[0])
                decision.update(
                    admission=op["admission"],
                    execution_reason=op["reason"],
                    trade_id=op["trade_id"],
                    reason=canonical_rejection(op["reason"]),
                )
            for k, v in result.items():
                totals[k] = totals.get(k, 0) + v
        evidence(store, rid, eid, hid, asset, "decision", decision, now)
        store.con.execute(
            "DELETE FROM paper_event_pending WHERE run_id=? AND event_id=? AND hypothesis_id=? AND asset=?",
            [rid, eid, hid, asset],
        )
    return totals


def canonical_rejection(reason):
    if reason in ("MAX_POSITIONS", "EXPOSURE_CAP", "MIN_NOTIONAL"):
        return "EXPOSURE_LIMIT"
    if reason in ("CONFLICT", "CONFLICT_EXISTING_POSITION"):
        return "CONFLICTING_EVENT"
    if reason in (
        "FUNDING_UNAVAILABLE",
        "GEOMETRY_UNAVAILABLE",
        "INVALID_GEOMETRY",
        "EXECUTABLE_QUOTE_UNAVAILABLE",
        "CONTEXT_UNAVAILABLE",
    ):
        return "DATA_DEGRADED"
    return reason


def lifecycle_updates(store, positions, now):
    updates = []
    for p in positions:
        ev = p["thesis"]["context_snapshot"].get("event_entry")
        if not ev:
            continue
        eid = ev["state"]["event_id"]
        st = ledger.state_asof(store, eid, ts(now).to_pydatetime())
        if st and (resolved(store, st, now) or conflict(store, st, p["asset"], p["side"], now)):
            updates.append(
                StateUpdate(
                    p["asset"],
                    ts(now).isoformat(),
                    ts(now).isoformat(),
                    {"event_id": eid, "event_active": False},
                )
            )
    return updates


def fill_metrics(position, quote, fill_price, now):
    ev = position["thesis"]["context_snapshot"].get("event_entry")
    if not ev:
        return {}
    reference = ev["reference"]["price"]
    s = sign(position["side"])
    return {
        "event_age_at_entry_seconds": (
            ts(now) - ts(ev["report"].get("information_at") or ev["report"]["published_at"])
        ).total_seconds(),
        "publication_age_at_entry_seconds": (
            ts(now) - ts(ev["report"]["published_at"])
        ).total_seconds(),
        "event_entry": ev,
        "PRE_ENTRY_REACTION_CAPTURED": {
            "reference_price": reference,
            "availability_price": ev["availability_price"],
            "fill_price": fill_price,
            "fill_mid": quote.mid,
            "signed_move_before_fill": s * (fill_price / reference - 1),
            "signed_mid_move_before_fill": s * (quote.mid / reference - 1),
            "signed_move_before_availability": s * (ev["availability_price"] / reference - 1)
            if ev["availability_price"]
            else None,
            "note": "positive means expected-direction movement missed before execution; subsequent_mfe_is_position_mfe",
        },
        "source_latency": {
            **position["thesis"]["source_latency"],
            "intent_at": position["intent_at"],
            "evaluator_noticed_at": position["evaluator_noticed_at"],
            "quote_at": quote.at,
            "quote_received_at": quote.received_at or quote.available_at,
            "quote_available_at": quote.available_at,
            "fill_at": ts(now).isoformat(),
        },
    }


def resolved(store, state, now):
    if state["confidence"] == "DENIED" or any(
        x["kind"] in ("resolution", "denial") for x in state["history"]
    ):
        return True
    rs = reports(store, state, now)
    authoritative = [
        r
        for r in rs
        if r["source_type"]
        in ("exchange_notice", "protocol_announcement", "security_advisory", "regulator")
    ]
    last = (authoritative or rs)[-1]
    a = last.get("attributes") or {}
    return (a.get("facts") or {}).get("status", a.get("status")) in TERMINAL_STATUS


def pending_rejection(store, position, now):
    ev = position["thesis"]["context_snapshot"].get("event_entry")
    if not ev:
        return None
    st = ledger.state_asof(store, ev["state"]["event_id"], ts(now).to_pydatetime())
    family = ev["family"]
    if resolved(store, st, now):
        return "INVALIDATED_BEFORE_FILL"
    if ts(now) - ts(
        ev["report"].get("information_at") or ev["report"]["published_at"]
    ) > pd.Timedelta(seconds=rules.FAMILIES[family]["age_seconds"]) or ts(now) >= ts(
        ev["available_at"]
    ) + pd.Timedelta(minutes=position["thesis"]["expiry"]["reaction_window_minutes"]):
        return "STALE_EVENT"
    if conflict(store, st, position["asset"], position["side"], now):
        return "CONFLICTING_EVENT"
    return None
