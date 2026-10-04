"""Perps co-pilot: policy rules, live-signal decisions, dedup, delivery, neutrality, separation."""

# ruff: noqa: F811  (helpers take the imported ``env`` fixture's value as ``env``)

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.copilot import engine, render
from market_signal.copilot import policy as pol
from market_signal.copilot.engine import CopilotError
from market_signal.copilot.policy import CopilotPolicy, evaluate
from market_signal.research.lab.evidence import EvidenceProfile, EvidenceSource, record_profiles
from tests.test_lab_forward import LENIENT, SW, day, env  # noqa: F401  (env is a fixture)

# Synthetic markets have tiny samples; a test-only policy relaxes only the sample/breadth
# thresholds so the synthetic evidence is eligible. Everything else equals v1.
TEST_POLICY = CopilotPolicy(
    version=99, min_independent_events=1, min_assets_with_events=1, min_excess=-1.0,
    max_asset_event_share=1.0, block_dominated_by_one_asset=False, block_isolated_spike=False,
)  # fmt: skip
EXECUTION = ("execut", "approv", "order", "leverage", "sizing", "position_size", "stop",
             "take_profit", "tp_", "risk_alloc", "notional", "allocation", "fill")  # fmt: skip
CONTEXT = {"semantics_ok": True, "semantics_detail": "ok", "max_interval_hours": 24.0,
           "duplicate_of": None, "retired": False}  # fmt: skip


def view(**over) -> dict:
    """A realistic EXPLORATORY evidence view (shape of ``engine.evidence_view``)."""
    ev = {
        "profile_id": "evidence_x", "profile_schema": "2", "baseline_profile_id": "evidence_x",
        "subject": {"name": "ma_trend_20_100_short", "family": "ma_trend", "params": {"fast": 20, "slow": 100},
                    "side": "short"},
        "tier": "EXPLORATORY", "historical_tier": "EXPLORATORY", "primary_horizon": "10d",
        "effect": {"excess_mean": 0.026, "net_mean": 0.013, "hit_rate": 0.56},
        "sample": {"independent_events": 39, "assets_with_events": 6},
        "assets": {"assets_with_events": 6, "positive": 4, "negative": 2, "positive_share": 0.67,
                   "max_asset_event_share": 0.21, "dominated_by_one_asset": False},
        "neighbourhood": {"label": "mixed", "isolated_spike": False},
        "statistics": {"raw_p": 0.11, "q": 0.91, "q_target": 0.1, "fdr_survivor": False},
        "full_research": None, "validation": None, "forward": None, "source_versions": {},
    }  # fmt: skip
    for k, v in over.items():
        ev[k] = {**ev[k], **v} if isinstance(v, dict) and isinstance(ev.get(k), dict) else v
    return ev


def full(status="FULL_RESEARCH_CONSISTENT"):
    return {"status": status, "walk_forward": {"adequate_folds": 4, "positive_adequate_folds": 3},
            "sensitivity": {"label": "plateau"}}  # fmt: skip


def fwd_block(level, excess, direction=None):
    return {"tracking_id": "t", "maturity": level, "signals_recorded": 120, "signals_resolved": 110,
            "independent_resolved": 105, "excess_mean": excess,
            "direction_vs_historical": direction}  # fmt: skip


def decide(ev=None, **ctx):
    return evaluate(pol.get_policy(1), view() if ev is None else ev, {**CONTEXT, **ctx})


def record(ev, result, symbol="HYPE"):
    return {"symbol": symbol, "side": ev["subject"]["side"], "bar_close": "2026-10-05T00:00:00+00:00",
            "evidence": ev, "result": result,
            "signal": {"fired": True, "cooldown_bars": 10, "features": {"close": 70.0, "ema_100": 80.0},
                       "conditions": {"close lt ema_100": True},
                       "definition_conditions": [{"left": {"name": "close", "timeframe": "1d"}, "op": "lt",
                                                  "right": {"name": "ema_100", "timeframe": "1d"}}]}}  # fmt: skip


# --------------------------------------------------------------------------- policy (pure)


