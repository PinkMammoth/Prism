"""Phase 16 structural-price primitives (state, events, chains): temporal correctness first.

Fixtures are hand-built and small enough to audit by eye. A flat base of 20 bars
(open = close = 100, high 100.5, low 99.5) gives a known ATR(14) of 1.0 and no pivots.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.intraday.align import Availability
from market_signal.research.structure import events as ev
from market_signal.research.structure import levels as lv
from market_signal.research.structure import registry as reg
from market_signal.research.structure.chain import run_chain
from market_signal.research.structure.regime import regime_features
from market_signal.research.structure.series import NAT, BarSeries, assumed

SRC = Path(__file__).resolve().parents[1] / "src" / "market_signal"
T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
BASE = [(100.0, 100.5, 99.5, 100.0)] * 20


def frame(rows, tf="1h", start=T0, drop=(), observed_delay=None):
    step = pd.Timedelta(tf)
    out = []
    for i, r in enumerate(rows):
        if i in drop:
            continue
        o, h, lo, c = r[:4]
        ot = start + i * step
        rec = {"open_time": ot, "close_time": ot + step, "open": o, "high": h, "low": lo,
               "close": c, "volume": r[4] if len(r) > 4 else 100.0}  # fmt: skip
        if observed_delay is not None:
            rec["first_observed_at"] = ot + step + observed_delay.get(i, pd.Timedelta(seconds=5))
            rec["observed_live"] = True
        out.append(rec)
    return pd.DataFrame(out)


def mk(rows, tf="1h", latency=0.0, **kw) -> BarSeries:
    return BarSeries.from_frame(frame(rows, tf, **kw), venue="test", coin="X", timeframe=tf,
                                availability=assumed(latency))  # fmt: skip


def same(a: pd.Series, b: pd.Series) -> None:
    """Record equality with every missing marker (None/NaN/NaT) treated alike."""
    assert list(a.index) == list(b.index)
    for k in a.index:
        x, y = a[k], b[k]
        assert (pd.isna(x) and pd.isna(y)) or x == y, (k, x, y)


def mirror(rows, k=300.0):
    """Price mirror p -> k - p: highs become lows. Ranges (and so ATR) are unchanged."""
    return [(k - o, k - lo, k - h, k - c) for o, h, lo, c in rows]


def random_walk(n=800, seed=7):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    return [tuple(map(float, r)) for r in zip(o, h, lo, c, strict=True)]


def bp(window=2, bps=0.0, atr=0.0, level=None):
    return reg.BreakoutParams(
        breach=reg.BreachParams(level=level or reg.SwingLevelParams(), min_excursion_bps=bps,
                                min_excursion_atr=atr),
        failure_window=window,
    )  # fmt: skip


def all_levels(series, params):
    return pd.concat([lv.levels(series, params, s) for s in ("high", "low")], ignore_index=True)


# A swing high of 105 at bar 22 (2 left / 2 right), confirmed by bar 24.
SWING = [*BASE,
         (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 105, 101, 103),
         (103, 103.5, 101, 102), (102, 102.5, 100.5, 101), (101, 102, 100, 101)]  # fmt: skip
SAME_BAR_FAIL = [*SWING, (101, 106, 100.5, 104), (104, 104.5, 102, 103), (103, 103.5, 101, 102)]
HELD = [*SWING, (101, 106, 100.5, 105.8), (105.8, 107, 105.5, 106.5), (106.5, 108, 106, 107.5)]
DELAYED = [*SWING, (101, 106, 100.5, 105.8), (105.8, 106.2, 105.2, 105.6), (105.6, 105.7, 103, 103.5),
           (103.5, 104, 102, 102.5)]  # fmt: skip

# Bullish: swing low 95 (bar 22, confirmed 24), swing high 99 (bar 24, confirmed 26),
# a wick through 95 at bar 27 closing back above (sweep), a close above 99 at bar 29
# (structure shift), and a return to 99 at bar 30 (retest, held).
BULL = [*BASE,
        (100, 100.5, 99, 99.5), (99.5, 100, 98, 98.5), (98.5, 98.8, 95, 96), (96, 98, 95.5, 97.5),
        (97.5, 99, 97, 98.5), (98.5, 98.8, 96.5, 97), (97, 97.5, 96, 96.5), (96.5, 97, 94, 96.8),
        (96.8, 98.5, 96.5, 98.2), (98.2, 99.6, 98, 99.4), (99.4, 99.8, 98.9, 99.5),
        (99.5, 100.5, 99.4, 100.2), (100.2, 100.8, 99.9, 100.5), (100.5, 101, 100.1, 100.8)]  # fmt: skip


# --------------------------------------------------------------------------- registry


def test_registry_bounds_and_normalises_parameters():
    assert reg.BreachParams(min_excursion_bps=5) == reg.BreachParams(min_excursion_bps=5.0)
    assert reg.spec_id("level_breach_v1", reg.BreachParams(min_excursion_bps=5)) == reg.spec_id(
        "level_breach_v1", reg.BreachParams(min_excursion_bps=5.0))  # fmt: skip
    for bad in (lambda: reg.SwingParams(left=4), lambda: reg.Tolerance(method="bps", value=7),
                lambda: reg.BreakoutParams(failure_window=4),
                lambda: reg.TradePathParams(horizons=(3,)),
                lambda: reg.TradePathParams(horizons=(4, 4)),
                lambda: reg.BreachParams(min_excursion_atr=0.3)):  # fmt: skip
        with pytest.raises(ValueError):
            bad()
    with pytest.raises(ValueError):
        reg.spec_id("swing_v2", reg.SwingParams())
    with pytest.raises(ValueError):
        reg.spec_id("swing_v1", reg.RetestParams())
    assert {p.version for p in reg.PRIMITIVES.values()} >= {
        "swing_v1", "level_cluster_v1", "failed_breakout_v1", "structure_shift_v1",
        "retest_v1", "trade_path_v1"}  # fmt: skip
    assert all(p["version"] for p in reg.catalog())


def test_chain_spec_validation_and_ablation_ladder():
    kw = {"venue": "hyperliquid", "structure_tf": "4h", "event_tf": "1h", "confirm_tf": "15m"}
    with pytest.raises(ValueError):
        reg.ChainSpec(venue="x", structure_tf="1h", event_tf="4h", confirm_tf="1h")
    with pytest.raises(ValueError):
        reg.ChainSpec(**kw, retest=reg.RetestParams())  # retest needs a shift
    with pytest.raises(ValueError):
        reg.ChainSpec(**kw, failed_only=False, rejection=reg.RejectionParams())
    ladder = [
        reg.ChainSpec(**kw, failed_only=False),
        reg.ChainSpec(**kw),
        reg.ChainSpec(**kw, rejection=reg.RejectionParams()),
        reg.ChainSpec(**kw, rejection=reg.RejectionParams(), structure_shift=reg.StructureShiftParams()),
        reg.ChainSpec(**kw, rejection=reg.RejectionParams(), structure_shift=reg.StructureShiftParams(),
                      retest=reg.RetestParams()),
    ]  # fmt: skip
    assert [s.ablation for s in ladder] == ["A_breach", "B_failed_breakout", "C_rejection",
                                            "D_structure_shift", "E_retest"]  # fmt: skip
    assert len({s.chain_id for s in ladder}) == 5


# --------------------------------------------------------------------------- series


def test_series_contract_refuses_mixed_venues_bad_bars_and_naive_times():
    f = frame(SWING)
    f["source"] = ["a"] * 10 + ["b"] * (len(f) - 10)
    with pytest.raises(ValueError, match="never mix venues"):
        BarSeries.from_frame(f, venue="a", coin="X", timeframe="1h", availability=assumed())
    bad = frame(SWING)
    bad.loc[3, "high"] = 90.0
    with pytest.raises(ValueError, match="bracketing"):
        BarSeries.from_frame(bad, venue="a", coin="X", timeframe="1h", availability=assumed())
    with pytest.raises(ValueError, match="first_observed_at"):
        BarSeries.from_frame(frame(SWING), venue="a", coin="X", timeframe="1h",
                             availability=Availability.observed())  # fmt: skip
    s = mk(SWING)
    with pytest.raises(ValueError, match="naive"):
        s.known_by(pd.Timestamp("2026-01-05 10:00"))
    a, b = mk(SWING), mk(SWING)
    b2 = BarSeries.from_frame(frame(SWING), venue="other", coin="X", timeframe="1h",
                              availability=assumed())  # fmt: skip
    ev.provenance(a, other=b)
    with pytest.raises(ValueError, match="share venue"):
        ev.provenance(a, other=b2)


def test_ready_at_is_cumulative_availability_and_atr_is_prior():
    late = {3: pd.Timedelta(hours=5)}  # bar 3 published 5h after its close
    s = BarSeries.from_frame(frame(SWING, observed_delay=late), venue="t", coin="X",
                             timeframe="1h", availability=Availability.observed())  # fmt: skip
    # nothing that reads bar 3 can be known before bar 3 was
    assert s.ready_at[4] == s.available_at[3] > s.available_at[4]
    assert np.all(np.diff(s.ready_at) >= 0)
    assert s.atr[19] == pytest.approx(1.0) and s.atr_prior[20] == pytest.approx(1.0)
    assert np.isnan(s.atr_prior[0])


# --------------------------------------------------------------------------- swings


def test_swing_unavailable_before_right_side_confirmation():
    s = mk(SWING, latency=30)
    full = lv.swings(s, reg.SwingParams(left=2, right=2), "high")
    row = full[full["origin_idx"] == 22].iloc[0]
    assert row["price"] == 105 and row["confirm_idx"] == 24
    assert row["origin_ns"] == (T0 + pd.Timedelta(hours=22)).value
    # known only once bar 24 (the 2nd right bar) has closed, plus the assumed latency
    assert row["available_ns"] == (T0 + pd.Timedelta(hours=25, seconds=30)).value
    assert 22 not in lv.swings(s.head(24), reg.SwingParams(), "high")["origin_idx"].tolist()
    assert 22 in lv.swings(s.head(25), reg.SwingParams(), "high")["origin_idx"].tolist()
    # an instant before availability: the series known then has no such swing
    before = s.known_by(T0 + pd.Timedelta(hours=25, seconds=29))
    assert 22 not in lv.swings(before, reg.SwingParams(), "high")["origin_idx"].tolist()


def test_false_visual_pivot_is_cancelled_by_a_higher_right_bar():
    rows = list(SWING)
    rows[24] = (102, 105.5, 100.5, 101)  # bar 24 exceeds the "pivot" at 22
    s = mk(rows)
    piv = lv.swings(s, reg.SwingParams(), "high")["origin_idx"].tolist()
    assert 22 not in piv
    # with one right bar, the same pattern WAS a swing by bar 23 (it stays one)
    assert 22 in lv.swings(s, reg.SwingParams(right=1), "high")["origin_idx"].tolist()


def test_equal_high_plateau_gives_one_pivot_and_gaps_break_windows():
    rows = [*BASE, (100, 101, 99.5, 100.5), (100.5, 103, 100, 102), (102, 103, 101, 102),
            (102, 102, 100, 101), (101, 101.5, 100, 100.5)]  # fmt: skip
    assert lv.swings(mk(rows), reg.SwingParams(), "high")["origin_idx"].tolist() == [21]
    gapped = mk(SWING, drop=(23,))  # bar 23 missing: the 2-right window spans a hole
    assert 22 not in lv.swings(gapped, reg.SwingParams(), "high")["origin_idx"].tolist()


def test_no_pivot_lookahead_on_every_prefix():
    s = mk(random_walk(300))
    for side in ("high", "low"):
        full = lv.swings(s, reg.SwingParams(left=3, right=2), side)
        for k in range(10, 300, 7):
            pre = lv.swings(s.head(k), reg.SwingParams(left=3, right=2), side)
            expect = full[full["confirm_idx"] < k]
            if expect.empty:
                assert pre.empty
                continue
            pd.testing.assert_frame_equal(pre.reset_index(drop=True), expect.reset_index(drop=True))


# --------------------------------------------------------------------------- prior extremes


def test_prior_extreme_matches_lab_donchian_and_is_causal():
    from market_signal.research.lab import features as lab_features
    from market_signal.research.lab.vocabulary import parse_feature

    s = mk(random_walk(400))
    f = pd.DataFrame({"open": s.o, "high": s.h, "low": s.l, "close": s.c, "volume": s.v})
    don = lab_features.compute(parse_feature("donchian_high_20"), f, None).to_numpy()
    levels = lv.prior_extremes(s, reg.PriorExtremeParams(n=20), "high")
    br = ev.breaches(levels, s, bp(window=0))
    assert len(br) > 5
    for _, r in br.iterrows():  # the level breached at bar i IS donchian_high_20 at i
        assert r["level_price"] == pytest.approx(don[int(r["bar_idx"])])
    # every bar exceeding its prior 20-bar high is exactly one breach
    assert sorted(br["bar_idx"]) == sorted(np.flatnonzero(s.h > don).tolist())
    rec = lv.RECORD_COLUMNS
    pre = lv.prior_extremes(s.head(200), reg.PriorExtremeParams(n=20), "high")
    full = levels[levels["confirm_idx"] < 200]
    pd.testing.assert_frame_equal(pre[rec].reset_index(drop=True), full[rec].reset_index(drop=True))


def test_prior_extreme_equal_bar_supersedes_without_event():
    rows = [*BASE, (100, 102, 99.5, 101), (101, 101.5, 100, 100.5), (100.5, 102, 100, 101),
            (101, 101.5, 100.5, 101)]  # fmt: skip
    s = mk(rows)
    levels = lv.prior_extremes(s, reg.PriorExtremeParams(n=10), "high")
    first = levels[levels["origin_idx"] == 20].iloc[0]
    assert first["retired_by"] == "superseded_equal"
    assert first["retired_ns"] == (T0 + pd.Timedelta(hours=23)).value
    # bar 20 breaches the flat base's 100.5; the equal bar 22 is not a breach
    assert ev.breaches(levels, s, bp(window=0))["bar_idx"].tolist() == [20]


# --------------------------------------------------------------------------- clusters

CLUSTER = [*BASE,
           (100, 101, 99.5, 100.5), (100.5, 102, 100, 101.5), (101.5, 105, 101, 103),
           (103, 103.5, 101, 102), (102, 102.5, 100.5, 101), (101, 103, 100.5, 102.5),
           (102.5, 105.08, 102, 104), (104, 104.5, 102, 103), (103, 103.5, 101.5, 102),
           (102, 103, 101.5, 102.5), (102.5, 105.02, 102, 104), (104, 104.2, 102, 103),
           (103, 103.3, 101.8, 102), (102, 102.5, 101, 101.5)]  # fmt: skip
TEN_BPS = reg.ClusterParams(tolerance=reg.Tolerance(method="bps", value=10))


def test_cluster_contains_only_already_confirmed_swings():
    s = mk(CLUSTER)
    cl = lv.clusters(s, TEN_BPS, "high")
    first = cl.iloc[0]
    assert first["members"] == 2 and first["confirm_idx"] == 28
    assert (first["lower"], first["upper"], first["center"]) == (105, 105.08, pytest.approx(105.04))
    assert first["price"] == 105.08  # a breach must exceed the whole cluster
    assert first["first_member_ns"] == (T0 + pd.Timedelta(hours=22)).value
    assert first["available_ns"] == s.ready_at[28]
    # before the second top is confirmed there is no cluster at all
    assert lv.clusters(s.head(28), TEN_BPS, "high").empty
    # 5 bps (0.0525) is tighter than the 0.08 spread: no cluster
    assert lv.clusters(
        s, reg.ClusterParams(tolerance=reg.Tolerance(method="bps", value=5)), "high"
    ).empty


def test_higher_high_between_separates_tops():
    rows = list(CLUSTER)
    rows[25] = (101, 106, 100.5, 102.5)  # a higher high between the two tops
    cl = lv.clusters(mk(rows), TEN_BPS, "high")
    # the tops at 22 and 26 are never grouped (26 may still pair with the later top at 30)
    assert not (cl["first_member_ns"] == (T0 + pd.Timedelta(hours=22)).value).any()
    assert not lv.clusters(mk(CLUSTER), TEN_BPS, "high").empty  # without it they are


def test_cluster_evolution_is_versioned_and_never_rewrites_the_past():
    s = mk(CLUSTER)
    full = lv.clusters(s, TEN_BPS, "high")
    assert full["members"].tolist() == [2, 3]
    v1, v2 = full.iloc[0], full.iloc[1]
    assert v2["extends"] == v1["level_id"] and v2["confirm_idx"] == 32
    assert v1["retired_ns"] == v2["formed_ns"] and v1["retired_by"] == v2["level_id"]
    # the snapshot as known before the third top: identical record, no retirement yet
    pre = lv.clusters(s.head(32), TEN_BPS, "high")
    assert len(pre) == 1 and pre.iloc[0]["retired_ns"] == NAT
    same(pre.iloc[0][lv.RECORD_COLUMNS], v1[lv.RECORD_COLUMNS])
    # the older snapshot cannot be referenced once superseded: no duplicate events
    a, b = lv.eligible_range(full, s)
    assert b[0] <= a[1]


def test_cluster_lows_are_the_mirror_of_highs():
    hi = lv.clusters(mk(CLUSTER), TEN_BPS, "high")
    lo = lv.clusters(mk(mirror(CLUSTER, 210.0)), TEN_BPS, "low")
    assert hi["members"].tolist() == lo["members"].tolist()
    assert hi["confirm_idx"].tolist() == lo["confirm_idx"].tolist()
    assert lo.iloc[0]["price"] == pytest.approx(210 - 105.08)  # the LOWEST low
    assert lo.iloc[0]["lower"] == pytest.approx(210 - 105.08) and lo.iloc[0][
        "upper"
    ] == pytest.approx(105)


# --------------------------------------------------------------------------- touches / state


def test_touches_are_separated_and_stop_at_the_breach():
    rows = [*SWING, (101, 104.8, 100.5, 104), (104, 104.9, 103, 104.5), (104.5, 104.6, 103, 103.5),
            (103.5, 104.7, 103, 104), (104, 106, 103.5, 105.5), (105.5, 105.6, 104.5, 105)]  # fmt: skip
    s = mk(rows)
    levels = lv.swing_levels(s, reg.SwingLevelParams(), "high")
    a = levels[levels["origin_idx"] == 22]
    life, touches = lv.lifecycle(
        a, s, reg.TouchParams(tolerance=reg.Tolerance(method="bps", value=30))
    )
    assert touches["bar_idx"].tolist() == [26, 29]  # 26-27 is ONE touch; 28 left the zone
    assert life.iloc[0]["breach_idx"] == 30 and life.iloc[0]["close_beyond_idx"] == 30
    st = lv.level_state(a, life, touches, s, "high")
    assert st["touches"].iloc[[25, 26, 27, 28, 29]].tolist() == [0, 1, 1, 1, 2]
    assert not st["breached"].iloc[29] and st["breached"].iloc[30]
    assert pd.isna(st["level_id"].iloc[23]) and st["price"].iloc[24] == 105  # known from bar 24
    assert st["age_bars"].iloc[26] == 2
    assert st["distance_bps"].iloc[24] == pytest.approx((101 - 105) / 105 * 1e4)
    assert st["last_touch"].iloc[29] == T0 + pd.Timedelta(hours=29)


# --------------------------------------------------------------------------- breach / sweep


def test_breach_excursion_and_close_classification():
    s = mk(HELD)
    br = ev.breaches(lv.swing_levels(s, reg.SwingLevelParams(), "high"), s, bp(window=2))
    r = br[br["level_price"] == 105].iloc[0]
    assert r["bar_idx"] == 26 and r["breach_kind"] == "CLOSE_BEYOND"
    assert r["overshoot"] == pytest.approx(1.0) and r["overshoot_bps"] == pytest.approx(
        1 / 105 * 1e4
    )
    assert r["overshoot_atr"] == pytest.approx(1.0 / s.atr_prior[26])
    assert r["close_vs_level_bps"] == pytest.approx(0.8 / 105 * 1e4)
    assert r["outcome"] == "HELD" and r["resolve_idx"] == 28  # a held breakout, not a sweep
    assert ev.failed_breakouts(br, bp(window=2)).query("level_price == 105").empty
    held = ev.held_breakouts(br, bp(window=2))
    assert (
        held.iloc[0]["parent_id"] == r["event_id"]
        and held.iloc[0]["available_ns"] == s.ready_at[28]
    )


def test_same_bar_failed_breakout():
    s = mk(SAME_BAR_FAIL, latency=60)
    br = ev.breaches(lv.swing_levels(s, reg.SwingLevelParams(), "high"), s, bp(window=2))
    r = br.iloc[0]
    assert (r["bar_idx"], r["breach_kind"], r["outcome"], r["failure_delay_bars"]) == (
        26,
        "WICK_ONLY",
        "FAILED",
        0,
    )
    assert r["close_back"] == pytest.approx(1.0) and r["max_excursion"] == pytest.approx(1.0)
    assert r["extreme"] == 106
    sw = ev.failed_breakouts(br, bp(window=2)).iloc[0]
    assert sw["primitive"] == "failed_breakout_v1" and sw["parent_id"] == r["event_id"]
    assert sw["available_ns"] == (T0 + pd.Timedelta(hours=27, seconds=60)).value
    assert sw["availability_mode"] == "assumed" and sw["assumed_latency_s"] == 60


def test_delayed_failed_breakout_and_window():
    s = mk(DELAYED)
    levels = lv.swing_levels(s, reg.SwingLevelParams(), "high")
    r = ev.breaches(levels, s, bp(window=2)).iloc[0]
    assert (r["outcome"], r["failure_delay_bars"], r["bars_beyond"]) == ("FAILED", 2, 2)
    assert r["max_excursion"] == pytest.approx(1.2) and r["close_back"] == pytest.approx(1.5)
    assert r["ret_to_resolve"] == pytest.approx(103.5 / 105.8 - 1)
    # with a one-bar window the same path is a held breakout
    assert ev.breaches(levels, s, bp(window=1)).iloc[0]["outcome"] == "HELD"
    # and before the failing bar exists the outcome is unresolved (no event)
    pre = mk(DELAYED[:28])
    r0 = ev.breaches(lv.swing_levels(pre, reg.SwingLevelParams(), "high"), pre, bp(window=2)).iloc[
        0
    ]
    assert r0["outcome"] == "UNRESOLVED"
    assert ev.failed_breakouts(ev.breaches(levels, s.head(28), bp(window=2)), bp()).empty


def test_minimum_excursion_filters_and_retires_the_level():
    rows = [*SWING, (101, 105.2, 100.5, 104), (104, 107, 103, 104.5), (104.5, 105, 103, 104)]
    s = mk(rows)
    levels = lv.swing_levels(s, reg.SwingLevelParams(), "high")
    a = levels[levels["origin_idx"] == 22]
    assert ev.breaches(a, s, bp(bps=25.0)).empty  # 0.2 < 25 bps of 105; level then retired
    assert ev.breaches(a, s, bp(bps=0.0)).iloc[0]["bar_idx"] == 26
    assert ev.breaches(a, s, bp(atr=0.25)).empty  # 0.2 < 0.25 ATR (ATR ~ 1.2)


def test_bullish_and_bearish_are_mirror_images():
    for rows in (SAME_BAR_FAIL, DELAYED, HELD, random_walk(500, seed=3)):
        a, b = mk(rows), mk(mirror(rows))
        p = bp(window=2, atr=0.1)
        x = ev.breaches(lv.swing_levels(a, reg.SwingLevelParams(), "high"), a, p)
        y = ev.breaches(lv.swing_levels(b, reg.SwingLevelParams(), "low"), b, p)
        # same-bar events of different levels are ordered by level ID, which differs
        x = x.sort_values(["bar_idx", "overshoot"], kind="stable")
        y = y.sort_values(["bar_idx", "overshoot"], kind="stable")
        cols = ["bar_idx", "breach_kind", "outcome", "resolve_idx", "failure_delay_bars",
                "overshoot", "overshoot_atr", "max_excursion_atr", "close_back_atr", "available_ns"]  # fmt: skip
        assert len(x) == len(y) > 0
        pd.testing.assert_frame_equal(
            x[cols].reset_index(drop=True), y[cols].reset_index(drop=True)
        )
        assert np.allclose(x["level_price"].to_numpy(), 300 - y["level_price"].to_numpy())
        assert set(y["side"]) == {"low"}


# --------------------------------------------------------------------------- rejection


def test_rejection_metrics_hand_calculated():
    m = ev.rejection_metrics(mk([*BASE, (10, 12, 9, 9.5), (9.5, 9.5, 9.5, 9.5)]))
    r = m.iloc[20]
    assert r["wick_high"] == 2 and r["wick_low"] == 0.5
    assert r["wick_body_high"] == pytest.approx(4) and r["wick_body_low"] == pytest.approx(1)
    assert r["wick_range_high"] == pytest.approx(2 / 3) and r["wick_range_low"] == pytest.approx(
        1 / 6
    )
    assert r["clv"] == pytest.approx(-2 / 3)
    assert r["range_atr"] == pytest.approx(3.0)  # prior ATR is exactly 1
    assert r["range_expansion"] == pytest.approx(3.0)
    assert r["reversal_atr_high"] == pytest.approx(2.5) and r["reversal_atr_low"] == pytest.approx(
        0.5
    )
    flat = m.iloc[21]  # zero range / zero body: undefined, never inf
    assert np.isnan(flat["clv"]) and np.isnan(flat["wick_body_high"])


def test_rejection_event_on_a_failed_breakout():
    s = mk(BULL)
    levels = lv.swing_levels(s, reg.SwingLevelParams(), "low")
    sw = ev.failed_breakouts(ev.breaches(levels, s, bp()), bp())
    rj = ev.rejections(sw, s, reg.RejectionParams(min_wick_range=0.66))
    r = rj.iloc[0]
    assert (
        r["status"] == "REJECTION"
        and r["bar_idx"] == 27
        and r["parent_id"] == sw.iloc[0]["event_id"]
    )
    assert r["wick_range"] == pytest.approx(2.5 / 3)
    assert r["available_ns"] >= sw.iloc[0]["available_ns"]


# --------------------------------------------------------------------------- shift / retest

SHIFT = reg.StructureShiftParams(swing=reg.SwingParams(left=2, right=2), window=5)
RETEST = reg.RetestParams(tolerance=reg.Tolerance(method="bps", value=10), max_delay=5)


def _sweeps(s):
    levels = lv.swing_levels(s, reg.SwingLevelParams(), "low")
    return ev.failed_breakouts(ev.breaches(levels, s, bp()), bp())


def test_structure_shift_after_bullish_sweep():
    s = mk(BULL, latency=30)
    sw = _sweeps(s)
    assert sw["bar_idx"].tolist() == [27] and sw.iloc[0]["extreme"] == 94
    sh = ev.structure_shifts(sw, s, s, SHIFT)
    r = sh.iloc[0]
    assert (r["status"], r["direction"], r["structure_price"], r["bar_idx"]) == (
        "SHIFT",
        "bullish",
        99,
        29,
    )
    assert r["structure_pivot_ns"] == (T0 + pd.Timedelta(hours=24)).value
    assert r["structure_available_ns"] <= r["sweep_available_ns"]  # reference known first
    assert r["available_ns"] == (T0 + pd.Timedelta(hours=30, seconds=30)).value
    assert r["delay_bars"] == 2 and r["delay_hours"] == pytest.approx(2.0)
    assert r["move"] == pytest.approx(99.4 - 96.8) and r["parent_id"] == sw.iloc[0]["event_id"]


def test_structure_shift_cannot_precede_its_reference_or_break():
    s = mk(BULL)
    sw = _sweeps(s)
    # the swing high at 24 needs 5 right bars -> confirmed at 29, AFTER the sweep: unusable
    late = reg.StructureShiftParams(swing=reg.SwingParams(left=2, right=5), window=5)
    assert ev.structure_shifts(sw, s, s, late).iloc[0]["status"] == "NO_REFERENCE"
    # data ending before the break: no shift event
    pre = s.head(29)
    r = ev.structure_shifts(_sweeps(pre), pre, pre, SHIFT).iloc[0]
    assert r["status"] == "UNRESOLVED" and r["event_id"] is None
    # a close below the swept low first invalidates
    rows = list(BULL)
    rows[28] = (96.8, 97, 93.5, 93.8)
    s2 = mk(rows)
    assert ev.structure_shifts(_sweeps(s2), s2, s2, SHIFT).iloc[0]["status"] == "INVALIDATED"


def test_bearish_structure_shift_mirrors_bullish():
    a, b = mk(BULL), mk(mirror(BULL))
    x = ev.structure_shifts(_sweeps(a), a, a, SHIFT)
    lv_b = lv.swing_levels(b, reg.SwingLevelParams(), "high")
    y = ev.structure_shifts(ev.failed_breakouts(ev.breaches(lv_b, b, bp()), bp()), b, b, SHIFT)
    assert y.iloc[0]["direction"] == "bearish"
    for c in ("status", "bar_idx", "delay_bars", "available_ns", "move_atr"):
        assert x.iloc[0][c] == pytest.approx(y.iloc[0][c] if c != "move_atr" else -y.iloc[0][c])
    assert y.iloc[0]["structure_price"] == pytest.approx(300 - 99)


def test_retest_only_after_the_return_happens():
    s = mk(BULL)
    sh = ev.structure_shifts(_sweeps(s), s, s, SHIFT)
    breaks = pd.DataFrame({"event_id": sh["event_id"], "break_side": "high",
                           "level_price": sh["structure_price"], "close_ns": sh["close_ns"],
                           "available_ns": sh["available_ns"]})  # fmt: skip
    r = ev.retests(breaks, s, RETEST).iloc[0]
    assert (r["status"], r["bar_idx"], r["delay_bars"]) == ("RETEST", 30, 1)
    assert r["penetration"] == pytest.approx(0.1) and r["held"]
    assert r["first_retest_price"] == pytest.approx(99 + 99 * 10 / 1e4)  # zone edge (opened above)
    assert r["closest_distance"] == pytest.approx(-0.1)
    pre = s.head(30)  # the return bar does not exist yet
    r0 = ev.retests(breaks, pre, RETEST).iloc[0]
    assert r0["status"] == "UNRESOLVED" and r0["event_id"] is None


def test_chain_links_stages_and_measures_entry_delay():
    s = mk(BULL)
    spec = reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h",
                         rejection=reg.RejectionParams(), structure_shift=SHIFT, retest=RETEST)  # fmt: skip
    res = run_chain(spec, s, s)
    c = res.chain[res.chain["side"] == "low"].iloc[0]
    assert c["ablation"] == "E_retest"
    assert c["failed_id"] == res.failed.query("side == 'low'").iloc[0]["event_id"]
    assert c["shift_id"] == res.shifts.query("status == 'SHIFT'").iloc[0]["event_id"]
    assert (
        res.retests.iloc[0]["parent_id"] == c["shift_id"]
        and c["retest_id"] == res.retests.iloc[0]["event_id"]
    )
    assert (
        c["rejection_delay_bars"] == 0
        and c["shift_delay_bars"] == 2
        and c["retest_delay_bars"] == 3
    )
    assert c["shift_delay_hours"] == pytest.approx(2.0)
    assert c["shift_price_diff"] == pytest.approx(99.4 - 96.8)
    assert c["shift_entry_cost_bps"] == pytest.approx((99.4 / 96.8 - 1) * 1e4)  # later = worse long
    assert c["shift_price_diff_atr"] == pytest.approx(2.6 / c["atr"])
    assert c["chain_available_ns"] == c["retest_available_ns"]
    # an ablation spec is the same detectors truncated: identical upstream IDs
    short = run_chain(
        reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h"), s, s
    )
    assert short.chain["failed_id"].tolist() == res.chain["failed_id"].tolist()
    with pytest.raises(ValueError, match="spec says"):
        run_chain(spec, mk(BULL, tf="4h"), s)


# --------------------------------------------------------------------------- multi-timeframe


def test_htf_level_only_referenced_by_ltf_bars_after_it_formed():
    four = [*BASE, (100, 101, 99.5, 100.5), (100.5, 105, 100, 103), (103, 103.5, 101, 102),
            (102, 102.5, 100.5, 101), (101, 101.5, 100.5, 101)]  # fmt: skip
    formed = T0 + pd.Timedelta(hours=4 * 24)  # close of 4h bar 23 (2nd right bar)
    ones = [(101, 101.5, 100.5, 101)] * (4 * 26)
    k = 4 * 24
    ones[k - 1] = (101, 106, 100.5, 101)  # inside the confirming 4h bar: too early
    ones[k] = (101, 105.6, 100.5, 104.5)  # the first 1h bar after the level formed: eligible
    delay = {23: pd.Timedelta(hours=2)}  # the confirming 4h bar was published 2h late
    h4 = BarSeries.from_frame(frame(four, "4h", observed_delay=delay), venue="t", coin="X",
                              timeframe="4h", availability=Availability.observed())  # fmt: skip
    h1 = BarSeries.from_frame(frame(ones, "1h", observed_delay={}), venue="t", coin="X",
                              timeframe="1h", availability=Availability.observed())  # fmt: skip
    levels = lv.swing_levels(h4, reg.SwingLevelParams(), "high")
    assert levels.iloc[0]["formed_ns"] == formed.value
    br = ev.breaches(levels, h1, bp(window=0))
    assert br["bar_idx"].tolist() == [k] and br.iloc[0]["bar_ns"] == formed.value
    # price-formed before the bar, but only KNOWN when the late 4h bar was published
    # (the 1h bar itself was ready at formed + 1h + 5 s)
    assert br.iloc[0]["available_ns"] == (formed + pd.Timedelta(hours=2)).value


def test_mtf_chain_4h_structure_1h_event_15m_confirmation():
    rows = random_walk(2000, seed=11)
    m15 = mk(rows, tf="15m")
    df = frame(rows, "15m")
    agg = lambda tf: (df.set_index("open_time").resample(tf, label="left", closed="left")  # noqa: E731
                      .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                      .dropna().reset_index())  # fmt: skip
    h1 = agg("1h").assign(close_time=lambda d: d["open_time"] + pd.Timedelta(hours=1))
    h4 = agg("4h").assign(close_time=lambda d: d["open_time"] + pd.Timedelta(hours=4))
    s1 = BarSeries.from_frame(h1, venue="test", coin="X", timeframe="1h", availability=assumed())
    s4 = BarSeries.from_frame(h4, venue="test", coin="X", timeframe="4h", availability=assumed())
    spec = reg.ChainSpec(venue="test", structure_tf="4h", event_tf="1h", confirm_tf="15m",
                         structure_shift=reg.StructureShiftParams(), retest=reg.RetestParams())  # fmt: skip
    res = run_chain(spec, s4, s1, m15)
    assert len(res.breaches) > 0
    lvl = res.levels.set_index("level_id")
    for _, r in res.breaches.iterrows():
        assert r["bar_ns"] >= lvl.loc[r["level_id"], "formed_ns"]
        assert r["available_ns"] >= lvl.loc[r["level_id"], "available_ns"]
    ok = res.shifts[res.shifts["status"] == "SHIFT"]
    assert (ok["bar_ns"] >= ok["sweep_close_ns"]).all() and (
        ok["available_ns"] >= ok["sweep_available_ns"]
    ).all()
    assert (res.breaches["event_tf"] == "1h").all() and (res.shifts["confirm_tf"] == "15m").all()


def test_assumed_latency_provenance_is_explicit():
    s = mk(SAME_BAR_FAIL, latency=90)
    br = ev.breaches(lv.swing_levels(s, reg.SwingLevelParams(), "high"), s, bp())
    r = br.iloc[0]
    assert r["availability_mode"] == "assumed" and r["assumed_latency_s"] == 90
    assert r["available_ns"] == r["close_ns"] + 90 * 10**9
    assert not r["inputs_live"]  # backfilled bars are never "observed live"
    obs = BarSeries.from_frame(frame(SAME_BAR_FAIL, observed_delay={}), venue="t", coin="X",
                               timeframe="1h", availability=Availability.observed())  # fmt: skip
    r2 = ev.breaches(lv.swing_levels(obs, reg.SwingLevelParams(), "high"), obs, bp()).iloc[0]
    assert r2["availability_mode"] == "observed" and r2["inputs_live"]
    assert r["event_id"] != r2["event_id"]  # availability policy is part of identity


# --------------------------------------------------------------------------- identity / immunity


def test_event_ids_are_deterministic_and_parameter_sensitive():
    s = mk(random_walk(500))
    spec = reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h",
                         rejection=reg.RejectionParams(), structure_shift=reg.StructureShiftParams(),
                         retest=reg.RetestParams())  # fmt: skip
    a, b = run_chain(spec, s, s), run_chain(spec, mk(random_walk(500)), mk(random_walk(500)))
    for name in ("breaches", "failed", "chain"):
        pd.testing.assert_frame_equal(getattr(a, name), getattr(b, name))
    wider = reg.BreakoutParams(breach=spec.breakout.breach, failure_window=3)
    other = reg.ChainSpec(**{**spec.model_dump(), "breakout": wider.model_dump()})
    c = run_chain(other, s, s)
    assert set(a.failed["event_id"]).isdisjoint(c.failed["event_id"])
    assert set(a.breaches["event_id"]) == set(c.breaches["event_id"])  # same breach definition
    assert a.breaches["event_id"].str.match(r"^sev_[0-9a-f]{64}$").all()
    assert a.levels["level_id"].is_unique and a.breaches["event_id"].is_unique


def test_future_immunity_appending_bars_never_changes_earlier_events():
    rows = random_walk(900, seed=5)
    full_s = mk(rows, latency=45)
    spec = reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h",
                         breakout=bp(window=3, atr=0.1),
                         rejection=reg.RejectionParams(within_bars=1),
                         structure_shift=reg.StructureShiftParams(window=10),
                         retest=reg.RetestParams(max_delay=10))  # fmt: skip
    full = run_chain(spec, full_s, full_s)
    for cut in (300, 451, 600, 777):
        s = full_s.head(cut)
        t = s.ready_at[-1]
        pre = run_chain(spec, s, s)
        # every event already known at t is identical, and nothing known by t is missing
        for name in ("failed", "held"):
            x = getattr(pre, name)
            y = getattr(full, name)
            y = y[y["available_ns"] <= t]
            pd.testing.assert_frame_equal(x.reset_index(drop=True), y.reset_index(drop=True))
        for name, ok in (("rejections", "REJECTION"), ("shifts", "SHIFT"), ("retests", "RETEST")):
            x = getattr(pre, name)
            y = getattr(full, name)
            x, y = x[x["status"] == ok], y[(y["status"] == ok) & (y["available_ns"] <= t)]
            pd.testing.assert_frame_equal(x.reset_index(drop=True), y.reset_index(drop=True))
        rb = pre.breaches[pre.breaches["outcome"] != "UNRESOLVED"]
        fb = full.breaches.set_index("event_id").loc[rb["event_id"]].reset_index()
        pd.testing.assert_frame_equal(rb.reset_index(drop=True), fb[rb.columns])
        rec = lv.RECORD_COLUMNS
        pd.testing.assert_frame_equal(
            pre.levels[rec].reset_index(drop=True),
            full.levels[full.levels["available_ns"] <= t][rec].reset_index(drop=True),
        )


# --------------------------------------------------------------------------- regime


def test_minimal_regime_primitives():
    rows = [(c, c + 0.5, c - 0.5, c) for c in (1.0, 2, 3, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    df = regime_features(
        mk(rows), ["efficiency_ratio_4", "donchian_pos_3", "ma_slope_2_1", "ret_2", "dist_ema_3"]
    )
    assert df["efficiency_ratio_4"].iloc[4] == pytest.approx(0.5)  # |3-1| / (1+1+1+1)
    assert df["donchian_pos_3"].iloc[4] == pytest.approx((3 - 1.5) / (3.5 - 1.5))
    assert df["ret_2"].iloc[4] == pytest.approx(0.0)
    for bad in ("funding_day", "roc_1m", "efficiency_ratio_5000"):
        with pytest.raises(ValueError):
            regime_features(mk(rows), [bad])


# --------------------------------------------------------------------------- isolation


def test_live_consumers_never_use_structural_primitives():
    for pkg in ("paper", "copilot"):
        for f in (SRC / pkg).rglob("*.py"):
            if pkg == "paper" and "v2" in f.parts:  # Phase 25A reuses causal BarSeries only
                continue
            assert "structure" not in f.read_text(), f
    for f in (SRC / "research" / "lab" / "forward.py", SRC / "ops" / "runtime.py",
              SRC / "research" / "lab" / "corroboration.py"):  # fmt: skip
        assert "research.structure" not in f.read_text(), f
    for f in (SRC / "research" / "structure").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "telegram", "httpx", "write_db", "INSERT ", "UPDATE "):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(  # word match: Phase 24A's `microstructure` job is unrelated market data
        re.search(r"\bstructure\b", " ".join(map(str, args)))
        for job in rt.JOBS.values()
        for _, args in job
    )
