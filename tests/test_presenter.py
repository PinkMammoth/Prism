"""Presentation layer: decisions never promote research; entry/no-action phrasing."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import market_signal.presenter as P


def _comp(name, pts, maxp, reasons=()):
    return NS(name=name, points=pts, max_points=maxp, reasons=list(reasons))


def _asset(status="STRONG", band="STRONG", verdict="PROMISING", state="ACTIVE", price=100.0,
           zone=(90.0, 102.0), inv=85.0, basis="setup stop", score=80.0, setup="breakout_retest"):  # fmt: skip
    st = NS(setup=setup, title="Breakout + Retest", state=state, detail="retest of 100 held",
            conditions={}, ideal_entry=100.0, research_verdict=verdict)  # fmt: skip
    comps = [_comp("fundamental", None, 25), _comp("structure", 15, 15), _comp("entry", 13, 15),
             _comp("macro", 10, 10), _comp("liquidity", 5, 5)]  # fmt: skip
    return NS(symbol="ETH", name="Ether", asset_class="crypto", status=status, band=band,
              status_text="", setup=st, score=score, coverage=0.7, price=price, regime="RISK_ON",
              zones={"entry_zone": zone, "invalidation": inv, "invalidation_basis": basis},
              components=comps, sizing=None, warnings=[])  # fmt: skip


@pytest.mark.parametrize("status", ["EXCEPTIONAL", "STRONG", "ACTIONABLE"])
def test_engine_act_statuses_map_to_actionable(status):
    v = P.build_view(_asset(status=status), {})
    assert v.decision == "ACTIONABLE"
    assert v.cap_note is None


@pytest.mark.parametrize("status", ["STRONG", "WAIT"])
def test_rejected_setup_never_actionable_or_wait(status):
    v = P.build_view(_asset(status=status, verdict="REJECT"), {})
    assert v.decision == "WATCH"
    assert "rejected" in v.cap_note
    assert v.evidence.label == "REJECTED"
    assert v.sizing_text is None


def test_unproven_evidence_kept_separate_from_score():
    v = P.build_view(_asset(verdict="INSUFFICIENT_DATA", score=90, band="EXCEPTIONAL"), {})
    assert v.strength == "EXCEPTIONAL"
    assert v.evidence.label == "INSUFFICIENT DATA"
    assert v.evidence.sample_limited
    assert any("not demonstrated" in r for r in v.risks)


def test_entry_distance_above_zone():
    e = P.entry_view(_asset(price=106.284, zone=(98.0, 102.0)))
    assert e.position == "ABOVE"
    assert e.distance_text == "4.2% above preferred entry"
    assert "breaks the setup" in e.invalidation_text


def test_entry_open_ended_zone_and_review_basis():
    e = P.entry_view(_asset(zone=(0.0, 150.0), inv=None, basis="investment: price already below …"))
    assert e.preferred == "≤ $150.00"
    assert e.position == "IN_ZONE"
    assert "review the thesis" in e.invalidation_text


def test_closest_candidate_when_nothing_actionable():
    far = P.build_view(_asset(status="WAIT", price=120.0), {})
    near = P.build_view(_asset(status="WAIT", price=104.0), {})
    near.symbol = "NEAR"
    act, wait, rest = P.triage([far, near])
    assert not act and len(wait) == 2 and not rest
    assert P.closest_candidate([far, near]).symbol == "NEAR"


def test_evidence_concentration_note():
    ev = P.Evidence("rerating", verdict="INSUFFICIENT_DATA", class_share={"equity": 1.0})
    assert "No historical crypto events" in ev.concentration_note("crypto")
    ev = P.Evidence("x", class_share={"crypto": 0.8, "equity": 0.2})
    assert "concentrated in crypto" in ev.concentration_note("equity")