def test_exploratory_alerts_even_when_q_is_high():
    r = decide()
    assert r["decision"] == "ALERT" and r["priority"] == "WATCH" and r["blocked_by"] == []
    assert any("did not survive family (BH) correction: q 0.91" in c for c in r["caveats"])


def test_fdr_honesty_in_message():
    ev = view()
    text = render.render_alert(record(ev, decide(ev)))
    assert "BH q 0.91 (did not survive family correction)" in text and "Raw p 0.11" in text
    surv = view(statistics={"q": 0.04, "fdr_survivor": True})
    assert "survived family correction at 0.10" in render.render_alert(record(surv, decide(surv)))


def test_stronger_evidence_maps_to_strong_watch():
    broad = {"label": "plateau"}
    strong = decide(view(full_research=full(), neighbourhood=broad))
    assert strong["priority"] == "STRONG_WATCH"
    assert strong["priority_reasons"] == [
        "consistent full research, plateau neighbourhood and broad asset support"
    ]
    # the evidence tier itself is untouched: EXPLORATORY evidence, STRONG_WATCH priority
    assert (
        decide(view(full_research=full("FULL_RESEARCH_MIXED"), neighbourhood=broad))["priority"]
        == "WATCH"
    )
    assert decide(view(tier="RESEARCH_SUPPORTED"))["priority"] == "STRONG_WATCH"
    narrow = view(full_research=full(), neighbourhood=broad, assets={"assets_with_events": 4},
                  sample={"assets_with_events": 4})  # fmt: skip
    assert decide(narrow)["priority"] == "WATCH"
    val = view(validation={"status": "VALIDATION_SUPPORTIVE", "sample": {"independent_events": 40}})
    assert decide(val)["priority"] == "STRONG_WATCH"


def test_adverse_validation_suppresses():
    r = decide(view(validation={"status": "VALIDATION_ADVERSE"}))
    assert r["decision"] == "SUPPRESS" and "validation_not_adverse" in r["blocked_by"]
    # Phase 9 caps the tier at INCONCLUSIVE too; either rule alone blocks
    r = decide(view(tier="INCONCLUSIVE", validation={"status": "VALIDATION_ADVERSE"}))
    assert set(r["blocked_by"]) == {"validation_not_adverse", "tier_eligible"}
    mixed = decide(view(full_research=full(), neighbourhood={"label": "plateau"},
                        validation={"status": "VALIDATION_MIXED"}))  # fmt: skip
    assert mixed["decision"] == "ALERT" and mixed["priority"] == "WATCH"


def test_insufficient_validation_and_immature_forward_do_not_suppress():
    ev = view(validation={"status": "VALIDATION_INSUFFICIENT", "window": ["2026-10-01", "2027-10-01"]},
              forward=fwd_block("TOO_EARLY", None) | {"independent_resolved": 0, "signals_recorded": 0})  # fmt: skip
    r = decide(ev)
    assert r["decision"] == "ALERT"
    assert "validation not mature / insufficient" in r["caveats"]
    assert "forward evidence too early" in r["caveats"]
    text = render.render_alert(record(ev, r))
    assert "Validation: not mature / insufficient (reserved period runs to 01 Oct 2027)" in text
    assert "Forward: TOO EARLY (0 resolved, 0 prospective signals so far)" in text
    # an EARLY adverse forward sample is a caveat, not a block (tiny samples)
    assert decide(view(forward=fwd_block("EARLY", -0.02, "opposite")))["decision"] == "ALERT"


def test_mature_adverse_forward_suppresses_developing_downgrades():
    r = decide(view(forward=fwd_block("MATURE", -0.01, "opposite")))
    assert r["decision"] == "SUPPRESS" and r["blocked_by"] == ["forward_not_adverse_mature"]
    assert decide(view(forward=fwd_block("MATURE", 0.01, "same")))["decision"] == "ALERT"
    dev = decide(view(full_research=full(), neighbourhood={"label": "plateau"},
                      forward=fwd_block("DEVELOPING", -0.01, "opposite")))  # fmt: skip
    assert dev["decision"] == "ALERT" and dev["priority"] == "WATCH"
    assert dev["priority_reasons"] == ["held at WATCH: forward evidence developing and adverse"]


