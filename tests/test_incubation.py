"""Phase 21: fast prospective candidate incubation (exploratory paper; never live)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.incubation import evidence as ie
from market_signal.research.incubation import machine as mc
from market_signal.research.incubation import policy as pm
from market_signal.research.incubation import prospective as pr
from market_signal.research.incubation import synthetic as sy
from market_signal.research.incubation.events import ExternalEvent
from market_signal.research.incubation.opportunity import opportunity_rate
from market_signal.research.incubation.pool import PoolRule, balance, members, pool_id
from market_signal.research.incubation.replay import VenueReplay
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle.policy import policy as lifecycle_policy
from market_signal.research.lifecycle.study import StrategyRef
from tests.conftest import make_daily_crypto
from tests.test_edge_lifecycle import events
from tests.test_lab_compiler import _definition, _funding, _insert_perp
from tests.test_lab_evidence import FORBIDDEN, _keys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
CATALOGUE = ROOT / "config" / "lab" / "families"
SW = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)
B, A, C = pm.BALANCED, pm.AGGRESSIVE, pm.CONSERVATIVE
FLOOR = B.floor.floor(10)
T0 = es.to_days("2022-01-03")

PINNED = {
    "CONSERVATIVE": "incpolicy_4356148786bda18d98916ca14b53caf48bd1a0d31d3e63d51cc4ea5d8048d76b",
    "BALANCED": "incpolicy_aaec7841f4b18d5222c6c50a0f34ced1c86e7c1decb77aa91da0d5608228a042",
    "AGGRESSIVE": "incpolicy_c2f2fe3583daa7203ff0e80a3d73966c192cdf5ccfdb0dda5691bd93c72f35f6",
}


def daily(first: float, last: float) -> np.ndarray:
    return first + np.arange(int(last - first) + 1, dtype=float)


def history(n: int, mean: float, sd: float, seed: int, end: float) -> tuple[list, list, list]:
    """``n`` outcomes resolving every 3 days up to ``end`` across 3 assets."""
    rng = np.random.default_rng(seed)
    t = end - 3 * np.arange(n)[::-1]
    return list(t), list(mean + sd * rng.standard_normal(n)), list(np.arange(n) % 3)


# --------------------------------------------------------------------------- policies


def test_policies_are_distinct_versioned_and_frozen():
    assert {n: p.policy_id for n, p in pm.POLICIES.items()} == PINNED
    assert len(set(PINNED.values())) == 3
    # CONSERVATIVE is the unchanged, unweakened Phase 20 lifecycle (cited by ID)
    assert C.lifecycle_policy_id == lifecycle_policy(1).policy_id
    assert C.lifecycle_policy_id.startswith("lcpolicy_2fae77e8")
    with pytest.raises(ValidationError):
        pm.ConservativeBenchmark(lifecycle_policy_id="lcpolicy_other")
    # nothing grants live use, and the field cannot be flipped
    for p in pm.POLICIES.values():
        assert p.grants_live is False
    with pytest.raises(ValidationError):
        pm.IncubationPolicy.model_validate({**B.model_dump(), "grants_live": True})
    # hysteresis is structural
    with pytest.raises(ValidationError, match="hysteresis"):
        pm.IncubationPolicy(profile="BALANCED", window=pm.Window(days=21, min_events=4),
                            admission=pm.Admission(min_t=1.0, loo_min_floors=0.0),
                            deactivation=pm.Deactivation(exit_max_t=1.0, lapse_min_events=2))  # fmt: skip
    # a changed threshold is a different policy ID
    other = B.model_copy(update={"admission": pm.Admission(min_t=2.5, loo_min_floors=0.0)})
    assert other.policy_id != B.policy_id
    assert pm.get("balanced") is B
    with pytest.raises(ValueError, match="unknown"):
        pm.get("live")


def test_frozen_thresholds_are_the_design_rule_selections():
    grid = sy.design_policies()
    for frozen, label in ((B, "w21_t2"), (A, "w21_t1")):
        g = grid[label]
        assert (g.window, g.admission, g.deactivation) == (
            frozen.window, frozen.admission, frozen.deactivation)  # fmt: skip
    assert "BALANCED" in sy.DESIGN_RULES and "AGGRESSIVE" in sy.DESIGN_RULES


# --------------------------------------------------------------------------- admission logic


def test_tiny_samples_never_admit_however_large_the_mean():
    t_res = [T0 + 1, T0 + 2, T0 + 3]
    ev = events(t_res, [0.5, 0.4, 0.6], assets=[0, 1, 2])
    lo, k = ie.window_bounds(ev, T0 + 4, A.window.days)
    ok, checks = ie.admission(ie.slice_est(ev, lo, k), ie.slice_est(ev, 0, k), A, FLOOR)
    assert not ok and not checks["sample_adequate"]


def test_one_asset_cannot_carry_an_admission():
    t = [T0 + i for i in range(1, 9)]
    ev = events(t, [0.30, 0.01, 0.30, -0.01, 0.30, 0.0, 0.30, -0.02], assets=[0, 1] * 4,
                t_sig=[x - 10 - 7 * i for i, x in enumerate(t)])  # fmt: skip
    w = ie.slice_est(ev, 0, 8)
    assert w.loo_mean is not None and w.loo_mean < 0 < w.mean
    _, checks = ie.admission(w, w, A, FLOOR)
    assert not checks["not_one_asset"]


def _recent_block(m: float, d: float, end: float, n: int = 8) -> tuple[list, list, list, list]:
    """``n`` outcomes in the last 21 days alternating m +- d, each in its own signal week."""
    t_res = [end - 2.5 * i for i in range(n)][::-1]
    t_sig = [end - 10 - 7 * (n - i) for i in range(n)]
    net = [m + (d if i % 2 else -d) for i in range(n)]
    return t_res, net, [i % 3 for i in range(n)], t_sig


def test_lifetime_weak_recent_strong_is_admitted_and_hostile_lifetime_raises_the_bar():
    end = T0 + 900
    h_t, h_x, h_a = history(150, -0.004, 0.08, 1, end - 30)  # weak lifetime, not hostile
    r_t, r_x, r_a, r_s = _recent_block(0.06, 0.02, end)
    ev = events(h_t + r_t, h_x + r_x, assets=h_a + r_a,
                t_sig=[t - 10 for t in h_t] + r_s)  # fmt: skip
    k = ev.resolved(end)
    lo, _ = ie.window_bounds(ev, end, B.window.days)
    w, life = ie.slice_est(ev, lo, k), ie.slice_est(ev, 0, k)
    assert life.mean < 0 and not ie.lifetime_hostile(life, B) and w.t >= 2.5
    assert ie.admission(w, life, B, FLOOR)[0] and ie.admission(w, life, A, FLOOR)[0]
    # a credibly negative lifetime raises the bar (never a ban)
    h_t, h_x, h_a = history(150, -0.03, 0.05, 2, end - 30)
    r_t, r_x, r_a, r_s = _recent_block(0.03, 0.033, end)
    ev = events(h_t + r_t, h_x + r_x, assets=h_a + r_a, t_sig=[t - 10 for t in h_t] + r_s)
    k = ev.resolved(end)
    lo, _ = ie.window_bounds(ev, end, B.window.days)
    w, life = ie.slice_est(ev, lo, k), ie.slice_est(ev, 0, k)
    assert ie.lifetime_hostile(life, B) and 2.0 <= w.t < 2.5, w
    assert ie.required_t(life, B) == 2.5 and not ie.admission(w, life, B, FLOOR)[0]
    assert ie.required_t(life, A) == 1.5 and ie.admission(w, life, A, FLOOR)[0]


def test_lifetime_strong_recent_dead_is_not_admitted():
    end = T0 + 900
    h_t, h_x, h_a = history(200, 0.05, 0.05, 3, end - 30)  # strong lifetime (t >> 2)
    r_t, r_x, r_a, r_s = _recent_block(-0.01, 0.02, end)
    ev = events(h_t + r_t, h_x + r_x, assets=h_a + r_a, t_sig=[t - 10 for t in h_t] + r_s)
    k = ev.resolved(end)
    life = ie.slice_est(ev, 0, k)
    assert life.t > 5
    for p in (B, A):
        lo, _ = ie.window_bounds(ev, end, p.window.days)
        assert not ie.admission(ie.slice_est(ev, lo, k), life, p, FLOOR)[0]
        rep = mc.replay(ev, daily(end - 5, end), p, FLOOR)
        assert rep.levels[-1] not in pm.ADMITTED


# --------------------------------------------------------------------------- machine


def _emerging(seed=7, edge=0.06):
    spec = sy.TempSpec(edge=edge)
    ev, mu, reg = sy.generate("edge90", seed, spec)
    times = sy._times(spec, "edge90")
    return ev, mu, reg, times, spec


def test_admission_is_immediate_at_the_first_daily_evaluation_meeting_the_rule():
    ev, _, _, times, _ = _emerging()
    rep = mc.replay(ev, times, B, FLOOR)
    first = rep.episodes[0]["start"]
    expect = None
    for t in times:  # brute force: the first time the rule holds (from a non-dormant state)
        lo, k = ie.window_bounds(ev, t, B.window.days)
        if ie.admission(ie.slice_est(ev, lo, k), ie.slice_est(ev, 0, k), B, FLOOR)[0]:
            expect = t
            break
    assert first == expect
    # the conservative benchmark needs two weekly confirmations: never before a week later
    cons = mc.replay(ev, times, C, FLOOR)
    assert not cons.episodes or cons.episodes[0]["start"] >= first + 7


def test_hysteresis_band_is_used_and_levels_do_not_chatter():
    seen_band = False
    for seed in range(12):
        ev, *_, times, _ = _emerging(seed=seed, edge=0.03)
        rep = mc.replay(ev, times, B, FLOOR)
        prev = None
        for t, lv in zip(times, rep.levels, strict=True):
            lo, k = ie.window_bounds(ev, t, B.window.days)
            w = ie.slice_est(ev, lo, k)
            if lv in pm.ADMITTED and prev in pm.ADMITTED and w.t is not None:
                assert w.t > B.deactivation.exit_max_t or w.n < B.window.min_events
                seen_band |= B.deactivation.exit_max_t < w.t < B.admission.min_t
            prev = lv
        # consecutive transitions never happen on unchanged evidence (skip-unchanged cadence)
        keys = [ie.window_bounds(ev, tr["at_days"], B.window.days) for tr in rep.transitions]
        assert all(a != b for a, b in pairwise(keys)), seed
        assert len(rep.transitions) < 0.1 * len(times)
    assert seen_band  # continuation is weaker than admission: the band is exercised


def test_dormant_candidates_reactivate_as_new_episodes_with_deterministic_ids():
    spec = sy.TempSpec(edge=0.06)
    ev, _, _ = sy.generate("intermittent", 3, spec)
    times = sy._times(spec, "intermittent")
    a = mc.replay(ev, times, A, FLOOR, strategy_key="s1")
    b = mc.replay(ev, times, A, FLOOR, strategy_key="s1")
    assert [e["episode_id"] for e in a.episodes] == [e["episode_id"] for e in b.episodes]
    assert len(a.episodes) >= 2 and any(e["reactivation"] for e in a.episodes)
    e0 = a.episodes[0]
    assert e0["episode_id"] == mc.episode_id(A.policy_id, "s1", e0["start"])
    other = mc.replay(ev, times, B, FLOOR, strategy_key="s1")
    assert not {e["episode_id"] for e in other.episodes} & {e["episode_id"] for e in a.episodes}
    # every deactivation names its reason; each episode has an end before the next start
    for x, y in zip(a.episodes, a.episodes[1:], strict=False):
        assert x["end"] is not None and x["end"] <= y["start"] and x["deactivation_reasons"]


def test_future_outcomes_never_change_an_earlier_decision():
    ev, _, _, times, _ = _emerging(seed=11)
    cut = times[len(times) // 2]
    base = mc.replay(ev, times, A, FLOOR)
    m = ev.t_res > cut
    ev2 = es.EventSet(**{**{f: getattr(ev, f) for f in ("assets", "t_sig", "t_res", "excess",
                         "gross", "mae", "mfe", "cost", "funding", "asset", "stress", "regime",
                         "all_res", "all_valid_cum", "history_start", "block_days")},
                         "net": np.where(m, -ev.net * 5, ev.net)})  # fmt: skip
    changed = mc.replay(ev2, times, A, FLOOR)
    j = int(np.searchsorted(times, cut, side="right"))
    assert base.levels[:j] == changed.levels[:j]


def test_conservative_benchmark_is_the_phase20_track():
    from market_signal.research.lifecycle.machine import Track

    ev, _, _, times, _ = _emerging(seed=5)
    rep = mc.replay(ev, times, C, FLOOR)
    lp = C.lifecycle()
    tr = Track(ev, lp, FLOOR)
    states = [tr.step(float(t)) for t in rep.times]
    assert np.allclose(np.diff(rep.times), lp.evaluation.cadence_days)
    mapped = [C.level(s) for s in states]
    assert [lv if lv != "CONFIRMED_PAPER" else "EXPLORATORY_PAPER" for lv in rep.levels] == mapped


# --------------------------------------------------------------------------- synthetic


def test_a_30_day_planted_edge_is_captured_by_an_exploratory_policy():
    spec = sy.TempSpec(edge=0.06)
    rows = [sy.run_one("edge30", 100 + s, spec, sy._policies()) for s in range(16)]
    cap = {p: np.mean([r[p]["captured_planted_share"] for r in rows]) for p in pm.PROFILES}
    det = {p: np.mean([r[p]["detected_during_edge"] for r in rows]) for p in pm.PROFILES}
    delays = [r["AGGRESSIVE"]["useful_detection_delay_days"] for r in rows
              if r["AGGRESSIVE"]["useful_detection_delay_days"] is not None]  # fmt: skip
    assert det["AGGRESSIVE"] >= 0.7 and cap["AGGRESSIVE"] >= 0.3
    assert cap["AGGRESSIVE"] > 2 * cap["CONSERVATIVE"]
    assert delays and np.median(delays) <= 30  # weeks, not six months


def test_null_false_activation_is_measured_correctly():
    spec = sy.TempSpec()
    ev, mu, reg = sy.generate("null", 21, spec)
    times = sy._times(spec, "null")
    cur = reg[np.clip((times - spec.t0).astype(int), 0, len(reg) - 1)]
    rep = mc.replay(ev, times, A, FLOOR, regime_at=cur)
    got = sy.truth_metrics("null", ev, mu, rep, spec)
    span = (ev.t_sig >= spec.t0 + spec.warmup_days) & (ev.t_sig < spec.end("null"))
    years = (spec.end("null") - spec.t0 - spec.warmup_days) / 365.25
    assert got["participation_share"] == pytest.approx(rep.participate[span].mean())
    assert got["null_participation_share"] == pytest.approx(rep.participate[span].mean())
    assert got["false_admissions_per_year"] == pytest.approx(len(rep.episodes) / years)
    assert got["trades_per_year"] == pytest.approx((rep.participate & span).sum() / years)
    # and the rates are ordered: exploratory admission is intentionally permissive
    rows = [sy.run_one("null", 300 + s, spec, sy._policies()) for s in range(10)]
    m = {p: np.mean([r[p]["null_participation_share"] for r in rows]) for p in pm.PROFILES}
    assert m["CONSERVATIVE"] < m["AGGRESSIVE"] and m["BALANCED"] < m["AGGRESSIVE"]
    assert 0.1 < m["AGGRESSIVE"] < 0.6


def test_synthetic_calibration_is_deterministic_for_any_worker_count():
    kw = dict(seeds=1, null_seeds=1, edges=(0.06,), surface=False, grid=False)
    a, b = sy.calibrate(**kw), sy.calibrate(workers=2, **kw)
    a.pop("seconds"), b.pop("seconds")
    assert a == b and a["policies"] == PINNED


# --------------------------------------------------------------------------- opportunity rate


def test_opportunity_rate_by_hand():
    sig = pd.DataFrame({
        "t_sig": [0.0, 0.0, 0.0, 2.0, 5.0, 5.0, 9.0],
        "asset": ["BTC", "BTC", "ETH", "BTC", "SOL", "SOL", "ETH"],
        "side": ["long", "long", "short", "long", "short", "short", "long"],
        "admitted": [True, True, True, False, True, True, False],
    })  # fmt: skip
    r = opportunity_rate(sig, 0.0, 10.0)
    assert r["candidate_signals"] == 7 and r["candidate_signals_per_day"] == pytest.approx(0.7)
    assert r["admissible_signals"] == 5 and r["admissible_signals_per_day"] == pytest.approx(0.5)
    # (BTC,long,0) (ETH,short,0) (SOL,short,5): correlated variants count once
    assert r["independent_opportunities"] == 3
    assert r["independent_opportunities_per_day"] == pytest.approx(0.3)
    assert r["median_days_between_opportunities"] == 5.0
    assert r["zero_opportunity_day_share"] == pytest.approx(0.8)
    assert r["opportunities_by_side"] == {"long": 1, "short": 2}


# --------------------------------------------------------------------------- pool / balance


def test_pool_is_frozen_symmetric_and_selection_free():
    rule = PoolRule()
    refs = members(rule, CATALOGUE)
    bal = balance(refs)
    assert bal["strategies"] == 128 and bal["long"] == bal["short"] == 64
    assert bal["asymmetric_families"] == []
    assert set(bal["by_style"]) == {"trend", "breakout", "pullback", "mean_reversion", "funding"}
    assert rule.selection_by_performance is False
    assert pool_id(rule, refs) == pool_id(rule, members(rule, CATALOGUE))
    assert {r.family for r in refs}.isdisjoint({"structure", "relative_strength", "oi_price"})


def _perp_frames(store, drift: float, coins=("BTC", "ETH", "SOL"), n=420):
    for i, c in enumerate(coins):
        df = make_daily_crypto(n=n, start="2023-01-01", seed=40 + i, drift=drift)
        df["close_time"] = df["ts"] + pd.Timedelta(days=1)
        _insert_perp(store, df, _funding(df, seed=60 + i), coin=c)


def test_short_strategies_activate_symmetrically(store):
    from market_signal.research.incubation import inputs as ip
    from market_signal.research.lab.datasets import SeriesSelection
    from market_signal.research.lab.forward import _snapshot

    def ledger(drift, side, rule):
        _perp_frames(store, drift)
        snaps = {c: _snapshot(store, tuple(
            SeriesSelection(kind=k, symbol=c, source="hyperliquid",
                            timeframe="1d" if k == "perp_bars" else None,
                            start=datetime(2023, 1, 1, tzinfo=UTC),
                            end=datetime(2024, 3, 1, tzinfo=UTC))
            for k in ("perp_bars", "perp_funding"))) for c in ("BTC", "ETH", "SOL")}  # fmt: skip
        lp = C.lifecycle()
        vi = ip.venue_inputs(snaps, "BTC", lp)
        d = _definition(rule, side=side, cooldown=0)
        _, ev = ip.strategy_ledger(d, vi, {c: (4.5, 2.0) for c in snaps}, 10, lp)
        store.con.execute("DELETE FROM perp_bars")
        store.con.execute("DELETE FROM perp_funding")
        return ev

    short = ledger(-0.006, "short", [("close", "lt", "sma_20")])
    long = ledger(+0.006, "long", [("close", "gt", "sma_20")])
    for ev in (short, long):
        assert ev.net.mean() > 0  # side-signed: a falling market pays the short
        times = daily(ev.t_res[0], ev.t_res[-1])
        for p in (B, A):
            rep = mc.replay(ev, times, p, FLOOR)
            assert rep.episodes, p.profile
    # no side-specific branch exists in the admission or machine code
    for f in ("evidence.py", "machine.py", "policy.py"):
        text = (SRC / "research" / "incubation" / f).read_text()
        assert '== "short"' not in text and "== 'short'" not in text and '== "long"' not in text


# --------------------------------------------------------------------------- external events


def test_external_event_availability_is_prisms_first_observation():
    prov = {"source": "manual", "source_ref": "x", "retrieved_by": "test", "raw_sha256": "a" * 64}
    pub = datetime(2026, 10, 1, 9, tzinfo=UTC)
    seen = datetime(2026, 10, 1, 15, tzinfo=UTC)
    e = ExternalEvent(category="protocol_hack", assets=("AAVE",), headline="h",
                      published_at=pub, first_seen_at=seen, confidence=0.7, provenance=prov)  # fmt: skip
    assert e.available_at == seen
    assert not e.usable_at(pub + timedelta(hours=1))  # published, but Prism had not seen it
    assert e.usable_at(seen)
    with pytest.raises(ValidationError):  # availability cannot be supplied / backdated
        ExternalEvent(category="listing", assets=("BTC",), headline="h", first_seen_at=seen,
                      available_at=pub, confidence=0.5, provenance=prov)  # fmt: skip
    with pytest.raises(ValidationError):
        ExternalEvent(category="listing", assets=("BTC",), headline="h", first_seen_at=seen,
                      expires_at=seen, confidence=0.5, provenance=prov)  # fmt: skip
    with pytest.raises(ValidationError):
        ExternalEvent(
            category="listing", assets=("BTC",), headline="h", confidence=0.5, provenance=prov
        )  # fmt: skip  (first_seen_at is required)
    assert e.event_id == ExternalEvent.model_validate(e.model_dump()).event_id


# --------------------------------------------------------------------------- prospective (DB)

COINS = ("BTC", "ETH", "SOL")
HIST_DAYS = 400
START = datetime(2023, 1, 1, tzinfo=UTC)
HIST_END = START + timedelta(days=HIST_DAYS)  # last historical bar close


def day(k: int, hours: float = 0) -> datetime:
    return HIST_END + timedelta(days=k, hours=hours)


class Market:
    """Full synthetic series per coin; the DB holds only what has been 'published'."""

    def __init__(self, store, n=HIST_DAYS + 40, drift=0.004):
        self.store = store
        self.bars, self.funding = {}, {}
        for i, c in enumerate(COINS):
            df = make_daily_crypto(n=n, start="2023-01-01", seed=70 + i, drift=drift)
            df["close_time"] = df["ts"] + pd.Timedelta(days=1)
            self.bars[c], self.funding[c] = df, _funding(df, seed=80 + i)
        self.published = 0
        for c in COINS:
            b, f = self.bars[c], self.funding[c]
            _insert_perp(store, b[b["close_time"] <= HIST_END], f[f["time"] <= HIST_END], coin=c)

    def publish(self, k: int) -> None:
        for c in COINS:
            b, f = self.bars[c], self.funding[c]
            lo, hi = day(self.published), day(k)
            _insert_perp(self.store, b[(b["close_time"] > lo) & (b["close_time"] <= hi)],
                         f[(f["time"] > lo) & (f["time"] <= hi)], coin=c)  # fmt: skip
        self.published = max(self.published, k)


def _freeze_def() -> pr.IncubationFreeze:
    rule = PoolRule()
    rules = {"up_long": ([("close", "gt", "sma_5")], "long"),
             "down_short": ([("close", "lt", "sma_5")], "short")}  # fmt: skip
    refs = []
    for name, (cond, side) in rules.items():
        d = _definition(cond, side=side, cooldown=0)
        refs.append(StrategyRef(name=name, strategy_id=d.strategy_id, family="fixture", version=1,
                                side=side, params={}, definition=d.model_dump(mode="json")))  # fmt: skip
    refs = tuple(refs)
    venue = VenueReplay(venue="hyperliquid", coins=COINS, bars_start="2023-01-01T00:00:00Z",
                        funding_start="2023-01-01T00:00:00Z",
                        first_evaluation="2023-09-01T00:00:00Z")  # fmt: skip
    from market_signal.research.structure.study.spec import CostRef

    return pr.IncubationFreeze(
        pool_rule=rule, pool_id=pool_id(rule, refs), members=refs,
        policies=tuple(pm.POLICIES[p].model_dump(mode="json") for p in pm.PROFILES),
        policy_ids={p: pm.POLICIES[p].policy_id for p in pm.PROFILES}, venue=venue,
        costs=tuple(CostRef(venue="hyperliquid", coin=c, fee_bps=4.5, slippage_bps=2.0)
                    for c in COINS), semantics=pr.semantics())  # fmt: skip


@pytest.fixture
def env(store):
    market = Market(store)
    ledger = Ledger(store)
    defn = _freeze_def()
    pr.freeze(ledger, defn, reason="test", origin="test", software=SW, now=day(0, 2))
    return market, ledger, defn


def _table(store, table: str) -> list:
    return store.con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def test_freeze_is_content_addressed_and_cannot_move(env):
    _, ledger, defn = env
    assert defn.freeze_id == _freeze_def().freeze_id
    with pytest.raises(pr.IncubationError, match="already frozen"):
        pr.freeze(ledger, defn, reason="again", origin="test", software=SW, now=day(3))
    with pytest.raises(ValidationError):  # an embedded policy cannot be swapped silently
        pr.IncubationFreeze.model_validate({**defn.model_dump(), "policy_ids": {
            **defn.policy_ids, "BALANCED": "incpolicy_other"}})  # fmt: skip
    with pytest.raises(ValidationError):  # nor can live use be granted
        pr.IncubationFreeze.model_validate({**defn.model_dump(), "grants_live": True})
    real = pr.build_freeze({"costs": {"taker_fee_bps": 4.5, "slippage_bps": {"default": 8}}},
                           CATALOGUE)  # fmt: skip
    assert len(real.members) == 128 and real.policy_ids == PINNED


def test_prospective_cycle_is_live_window_only_write_once_and_immutable(env):
    market, ledger, _ = env
    store = ledger.store
    paper_before = {t: _table(store, t) for t in ("paper_runs", "paper_events", "paper_cycles")}
    # history bars (closed before the freeze) are never evaluated
    out = pr.run(ledger, software=SW, now=day(0, 3))
    assert out["recorded"] == {} and "no completed bar after the freeze" in out["notes"][0]["note"]
    for k in range(1, 7):
        market.publish(k)
        out = pr.run(ledger, software=SW, now=day(k, 1))
        assert out["recorded"]["incubation_evaluations"] == 2
        assert out["recorded"]["incubation_decisions"] == 6
    snap = {t: _table(store, t) for t in ("incubation_evaluations", "incubation_decisions",
                                          "incubation_transitions", "incubation_intents")}  # fmt: skip
    # idempotent: the same bar again records nothing
    again = pr.run(ledger, software=SW, now=day(6, 5))
    assert "already recorded" in again["notes"][0]["note"] and not again["recorded"]
    # a missed bar is a gap, never backfilled
    market.publish(8)
    pr.run(ledger, software=SW, now=day(8, 1))
    bars = {
        r[0] for r in store.con.execute("SELECT bar_close FROM incubation_evaluations").fetchall()
    }
    assert pd.Timestamp(day(7)) not in {pd.Timestamp(b) for b in bars}
    # outside the live window nothing is evaluated
    market.publish(9)
    late = pr.run(ledger, software=SW, now=day(10, 2))
    assert "outside its live window" in late["notes"][0]["note"]
    # future data never rewrite earlier rows
    for k in range(11, 20):
        market.publish(k)
        pr.run(ledger, software=SW, now=day(k, 1))
    for t, rows in snap.items():
        now_rows = set(_table(store, t))
        assert set(rows) <= now_rows, t
    # every decision cites its policy; admitted levels carry an episode
    for lvl, ep, payload in store.con.execute(
            "SELECT level, episode_id, payload FROM incubation_decisions").fetchall():  # fmt: skip
        p = json.loads(payload)
        assert p["policy_id"] in PINNED.values() and (lvl in pm.ADMITTED) == (ep is not None)
        assert not set(_keys(p["explanation"])) & {k for k in FORBIDDEN if k != "execut"}
    # the Phase 12 paper account is never touched
    assert {t: _table(store, t) for t in paper_before} == paper_before


def test_shadow_intents_and_outcomes_apply_frozen_costs(env):
    market, ledger, _ = env
    store = ledger.store
    for k in range(1, 26):
        market.publish(k)
        pr.run(ledger, software=SW, now=day(k, 1))
    intents = pr._rows(store, "SELECT * FROM incubation_intents ORDER BY signal_bar_close")
    assert intents, "the planted drift should admit the long candidate somewhere"
    assert {i["status"] for i in intents} == {"entered"}
    outs = pr._rows(store, "SELECT * FROM incubation_outcomes WHERE status='resolved'")
    assert outs
    by_id = {i["intent_id"]: i for i in intents}
    for o in outs:
        i = by_id[o["intent_id"]]
        p = json.loads(o["payload"])
        bars = market.bars[i["asset"]].set_index("close_time")
        t = pd.Timestamp(i["signal_bar_close"])
        entry = float(market.bars[i["asset"]].set_index("ts").loc[t, "open"])
        exit_ = float(bars.loc[t + pd.Timedelta(days=10), "close"])
        sign = 1 if i["side"] == "long" else -1
        assert p["entry_price"] == pytest.approx(entry) and p["exit_price"] == pytest.approx(exit_)
        assert o["gross"] == pytest.approx(sign * (exit_ / entry - 1))
        assert p["round_trip_cost"] == pytest.approx(2 * (4.5 + 2.0) / 1e4)
        assert o["net"] == pytest.approx(o["gross"] - p["round_trip_cost"] - p["funding_paid"])
        assert o["pnl_usd"] == pytest.approx(o["net"] * 1_000.0)
    # one open intent per (policy, strategy, asset): no overlapping intents within h bars
    seen: dict = {}
    for i in intents:
        key = (i["profile"], i["strategy_id"], i["asset"])
        t = pd.Timestamp(i["signal_bar_close"])
        assert key not in seen or t - seen[key] >= pd.Timedelta(days=10)
        seen[key] = t


def test_queries_and_cli_for_agents(env, settings, monkeypatch):
    from market_signal.cli.main import app

    market, ledger, defn = env
    for k in range(1, 14):
        market.publish(k)
        pr.run(ledger, software=SW, now=day(k, 1))
    rows = pr.candidates(ledger.store)
    assert {r["policy"] for r in rows} == set(pm.PROFILES)
    one = pr.candidate(ledger.store, "up_long")
    assert set(one["decisions"]) == set(pm.PROFILES)
    ev = one["evidence"]
    assert {"lifetime", "last_730d", "last_365d", "last_180d", "last_90d"} <= set(ev["eras"])
    assert {"retrospective", "prospective_candidate_outcomes"} <= set(ev)
    st = pr.status(ledger.store)
    assert st["freezes"][0]["balance"]["long"] == st["freezes"][0]["balance"]["short"] == 1
    cmp_ = pr.compare(ledger.store)[defn.freeze_id]["policies"]
    assert set(cmp_) == set(pm.PROFILES) and "synthetic_only" in cmp_["BALANCED"]
    opp = pr.opportunities(ledger.store)[defn.freeze_id]
    assert opp["evaluated_bars"] == 13
    ledger.store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(settings.paths.db.parent / "test.duckdb"))
    for args in (["candidates", "--json", "--all"], ["candidate", "up_long"],
                 ["status", "--json"], ["opportunities", "--json"], ["compare", "--json"],
                 ["episodes", "--json"], ["policies"], ["event-schema"]):  # fmt: skip
        res = CliRunner().invoke(app, ["lab", "incubation", *args])
        assert res.exit_code == 0, (args, res.output)
        json.loads(res.output)
    res = CliRunner().invoke(app, ["lab", "incubation", "candidates", "--window", "30"])
    assert res.exit_code != 0  # agents cannot choose windows or thresholds
    res = CliRunner().invoke(app, ["lab", "incubation", "candidates", "--policy", "live"])
    assert res.exit_code != 0


# --------------------------------------------------------------------------- isolation


def test_no_live_capability_and_consumer_isolation():
    inc = SRC / "research" / "incubation"
    for f in inc.rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "telegram", "httpx", "requests", "UPDATE ", "DELETE ", "place_order",
                       "PaperExecutionAdapter", "record_forward_evidence", "fw.enroll"):  # fmt: skip
            assert banned not in text, (f, banned)
    for pkg in ("paper", "copilot", "perps", "scoring", "portfolio"):
        for f in (SRC / pkg).rglob("*.py"):
            assert "incubation" not in f.read_text(), f
    for name in ("forward.py", "evidence.py", "validation.py", "corroboration.py"):
        assert "incubation" not in (SRC / "research" / "lab" / name).read_text(), name
    for f in (SRC / "research" / "lifecycle").rglob("*.py"):
        assert "incubation" not in f.read_text(), f
    # the paper/co-pilot policies are untouched by Phase 21
    text = (SRC / "copilot" / "policy.py").read_text() + (SRC / "paper" / "policy.py").read_text()
    assert "incubation" not in text and "EXPLORATORY_PAPER" not in text
    # the runtime runs the incubation cycle (shadow records only) and nothing else of Phase 21
    from market_signal.ops import runtime as rt

    steps = [args for job in rt.JOBS.values() for _, args in job if "incubation" in args]
    assert steps == [["lab", "incubation", "run"]]
    assert pm.LEVELS[-1] == "RETIRED" and "LIVE" not in " ".join(pm.LEVELS)
