"""Phase 20: time-varying edge evidence, edge states and the strategy lifecycle (research only)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lifecycle import changepoint as cp
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle import machine as mc
from market_signal.research.lifecycle import market as mk
from market_signal.research.lifecycle import profile as pf
from market_signal.research.lifecycle import state as stt
from market_signal.research.lifecycle import synthetic as sy
from market_signal.research.lifecycle.policy import (
    Activation,
    Continuation,
    LifecyclePolicy,
    policy,
)
from tests.test_lab_evidence import FORBIDDEN, _keys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
SW = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)
P = policy()
FLOOR = P.floor.floor(10)
T0 = es.to_days("2022-01-03")


def events(t_res, net, assets=None, t_sig=None, regime=None, stress=None, history_start=None,
           excess=None) -> es.EventSet:  # fmt: skip
    """A hand-built EventSet (sorted by resolution time, as ``from_frame`` guarantees)."""
    t_res = np.asarray(t_res, dtype=float)
    order = np.argsort(t_res, kind="mergesort")
    t_res = t_res[order]
    net = np.asarray(net, dtype=float)[order]
    n = len(net)
    assets = np.asarray(assets if assets is not None else np.arange(n) % 3, dtype=np.int64)[order]
    t_sig = np.asarray(t_sig, dtype=float)[order] if t_sig is not None else t_res - 10
    reg = {d: np.full(n, -1, dtype=np.int64) for d in es.REGIME_DIMS}
    if regime is not None:
        reg["trend"] = np.asarray(regime, dtype=np.int64)[order]
    st = np.zeros(n, dtype=bool) if stress is None else np.asarray(stress, dtype=bool)[order]
    ex = net.copy() if excess is None else np.asarray(excess, dtype=float)[order]
    nan = np.full(n, np.nan)
    return es.EventSet(assets=("A", "B", "C", "D", "E")[: int(assets.max()) + 1 if n else 1],
                       t_sig=t_sig, t_res=t_res, net=net, excess=ex, gross=net, mae=nan,
                       mfe=nan, cost=np.zeros(n), funding=np.zeros(n), asset=assets, stress=st,
                       regime=reg, all_res=t_res, all_valid_cum=np.arange(n + 1),
                       history_start=history_start if history_start is not None else T0 - 3000)  # fmt: skip


# --------------------------------------------------------------------------- policy


def test_policy_v1_is_frozen_and_hysteretic():
    assert P.policy_id == ("lcpolicy_" + PINNED_POLICY)
    assert [w.days for w in P.calendar_windows] == [30, 90, 180, 365, 730]
    assert [w.events for w in P.event_windows] == [20, 50, 100]
    assert P.weighting.half_lives_days == (30, 90, 180, 365)
    assert P.activation.min_t > P.continuation.min_t and P.continuation.min_mean < FLOOR
    other = LifecyclePolicy(activation=Activation(min_t=2.5))
    assert other.policy_id != P.policy_id
    with pytest.raises(ValidationError, match="hysteresis"):
        LifecyclePolicy(continuation=Continuation(min_t=2.5))
    with pytest.raises(ValueError, match="economic floor"):
        P.floor.floor(7)


PINNED_POLICY = "2fae77e8a8263d9254c480f7b4e16bf869714fd5f3dc7d8d6ddb2b7c08b7255b"


# --------------------------------------------------------------------------- windows


def test_calendar_window_inclusion_is_resolved_in_half_open_interval():
    T = T0 + 400
    t = [T - 30, T - 29.999, T - 1, T, T + 0.001]  # boundary excluded, T included, future out
    ev = events(t, [1, 2, 3, 4, 100], assets=[0, 1, 2, 0, 1])
    w = es.window(ev, T, P.window("d30"), P)
    assert w["n"] == 3 and w["mean"] == pytest.approx(3.0)
    assert w["first_resolved"] == es.iso(T - 29.999) and w["last_resolved"] == es.iso(T)


def test_event_count_window_takes_latest_n_and_respects_span():
    T = T0 + 2000
    t = np.linspace(T - 300, T, 60)
    ev = events(t, np.arange(60.0))
    w = es.window(ev, T, P.window("e20"), P)
    assert w["n"] == 20 and w["kind"] == "event_count"
    assert w["mean"] == pytest.approx(np.arange(40, 60).mean()) and w["adequate"]
    sparse = events(np.linspace(T - 900, T, 20), np.ones(20))  # 20 events over 900 days
    w2 = es.window(sparse, T, P.window("e20"), P)
    assert not w2["adequate"] and any("span" in r for r in w2["insufficient_reasons"])
    few = es.window(events([T - 5, T - 1], [1, 1]), T, P.window("e20"), P)
    assert not few["adequate"] and few["n"] == 2


def test_minimum_sample_gates_make_tiny_recent_windows_insufficient():
    T = T0 + 1000
    ev = events([T - 20, T - 15, T - 10, T - 5], [0.2, 0.3, 0.25, 0.4], assets=[0, 1, 2, 0])
    w = es.window(ev, T, P.window("d30"), P)
    assert not w["adequate"] and "4 independent events < 20" in w["insufficient_reasons"]
    assert stt.sign_class(w, FLOOR, P) == "NA"  # a huge recent mean is still INSUFFICIENT
    one_asset = events(np.linspace(T - 25, T, 30), np.full(30, 0.05), assets=np.zeros(30))
    assert any("assets" in r for r in es.window(one_asset, T, P.window("d30"), P)
               ["insufficient_reasons"])  # fmt: skip
    young = events(np.linspace(T - 25, T, 30), np.full(30, 0.05), history_start=T - 26)
    assert not es.window(young, T, P.window("d90"), P)["adequate"]  # history must cover window


def test_evaluable_share_gate():
    T = T0 + 1000
    t = np.linspace(T - 25, T, 30)
    ev = events(t, np.full(30, 0.01))
    # 30 evaluable among 60 signals in the window -> share 0.5 < 0.8
    allres = np.sort(np.r_[t, t - 0.001])
    valid = np.r_[np.ones(30, bool), np.zeros(30, bool)][
        np.argsort(np.r_[t, t - 0.001], kind="mergesort")
    ]
    ev.all_res = allres
    ev.all_valid_cum = np.r_[0, np.cumsum(valid)]
    w = es.window(ev, T, P.window("d30"), P)
    assert w["evaluable_share"] == pytest.approx(0.5) and not w["adequate"]


# --------------------------------------------------------------------------- weighting


def test_recency_weighting_and_effective_sample_size_by_hand():
    T = T0 + 1000
    ev = events([T, T - 90, T - 180], [0.03, 0.01, -0.02], assets=[0, 1, 2],
                t_sig=[T - 10, T - 100, T - 190])  # fmt: skip
    w = es.recency_weights(ev.t_res, T, 90)
    assert np.allclose(sorted(w), [0.25, 0.5, 1.0])
    s = es.stats(ev, 0, 3, weights=w)
    want = (1 * 0.03 + 0.5 * 0.01 + 0.25 * -0.02) / 1.75
    assert s["mean"] == pytest.approx(want)
    assert s["ess"] == pytest.approx(1.75**2 / (1 + 0.25 + 0.0625))
    x = np.array([-0.02, 0.01, 0.03])
    ww = np.array([0.25, 0.5, 1.0])
    iid = 3 / 2 * ((ww * (x - want)) ** 2).sum() / 1.75**2
    assert s["se"] == pytest.approx(np.sqrt(iid))  # 3 singleton clusters: cluster == iid
    full = es.weighted(ev, T, 90, P)
    assert sum(b["weight_share"] for b in full["contribution_by_age"].values()) == pytest.approx(1)
    assert not full["adequate"]  # ESS 2.3 < 20


def test_cluster_robust_se_is_conservative_for_co_timed_outcomes():
    x = np.array([0.1, 0.1, 0.1, -0.1, -0.1, -0.1])
    w = np.ones(6)
    same = np.array([0, 0, 0, 1, 1, 1])  # each block shares its shock
    mean, se = es.weighted_mean_se(x, w, same)
    _, se_iid = es.weighted_mean_se(x, w, np.arange(6))
    assert mean == pytest.approx(0) and se > se_iid
    assert se == pytest.approx(np.sqrt(2 * (0.3**2 + 0.3**2) / 36))


# --------------------------------------------------------------------------- leakage


def _mutate_after(ev: es.EventSet, T: float, rng) -> es.EventSet:
    out = ev.subset(np.ones(len(ev), dtype=bool))
    later = out.t_res > T
    out.net = out.net.copy()
    out.net[later] = rng.normal(0.5, 1.0, later.sum())  # absurd future outcomes
    out.excess = out.net.copy()
    out.__post_init__()
    return out


def test_profile_at_T_never_reads_later_outcomes():
    ev, _, _ = sy.generate("stable", 7, sy.SyntheticSpec(years=3))
    T = ev.t_res[len(ev) // 2]
    ev2 = _mutate_after(ev, T, np.random.default_rng(0))
    a, b = stt.core_evidence(ev, T, P), stt.core_evidence(ev2, T, P)
    assert json.dumps(a, default=str) == json.dumps(b, default=str)
    assert cp.online_cusum(ev, T, P.cusum) == cp.online_cusum(ev2, T, P.cusum)
    times = mc.evaluation_times(ev.t_res[0] + 60, T, 7)
    s1 = mc.simulate(ev, times, P, FLOOR, "recent")
    s2 = mc.simulate(ev2, times, P, FLOOR, "recent")
    assert s1["history"] == s2["history"]
    meta = {"strategy_id": "s", "strategy_name": "s", "family": None, "side": "long",
            "venue": "synthetic", "horizon_bars": 10, "data_cutoff": es.to_time(T),
            "source": {"kind": "synthetic"}}  # fmt: skip
    cur = {d: "unknown" for d in es.REGIME_DIMS}
    p1 = pf.build_profile(
        ev, T, policy=P, meta=meta, current_regime=cur, sims={}, curve_start=T - 300
    )
    p2 = pf.build_profile(
        ev2, T, policy=P, meta=meta, current_regime=cur, sims={}, curve_start=T - 300
    )
    assert p1.model_dump() | {"change_detection": None} == p2.model_dump() | {
        "change_detection": None
    }
    assert p1.change_detection["online"] == p2.change_detection["online"]


def test_online_change_detection_never_uses_retrospective_segmentation(monkeypatch):
    ev, _, _ = sy.generate("reversing", 3, sy.SyntheticSpec(years=4))
    T = ev.t_res[-1]
    full = cp.retrospective_segments(ev, T)
    assert full["retrospective"] and len(full["segments"]) >= 2  # it sees the reversal
    mid = ev.t_res[len(ev) // 2]
    early = cp.retrospective_segments(ev, mid - 100)
    assert early["segments"] != full["segments"]  # segmentation depends on the future
    monkeypatch.setattr(cp, "retrospective_segments", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("lifecycle decisions must not use retrospective segmentation")))  # fmt: skip
    times = mc.evaluation_times(ev.t_res[0] + 60, T, 7)
    mc.simulate(ev, times, P, FLOOR, "recent")  # does not raise
    # appending outcomes after T never changes an alarm raised before T
    cut = mid
    a = cp.online_cusum(ev, cut, P.cusum)
    b = cp.online_cusum(_mutate_after(ev, cut, np.random.default_rng(1)), cut, P.cusum)
    assert a == b


# --------------------------------------------------------------------------- synthetic lifecycle


SPEC = sy.SyntheticSpec(edge=0.05)


def _runs(scenario, n=6, spec=SPEC, modes=("recent",)):
    return [sy.run_one(scenario, 200 + s, spec, P, modes=modes) for s in range(n)]


def test_emerging_edge_is_detected_only_after_emergence():
    runs = [r["recent"] for r in _runs("emerging")]
    detected = [r["participating_at_emergence"] or (r["detection_delay_days"] is not None
                and 0 <= r["detection_delay_days"] < 365) for r in runs]  # fmt: skip
    assert sum(detected) >= 5
    # before emergence the strategy behaves like the null (false activations at the null rate)
    assert np.mean([r["dead_period_participation_share"] for r in runs]) < 0.25
    assert np.mean([r["captured_edge_share"] for r in runs]) > 0.5


def test_decaying_edge_is_eventually_deactivated():
    runs = [r["recent"] for r in _runs("decaying")]
    assert all(r["active_at_change"] for r in runs)
    assert sum(r["deactivation_delay_days"] is not None for r in runs) >= 5
    assert np.mean([r["dead_period_participation_share"] for r in runs]) < 0.35


def test_stable_edge_remains_active():
    runs = [r["recent"] for r in _runs("stable")]
    assert np.mean([r["participation_share"] for r in runs]) > 0.7
    assert np.mean([r["deactivations_per_year"] for r in runs]) < 0.4


def test_null_strategies_rarely_activate():
    runs = [r["recent"] for r in _runs("dead", n=12, spec=sy.SyntheticSpec())]
    assert np.mean([r["participation_share"] for r in runs]) < 0.2
    assert np.mean([r["activations_per_year"] for r in runs]) < 0.35
    assert np.mean([r["transitions_per_year"] for r in runs]) < 1.5


def test_regime_local_lifecycle_participates_in_the_right_regime():
    up_share, down_share = [], []
    for s in range(6):
        ev, _, _ = sy.generate("regime", 300 + s, SPEC)
        times = mc.evaluation_times(SPEC.t0 + SPEC.warmup_days, SPEC.end, 7)
        sim = mc.simulate(ev, times, P, FLOOR, "regime")
        part = sy._participation(ev, times, sim, "regime", P)
        up_share.append(part[ev.regime["trend"] == 2].mean())
        down_share.append(part[ev.regime["trend"] == 0].mean())
        assert set(sim["final_states"]) == {"down", "neutral", "up"}
    assert np.mean(up_share) > 0.5 and np.mean(down_share) < 0.15


def test_hysteresis_keeps_a_middling_edge_in_its_current_state():
    def core(t, mean=0.02):
        r = {
            "adequate": True,
            "mean": mean,
            "t": t,
            "n": 60,
            "se": mean / t if t else 0.01,
            "positive_asset_share": 1.0,
            "excess_mean": mean,
            "selected": "d180",
        }
        return {"lifetime": {"adequate": True, "mean": 0.01, "t": 1.0}, "recent": r,
                "short": r, "contemporary": r, "weighted": {"mean": mean, "ess": 50}}  # fmt: skip

    mid = core(1.2)  # between continuation (0.5) and activation (2.0)
    assert not mc.activation(mid, FLOOR, P)[0]  # cannot activate from WATCH / DORMANT ...
    ok, hard, _ = mc.continuation(mid, FLOOR, P, cusum_alarm=False)
    assert ok and not hard  # ... but does not deactivate an active strategy either
    assert mc.activation(core(2.1), FLOOR, P)[0]
    _, hard, _ = mc.continuation(core(-2.5, mean=-0.03), FLOOR, P, cusum_alarm=False)
    assert hard


def test_hysteresis_prevents_rapid_oscillation(monkeypatch):
    """Recent t alternating 2.2 / 1.2 every evaluation: one activation, then stable."""
    seq = iter([2.2, 2.2] + [1.2, 2.2] * 20)

    def fake(ev, at, policy):
        t = next(seq)
        r = {
            "adequate": True,
            "mean": 0.02,
            "t": t,
            "n": 60,
            "se": 0.02 / t,
            "positive_asset_share": 1.0,
            "excess_mean": 0.02,
            "selected": "d180",
        }
        return {"lifetime": {"adequate": True, "mean": 0.02, "t": 2.0}, "recent": r, "short": r,
                "contemporary": r, "weighted": {"mean": 0.02, "ess": 50}}  # fmt: skip

    monkeypatch.setattr(mc, "core_evidence", fake)
    ev = events(np.linspace(T0, T0 + 400, 100), np.full(100, 0.02))
    tr = mc.Track(ev, P, FLOOR)
    states = [tr.step(T0 + 7 * i) for i in range(42)]
    assert states[1] == "PAPER_ACTIVE" and set(states[1:]) == {"PAPER_ACTIVE"}
    assert [h["to"] for h in tr.transitions] == ["WATCH", "ACTIVE_CANDIDATE", "PAPER_ACTIVE"]


def test_dormant_strategy_can_be_reacquired_and_history_is_append_only():
    rng = np.random.default_rng(5)
    days = np.arange(0, 6 * 365, 3.0)
    t = T0 + days
    mu = np.where(days < 2 * 365, 0.05, np.where(days < 3.5 * 365, -0.05, 0.05))
    ev = events(t + 10, mu + rng.normal(0, 0.06, len(t)), assets=np.arange(len(t)) % 4, t_sig=t,
                history_start=T0)  # fmt: skip
    times = mc.evaluation_times(T0 + 60, T0 + 6 * 365, 7)
    sim = mc.simulate(ev, times, P, FLOOR, "recent")
    tos = [(h["from"], h["to"]) for h in sim["history"]]
    assert ("PAPER_ACTIVE", "DEGRADED") in tos or ("PAPER_ACTIVE", "DORMANT") in tos
    re = [h for h in sim["history"] if h["to"] == "PAPER_ACTIVE" and h.get("reacquisition")]
    assert re and sim["reactivations"] >= 1
    assert re[0]["at"] > next(h["at"] for h in sim["history"] if h["to"] == "DORMANT")
    ats = [h["at"] for h in sim["history"]]
    assert ats == sorted(ats)  # chronological, nothing rewritten


def test_economic_floor_blocks_a_tiny_but_clean_effect():
    T = T0 + 2000
    t = np.linspace(T - 179, T, 90)
    ev = events(t, 0.0001 + np.random.default_rng(0).normal(0, 0.00001, 90), history_start=T0)
    core = stt.core_evidence(ev, T, P)
    assert core["recent"]["t"] > 20  # statistically unmistakable
    assert stt.sign_class(core["recent"], FLOOR, P) == "FLAT"
    ok, checks = mc.activation(core, FLOOR, P)
    assert not ok and not checks["recent_mean_at_least_floor"]


def test_drawdown_alone_does_not_deactivate():
    """A deep drawdown inside an edge whose recent expectancy is still positive keeps it."""
    T = T0 + 2000
    t = np.linspace(T - 179, T, 90)
    x = np.full(90, 0.03)
    x[40:46] = -0.15  # a 90 % cumulative drawdown streak, expectancy still positive
    ev = events(t, x + np.random.default_rng(1).normal(0, 0.01, 90), history_start=T0)
    core = stt.core_evidence(ev, T, P)
    assert es.tails(ev, 0, len(ev))["max_drawdown"] > 0.8
    assert core["recent"]["mean"] > 0
    ok, hard, _ = mc.continuation(core, FLOOR, P, cusum_alarm=False)
    assert ok and not hard


# --------------------------------------------------------------------------- edge states


def test_edge_states_and_divergence_patterns():
    def w(mean, t, adequate=True):
        return {"adequate": adequate, "mean": mean, "t": t, "se": abs(mean / t) if t else 0.01,
                "selected": "d180"}  # fmt: skip

    def core(life, rec, short=None, cont=None):
        return {
            "lifetime": life,
            "recent": rec,
            "short": short or rec,
            "contemporary": cont or rec,
            "weighted": rec,
        }

    pos, flat, neg, na = w(0.02, 2.5), w(0.001, 0.2), w(-0.02, -2.5), w(0.0, 0.0, False)
    cases = {
        "INSUFFICIENT": core(na, na),
        "DEAD": core(pos, neg),
        "STABLE": core(pos, w(0.022, 2.4)),
        "EMERGING": core(flat, pos),
        "ACTIVE": core(w(0.01, 3.0), w(0.06, 3.0)),
        "DECAYING": core(pos, flat),
        "DORMANT": core(pos, na),
    }
    for want, c in cases.items():
        assert stt.classify(c, FLOOR, P)["state"] == want, want
    assert stt.divergence(core(flat, pos), FLOOR, P)["pattern"] == "emerging"
    assert stt.divergence(core(pos, neg), FLOOR, P)["pattern"] == "reversing"
    assert stt.divergence(core(pos, flat, cont=pos), FLOOR, P)["pattern"] == "decaying"
    assert stt.divergence(core(pos, flat, cont=flat), FLOOR, P)["pattern"] == "historical_only"
    assert stt.divergence(core(pos, w(0.021, 2.6)), FLOOR, P)["pattern"] == "stable"
    assert stt.divergence(core(neg, flat), FLOOR, P)["pattern"] == "consistently_absent"


# --------------------------------------------------------------------------- stress


def _btc(n=400, start="2022-01-01"):
    ts = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    close = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.01, n)))
    return pd.DataFrame({"close_time": ts + pd.Timedelta(days=1), "close": close})


def test_stress_labelling_is_objective():
    df = _btc()
    df.loc[200, "close"] = df.loc[199, "close"] * 0.85  # -15 % day
    df = df.drop(index=300).reset_index(drop=True)  # missing bar -> discontinuity
    days = mk.stress_days(df, P.stress)
    flagged = days.index[days["stress_day"]]
    assert df["close_time"].iloc[200] in flagged
    assert days.loc[df["close_time"].iloc[200], "extreme_return"]
    assert days["discontinuity"].sum() == 1
    ep = mk.episodes(days)
    assert any("extreme_return" in e["triggers"] for e in ep)
    first = next(e for e in ep if "extreme_return" in e["triggers"])
    assert pd.Timestamp(first["end"]) - pd.Timestamp(first["start"]) >= pd.Timedelta(days=7)
    # an event whose holding window overlaps the episode is stress-affected; a later one is not
    sig = df["close_time"].iloc[[195, 250]].reset_index(drop=True)
    evs = pd.DataFrame({"signal_time": sig, "resolved_at": sig + pd.Timedelta(days=10)})
    state = mk.market_state({"BTC": df}, "BTC", P.market_state)
    lab = mk.label_events(evs, state, days)
    assert lab["stress"].tolist() == [True, False]
    assert set(lab["regime_trend"]) <= {"up", "down", "neutral", "unknown"}


def test_stress_split_keeps_the_full_series():
    T = T0 + 2000
    t = np.linspace(T - 900, T, 120)
    stress = np.zeros(120, bool)
    stress[50:60] = True
    x = np.where(stress, -0.2, 0.02)
    ev = events(t, x, stress=stress, history_start=T0)
    s = pf.stress_split(ev, T, P)
    assert s["all"]["lifetime"]["n"] == s["normal"]["lifetime"]["n"] + s["stress"]["lifetime"]["n"]
    assert s["all"]["net_sum"] == pytest.approx(s["normal"]["net_sum"] + s["stress"]["net_sum"])
    assert s["all"]["lifetime"]["mean"] < s["normal"]["lifetime"]["mean"]  # never hidden
    assert s["stress_event_share"] == pytest.approx(10 / 120)


# --------------------------------------------------------------------------- outcomes


def test_outcomes_match_the_phase4_screen_and_baseline_is_causal(store):
    from market_signal.research.lab.screen import screen
    from market_signal.research.lifecycle.outcomes import coin_outcomes
    from tests.test_lab_compiler import _definition
    from tests.test_lab_screen import COINS, _plan, _seed, _selections, _snapshot

    _seed(store)
    plan = _plan()
    snap = _snapshot(store, _selections(plan))
    d = _definition([("close", "crosses_above", "ema_20"), ("rsi_14", "gt", 50)])
    res = screen(d, plan, snap, "discovery", COINS)
    period = plan.period("discovery")
    ev = res.events[res.events["horizon"] == "5d"]
    for coin in COINS:
        c = next(x for x in plan.costs if x.symbol == coin)
        df, _ = coin_outcomes(d, snap, coin, fee_bps=c.fee_bps, slippage_bps=c.slippage_bps,
                              horizon_bars=5, baseline=P.baseline, start=period.start)  # fmt: skip
        mine = df[df["evaluable"]].set_index("bar")
        theirs = ev[ev["symbol"] == f"{coin}:long"].set_index("bar")
        assert list(mine.index) == list(theirs.index)
        assert np.allclose(mine["net"], theirs["ret"])
        assert (mine["independent"] == theirs["independent"]).all()
    # the causal baseline at a signal never moves when later bars are rewritten
    df, _ = coin_outcomes(d, snap, "BTC", fee_bps=4.5, slippage_bps=2, horizon_bars=5,
                          baseline=P.baseline)  # fmt: skip
    cut = df["signal_time"].iloc[len(df) // 2]
    store.con.execute("UPDATE perp_bars SET close = close * 1.5, open = open * 1.5 "
                      "WHERE coin='BTC' AND close_time > ?", [cut + pd.Timedelta(days=6)])  # fmt: skip
    snap2 = _snapshot(store, _selections(plan))
    df2, _ = coin_outcomes(d, snap2, "BTC", fee_bps=4.5, slippage_bps=2, horizon_bars=5,
                           baseline=P.baseline)  # fmt: skip
    early = df["resolved_at"] <= cut
    base1 = (df["net"] - df["excess"])[early]
    base2 = (df2["net"] - df2["excess"])[early.reindex(df2.index, fill_value=False)]
    assert np.allclose(base1.dropna(), base2.dropna())


# --------------------------------------------------------------------------- profiles


def _profile(seed=4, T=None):
    ev, _, _ = sy.generate("stable", seed, sy.SyntheticSpec(years=3))
    T = T or float(ev.t_res[-1])
    times = mc.evaluation_times(ev.t_res[0] + 60, T, 7)
    sims = {m: mc.simulate(ev, times, P, FLOOR, m) for m in mc.MODES}
    meta = {"strategy_id": "strategy_x", "strategy_name": "ma_trend_10_50_long",
            "family": "ma_trend", "side": "long", "venue": "synthetic", "horizon_bars": 10,
            "data_cutoff": es.to_time(T), "source": {"kind": "synthetic"}}  # fmt: skip
    cur = {"trend": "up", "vol": "unknown", "breadth": "unknown", "funding": "unknown"}
    return pf.build_profile(ev, T, policy=P, meta=meta, current_regime=cur, sims=sims,
                            curve_start=float(times[0]))  # fmt: skip


def test_profile_is_deterministic_and_consumer_neutral():
    a, b = _profile(), _profile()
    assert a.profile_id == b.profile_id and a.profile_id.startswith("edgeprofile_")
    keys = set(_keys(json.loads(a.model_dump_json())))
    assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN)]
    with pytest.raises(ValidationError):
        pf.EdgeProfile.model_validate({**a.model_dump(), "paper_allocation": 1.0})
    # every evidence dimension coexists; recent never replaces lifetime
    assert a.lifetime["n"] > 0 and a.research_modes["recent"] and a.weighted["hl90"]["ess"] > 0
    assert set(a.windows) == {"d30", "d90", "d180", "d365", "d730", "e20", "e50", "e100"}
    assert a.lifecycle["simulated"] and a.edge_state["state"] in stt.STATES
    assert {"all", "normal", "stress"} <= set(a.stress)
    assert a.regime["hierarchy"]["forward"] == "none"
    assert a.curve and a.curve[-1]["as_of"] == a.as_of.isoformat()


def test_forward_integration_is_read_only(project, monkeypatch):
    from market_signal.data.store import Store
    from market_signal.research.lab import forward as fw

    db = project / "data" / "fw.duckdb"
    Store(db).close()  # migrate (forward tables exist, empty)
    ro = Store(db, read_only=True)
    try:
        assert (
            pf.forward_block(Ledger(ro), "strategy_x", "hyperliquid", pd.Timestamp.now(tz="UTC"))
            is None
        )
    finally:
        ro.close()

    class D:
        strategy_id, source = "strategy_x", "hyperliquid"

    monkeypatch.setattr(
        fw,
        "trackings",
        lambda ledger: [{"tracking_id": "tracking_1", "status": "active", "definition": D()}],
    )
    monkeypatch.setattr(fw, "forward_summary", lambda ledger, tid, as_of: {
        "maturity": {"level": "EARLY"},
        "horizons": [{"primary": True, "independent_resolved": 12, "net": {"mean": 0.01},
                      "excess_mean": 0.004, "direction_vs_historical": "same"}]})  # fmt: skip
    fb = pf.forward_block(None, "strategy_x", "hyperliquid", pd.Timestamp("2026-10-05", tz="UTC"))
    assert fb["maturity"] == "EARLY" and fb["independent_resolved"] == 12
    p = _profile()
    q = pf.with_forward(p, fb, {"run_id": "srun_x"})
    assert q.forward["status"] == "tracked" and q.regime["hierarchy"]["forward"] == "EARLY"
    assert q.edge_state == p.edge_state and q.lifecycle == p.lifecycle  # reported, not weighted
    assert q.profile_id != p.profile_id


def test_profiles_persist_append_only(store):
    led = Ledger(store)
    p = _profile()
    assert pf.record_profiles(led, [p], software=SW) == {"inserted": 1, "existing": 0}
    assert pf.record_profiles(led, [p], software=SW) == {"inserted": 0, "existing": 1}
    later = _profile(T=float(_profile().as_of.timestamp() / 86400) - 30)
    pf.record_profiles(led, [later], software=SW)
    rows = pf.load_profiles(led, strategy="ma_trend_10_50_long")
    assert [r["profile_id"] for r in rows] == [p.profile_id, later.profile_id]  # newest first
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        store.con.execute("UPDATE lab_edge_profiles SET edge_state='NOT_A_STATE'")


# --------------------------------------------------------------------------- governed study


def _tiny_manifest(tmp_path):
    from market_signal.research.lifecycle.study import load_manifest

    text = """