@pytest.mark.parametrize("tier", ["NEGATIVE", "INSUFFICIENT", "UNAVAILABLE", "INCONCLUSIVE"])
def test_ineligible_tiers_suppress(tier):
    r = decide(view(tier=tier))
    assert r["decision"] == "SUPPRESS" and "tier_eligible" in r["blocked_by"]


def test_blocking_rules():
    cases = {
        "full_research_not_adverse": view(full_research=full("FULL_RESEARCH_INCONSISTENT")),
        "effect_positive": view(effect={"excess_mean": -0.001}),
        "sample_adequate": view(sample={"independent_events": 12}),
        "breadth_ok": view(assets={"dominated_by_one_asset": True}),
        "not_isolated_spike": view(neighbourhood={"isolated_spike": True}),
    }
    for rule, ev in cases.items():
        assert decide(ev)["blocked_by"] == [rule], rule
    assert decide(max_interval_hours=48.0)["blocked_by"] == ["data_quality"]
    assert decide(semantics_ok=False)["blocked_by"] == ["versions_compatible"]
    assert decide(duplicate_of="copdecision_1")["blocked_by"] == ["signal_new"]
    assert decide(retired=True)["blocked_by"] == ["strategy_not_retired"]
    none = evaluate(pol.get_policy(1), None, CONTEXT)
    assert none["decision"] == "SUPPRESS" and "evidence_available" in none["blocked_by"]
    # q, missing validation and missing forward evidence never block
    assert decide(view(statistics={"q": 0.99, "raw_p": 0.24}))["decision"] == "ALERT"


def test_policy_identity_and_strictness():
    p1 = pol.get_policy(1)
    assert p1.policy_id == pol.get_policy().policy_id == CopilotPolicy().policy_id
    assert CopilotPolicy(min_independent_events=31).policy_id != p1.policy_id
    assert TEST_POLICY.policy_id != p1.policy_id
    with pytest.raises(ValidationError):
        CopilotPolicy.model_validate({**p1.model_dump(), "approved_for_trading": True})
    with pytest.raises(ValueError, match="unknown co-pilot policy"):
        pol.get_policy(42)


def test_message_wording_and_digest():
    ev = view(full_research=full("FULL_RESEARCH_MIXED"))
    r = decide(ev)
    text = render.render_alert(record(ev, r))
    assert text.startswith("<b>HYPE · SHORT BIAS</b>\nMA Trend 20/100 · <b>WATCH</b>")
    assert "Close below 100D EMA (70 vs 80)" in text and "New signal on this close" in text
    assert "Why surfaced: " in text and "Exploratory evidence — human review only" in text
    for word in ("BUY", "SELL", "APPROVED", "HIGH-CONFIDENCE", "probability", "confidence"):
        assert word not in text
    digest = render.render_digest([record(ev, r, s) for s in ("BTC", "ETH", "SOL", "HYPE")])
    assert (
        digest.count("SHORT BIAS") == 4 and "4 setups" in digest and "not FDR-significant" in digest
    )


# --------------------------------------------------------------------------- live (integration)


@pytest.fixture
def cop(env, monkeypatch):
    monkeypatch.setitem(pol.POLICIES, 99, TEST_POLICY)
    return env


class Sender:
    def __init__(self, fail: int = 0):
        self.sent, self.fail = [], fail

    def __call__(self, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Telegram unreachable")
        self.sent.append(text)

    def factory(self):
        return self


def _watch(env, which="long", at=None, policy=TEST_POLICY, **kw):
    return engine.register_watch(env.ledger, env.profile[which]["profile_id"], reason="test",
                                 origin="test", software=SW, policy=policy, now=at or day(0, 2), **kw)  # fmt: skip


def _first_signal(env, which="long", start=1, upto=40):
    """Publish one bar a day; return the first k whose newest bar fires on some coin."""
    for k in range(start, upto + 1):
        env.market.publish(k)
        cands = engine.candidates(env.ledger, now=day(k, 3))
        hits = [c for c in cands if c["state"] == "SIGNAL" and c["strategy_id"] == env.ids[which]]
        if hits:
            return k, hits
    raise AssertionError("no signal in the synthetic window")


def _count(env, table):
    return env.store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _lab_digest(env) -> dict:
    out = {}
    for (t,) in env.store.con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'lab_%' "
        "AND table_type='BASE TABLE' ORDER BY 1"
    ).fetchall():
        rows = env.store.con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        out[t] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return out


