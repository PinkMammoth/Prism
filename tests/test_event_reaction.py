"""Phase 30 deterministic scenarios. Mechanics calibration, never fitted profit evidence."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_signal.context import ledger
from market_signal.context.model import GenericAttrs, SecurityAttrs
from market_signal.context.model import Observation as ContextObservation
from market_signal.context.taxonomy import SourceType
from market_signal.paper.nimble import engine, event_report, event_spec, events, spec, triggers
from market_signal.paper.nimble.model import StateUpdate
from tests.test_nimble_paper import T0, Source, h, observation, quote, time


def venue(at=None, entity="coinbase", status="investigating", impact="major", **kw):
    at = at or time(1)
    host = event_spec.OFFICIAL_STATUS[entity + "_status_v1"][1]
    return ContextObservation(
        source_id=entity + "_status_v1",
        source_type="exchange_notice",
        source_ref="https://" + host + "/incidents/frozen1",
        confidence="OFFICIAL",
        title="Trading unavailable",
        subcategory="exchange_outage",
        scope="exchange",
        entities=(entity,),
        published_at=at,
        dedup_key=f"statuspage:{entity}:frozen1",
        attributes=GenericAttrs(
            facts={
                "status": status,
                "impact": impact,
                "component_states_json": json.dumps(
                    [{"name": "Trading", "status": "major_outage"}]
                ),
            }
        ),
        **kw,
    )


def protocol(at=None, asset="AAVE", entity="aave", **kw):
    return ContextObservation(
        source_id="work_security_stress_monitor_v1",
        source_type="ai_monitor",
        source_ref="https://aave.com/security/frozen1",
        confidence="OFFICIAL",
        title="Confirmed exploit",
        subcategory="exploit_confirmed",
        scope="asset",
        entities=(entity,),
        assets=(asset,),
        published_at=at or time(1),
        dedup_key="work:frozen_protocol",
        attributes=SecurityAttrs(
            status="confirmed",
            protocol=entity,
            target_kind="protocol",
            loss_usd_estimate=20_000_000,
        ),
        **kw,
    )


def regulatory(at=None):
    return ContextObservation(
        source_id="sec_rss_v1",
        source_type="regulator",
        confidence="OFFICIAL",
        source_ref="https://www.sec.gov/newsroom/release/frozen",
        title="Formal approval",
        subcategory="regulatory_action",
        entities=("bitcoin",),
        assets=("BTC",),
        published_at=at or time(1),
        dedup_key="regulator:approval_frozen",
        attributes=GenericAttrs(
            facts={
                "decision": "approved",
                "product": "spot_etf",
                "underlying_asset": "BTC",
                "docket_id": "SR-frozen",
            }
        ),
    )


def expectation(store):
    o = ContextObservation(
        source_id="sec_rss_v1",
        source_type="regulator",
        confidence="OFFICIAL",
        source_ref="https://www.sec.gov/newsroom/expectation/frozen",
        title="Expected rejection",
        subcategory="regulatory_action",
        entities=("bitcoin",),
        assets=("BTC",),
        published_at=time(0, 30),
        dedup_key="regulator:expectation_frozen",
        attributes=GenericAttrs(facts={"expected_decision": "rejected", "docket_id": "SR-frozen"}),
    )
    ledger.ingest(store, [o], now=time(0, 30).to_pydatetime())


def input_report(o):
    return {**o.model_dump(mode="json"), "observed_at": time(1).isoformat()}


@pytest.fixture
def cohorts(store):
    engine.register(store, now=T0)
    base = engine.create(store, now=T0)["run_id"]
    engine.register(store, now=T0, events=True)
    ids = {
        v: engine.create(store, now=T0, events=True, variant=v)["run_id"]
        for v in ("immediate", "confirmed")
    }
    return {"baseline": base, **ids}


@pytest.fixture
def source(store, monkeypatch):
    ref = {
        "price": 100,
        "bid": 99.99,
        "ask": 100.01,
        "quote_at": time(0).isoformat(),
        "available_at": time(0, 5).isoformat(),
        "volatility_move": 0.02,
        "local_low": 99,
        "local_high": 101,
        "basis": "synthetic_causal_pre_event",
    }
    monkeypatch.setattr(events, "reference", lambda *args: ref)
    monkeypatch.setattr(events, "confirmation", lambda *args: None)
    return SimpleNamespace(
        store=store, context=lambda asset: {"positioning": {}, "funding_rate": 0}
    )


def enroll(store, o=None, at=None):
    at = at or time(1)
    o = o or venue()
    result = ledger.ingest(store, [o], now=at.to_pydatetime())
    eid = (
        result.new_events[0]
        if result.new_events
        else store.con.execute(
            "SELECT event_id FROM context_events WHERE dedup_key=?", [o.dedup_key]
        ).fetchone()[0]
    )
    return {
        "source": "context",
        "event_ids": [eid],
        "available_at": at.isoformat(),
        "assets": ["BTC"],
        "metadata": {"context_available_at": at.isoformat()},
    }, eid


def evaluate(store, rid, source, pending=(), minute=1, second=0, price=100, asset="BTC", rate=0):
    with store.transaction():
        return events.evaluate(
            store,
            rid,
            pending,
            [quote(minute, second, price, asset)],
            source,
            time(minute, second),
            {asset: {"funding_rate": rate}},
        )


def monitor(store, rid, minute=1, second=2, price=100, asset="BTC", updates=(), rate=0):
    with store.transaction():
        engine.monitor(
            store,
            rid,
            [quote(minute, second, price, asset)],
            updates=updates,
            contexts={asset: {"funding_rate": rate}},
            now=time(minute, second),
        )


def test_bounded_universe_original_policy_and_cohorts(store, cohorts):
    assert (
        spec.policy_id()
        == "paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501"
    )
    assert len(event_spec.bootstrap()) == 6
    assert len(engine.hypotheses(store, cohorts["baseline"])) == 28
    for variant in ("immediate", "confirmed"):
        hs = engine.hypotheses(store, cohorts[variant])
        assert len(hs) == 31 and hs[:28] == spec.baseline.bootstrap()
        assert {h.variant for h in hs[28:]} == {variant}
        assert engine.account(store, cohorts[variant])["equity"] == 100
    with pytest.raises(ValueError, match="already active"):
        engine.create(store, now=time(1), events=True, variant="immediate")
    with pytest.raises(ValueError, match="cohort"):
        engine.create(store, now=time(1), events=True)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"title": "Very alarming hack"}, None),
        ({"subcategory": "stablecoin_depeg"}, "UNSUPPORTED_EVENT_CLASS"),
        ({"confidence": "UNCONFIRMED"}, "LOW_MATERIALITY"),
        ({"source_type": "ai_monitor"}, "UNSUPPORTED_EVENT_CLASS"),
        ({"published_at": time(-5).isoformat()}, "STALE_EVENT"),
        ({"observation_mode": "historical"}, "DATA_DEGRADED"),
    ],
)
def test_classification(change, reason):
    r = {**input_report(venue()), **change}
    assert events.classify(r, time(1))[2] == reason


def test_no_headline_trade_or_generic_panic():
    r = input_report(venue())
    r["attributes"] = {"kind": "generic", "facts": {}}
    assert events.classify(r, time(1))[2] == "LOW_MATERIALITY"
    assert events.classify(input_report(regulatory()), time(1))[2] == "AMBIGUOUS_DIRECTION"
    p = events.classify(input_report(protocol()), time(1))
    assert p[:3] == ("protocol_stress", ("AAVE",), None)
    assert p[3]["confidence"] == "REPORTED"  # Work never acquires official provenance.
    assert (
        events.classify(input_report(protocol(asset="DOGE", entity="dogecoin")), time(1))[2]
        == "ASSET_OUTSIDE_UNIVERSE"
    )


def test_work_age_and_economic_remaining_opportunity():
    r = input_report(protocol(at=time(-29)))
    assert events.classify(r, time(1))[2] is None
    assert events.classify(r, time(32))[2] == "STALE_EVENT"


def test_immediate_confirmed_separate_and_fast_future_fill(store, cohorts, source, monkeypatch):
    trigger, eid = enroll(store)
    assert evaluate(store, cohorts["immediate"], source, [trigger]) == {"ADMITTED": 1}
    assert evaluate(store, cohorts["confirmed"], source, [trigger]) == {"CONFIRMATION_MISSING": 1}
    p = engine.positions(store, cohorts["immediate"])[0]
    assert p["state"] == "PENDING"
    assert p["thesis"]["volatility_move"] == 0.02
    assert p["thesis"]["volatility_move_definition"]["version"] == "entry_volatility_move_v1"
    assert (
        p["thesis"]["episode_id"]
        and p["thesis"]["context_snapshot"]["event_entry"]["state"]["event_id"] == eid
    )
    monitor(store, cohorts["immediate"], second=0)
    assert engine.positions(store, cohorts["immediate"])[0]["state"] == "PENDING"
    monitor(store, cohorts["immediate"])
    p = engine.positions(store, cohorts["immediate"])[0]
    assert p["state"] == "OPEN"
    assert p["source_latency"]["prism_first_seen_at"] == time(1).isoformat()
    assert p["intent_at"] < p["entry_quote_at"] <= p["entry_at"]
    assert p["PRE_ENTRY_REACTION_CAPTURED"]["reference_price"] == 100
    assert p["PRE_ENTRY_REACTION_CAPTURED"]["signed_mid_move_before_fill"] == 0
    monkeypatch.setattr(
        events,
        "confirmation",
        lambda *args: {
            "observed_at": time(2).isoformat(),
            "available_at": time(2).isoformat(),
            "signed_return": 0.001,
            "signed_taker_fraction": 0.2,
        },
    )
    assert evaluate(store, cohorts["confirmed"], source, minute=2, price=99.8) == {"ADMITTED": 1}
    monitor(store, cohorts["confirmed"], minute=2, price=99.8)
    pc = engine.positions(store, cohorts["confirmed"])[0]
    assert pc["state"] == "OPEN" and pc["event_entry"]["confirmation_delay_seconds"] == 60
    assert pc["PRE_ENTRY_REACTION_CAPTURED"]["signed_mid_move_before_fill"] == pytest.approx(0.002)
    assert p["hypothesis_id"] != pc["hypothesis_id"]


def test_duplicate_corroboration_resolution_and_meaningful_update(store, cohorts, source):
    t, eid = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    before = engine.positions(store, rid)[0]
    assert ledger.ingest(store, [venue()], now=time(1, 3).to_pydatetime()).duplicates == 1
    later = venue().model_copy(
        update={
            "source_id": "work_exchange_incident_monitor_v1",
            "source_type": SourceType.AI_MONITOR,
            "title": "Corroborated",
            "update_kind": "corroboration",
        }
    )
    t2, eid2 = enroll(store, later, time(1, 4))
    assert eid2 == eid
    evaluate(store, rid, source, [t2], second=4)
    assert len(engine.positions(store, rid)) == 1
    assert engine.positions(store, rid)[0]["notional"] == before["notional"]
    resolved = venue(status="resolved").model_copy(update={"update_kind": "resolution"})
    t3, _ = enroll(store, resolved, time(1, 5))
    evaluate(store, rid, source, [t3], second=5)
    monitor(store, rid, second=6)
    p = engine.positions(store, rid)[0]
    assert p["state"] == "EXIT_PENDING" and p["exit_detail"] == "EVENT_STATE_INVALIDATED"
    assert engine.rows(store, "SELECT * FROM paper_event_evidence WHERE kind='duplicate'")
    monitor(store, rid, second=8)
    assert engine.positions(store, rid) == []
    assert not engine.positions(store, cohorts["baseline"])


@pytest.mark.parametrize(
    ("event", "asset", "side", "price"),
    [
        (venue(), "BTC", "short", 98),
        (protocol(), "AAVE", "short", 97),
        (regulatory(), "BTC", "long", 104),
    ],
)
def test_synthetic_sharp_reactions_and_tp(store, cohorts, source, event, asset, side, price):
    if side == "long":
        expectation(store)
    t, _ = enroll(store, event)
    rid = cohorts["immediate"]
    assert evaluate(store, rid, source, [t], asset=asset) == {"ADMITTED": 1}
    monitor(store, rid, asset=asset)
    original = engine.positions(store, rid)[0]["thesis"]
    assert original["objective"]["family"] == "event_reaction"
    monitor(store, rid, minute=2, second=0, asset=asset, price=price)
    assert engine.positions(store, rid)[0]["exit_reason"] == "TAKE_PROFIT"
    monitor(store, rid, minute=2, second=2, asset=asset, price=price)
    assert engine.positions(store, rid) == []
    assert (
        store.con.execute(
            "SELECT count(*) FROM paper_nimble_research_pending WHERE run_id=?", [rid]
        ).fetchone()[0]
        == 1
    )
    assert engine.positions(store, rid, terminal=True)[0]["thesis"] == original


@pytest.mark.parametrize(
    ("event", "asset", "minutes"), [(venue(), "BTC", 21), (protocol(), "AAVE", 61)]
)
def test_synthetic_no_reaction_expiry(store, cohorts, source, event, asset, minutes):
    t, _ = enroll(store, event)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t], asset=asset)
    monitor(store, rid, asset=asset)
    monitor(store, rid, minute=minutes, asset=asset)
    assert engine.positions(store, rid)[0]["exit_reason"] == "THESIS_EXPIRED"


@pytest.mark.parametrize(
    ("event", "asset", "adverse"), [(venue(), "BTC", 100.1), (regulatory(), "BTC", 99.9)]
)
def test_synthetic_false_alarm_failed_rally_price_invalidation(
    store, cohorts, source, event, asset, adverse
):
    if event.subcategory == "regulatory_action":
        expectation(store)
    t, _ = enroll(store, event)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t], asset=asset)
    monitor(store, rid, asset=asset)
    monitor(store, rid, minute=2, asset=asset, price=adverse)
    assert engine.positions(store, rid)[0]["exit_reason"] == "INVALIDATED"


def test_confirmation_arrives_too_late_and_target_not_reset(store, cohorts, source, monkeypatch):
    t, _ = enroll(store)
    rid = cohorts["confirmed"]
    evaluate(store, rid, source, [t])
    monkeypatch.setattr(
        events,
        "confirmation",
        lambda *args: {"observed_at": time(2).isoformat(), "available_at": time(2).isoformat()},
    )
    assert evaluate(store, rid, source, minute=2, price=95) == {"REJECTED": 1}
    evidence = event_report.summary(store, rid)
    assert evidence["hypotheses"][0]["rejections"] == {"UNECONOMIC_TARGET": 1}
    assert not engine.positions(store, rid)


def test_confirmation_expired_no_reaction(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["confirmed"]
    evaluate(store, rid, source, [t])
    evaluate(store, rid, source, minute=7)
    assert (
        store.con.execute(
            "SELECT count(*) FROM paper_event_pending WHERE run_id=?", [rid]
        ).fetchone()[0]
        == 0
    )
    assert event_report.summary(store, rid)["hypotheses"][0]["rejections"] == {"STALE_EVENT": 1}


def test_flow_invalidation(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    u = StateUpdate("BTC", time(4).isoformat(), time(4).isoformat(), {"flow_fractions": [0.5, 0.5]})
    monitor(store, rid, minute=4, price=99.8, updates=[u])
    assert engine.positions(store, rid)[0]["exit_detail"] == "FLOW_REVERSAL"


def test_hyperliquid_outage_prevents_pending_and_exit_fills(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    enroll(store, venue(entity="hyperliquid"), time(1, 1))
    monitor(store, rid)
    assert engine.positions(store, rid) == []
    assert engine.positions(store, rid, terminal=True)[0]["reason"] == "VENUE_UNAVAILABLE"
    assert events.unavailable(store, time(20)) is not None
    enroll(store, venue(entity="hyperliquid", status="resolved"), time(20))
    assert events.unavailable(store, time(20)) is None


def test_existing_opposite_position_conflict(store, cohorts, source):
    rid = cohorts["immediate"]
    with store.transaction():
        assert engine.admit(
            store,
            rid,
            [observation(h(), minute=1)],
            [quote()],
            Source(),
            {"BTC": {"funding_rate": 0}},
            now=time(1),
        ) == {"ADMITTED": 1}
    monitor(store, rid)
    t, _ = enroll(store, venue(at=time(2)), time(2))
    evaluate(store, rid, source, [t], minute=2)
    assert engine.positions(store, rid)[0]["side"] == "long"
    assert event_report.summary(store, rid)["hypotheses"][0]["rejections"] == {
        "CONFLICTING_EVENT": 1
    }


def test_opposite_event_uses_its_own_prior_expectation_clock(store, cohorts, source):
    t, eid = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    # The contrary expectation is later than the existing short event but earlier than approval.
    expected = regulatory(time(2)).model_copy(
        update={
            "dedup_key": "regulator:later_expectation",
            "attributes": GenericAttrs(
                facts={"expected_decision": "rejected", "docket_id": "SR-frozen"}
            ),
        }
    )
    enroll(store, expected, time(2))
    enroll(store, regulatory(time(3)), time(3))
    state = ledger.state_asof(store, eid, time(3).to_pydatetime())
    assert events.conflict(store, state, "BTC", "short", time(3)) is not None
    monitor(store, rid, minute=3, price=99.8)
    assert engine.positions(store, rid)[0]["exit_detail"] == "EVENT_STATE_INVALIDATED"


@pytest.mark.parametrize(("rate", "admitted"), [(0.05, True), (-0.05, False)])
def test_funding_cost_credit_economics(store, cohorts, source, rate, admitted):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t], rate=rate)
    assert bool(engine.positions(store, rid)) == admitted


def test_no_source_trading_or_llm_live_execution():
    root = Path(__file__).resolve().parents[1] / "src/market_signal"
    for directory in (root / "context/free_sources", root / "context/gateway"):
        for p in directory.glob("*.py"):
            tree = ast.parse(p.read_text())
            modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            assert not any(m.startswith("market_signal.paper") for m in modules)
    for p in (root / "paper/nimble").glob("*.py"):
        text = p.read_text().lower()
        assert (
            "import openai" not in text
            and "create_order(" not in text
            and "private_key" not in text
        )
    assert event_spec.definition()["execution"]["mode"] == "paper"


def seed_minutes(store, asset="BTC", start=-20, end=6):
    from market_signal.microstructure import definitions as d

    store.con.execute(
        "INSERT INTO microstructure_cutover VALUES (?,?,?,?,?,?,?)",
        [d.FEATURE_VERSION, "test", time(start), asset, time(start + 1, 5), "synthetic", T0],
    )
    for m in range(start, end):
        px = 99.8 if m >= 2 else 100.0
        row = dict(
            feature_version=d.FEATURE_VERSION,
            coin=asset,
            minute_open=time(m),
            revision=0,
            status="COMPLETE",
            trade_cov=1,
            book_samples=10,
            depth20_samples=10,
            n_buy=2,
            n_sell=8,
            buy_vol=2,
            sell_vol=8,
            buy_ntl=200,
            sell_ntl=800,
            high_px=px + 0.2,
            low_px=px - 0.2,
            first_px=px,
            last_px=px,
            bid_end=px - 0.01,
            ask_end=px + 0.01,
            bid5_end=1000,
            ask5_end=1100,
            bid5_mean=1000,
            ask5_mean=1100,
            bid20_mean=1000,
            ask20_mean=1000,
            oi_end=1000,
            funding_end=0,
            n_dup=0,
            n_late=0,
            finalized_at=time(m + 1, 5),
            ingested_at=time(m + 1, 6),
            session_id="synthetic",
            content_sha="0" * 64,
        )
        cols = ",".join(row)
        store.con.execute(
            f"INSERT INTO microstructure_minutes ({cols}) VALUES ({','.join('?' for _ in row)})",
            list(row.values()),
        )


def test_real_reference_confirmation_adapters_are_causal(store):
    seed_minutes(store)
    source = SimpleNamespace(store=store)
    ref = events.reference(source, "BTC", time(1, 15))
    assert ref["price"] == 100 and ref["quote_at"] == time(1).isoformat()
    assert ref["available_at"] == time(1, 6).isoformat()
    assert ref["volatility_move"] == pytest.approx(15**0.5 * 0.4 / 100)
    definition = ref["volatility_move_definition"]
    assert definition["version"] == "entry_volatility_move_v1"
    assert definition["available_at"] < time(1, 15).isoformat()
    assert "final_bid_ask_mid" in definition["formula"]
    # Later known minutes cannot change the frozen pre-first-seen reference.
    assert events.reference(source, "BTC", time(1, 15)) == ref
    assert events.confirmation(source, "BTC", time(1, 15), "short", time(3, 4)) is None
    c = events.confirmation(source, "BTC", time(1, 15), "short", time(3, 10))
    assert c["observed_at"] == time(3).isoformat() and c["available_at"] == time(3, 6).isoformat()
    assert c["signed_taker_fraction"] == pytest.approx(0.6)
    assert events.confirmation(source, "BTC", time(1, 15), "long", time(3, 10)) is None
    assert events.confirmation(source, "BTC", time(1, 15), "short", time(6, 10)) is None
    store.con.execute("DELETE FROM microstructure_minutes WHERE minute_open=?", [time(-5)])
    with pytest.raises(ValueError, match="DATA_DEGRADED"):
        events.reference(source, "BTC", time(1, 15))


def test_structured_status_parser_preserves_severity_and_sol_mapping():
    from market_signal.context.free_sources.parsers import parse
    from tests.test_free_event_sources import incident
    from tests.test_free_event_sources import source as provider

    raw = incident(at=time(1).to_pydatetime())
    raw.update(
        created_at=time(1).isoformat(),
        impact="critical",
        name="Chain halt: block production halted",
        components=[
            {"id": "qxs6x3yc9cxg", "name": "Mainnet Beta - Cluster", "status": "major_outage"}
        ],
    )
    parsed, _ = parse(
        provider("solana_status_v1"),
        json.dumps({"incidents": [raw]}).encode(),
        time(1).to_pydatetime(),
    )
    r = {**parsed[0], "observed_at": time(1).isoformat()}
    assert events.classify(r, time(1))[:3] == ("protocol_stress", ("SOL",), None)
    raw["components"][0]["id"] = "b21c1qbg5g4n"
    raw["components"][0]["name"] = "Mainnet Beta - RPC Nodes"
    parsed, _ = parse(
        provider("solana_status_v1"),
        json.dumps({"incidents": [raw]}).encode(),
        time(1).to_pydatetime(),
    )
    assert (
        events.classify({**parsed[0], "observed_at": time(1).isoformat()}, time(1))[2]
        == "LOW_MATERIALITY"
    )


def test_work_generic_structured_security_supported_without_confidence_escalation():
    r = input_report(protocol())
    r["attributes"] = {
        "kind": "generic",
        "facts": {
            "status": "confirmed",
            "target_kind": "protocol",
            "protocol": "aave",
            "loss_usd_estimate": 20_000_000,
        },
    }
    family, assets, reason, normalized = events.classify(r, time(1))
    assert (family, assets, reason) == ("protocol_stress", ("AAVE",), None)
    assert normalized["confidence"] == "REPORTED"
    r["attributes"]["facts"]["loss_usd_estimate"] = 1000
    assert events.classify(r, time(1))[2] == "LOW_MATERIALITY"


def test_minor_to_major_transition_only_one_episode(store, cohorts, source):
    rid = cohorts["immediate"]
    t, eid = enroll(store, venue(impact="minor"))
    evaluate(store, rid, source, [t])
    assert not engine.positions(store, rid)
    t2, same = enroll(store, venue(), time(1, 10))
    assert same == eid
    evaluate(store, rid, source, [t2], second=10)
    assert engine.positions(store, rid)[0]["state"] == "PENDING"
    monitor(store, rid, second=12)
    t3, _ = enroll(store, venue(impact="critical"), time(1, 14))
    evaluate(store, rid, source, [t3], second=14)
    assert len(engine.positions(store, rid)) == 1


def test_resolved_before_fill_cancels_without_price_fill(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    enroll(store, venue(status="resolved"), time(1, 1))
    monitor(store, rid)
    assert not engine.positions(store, rid)
    assert engine.positions(store, rid, terminal=True)[0]["reason"] == "INVALIDATED_BEFORE_FILL"


def test_hyperliquid_outage_cannot_fill_exit_until_recovery(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    monitor(store, rid, minute=2, second=0, price=98)
    assert engine.positions(store, rid)[0]["state"] == "EXIT_PENDING"
    enroll(store, venue(entity="hyperliquid"), time(2, 1))
    monitor(store, rid, minute=2, second=2, price=98)
    assert engine.positions(store, rid)[0]["state"] == "EXIT_PENDING"
    enroll(store, venue(entity="hyperliquid", status="resolved"), time(2, 3))
    monitor(store, rid, minute=2, second=4, price=98)
    assert not engine.positions(store, rid)


def test_conflicting_event_deterministically_invalidates_thesis(store, cohorts, source):
    expectation(store)
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    t2, _ = enroll(store, regulatory(at=time(2)), time(2))
    evaluate(store, rid, source, [t2], minute=2)
    monitor(store, rid, minute=2, second=4)
    assert engine.positions(store, rid)[0]["exit_detail"] == "EVENT_STATE_INVALIDATED"


def test_state_event_id_cannot_invalidate_unrelated_thesis(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    unrelated = StateUpdate(
        "BTC",
        time(2).isoformat(),
        time(2).isoformat(),
        {"event_id": "different-event", "event_active": False},
    )
    monitor(store, rid, minute=2, updates=[unrelated])
    assert engine.positions(store, rid)[0]["state"] == "OPEN"


def test_research_curves_continue_after_event_exit(store, cohorts, source):
    t, _ = enroll(store)
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    monitor(store, rid, minute=2, second=0, price=98)
    monitor(store, rid, minute=2, second=2, price=98)
    assert not engine.positions(store, rid)
    path = [
        quote(m, price=98, high=98.1, low=97.9, interval_start=time(m - 1).isoformat())
        for m in range(2, 242)
    ]
    with store.transaction():
        engine.research(store, rid, path, now=time(242))
    assert set(
        r[0]
        for r in store.con.execute("SELECT horizon_minutes FROM paper_nimble_research").fetchall()
    ) == set((*event_spec.HORIZONS, 20))
    assert event_report.summary(store, rid)["hypotheses"][0]["trades"] == 1


def test_event_funding_cost_decay_and_beneficial_funding(store, cohorts, source):
    t, _ = enroll(store, protocol(asset="BTC", entity="bitcoin"))
    rid = cohorts["immediate"]
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    monitor(store, rid, minute=59, second=0, price=99.9, rate=0.01)
    assert engine.positions(store, rid)[0]["state"] == "OPEN"
    monitor(store, rid, minute=59, second=2, price=99.9, rate=-0.01)
    assert engine.positions(store, rid)[0]["exit_reason"] == "COST_DECAY"


def test_worker_paired_cohorts_and_baseline_dispatch(store, cohorts, source, monkeypatch):
    from market_signal.paper.nimble import worker
    from market_signal.paper.nimble.worker import Worker, marker, publish

    path = store.path
    _t, eid = enroll(store)
    triggers.enqueue(store, "context", "phase30-test", ["BTC"], time(1), event_ids=[eid])
    publish(marker(path), {"run_id": cohorts["baseline"], "run_ids": list(cohorts.values())})
    store.close()
    monkeypatch.setattr(worker, "require_authoritative", lambda p: None)
    monkeypatch.setattr(worker, "runtime_id", lambda: "test")

    class Inputs:
        def __init__(self, s, now):
            self.store, self.now = s, now

        def context(self, asset):
            return {"positioning": {}}

        def observations(self, *args, **kwargs):
            return []

        def updates(self, assets):
            return []

        def funding(self, since):
            return []

        def research_quotes(self, *args):
            return []

    monkeypatch.setattr(worker, "Sources", Inputs)
    monkeypatch.setattr(
        worker, "quotes_for_store", lambda *args: ([quote()], {"BTC": {"funding_rate": 0}})
    )
    monkeypatch.setattr(
        worker, "live_cache", lambda *args, **kwargs: ([quote()], {"BTC": {"funding_rate": 0}})
    )
    monkeypatch.setattr(worker, "utcnow", lambda: time(1))
    evaluate_events = events.evaluate
    clocks = []

    def causal_evaluate(*args, **kwargs):
        clocks.append(kwargs["clock"]())
        assert kwargs["quote_provider"](time(1))[0][0].at == quote().at
        return evaluate_events(*args, **kwargs)

    monkeypatch.setattr(events, "evaluate", causal_evaluate)
    results = Worker(path).tick_all()
    assert results["state"] == "OK"
    assert clocks == [time(1)] * 3
    by = {r["run_id"]: r for r in results["runs"]}
    assert by[cohorts["baseline"]]["admission"] == {}
    assert by[cohorts["immediate"]]["admission"]["ADMITTED"] == 1
    assert by[cohorts["confirmed"]]["admission"]["CONFIRMATION_MISSING"] == 1


def test_official_structured_transition_after_activation_and_no_prose_refresh(
    store, cohorts, source
):
    rid = cohorts["immediate"]
    old = venue(at=time(-1), impact="minor")
    _t, eid = enroll(store, old, time(-1))
    updated = venue(at=time(-1)).model_copy(
        update={
            "attributes": GenericAttrs(
                facts={
                    "impact": "critical",
                    "status": "identified",
                    "source_revision_at": time(1).isoformat(),
                    "component_states_json": json.dumps(
                        [{"id": "tjhgmj0ysvln", "name": "Trading", "status": "major_outage"}]
                    ),
                }
            )
        }
    )
    t2, same = enroll(store, updated, time(1))
    assert eid == same
    evaluate(store, rid, source, [t2])
    assert engine.positions(store, rid)[0]["state"] == "PENDING"
    assert (
        engine.positions(store, rid)[0]["thesis"]["context_snapshot"]["event_entry"]["report"][
            "information_at"
        ]
        == time(1).isoformat()
    )
    monitor(store, rid)
    prose = updated.model_copy(update={"title": "New prose only"})
    t3, _ = enroll(store, prose, time(1, 3))
    evaluate(store, rid, source, [t3], second=3)
    assert len(engine.positions(store, rid)) == 1


def test_exposure_limit_and_same_side_confluence_do_not_increase_size(store, cohorts, source):
    rid = cohorts["immediate"]
    for asset in ("ETH", "SOL", "LINK"):
        with store.transaction():
            engine.admit(
                store,
                rid,
                [observation(h(), asset=asset)],
                [quote(asset=asset)],
                Source(),
                {asset: {"funding_rate": 0}},
                now=time(1),
            )
        monitor(store, rid, asset=asset)
    t, _ = enroll(store, venue(at=time(2)), time(2))
    evaluate(store, rid, source, [t], minute=2)
    assert event_report.summary(store, rid)["hypotheses"][0]["rejections"] == {"EXPOSURE_LIMIT": 1}
    assert len(engine.positions(store, rid)) == 3


def test_duplicate_burst_bounded_writes_and_exposure(store, cohorts, source):
    rid = cohorts["immediate"]
    t, _ = enroll(store)
    evaluate(store, rid, source, [t])
    monitor(store, rid)
    before = engine.positions(store, rid)[0]["notional"]
    for second in range(3, 23):
        evaluate(store, rid, source, [t], second=second)
    assert len(engine.positions(store, rid)) == 1
    assert engine.positions(store, rid)[0]["notional"] == before
    assert (
        store.con.execute(
            "SELECT count(*) FROM paper_event_evidence WHERE run_id=?", [rid]
        ).fetchone()[0]
        <= 6
    )


def test_intent_clock_after_preparation_rejects_pre_intent_quote(store, cohorts, source):
    from market_signal.paper.nimble.model import ExecutableQuote

    t, _ = enroll(store)
    rid = cohorts["immediate"]
    captured = []
    original = engine.admit

    def delayed(store, rid, observations, quotes, wrapper, contexts=None, **kwargs):
        wrapper.intent_clock = lambda: time(1, 10)
        captured.extend(observations)
        return original(store, rid, observations, quotes, wrapper, contexts, **kwargs)

    from unittest.mock import patch

    with patch.object(engine, "admit", delayed):
        evaluate(store, rid, source, [t])
    p = engine.positions(store, rid)[0]
    assert p["intent_at"] == time(1, 10).isoformat()
    before = ExecutableQuote("BTC", time(1, 9).isoformat(), time(1, 9).isoformat(), 99.99, 100.01)
    with store.transaction():
        engine.monitor(store, rid, [before], now=time(1, 11), contexts={"BTC": {"funding_rate": 0}})
    assert engine.positions(store, rid)[0]["state"] == "PENDING"
    monitor(store, rid, second=12)
    assert engine.positions(store, rid)[0]["state"] == "OPEN"
    assert engine.positions(store, rid)[0]["entry_quote_at"] > p["intent_at"]


def test_protocol_mapping_never_uses_host_chain_or_broad_mentions():
    p = protocol().model_dump(mode="json")
    p["entities"] = ["aave", "ethereum"]
    p["assets"] = ["AAVE", "ETH"]
    p["observed_at"] = time(1).isoformat()
    assert events.classify(p, time(1))[:3] == ("protocol_stress", ("AAVE",), None)
    p["attributes"]["protocol"] = "lido"
    assert events.classify(p, time(1))[2] == "ASSET_OUTSIDE_UNIVERSE"
    p["attributes"]["protocol"] = None
    assert events.classify(p, time(1))[2] == "ASSET_OUTSIDE_UNIVERSE"


def test_batched_transition_starts_at_its_own_context_availability(store, cohorts, source):
    initial = venue(impact="minor")
    t1, eid = enroll(store, initial)
    updated = venue().model_copy(
        update={
            "attributes": GenericAttrs(
                facts={
                    **venue().attributes.facts,
                    "information_time": time(40).isoformat(),
                    "source_revision_at": time(40).isoformat(),
                }
            )
        }
    )
    t2, same = enroll(store, updated, time(40))
    assert eid == same
    # Both receipts were drained late: availability alone cannot identify the report.
    t1["available_at"] = time(40, 1).isoformat()
    t1["metadata"]["gateway_received_at"] = time(1).isoformat()
    t2["available_at"] = time(40, 2).isoformat()
    t2["metadata"]["gateway_received_at"] = time(40).isoformat()
    rid = cohorts["immediate"]
    assert evaluate(store, rid, source, [t1, t2], minute=40, second=2) == {"ADMITTED": 1}
    p = engine.positions(store, rid)[0]
    assert p["evidence_available_at"] == time(40, 2).isoformat()
    assert (
        p["thesis"]["context_snapshot"]["event_entry"]["state"]["first_seen_at"]
        == time(1).isoformat()
    )


def test_cohort_pair_atomic_activation_and_rollback(store):
    engine.register(store, events=True, now=T0)
    existing = engine.create(store, events=True, variant="confirmed", now=T0)
    with pytest.raises(ValueError, match="already active"):
        engine.create_cohorts(store, now=time(1))
    assert [r["run_id"] for r in engine.runs(store)] == [existing["run_id"]]
    engine.kill(store, existing["run_id"], reason="test", now=time(1))
    paired = engine.create_cohorts(store, now=time(2))
    assert {r["activated_at"] for r in paired["cohorts"].values()} == {time(2).isoformat()}
    assert all(
        engine.account(store, r["run_id"])["cash"] == 100 for r in paired["cohorts"].values()
    )
    assert all(engine.load(store, r["run_id"]) for r in paired["cohorts"].values())
