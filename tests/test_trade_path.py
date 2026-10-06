"""Phase 16 trade-path analytics (``trade_path_v1``) and build manifests.

MFE/MAE conventions, first-touch ordering, explicit OHLC ambiguity, nested-timeframe
resolution, ex-ante R, and that appending bars never changes a completed path.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_signal.intraday.align import Availability
from market_signal.research.structure import manifest as mf
from market_signal.research.structure import path as tp
from market_signal.research.structure import registry as reg
from market_signal.research.structure.chain import run_chain
from market_signal.research.structure.series import BarSeries, assumed
from tests.test_structure import BULL, SAME_BAR_FAIL, SWING, T0, bp, frame, mk, random_walk

PATH = [
    (100, 101, 99.5, 100.5),
    (100.5, 102, 100, 101.5),
    (101.5, 101.8, 98, 98.5),
    (98.5, 99, 97, 98),
]
ENTRY = (T0 - pd.Timedelta(minutes=30)).value  # the first bar opening after this is bar 0


def entries(direction=1, atr=1.0, inval=np.nan, eid="e1"):
    return pd.DataFrame({"event_id": [eid], "entry_after_ns": [ENTRY], "direction": [direction],
                         "atr": [atr], "invalidation": [inval]})  # fmt: skip


P4 = reg.TradePathParams(
    horizons=(4,), pct_levels=(1.0,), atr_levels=(1.0,), r_targets=(1.0, 2.0, 3.0)
)


def test_mfe_mae_long_hand_calculated():
    p, _ = tp.trade_paths(mk(PATH), entries(1), P4)
    r = p.iloc[0]
    assert r["complete"] and r["entry_price"] == 100 and r["entry_ns"] == T0.value
    assert (r["mfe"], r["mae"]) == (pytest.approx(0.02), pytest.approx(0.03))
    assert (r["mfe_close"], r["mae_close"]) == (pytest.approx(0.015), pytest.approx(0.02))
    assert (r["bars_to_mfe"], r["bars_to_mae"]) == (1, 3)
    assert r["mfe_by_ns"] == (T0 + pd.Timedelta(hours=2)).value  # close of bar 1
    assert r["mae_by_ns"] == (T0 + pd.Timedelta(hours=4)).value
    assert r["terminal_return"] == pytest.approx(-0.02) and r["mfe_mae_order"] == tp.FAV


def test_mfe_mae_short_hand_calculated():
    p, _ = tp.trade_paths(mk(PATH), entries(-1), P4)
    r = p.iloc[0]
    assert r["entry_price"] == 100
    assert (r["mfe"], r["mae"]) == (pytest.approx(0.03), pytest.approx(0.02))
    assert (r["mfe_close"], r["mae_close"]) == (pytest.approx(0.02), pytest.approx(0.015))
    assert (r["bars_to_mfe"], r["bars_to_mae"]) == (3, 1)
    assert r["terminal_return"] == pytest.approx(0.02) and r["mfe_mae_order"] == tp.ADV


def test_incomplete_paths_are_nan_not_truncated():
    p, t = tp.trade_paths(mk(PATH[:3]), entries(1), P4)
    assert not p.iloc[0]["complete"] and np.isnan(p.iloc[0]["mfe"]) and t.empty
    p2, _ = tp.trade_paths(mk(PATH + [(98, 99, 97, 98)] * 2, drop=(2,)), entries(1), P4)
    assert not p2.iloc[0]["complete"]  # a gap inside the path


def test_threshold_first_reach_and_same_bar_ambiguity():
    rows = [
        (100, 100.8, 99.6, 100.2),
        (100.2, 101.5, 98.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
    ]
    _, t = tp.trade_paths(mk(rows), entries(1), P4)
    pct = t[t["criterion"] == "pct_1"].iloc[0]
    assert (pct["favourable_bar"], pct["adverse_bar"], pct["order"]) == (1, 1, tp.AMBIGUOUS)
    assert pct["favourable_by_ns"] == (T0 + pd.Timedelta(hours=2)).value
    # a bar that OPENS through the stop touched the stop first: that order is known
    gapped = list(rows)
    gapped[1] = (98.8, 101.5, 98.5, 100)
    _, t2 = tp.trade_paths(mk(gapped), entries(1), P4)
    assert t2[t2["criterion"] == "pct_1"].iloc[0]["order"] == tp.ADV
    # the entry bar itself: both thresholds inside bar 0 is ambiguous, never resolved by open
    _, t3 = tp.trade_paths(mk([(100, 101.5, 98.5, 100), *rows[1:]]), entries(1), P4)
    assert t3[t3["criterion"] == "pct_1"].iloc[0]["order"] == tp.AMBIGUOUS


def _children(rows):
    return BarSeries.from_frame(frame(rows, "15m", start=T0 + pd.Timedelta(hours=1)), venue="test",
                                coin="X", timeframe="15m", availability=assumed())  # fmt: skip


AMBIG = [
    (100, 100.8, 99.6, 100.2),
    (100.2, 101.5, 98.5, 100),
    (100, 100.5, 99.5, 100),
    (100, 100.5, 99.5, 100),
]
KIDS = [
    (100.2, 100.4, 98.5, 98.7),
    (98.7, 99.5, 98.6, 99.2),
    (99.2, 101.5, 99, 101.2),
    (101.2, 101.3, 100, 100),
]


def test_lower_timeframe_resolves_only_with_complete_causal_children():
    s = mk(AMBIG)
    _, t = tp.trade_paths(s, entries(1), P4, resolve_with=_children(KIDS))
    r = t[t["criterion"] == "pct_1"].iloc[0]
    assert (r["order"], r["resolution"], r["resolution_tf"], r["coverage_complete"]) == (
        tp.ADV, "RESOLVED", "15m", True)  # fmt: skip
    assert r["resolved_available_ns"] == (T0 + pd.Timedelta(hours=2)).value
    # one child missing: stays ambiguous, reason recorded
    miss = BarSeries.from_frame(frame(KIDS, "15m", start=T0 + pd.Timedelta(hours=1), drop=(2,)),
                                venue="test", coin="X", timeframe="15m", availability=assumed())  # fmt: skip
    _, t2 = tp.trade_paths(s, entries(1), P4, resolve_with=miss)
    r2 = t2[t2["criterion"] == "pct_1"].iloc[0]
    assert (r2["order"], r2["resolution"], r2["coverage_complete"]) == (
        tp.AMBIGUOUS,
        "INCOMPLETE_COVERAGE",
        False,
    )
    # both levels inside ONE child bar: still unknowable
    one = [(100.2, 101.5, 98.5, 100), *KIDS[1:]]
    _, t3 = tp.trade_paths(s, entries(1), P4, resolve_with=_children(one))
    assert t3[t3["criterion"] == "pct_1"].iloc[0]["resolution"] == "STILL_AMBIGUOUS"
    # children that never reach a level the parent reached: inconsistent, not resolved
    bad = [(100.2, 100.4, 99.5, 99.7), (99.7, 99.9, 98.6, 99.2), (99.2, 100.5, 99, 100.2),
           (100.2, 100.6, 100, 100)]  # fmt: skip
    _, t4 = tp.trade_paths(s, entries(1), P4, resolve_with=_children(bad))
    assert t4[t4["criterion"] == "pct_1"].iloc[0]["resolution"] == "INCONSISTENT"
    with pytest.raises(ValueError, match="FASTER"):
        tp.trade_paths(_children(KIDS), entries(1), P4, resolve_with=s)


def test_ex_ante_risk_and_r_paths():
    rows = [
        (100, 100.5, 99.5, 100.2),
        (100.2, 102.4, 100, 102.2),
        (102.2, 104.5, 101, 104),
        (104, 104.2, 97.5, 98),
    ]
    p, t = tp.trade_paths(mk(rows), entries(1, atr=0.5, inval=98.0), P4)
    r = p.iloc[0]
    assert (r["risk_status"], r["risk"], r["risk_bps"], r["risk_pct"], r["risk_atr"]) == (
        "OK", 2.0, pytest.approx(200), pytest.approx(2.0), pytest.approx(4.0))  # fmt: skip
    assert r["r_terminal"] == pytest.approx(-1.0)
    assert r["r_mfe"] == pytest.approx(2.25) and r["r_mae"] == pytest.approx(1.25)
    order = t.set_index("criterion")["order"]
    assert (order["r_1"], order["r_2"], order["r_3"]) == (tp.FAV, tp.FAV, tp.ADV)
    # no natural invalidation: R unavailable, never invented
    p2, t2 = tp.trade_paths(mk(rows), entries(1), P4)
    assert p2.iloc[0]["risk_status"] == "UNAVAILABLE" and np.isnan(p2.iloc[0]["r_terminal"])
    assert (t2[t2["criterion"].str.startswith("r_")]["order"] == tp.UNDEFINED).all()
    # an invalidation already beyond the entry is not a risk distance
    p3, _ = tp.trade_paths(mk(rows), entries(1, inval=100.5), P4)
    assert p3.iloc[0]["risk_status"] == "INVALID_AT_ENTRY"
    # short: invalidation above
    p4, _ = tp.trade_paths(mk(rows), entries(-1, inval=101.0), P4)
    assert p4.iloc[0]["risk"] == pytest.approx(1.0) and p4.iloc[0]["r_terminal"] == pytest.approx(
        2.0
    )


def test_entry_is_the_next_open_after_availability():
    late = pd.DataFrame({"event_id": ["e"], "entry_after_ns": [(T0 + pd.Timedelta(minutes=1)).value],
                         "direction": [1]})  # fmt: skip
    p, _ = tp.trade_paths(mk(PATH + [(98, 99, 97, 98)] * 4), late, P4)
    assert p.iloc[0]["entry_ns"] == (T0 + pd.Timedelta(hours=1)).value  # not bar 0's open


def test_completed_paths_are_immune_to_appended_bars():
    full_s = mk(random_walk(900, seed=5), latency=45)
    spec = reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h",
                         breakout=bp(window=3, atr=0.1))  # fmt: skip
    pp = reg.TradePathParams(horizons=(4, 16))

    def path_of(s):
        f = run_chain(spec, s, s).failed
        e = pd.DataFrame({"event_id": f["event_id"], "entry_after_ns": f["available_ns"],
                          "direction": np.where(f["side"] == "low", 1, -1), "atr": f["atr"],
                          "invalidation": f["extreme"]})  # fmt: skip
        return tp.trade_paths(s, e, pp)

    full_p, full_t = path_of(full_s)
    for cut in (300, 451, 600, 777):
        pre_p, pre_t = path_of(full_s.head(cut))
        done = pre_p[pre_p["complete"]].reset_index(drop=True)
        assert len(done) > 10
        ref = (
            full_p.set_index(["event_id", "horizon"])
            .loc[list(zip(done["event_id"], done["horizon"], strict=True))]
            .reset_index()
        )
        pd.testing.assert_frame_equal(done, ref[done.columns])
        keys = ["event_id", "horizon", "criterion"]
        got = pre_t.merge(done[["event_id", "horizon"]], on=["event_id", "horizon"])
        exp = full_t.set_index(keys).loc[list(map(tuple, got[keys].to_numpy()))].reset_index()
        pd.testing.assert_frame_equal(got.reset_index(drop=True), exp[got.columns])


# --------------------------------------------------------------------------- manifest


def test_input_records_state_the_availability_basis():
    rec = mf.input_record("event", mk(SAME_BAR_FAIL, latency=90))
    assert rec["availability_basis"] == "assumed_latency" and rec["live_rows"] == 0
    assert rec["assumed_latency_s"] == 90
    obs = BarSeries.from_frame(frame(SAME_BAR_FAIL, observed_delay={}), venue="t", coin="X",
                               timeframe="1h", availability=Availability.observed())  # fmt: skip
    assert mf.input_record("event", obs)["availability_basis"] == "observed"


def test_build_manifest_and_immutable_export(tmp_path):
    s = mk(BULL)
    spec = reg.ChainSpec(venue="test", structure_tf="1h", event_tf="1h", confirm_tf="1h")
    res = run_chain(spec, s, s)
    outs = {"breaches": res.breaches, "chain": res.chain}
    m1 = mf.build_manifest(
        spec.model_dump(mode="json"), [mf.input_record("event", s)], outs, {"git": "x"}
    )
    m2 = mf.build_manifest(
        spec.model_dump(mode="json"), [mf.input_record("event", s)], outs, {"git": "x"}
    )
    assert m1["build_id"] == m2["build_id"] and m1["outputs"]["chain"]["rows"] == len(res.chain)
    target = mf.export(tmp_path, m1, outs)
    assert (target / "manifest.json").exists() and (target / "chain.parquet").exists()
    with pytest.raises(FileExistsError):
        mf.export(tmp_path, m1, outs)
    changed = mf.build_manifest(
        spec.model_dump(mode="json"), [mf.input_record("event", mk(SWING))], outs, {"git": "x"}
    )
    assert changed["build_id"] != m1["build_id"]


def test_r_target_and_stop_in_one_bar_is_ambiguous_never_resolved_favourably():
    # long, entry 100 (next open), invalidation 99 -> 1R = 1.0; bar 1 spans 98.5..103.5:
    # both the -1R stop (99) and the +3R target (103) trade inside it
    rows = [(100, 100.4, 99.6, 100.2), (100.2, 103.5, 98.5, 100), (100, 100.5, 99.5, 100),
            (100, 100.5, 99.5, 100)]  # fmt: skip
    _, t = tp.trade_paths(mk(rows), entries(1, inval=99.0), P4)
    order = t.set_index("criterion")["order"]
    assert order["r_3"] == tp.AMBIGUOUS and order["r_1"] == tp.AMBIGUOUS
    assert t.set_index("criterion").loc["r_3", "favourable_bar"] == 1
    # 15m children establish the order: the stop traded first
    kids = [(100.2, 100.5, 98.5, 98.8), (98.8, 101, 98.7, 100.9), (100.9, 103.5, 100.8, 103),
            (103, 103.1, 99.9, 100)]  # fmt: skip
    _, t2 = tp.trade_paths(mk(rows), entries(1, inval=99.0), P4, resolve_with=_children(kids))
    r3 = t2.set_index("criterion").loc["r_3"]
    assert (r3["order"], r3["resolution"]) == (tp.ADV, "RESOLVED")