def test_watch_registration_rules(cop):
    dry = _watch(cop, dry_run=True)
    assert _count(cop, "copilot_watchlist") == 0 and dry["dry_run"]
    w = _watch(cop)
    assert w["watch_id"] == dry["watch_id"]
    assert w["definition"]["baseline_profile_id"] == cop.profile["long"]["profile_id"]
    with pytest.raises(CopilotError, match="already registered"):
        _watch(cop)
    with pytest.raises(CopilotError, match="open watch"):
        _watch(cop, label="second")
    with pytest.raises(Exception, match="not trackable"):
        _watch(cop, "never")
    with pytest.raises(CopilotError, match="reason"):
        engine.register_watch(cop.ledger, cop.profile["long"]["profile_id"], reason=" ",
                              origin="t", software=SW)  # fmt: skip


def test_no_signal_nothing_alerted(cop):
    _watch(cop)
    for k in range(1, 40):  # first day on which no watched asset fires
        cop.market.publish(k)
        states = {c["state"] for c in engine.candidates(cop.ledger, now=day(k, 3))}
        if "SIGNAL" not in states:
            break
    assert states == {"NO_SIGNAL"}
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert out["decisions"] == 0 and sender.sent == [] and _count(cop, "copilot_decisions") == 0
    assert _count(cop, "copilot_runs") == 1  # NO_SIGNAL is auditable in the run summary
    notes = json.loads(cop.store.con.execute("SELECT summary FROM copilot_runs").fetchone()[0])[
        "notes"
    ]
    assert {n["state"] for n in notes} == states


def test_signal_alert_recorded_sent_once_and_idempotent(cop):
    _watch(cop)
    k, hits = _first_signal(cop)
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert out["alerts"] == len(hits) and out["suppressed"] == 0
    assert len(sender.sent) == len(hits) and "LONG BIAS" in sender.sent[0]
    rows = engine.list_decisions(cop.store)
    assert {r["symbol"] for r in rows} == {h["symbol"] for h in hits}
    assert all(r["decision"] == "ALERT" and r["delivery"] == "sent" for r in rows)
    # same bar, later run (still inside the live window): no new decision, no resend
    again = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 20))
    assert again["decisions"] == 0 and again["deliveries"] == [] and len(sender.sent) == len(hits)
    assert all(
        "already decided" in n.get("note", "") for n in again["notes"] if n["state"] == "SIGNAL"
    )
    shown = engine.show_decision(cop.store, rows[0]["decision_id"])
    rec = shown["record"]
    assert rec["policy_id"] == TEST_POLICY.policy_id and rec["evidence"]["tier"] == "EXPLORATORY"
    assert rec["signal"]["data_cutoff"]["bars_close_time_lte"] == day(k).isoformat()
    assert {c["rule"] for c in rec["result"]["checks"]} >= {
        "tier_eligible",
        "signal_new",
        "data_quality",
    }
    assert rec["message"]["sha256"] == render.sha256(rec["message"]["text"])
    assert [a["status"] for a in shown["delivery"]["attempts"]] == ["attempted", "sent"]
    # the live signal is the Phase 3 compiler's: the forward tracker sees the same bar fire
    assert rec["signal"]["fired"] is True


def test_old_bars_never_alert(cop):
    _watch(cop)
    k, _ = _first_signal(cop)
    sender = Sender()
    late = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 25))
    assert late["decisions"] == 0 and sender.sent == []
    assert {n["state"] for n in late["notes"]} == {"OUTSIDE_WINDOW"}


def test_dry_run_writes_and_sends_nothing(cop):
    _watch(cop)
    k, hits = _first_signal(cop)
    before = {t: _count(cop, t) for t in ("copilot_runs", "copilot_decisions", "copilot_deliveries",
                                          "copilot_software")}  # fmt: skip
    sender = Sender()
    out = engine.run(
        cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3), dry_run=True
    )
    assert out["alerts"] == len(hits) and len(out["would_send"]) == len(hits)
    assert "LONG BIAS" in out["would_send"][0] and sender.sent == []
    assert {t: _count(cop, t) for t in before} == before