name: phase20_test_study
description: test
policy_version: 1
cutoff: "2024-06-01T00:00:00Z"
horizon_bars: 10
reference_coin: BTC
venues:
  - venue: hyperliquid
    role: primary
    coins: [BTC, ETH, SOL]
    bars_start: "2023-01-01T00:00:00Z"
    funding_start: "2023-01-01T00:00:00Z"
    first_evaluation: "2023-10-02T00:00:00Z"
families:
  - {family: ma_trend, version: 1}
"""
    path = tmp_path / "m.yaml"
    path.write_text(text)
    return load_manifest(path)


def test_governed_study_register_run_build_and_query(store, settings, tmp_path, monkeypatch):
    from market_signal.cli.main import app
    from market_signal.perps.data import perp_config
    from market_signal.research.lab import structure_study as ss
    from tests.test_lab_screen import _seed

    _seed(store)
    led = Ledger(store)
    man = _tiny_manifest(tmp_path)
    defn = ss.register(led, man, perps_cfg=perp_config(settings), software=SW, origin="test",
                       reason="test", catalogue_dir=ROOT / "config" / "lab" / "families")  # fmt: skip
    assert defn.study_version == "edge_lifecycle_v1" and len(defn.strategies) == 14
    assert defn.policy_id == P.policy_id
    with pytest.raises(ValidationError):  # the embedded policy cannot be swapped silently
        type(defn).model_validate({**defn.model_dump(), "policy_id": "lcpolicy_other"})
    out = ss.run(led, defn.study_id, software=SW)
    assert out["status"] == "COMPLETED", out["payload"].get("error")
    v = out["payload"]["venues"]["hyperliquid"]
    assert v["evaluations"] > 20 and v["aggregate"]["modes"]["static"]["strategies"] > 0
    with pytest.raises(ss.StudyError, match="rerun"):
        ss.run(led, defn.study_id, software=SW)
    again = ss.run(
        led, defn.study_id, software=SW, rerun_of=out["run_id"], rerun_reason="determinism"
    )
    assert again["result_digest"] == out["result_digest"]  # same data, same cutoff, same result
    profs = pf.profiles_from_run(led, out["run_id"])
    assert pf.record_profiles(led, profs, software=SW)["inserted"] == len(profs) > 0
    name = profs[0].strategy_name
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(settings.paths.db.parent / "test.duckdb"))
    res = CliRunner().invoke(app, ["lab", "edge", "status", name, "--json"])
    assert res.exit_code == 0, res.output
    got = json.loads(res.output)
    assert got[0]["strategy"] == name and got[0]["edge_state"] in stt.STATES
    for cmd in ("history", "compare", "lifecycle"):
        r = CliRunner().invoke(app, ["lab", "edge", cmd, name, "--json"])
        assert r.exit_code == 0, r.output
    r = CliRunner().invoke(app, ["lab", "edge", "status", name, "--window", "30"])
    assert r.exit_code != 0  # no ad-hoc windows for agents


# --------------------------------------------------------------------------- isolation


def test_no_live_consumer_reads_phase20():
    for pkg in ("paper", "copilot", "ops", "perps", "scoring", "portfolio"):
        for f in (SRC / pkg).rglob("*.py"):
            text = f.read_text()
            assert "research.lifecycle" not in text and "edge_cmds" not in text, f
    for name in ("forward.py", "evidence.py", "validation.py", "corroboration.py", "families.py",
                 "screen.py", "batch.py"):  # fmt: skip
        assert "research.lifecycle" not in (SRC / "research" / "lab" / name).read_text(), name
    for f in (SRC / "research" / "lifecycle").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "telegram", "httpx", "UPDATE ", "DELETE ", "fw.check", "fw.enroll",
                       "record_forward_evidence"):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(
        "edge" in " ".join(map(str, args)) for job in rt.JOBS.values() for _, args in job
    )


def test_paper_and_copilot_policies_are_unchanged():
    from market_signal.copilot import policy as cop
    from market_signal.paper import policy as pap

    text = (SRC / "copilot" / "policy.py").read_text() + (SRC / "paper" / "policy.py").read_text()
    assert "lifecycle" not in text and "edge_state" not in text
    assert cop and pap


def test_synthetic_calibration_is_deterministic():
    a = sy.calibrate(seeds={k: 1 for k in sy.SCENARIOS}, edges=(0.03,), threshold_grid=(2.0,),
                     grid_seeds=1)  # fmt: skip
    b = sy.calibrate(seeds={k: 1 for k in sy.SCENARIOS}, edges=(0.03,), threshold_grid=(2.0,),
                     grid_seeds=1, workers=2)  # fmt: skip
    a.pop("seconds"), b.pop("seconds")
    assert a == b and a["policy_id"] == P.policy_id
    assert replace(sy.SyntheticSpec(), edge=0.01).edge == 0.01
