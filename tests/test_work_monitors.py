"""Phase 28A isolated replay. Synthetic facts never touch production storage/tools."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from market_signal.context import ledger
from market_signal.context.entities import load_entities
from market_signal.context.gateway.auth import Verifier
from market_signal.context.gateway.contract import validate
from market_signal.context.gateway.demo import sample
from market_signal.context.gateway.health import inspect, latencies
from market_signal.context.gateway.ingest import drain
from market_signal.context.gateway.quality import monitor_metrics
from market_signal.context.gateway.server import create_app
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.model import Observation
from market_signal.context.taxonomy import Confidence, SourceType
from market_signal.context.work_monitors import PROVIDER_IDS, definitions, prompt
from market_signal.context.work_research import HORIZONS, collect, measure
from market_signal.data.store import MIGRATIONS

TYPES = (
    "chain_halt",
    "exchange_outage",
    "global_risk_shock",
    "regulatory_action",
    "critical_vulnerability",
)
CATEGORIES = ("protocol", "market_structure", "macro", "protocol", "security")


def test_migration_does_not_touch_execution_or_existing_context():
    ddl = MIGRATIONS[27]
    assert "ALTER" not in ddl and "DROP" not in ddl and "paper_" not in ddl
    assert "context_work_observations" in ddl and "context_work_reactions" in ddl


def candidate(pid, *, at=None):
    t = at or now()
    index = PROVIDER_IDS.index(pid)
    raw = sample(test=False)
    raw.update(sender_version=pid, sent_at=t.isoformat(), external_event_id="isolated-episode")
    raw["item"].update(
        title="SYNTHETIC significant incident",
        summary="Isolated replay only.",
        category=CATEGORIES[index],
        subcategory=TYPES[index],
        url="https://example.com/isolated-episode",
        entities=["ethereum"],
        assets=["ETH"],
        first_seen_at=(t - timedelta(seconds=1)).isoformat(),
        event_time=(t - timedelta(seconds=2)).isoformat(),
        published_at=(t - timedelta(seconds=2)).isoformat(),
        factual_claims=["Synthetic critical system disruption."],
    )
    raw["monitor"] = dict(
        policy_version="work_sensor_policy_v1",
        novelty="new",
        immediate_impact=True,
        impact_reason="A critical system disruption may affect immediate liquidity.",
        evidence="primary",
        information_time=(t - timedelta(seconds=2)).isoformat(),
    )
    return raw


def receive(spool, raw, received=None):
    t = received or now()
    sub, obs = validate(raw, t, load_entities())
    return spool.accept(raw, sub, obs, "isolated_work_oauth", t)


@pytest.mark.parametrize("pid", PROVIDER_IDS)
def test_each_specialist_valid_and_boundary_replays(pid, tmp_path):
    raw = candidate(pid)
    sub, obs = validate(raw, now(), load_entities())
    assert obs.source_id == pid and obs.confidence == "REPORTED"
    assert sub.monitor.immediate_impact
    mutations = [
        ("ordinary", lambda p: p["item"].update(subcategory="partnership", category="protocol")),
        ("uncertain_rumour", lambda p: p["monitor"].update(evidence="unsupported_rumour")),
        ("unknown_asset", lambda p: p["item"].update(assets=["NOT_A_TOKEN"])),
        ("malformed_url", lambda p: p["item"].update(url="javascript:alert(1)")),
        ("trade_field", lambda p: p["item"].update(direction="short")),
        ("trade_text", lambda p: p["item"].update(summary="SHORT ETH. Use leverage.")),
        (
            "stale",
            lambda p: p["monitor"].update(
                information_time=(now() - timedelta(hours=3)).isoformat()
            ),
        ),
        ("no_immediate_impact", lambda p: p["monitor"].update(immediate_impact=False)),
        (
            "future",
            lambda p: p["item"].update(first_seen_at=(now() + timedelta(hours=1)).isoformat()),
        ),
        ("fake_confidence", lambda p: p["item"].update(confidence="OFFICIAL")),
    ]
    for _name, change in mutations:
        bad = copy.deepcopy(raw)
        change(bad)
        with pytest.raises(ValueError):
            validate(bad, now(), load_entities())
    # A credible but still uncertain breaking report is allowed; confidence never inflates.
    raw["item"]["confidence"] = "UNCONFIRMED"
    raw["monitor"]["evidence"] = "credible_reporting"
    assert validate(raw, now(), load_entities())[1].confidence == "UNCONFIRMED"
    raw["item"].update(assets=[], entities=[], market_wide=True)
    assert validate(raw, now(), load_entities())[1].market_wide


@pytest.mark.parametrize("pid", PROVIDER_IDS)
def test_episode_dedupe_update_and_restart(pid, store, tmp_path):
    sp = Spool(tmp_path / "spool", rate=60)
    raw = candidate(pid)
    first = receive(sp, raw)
    assert drain(store, sp)["errors"] == 0
    eid = inspect(sp, first["receipt_id"])["completion"]["logical_event_id"]
    before = ledger.state_asof(store, eid, None)
    assert before["first_source"] == pid
    assert receive(sp, raw)["status"] == "DUPLICATE"
    rewrite = copy.deepcopy(raw)
    rewrite.update(submission_id=uuid.uuid4().hex, sent_at=now().isoformat())
    rewrite["item"]["title"] = "SYNTHETIC rewritten headline"
    second = receive(sp, rewrite)
    drain(store, sp)
    assert inspect(sp, second["receipt_id"])["completion"]["duplicates"] == 1
    update = copy.deepcopy(rewrite)
    update.update(submission_id=uuid.uuid4().hex, sent_at=now().isoformat())
    update["monitor"]["novelty"] = "update"
    update["item"].update(
        url="https://example.com/material-update",
        factual_claims=["New material loss estimate."],
        attributes={"kind": "generic", "facts": {"loss_usd_estimate": 120000000}},
    )
    ack = receive(sp, update)
    restarted = Spool(sp.root, rate=60)
    assert drain(store, restarted)["errors"] == 0
    completion = inspect(sp, ack["receipt_id"])["completion"]
    assert completion["logical_event_id"] == eid and completion["new_updates"] == 1
    after = ledger.state_asof(store, eid, None)
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["attributes"]["facts"]["loss_usd_estimate"] == 120000000
    assert drain(store, restarted)["ingested"] == 0
    assert (
        store.con.execute("SELECT count(*) FROM context_work_research_pending").fetchone()[0] == 1
    )
    assert store.con.execute("SELECT count(*) FROM paper_nimble_runs").fetchone()[0] == 0


def test_distinct_events_same_asset_do_not_merge(store, tmp_path):
    sp = Spool(tmp_path / "spool")
    a = candidate(PROVIDER_IDS[0])
    b = copy.deepcopy(a)
    b.update(submission_id=uuid.uuid4().hex, external_event_id="different-episode")
    b["item"]["url"] = "https://example.com/another-incident"
    receive(sp, a)
    receive(sp, b)
    drain(store, sp)
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 2


@pytest.mark.parametrize("pid", PROVIDER_IDS)
def test_specialist_smoke_never_enters_context_or_research(pid, store, tmp_path):
    sp = Spool(tmp_path / "spool")
    p = candidate(pid)
    p["test"] = True
    ack = receive(sp, p)
    assert drain(store, sp)["errors"] == 0
    result = inspect(sp, ack["receipt_id"])["completion"]
    assert result["ingest_status"] == "TEST_EXCLUDED" and result["logical_event_id"] is None
    for table in (
        "context_events",
        "context_work_observations",
        "context_work_research_pending",
        "context_work_reactions",
        "paper_nimble_triggers",
    ):
        assert store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_corroboration_and_independent_quality_not_ai_self_confirmation(store, tmp_path):
    sp = Spool(tmp_path / "spool")
    p = candidate(PROVIDER_IDS[0])
    ack = receive(sp, p)
    drain(store, sp)
    eid = inspect(sp, ack["receipt_id"])["completion"]["logical_event_id"]
    p.update(submission_id=uuid.uuid4().hex)
    p["monitor"]["novelty"] = "corroboration"
    p["item"]["url"] = "https://independent.example.com/corroboration"
    receive(sp, p)
    drain(store, sp)
    q = monitor_metrics(store, sp)[PROVIDER_IDS[0]]
    assert q["corroborations"] == 1 and q["later_independently_corroborated"] == 0
    assert ledger.state_asof(store, eid, None)["confidence"] == "REPORTED"
    obs = validate(p, now(), load_entities())[1].model_copy(
        update={
            "source_id": "official_later",
            "source_type": SourceType.PROTOCOL_ANNOUNCEMENT,
            "confidence": Confidence.OFFICIAL,
        }
    )
    ledger.ingest(store, [obs], now=now())
    assert monitor_metrics(store, sp)[PROVIDER_IDS[0]]["later_independently_corroborated"] == 1
    ledger.ingest(
        store,
        [obs.model_copy(update={"confidence": Confidence.DENIED, "update_kind": "denial"})],
        now=now(),
    )
    assert monitor_metrics(store, sp)[PROVIDER_IDS[0]]["later_denied_invalidated"] == 1


def test_official_protected_cross_provider_confidence_and_denial(store, tmp_path):
    sp = Spool(tmp_path / "spool")
    p = candidate(PROVIDER_IDS[0])
    official = validate(p, now(), load_entities())[1].model_copy(
        update={
            "source_id": "official_fixture",
            "source_type": SourceType.PROTOCOL_ANNOUNCEMENT,
            "confidence": Confidence.OFFICIAL,
        }
    )
    official = Observation.model_validate(official.model_dump())
    eid = ledger.ingest(store, [official], now=now()).new_events[0]
    old = ledger.state_asof(store, eid, None)
    p["item"]["title"] = "SYNTHETIC Work alternative title"
    ack = receive(sp, p)
    drain(store, sp)
    assert inspect(sp, ack["receipt_id"])["completion"]["logical_event_id"] == eid
    folded = ledger.state_asof(store, eid, None)
    assert folded["title"] == old["title"] and folded["confidence"] == "OFFICIAL"
    denial = official.model_copy(update={"confidence": Confidence.DENIED, "update_kind": "denial"})
    ledger.ingest(store, [denial], now=now())
    p.update(submission_id=uuid.uuid4().hex)
    p["item"]["factual_claims"] = ["Fresh disputed loss report."]
    p["monitor"]["novelty"] = "update"
    receive(sp, p)
    drain(store, sp)
    assert ledger.state_asof(store, eid, None)["confidence"] == "DENIED"
    assert store.con.execute("SELECT count(*) FROM context_work_observations").fetchone()[0] == 2


def test_metrics_causality_and_reject_attribution(store, tmp_path):
    sp = Spool(tmp_path / "spool")
    pid = PROVIDER_IDS[0]
    app = create_app(sp, Verifier(tokens={"t" * 48: "isolated_work_oauth"}), dev=True)
    with TestClient(app) as client:
        p = candidate(pid)
        headers = {"Authorization": "Bearer " + "t" * 48}
        ack = client.post("/context/v1/events", json=p, headers=headers).json()
        bad = copy.deepcopy(p)
        bad["item"]["direction"] = "short"
        assert client.post("/context/v1/events", json=bad, headers=headers).status_code == 422
    drain(store, sp)
    rec = inspect(sp, ack["receipt_id"])
    q = monitor_metrics(store, sp)[pid]
    assert q["accepted"] == 1 and q["rejected"] == 1 and q["new_logical_events"] == 1
    assert q["affected_resolved_asset_count_distribution"] == {1: 1}
    assert q["submissions"] == 2
    assert rec["gateway_received_at"] <= rec["completion"]["context_available_at"]
    assert latencies(rec, rec["completion"])["send_to_gateway_s"] >= 0
    no_publication = copy.deepcopy(rec)
    no_publication["payload"]["item"]["published_at"] = None
    assert latencies(no_publication, rec["completion"])["publication_to_gateway_s"] is None
    eid = rec["completion"]["logical_event_id"]
    assert ledger.state_asof(store, eid, now() - timedelta(hours=1)) is None
    assert q["latency_s"]["gateway_to_available_s"]["n"] == 1


def test_monitor_prompts_complete_and_routing():
    d = definitions()
    assert set(d["monitors"]) == set(PROVIDER_IDS)
    for pid, m in d["monitors"].items():
        text = prompt(d, pid)
        assert "submit_market_event" in text and pid in text and "No quota" in text
        assert len(m["positive"]) >= 5 and len(m["negative"]) >= 5


def test_text_instructions_rejected_but_observed_reaction_allowed():
    p = candidate(PROVIDER_IDS[0])
    for instruction in (
        "Open a short BTC position.",
        "Allocate 20% to ETH.",
        "Place a BTC buy order.",
        "Set a stop at 90.",
    ):
        p["item"]["summary"] = instruction
        with pytest.raises(ValueError, match="trade instruction"):
            validate(p, now(), load_entities())
    p["item"]["summary"] = "The source reports BTC fell 2% following the announcement."
    assert validate(p, now(), load_entities())[1].source_id == PROVIDER_IDS[0]


def minutes(store, t0, n=242, *, gap=None, future_reference=False):
    # Named-column inserts deliberately exercise research against the actual minute schema.
    values = []
    for i in range(-245, n):
        if i == gap:
            continue
        minute = t0 + timedelta(minutes=i)
        px = 100 + i * 0.01
        ingest = minute + timedelta(minutes=1, seconds=1)
        if future_reference and i < 0:
            ingest = t0 + timedelta(minutes=1)
        values.append([minute, px, px + 0.01, px - 0.01, minute + timedelta(minutes=1), ingest])
    with store.transaction():
        store.con.executemany(
            "INSERT INTO microstructure_minutes (feature_version,coin,minute_open,revision,status,"
            "trade_cov,book_samples,depth20_samples,last_px,high_px,low_px,buy_vol,sell_vol,"
            "oi_end,funding_end,n_dup,n_late,finalized_at,ingested_at,session_id,content_sha) "
            "VALUES ('micro_test','ETH',?,0,'COMPLETE',1,30,30,?,?,?,10,10,1000,0.00001,0,0,?,?,"
            "'isolated','test')",
            values,
        )


def test_research_all_horizons_no_lookahead_and_immutable_denial(store, tmp_path):
    t0 = now().replace(second=30, microsecond=0) - timedelta(hours=5)
    minutes(store, t0.replace(second=0))
    sp = Spool(tmp_path / "spool")
    p = candidate(PROVIDER_IDS[0], at=t0)
    ack = receive(sp, p, received=t0)
    drain(store, sp)
    # drain uses actual completion clock; use explicit first availability for historical replay.
    store.con.execute("UPDATE context_work_research_pending SET available_at=?", [t0])
    assert collect(store, now=t0 + timedelta(seconds=30))["recorded"] == 0
    assert collect(store, now=now())["recorded"] == 7
    rows = store.con.execute(
        "SELECT horizon_minutes,payload FROM context_work_reactions ORDER BY 1"
    ).fetchall()
    assert [h for h, _ in rows] == list(HORIZONS)
    for _h, raw in rows:
        r = json.loads(raw)
        assert r["status"] == "ok" and r["research_only"] and r["return_pct"] > 0
        assert r["reference_close_at"] <= r["available_at"] < r["outcome_close_at"]
        assert r["partial_first_minute_excluded"] and r["endpoint_delay_s"] == 30
    assert collect(store, now=now())["recorded"] == 0
    assert inspect(sp, ack["receipt_id"])["completion"]["ingest_status"] == "INGESTED"
    eid = inspect(sp, ack["receipt_id"])["completion"]["logical_event_id"]
    denied = validate(candidate(PROVIDER_IDS[0]), now(), load_entities())[1].model_copy(
        update={
            "source_id": "official_denial",
            "source_type": SourceType.PROTOCOL_ANNOUNCEMENT,
            "confidence": Confidence.DENIED,
            "update_kind": "denial",
        }
    )
    ledger.ingest(store, [denied], now=now())
    assert ledger.state_asof(store, eid, None)["confidence"] == "DENIED"
    assert collect(store, now=now())["recorded"] == 0
    assert (
        store.con.execute(
            "SELECT horizon_minutes,payload FROM context_work_reactions ORDER BY 1"
        ).fetchall()
        == rows
    )


@pytest.mark.parametrize("problem", ["gap", "future_reference", "no_data"])
def test_research_missingness_never_invents_prices(store, problem):
    t0 = now().replace(second=30, microsecond=0) - timedelta(hours=5)
    if problem != "no_data":
        minutes(
            store,
            t0.replace(second=0),
            gap=2 if problem == "gap" else None,
            future_reference=problem == "future_reference",
        )
    assert measure(store, "ETH", t0, 5, now())["status"] != "ok"