def test_consumer_neutrality(cop):
    from market_signal.research.lab import forward as fwd

    fwd.enroll(cop.ledger, cop.profile["long"]["profile_id"], reason="cohort", origin="test",
               software=SW, now=day(0, 1))  # fmt: skip
    _watch(cop)
    before = _lab_digest(cop)
    k, _ = _first_signal(cop)
    engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(k, 3))
    engine.candidates(cop.ledger, now=day(k, 4))
    assert _lab_digest(cop) == before  # no Lab record (incl. lab_software) was written
    # the forward tracker still evaluates the bar itself, and agrees on the signal
    fwd.check(cop.ledger, software=SW, now=day(k, 5))
    rec = engine.show_decision(cop.store, engine.list_decisions(cop.store)[0]["decision_id"])[
        "record"
    ]
    assert rec["evidence"]["forward"]["maturity"] == "TOO_EARLY"
    status = cop.store.con.execute(
        "SELECT status FROM lab_forward_evaluations WHERE symbol=? AND bar_close=?",
        [rec["symbol"], day(k)],
    ).fetchone()[0]
    assert status == "signal"


def test_telegram_failure_is_auditable_and_retry_safe(cop):
    _watch(cop)
    k, hits = _first_signal(cop)
    sender = Sender(fail=len(hits))
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert out["alerts"] == len(hits) and all(d["status"] == "failed" for d in out["deliveries"])
    ids = [r["decision_id"] for r in engine.list_decisions(cop.store)]
    assert all(engine.show_decision(cop.store, i)["delivery"]["state"] == "failed" for i in ids)
    # retry inside the live window: same decisions, delivered once, nothing duplicated
    retry = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 6))
    assert retry["decisions"] == 0 and len(sender.sent) == len(hits)
    assert [r["decision_id"] for r in engine.list_decisions(cop.store)] == ids
    third = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 7))
    assert third["deliveries"] == [] and len(sender.sent) == len(hits)
    shown = engine.show_decision(cop.store, ids[0])["delivery"]
    assert [(a["attempt"], a["status"]) for a in shown["attempts"]] == [
        (1, "attempted"), (1, "failed"), (2, "attempted"), (2, "sent")]  # fmt: skip

    # Telegram not configured at all: recorded as failed, decision intact
    def broken():
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    k2, _ = _first_signal(cop, start=k + 1)
    out2 = engine.run(cop.ledger, software=SW, sender_factory=broken, now=day(k2, 3))
    assert out2["alerts"] >= 1 and "TELEGRAM_BOT_TOKEN" in out2["deliveries"][0]["error"]


def test_unknown_delivery_outcome_is_never_resent(cop):
    _watch(cop)
    k, _ = _first_signal(cop)

    def crash():  # the process dies after "attempted" and before the outcome is written
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        engine.run(
            cop.ledger, software=SW, sender_factory=lambda: lambda text: crash(), now=day(k, 3)
        )
    later = engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(k, 4))
    assert later["deliveries"] == []
    d = engine.list_decisions(cop.store)[0]
    assert d["delivery"] == "unknown"


def test_digest_when_many_fire(cop, monkeypatch):
    monkeypatch.setattr(render, "MAX_SINGLE", 0)
    _watch(cop)
    k, hits = _first_signal(cop)
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert len(sender.sent) == 1 and f"{len(hits)} setups" in sender.sent[0]
    assert out["deliveries"][0]["kind"] == "digest"
    kinds = {
        r[0]
        for r in cop.store.con.execute("SELECT message_kind FROM copilot_deliveries").fetchall()
    }
    assert kinds == {"digest"}


def test_policy_change_never_replays_old_bars(cop, monkeypatch):
    w = _watch(cop)
    k, _ = _first_signal(cop)
    engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(k, 3))
    engine.set_watch_status(cop.store, w["watch_id"], "stopped", reason="policy v98", now=day(k, 4))
    v98 = CopilotPolicy(version=98, **{f: getattr(TEST_POLICY, f) for f in (
        "min_independent_events", "min_assets_with_events", "min_excess", "max_asset_event_share",
        "block_dominated_by_one_asset", "block_isolated_spike")}, strong_min_assets=1)  # fmt: skip
    monkeypatch.setitem(pol.POLICIES, 98, v98)
    _watch(cop, at=day(k, 5), policy=v98)
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 6))
    assert out["decisions"] == 0 and sender.sent == []  # bar k closed before the new watch
    assert {n["state"] for n in out["notes"] if n.get("symbol")} == {"BEFORE_REGISTRATION"}
    k2, _ = _first_signal(cop, start=k + 1)
    engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k2, 3))
    by_policy = cop.store.con.execute(
        "SELECT policy_id, min(bar_close) FROM copilot_decisions GROUP BY 1").fetchall()  # fmt: skip
    assert dict(by_policy)[v98.policy_id] == day(k2) and len(by_policy) == 2


def test_cross_policy_duplicate_is_suppressed(cop, monkeypatch):
    """Even if a second watch could see the same bar, a surfaced signal is never resent."""
    _watch(cop)
    k, _ = _first_signal(cop)
    engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(k, 3))
    c = next(c for c in engine.candidates(cop.ledger, now=day(k, 4)) if c["state"] == "SIGNAL")
    assert c["result"]["blocked_by"] == ["signal_new"]


def test_version_mismatch_suppresses(cop, monkeypatch):
    _watch(cop)
    monkeypatch.setattr(engine, "semantics", lambda: {"compiler_version": "lab_daily_compiler_v2",
                                                      "vocabulary_version": "lab_features_v1"})  # fmt: skip
    k, hits = _first_signal(cop)
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert out["alerts"] == 0 and out["suppressed"] == len(hits) and sender.sent == []
    d = engine.show_decision(cop.store, engine.list_decisions(cop.store)[0]["decision_id"])
    assert d["decision"] == "SUPPRESS" and d["priority"] is None
    assert "versions_compatible" in d["record"]["result"]["blocked_by"]


def _extend(env, which, *, tier, full_status, val_status):
    """Record a schema-4 profile extending the baseline (as Phase 9 would)."""
    base = EvidenceProfile.model_validate(
        {k: v for k, v in env.profile[which].items() if k in EvidenceProfile.model_fields}
    )
    data = base.model_dump(mode="python")
    data.update(
        profile_schema="4", builder={"version": "test_extension", "software_id": "sw"},
        extends=base.profile_id, tier=tier,
        sources=(*data["sources"],
                 EvidenceSource(stage="full_research", records={"result_id": f"fr_{full_status}"}).model_dump(),
                 EvidenceSource(stage="validation", records={"result_id": f"val_{val_status}"}).model_dump()),
        full_research={"status": full_status, "walk_forward": {"adequate_folds": 4, "positive_adequate_folds": 1},
                       "sensitivity": {"label": "mixed"}},
        validation={"status": val_status, "sample": {"independent_events": 35}, "window": []},
    )  # fmt: skip
    p = EvidenceProfile.model_validate(data)
    record_profiles(env.ledger, [p], LENIENT)
    return p.profile_id


def test_latest_extended_evidence_is_used(cop):
    _watch(cop)
    ext = _extend(cop, "long", tier="INCONCLUSIVE", full_status="FULL_RESEARCH_MIXED",
                  val_status="VALIDATION_ADVERSE")  # fmt: skip
    k, hits = _first_signal(cop)
    sender = Sender()
    out = engine.run(cop.ledger, software=SW, sender_factory=sender.factory, now=day(k, 3))
    assert out["alerts"] == 0 and out["suppressed"] == len(hits) and sender.sent == []
    d = engine.show_decision(cop.store, engine.list_decisions(cop.store)[0]["decision_id"])
    assert d["profile_id"] == ext
    ev = d["record"]["evidence"]
    assert ev["tier"] == "INCONCLUSIVE" and ev["historical_tier"] == "EXPLORATORY"
    assert ev["validation"]["status"] == "VALIDATION_ADVERSE"
    assert set(d["record"]["result"]["blocked_by"]) == {"tier_eligible", "validation_not_adverse"}
    # the historical baseline profile is untouched
    row = cop.store.con.execute("SELECT tier FROM lab_evidence_profiles WHERE profile_id=?",
                                [cop.profile["long"]["profile_id"]]).fetchone()  # fmt: skip
    assert row[0] == "EXPLORATORY"


def test_paused_watch_and_decision_window_check(cop):
    w = _watch(cop)
    engine.set_watch_status(cop.store, w["watch_id"], "paused", reason="away", now=day(0, 3))
    cop.market.publish(1)
    out = engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(1, 3))
    assert out["decisions"] == 0 and out["notes"][0]["state"] == "WATCH_PAUSED"
    with pytest.raises(CopilotError, match="already paused"):
        engine.set_watch_status(cop.store, w["watch_id"], "paused", reason="x", now=day(1, 4))
    # the database refuses a decision outside the bar's live window
    with pytest.raises(Exception, match="CHECK"):
        cop.store.con.execute(
            "INSERT INTO copilot_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["d", w["watch_id"], TEST_POLICY.policy_id, cop.ids["long"], "BTC", day(1), day(2, 1),
             out["run_id"], "SUPPRESS", None, None, SW.software_id, "{}"],
        )  # fmt: skip


# --------------------------------------------------------------------------- separation


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from _keys(v)


def test_no_auto_trader_leakage(cop):
    _watch(cop)
    k, _ = _first_signal(cop)
    engine.run(cop.ledger, software=SW, sender_factory=Sender().factory, now=day(k, 3))
    rec = engine.show_decision(cop.store, engine.list_decisions(cop.store)[0]["decision_id"])
    keys = {k.lower() for k in _keys(rec)} | {k.lower() for k in _keys(TEST_POLICY.model_dump())}
    keys |= {k.lower() for k in _keys(engine.list_watches(cop.store))}
    cols = {r[0].lower() for r in cop.store.con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name LIKE 'copilot_%'").fetchall()}  # fmt: skip
    leaked = [k for k in keys | cols if any(f in k for f in EXECUTION)]
    assert leaked == [], leaked
    # the compiled exit-intent stop level is never copied into a co-pilot record
    assert "stop" not in rec["record"]["signal"]


def test_research_never_imports_the_consumer():
    lab = Path(__file__).resolve().parents[1] / "src" / "market_signal" / "research"
    offenders = [p.name for p in lab.rglob("*.py")
                 if "market_signal.copilot" in p.read_text(encoding="utf-8")]  # fmt: skip
    assert offenders == []
    src = Path(__file__).resolve().parents[1] / "src" / "market_signal" / "copilot"
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    for banned in ("place_order", "submit_order", "perps.paper", "portfolio.book", "PerpRules"):
        assert banned not in text


def test_cli_copilot_lifecycle(cop):
    from market_signal.cli.main import app

    profile_id = cop.profile["long"]["profile_id"]
    path = cop.store.path
    cop.store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab", "copilot"]

    def run(*args, code=0):
        res = runner.invoke(app, [*base, *args])
        assert res.exit_code == code, res.output
        out = res.output.strip()
        return json.loads(out) if code == 0 and out.startswith(("{", "[")) else res.output

    assert run("policy")["policy_id"] == pol.get_policy().policy_id
    assert run("watch", profile_id, "--reason", "cli", "--dry-run")["dry_run"] is True
    assert run("watchlist") == []
    w = run("watch", profile_id, "--reason", "cli", "--policy-version", "99")
    assert [x["watch_id"] for x in run("watchlist")] == [w["watch_id"]]
    cands = run("candidates")  # real clock: the synthetic bars are long out of their window
    assert cands and all(c["state"] == "BEFORE_REGISTRATION" for c in cands)
    dry = run("run", "--dry-run")
    assert dry["decisions"] == 0 and dry["dry_run"] is True
    assert run("decisions") == []
    run("show", "copdecision_missing", code=1)
    run("pause", w["watch_id"], "--reason", "x")
    run("resume", w["watch_id"], "--reason", "y")
    run("stop", w["watch_id"], "--reason", "z")
    run("resume", w["watch_id"], "--reason", "again", code=1)
