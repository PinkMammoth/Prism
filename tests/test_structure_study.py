"""Phase 17 falsification study: governance and research correctness.

Synthetic fixtures only (random walks and hand-built bars). Nothing here reads real
market data, and no test result says anything about whether a pattern works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.structure import path as tp
from market_signal.research.structure.chain import run_chain
from market_signal.research.structure.registry import ChainSpec, TradePathParams
from market_signal.research.structure.series import NAT, BarSeries, assumed
from market_signal.research.structure.study import analysis as an
from market_signal.research.structure.study import populations as pop
from market_signal.research.structure.study import verdicts as vd
from market_signal.research.structure.study.collect import collect
from market_signal.research.structure.study.data import CoinData
from market_signal.research.structure.study.spec import (
    LADDER,
    Architecture,
    CentralChain,
    CostRef,
    DatasetRef,
    StatisticsSpec,
    StudyDefinition,
    StudyManifest,
    family_sizes,
    load_manifest,
    semantics,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
MANIFEST = ROOT / "config" / "structure" / "phase17_falsification.v1.yaml"
H = pd.Timedelta("1h")


# --------------------------------------------------------------------------- fixtures


def walk(n: int, seed: int, sig: float = 0.006):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, sig, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, sig / 2, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, sig / 2, n)))
    return o, h, lo, c


def series(
    tf: str, start, end, seed: int, venue="hyperliquid", coin="BTC", latency=60.0
) -> BarSeries:
    step = pd.Timedelta(tf)
    ot = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq=step, inclusive="left")
    ot = ot[ot + step <= pd.Timestamp(end)]
    o, h, lo, c = walk(len(ot), seed, {"15m": 0.003, "1h": 0.006, "4h": 0.012}[tf])
    df = pd.DataFrame({"open_time": ot, "close_time": ot + step, "open": o, "high": h, "low": lo,
                       "close": c, "volume": 100.0})  # fmt: skip
    return BarSeries.from_frame(df, venue=venue, coin=coin, timeframe=tf,
                                availability=assumed(latency), dataset_key="dataset_test")  # fmt: skip


def small_manifest(**over) -> StudyManifest:
    """A two-coin, one-venue (or two) manifest over a few weeks of synthetic history."""
    d = {
        "name": "p17_test", "description": "test", "coins": ["BTC", "ETH"],
        "venues": [{"venue": "hyperliquid", "start_4h": "2026-01-01T00:00:00Z",
                    "start_1h": "2026-02-01T00:00:00Z", "start_15m": "2026-03-01T00:00:00Z",
                    "start_funding": "2026-02-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"}],
        "architectures": [{
            "name": "primary", "structure_tf": "4h", "event_tf": "1h", "confirm_tf": "1h",
            "resolve_tf": "15m", "horizons": [4, 24], "primary_horizon": 24,
            "windows": [{"venue": "hyperliquid", "event_start": "2026-02-15T00:00:00Z",
                         "event_end": "2026-04-01T00:00:00Z"}],
            "axes": [{"name": "overshoot_atr", "values": [0.0, 0.1, 0.25]},
                     {"name": "failure_window", "values": [0, 2, 5]}],
            "stretch_control": {"lookback_bars": 4, "threshold_atr": 2.0}, "subgroups": True,
        }],
    }  # fmt: skip
    d.update(over)
    return StudyManifest.model_validate(d)


def definition(man: StudyManifest, fee=4.5, slip=2.0) -> StudyDefinition:
    costs = tuple(CostRef(venue=v.venue, coin=c, fee_bps=fee, slippage_bps=slip)
                  for v in man.venues for c in man.coins)  # fmt: skip
    ds = tuple(DatasetRef(venue=v.venue, coin=c, dataset_id="dataset_" + f"{i:064x}")
               for i, (v, c) in enumerate((v, c) for v in man.venues for c in man.coins))  # fmt: skip
    return StudyDefinition(manifest=man, costs=costs, datasets=ds, semantics=semantics())


def aggregate(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    g = df.set_index("open_time").resample(tf, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()}).dropna().reset_index()  # fmt: skip
    out["close_time"] = out["open_time"] + pd.Timedelta(tf)
    return out


def coin_data(man: StudyManifest, venue: str, coin: str, seed: int = 1) -> CoinData:
    """One 15m random walk aggregated to 1h and 4h, so the timeframes describe one market."""
    v = next(x for x in man.venues if x.venue == venue)
    ot = pd.date_range(
        pd.Timestamp(v.start_4h), pd.Timestamp(v.end), freq="15min", inclusive="left"
    )
    o, h, lo, c = walk(len(ot), seed, 0.003)
    df = pd.DataFrame({"open_time": ot, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0})
    s = {}
    for tf in ("4h", "1h", "15m"):
        a = (
            df.assign(close_time=df["open_time"] + pd.Timedelta("15min"))
            if tf == "15m"
            else aggregate(df, tf)
        )
        a = a[a["close_time"] >= pd.Timestamp(v.start(tf))]
        s[tf] = BarSeries.from_frame(a, venue=venue, coin=coin, timeframe=tf, availability=assumed(60.0),
                                     dataset_key="dataset_test")  # fmt: skip
    ft = pd.date_range(v.start_funding, v.end, freq="1h", inclusive="left").as_unit("ns").asi8
    return CoinData(venue, coin, "dataset_test", s, ft.astype(np.int64), np.full(len(ft), 2e-5))


@pytest.fixture(scope="module")
def collected():
    man = small_manifest()
    defn = definition(man)
    arch = man.architecture("primary")
    coins = {c: coin_data(man, "hyperliquid", c, seed=i + 1) for i, c in enumerate(man.coins)}
    return defn, arch, coins, collect(defn, arch, "hyperliquid", coins)


# --------------------------------------------------------------------------- manifest / identity


def test_shipped_manifest_is_valid_and_family_sizes_are_preregistered():
    man = load_manifest(MANIFEST)
    prim, sec = man.architecture("primary"), man.architecture("secondary")
    assert (prim.structure_tf, prim.event_tf, prim.confirm_tf, prim.resolve_tf) == (
        "4h",
        "1h",
        "1h",
        "15m",
    )
    assert family_sizes(prim) == {"primary": 38, "contrasts": 6, "subgroups": 54}
    assert family_sizes(sec) == {"primary": 36}
    # one-at-a-time neighbours: 17 chain variants per level kind, never a grid
    assert {k: len(prim.variants(k)) for k in ("swing", "prior_extreme", "cluster")} == {
        "swing": 17,
        "prior_extreme": 17,
        "cluster": 17,
    }
    assert all(len(sec.variants(k)) == 1 for k in ("swing", "prior_extreme", "cluster"))
    # both windows end on or before 2026-10-01; venues' outcome windows never overlap
    hl, bn = prim.window("hyperliquid"), prim.window("binance")
    assert bn.event_end <= hl.event_start and hl.event_end <= datetime(2026, 10, 1, tzinfo=UTC)


def test_axis_central_value_must_sit_in_the_middle():
    with pytest.raises(ValueError, match="middle"):
        small_manifest(architectures=[{**small_manifest().architecture("primary").model_dump(mode="json"),
                                       "axes": [{"name": "overshoot_atr", "values": [0.1, 0.25, 0.5]}]}])  # fmt: skip


def test_overlapping_venue_windows_are_refused():
    man = small_manifest()
    d = man.model_dump(mode="json")
    d["venues"].append({**d["venues"][0], "venue": "binance"})
    a = d["architectures"][0]
    a["windows"].append({**a["windows"][0], "venue": "binance"})
    with pytest.raises(ValueError, match="overlap"):
        StudyManifest.model_validate(d)


def test_definition_is_exploratory_records_latency_and_is_content_addressed():
    man = small_manifest()
    d1 = definition(man)
    assert d1.evidence_class == "EXPLORATORY" and d1.validated_reachable is False
    assert d1.availability_mode == "assumed" and man.assumed_latency_s == 60.0
    assert "latency assumption" in d1.availability_statement
    with pytest.raises(ValueError):
        StudyDefinition.model_validate(
            {**d1.model_dump(mode="json"), "evidence_class": "VALIDATED"}
        )
    # a different dataset (exact source) or cost is a different study
    other = d1.model_copy(update={"datasets": tuple(
        r.model_copy(update={"dataset_id": "dataset_" + "f" * 64}) if i == 0 else r
        for i, r in enumerate(d1.datasets))})  # fmt: skip
    assert StudyDefinition.model_validate(other.model_dump()).study_id != d1.study_id
    assert definition(man, fee=5.0).study_id != d1.study_id
    assert definition(man).study_id == d1.study_id


def test_event_identity_carries_dataset_and_assumed_latency(collected):
    _, arch, coins, _ = collected
    s = coins["BTC"].series["1h"]
    assert s.prov.dataset_key == "dataset_test" and s.prov.assumed_latency_s == 60.0
    res = run_chain(
        arch.chain_spec("hyperliquid", "swing", arch.central), coins["BTC"].series["4h"], s, s
    )
    assert (res.breaches["assumed_latency_s"] == 60.0).all()
    assert (res.breaches["dataset_key"] == "dataset_test").all()
    s2 = series("1h", s.open_time[0], int(s.close_time[-1]), 0, latency=120.0)
    assert s2.prov != s.prov  # a different latency is a different provenance (and event ID)


# --------------------------------------------------------------------------- entries / returns


def test_each_rung_enters_only_after_it_is_observable(collected):
    _, _, coins, col = collected
    ev = col.frame("events")
    assert len(ev) and set(ev["rung"]) >= {"A_breach", "B_failed", "H_held"}
    ok = ev[ev["entry_ns"] != NAT]
    assert (ok["entry_ns"] >= ok["available_ns"]).all()
    # the entry is the FIRST bar opening at/after availability (60 s latency: never the
    # bar that opened at the event close)
    s = coins["BTC"].series["1h"]
    b = ok[ok["coin"] == "BTC"]
    e = b["e_idx"].to_numpy()
    assert (s.open_time[e] >= b["available_ns"].to_numpy()).all()
    assert (s.open_time[e - 1] < b["available_ns"].to_numpy()).all()
    # later rungs are never back-dated: availability is monotone along each chain
    central = ev[(ev["kind"] == "swing") & (ev["direction"] == "reversal") & (ev["horizon"] == 24)]
    by = {r: g.set_index(["coin", "breach_id"])["available_ns"] for r, g in central.groupby("rung")}
    for a, b_ in pairwise(LADDER):
        if a in by and b_ in by:
            j = by[b_].to_frame("later").join(by[a].rename("earlier"), how="inner")
            assert len(j) == by[b_].index.nunique()  # every later event has its predecessor
            assert (j["later"] >= j["earlier"]).all()


def test_rungs_are_linked_subsets_and_held_is_disjoint_from_failed(collected):
    _, arch, coins, _ = collected
    c = coins["BTC"]
    res = run_chain(
        arch.chain_spec("hyperliquid", "cluster", arch.central),
        c.series["4h"],
        c.series["1h"],
        c.series["1h"],
    )
    r = pop.rung_frames(res)
    a, b, h = (
        set(r["A_breach"]["breach_id"]),
        set(r["B_failed"]["breach_id"]),
        set(r["H_held"]["breach_id"]),
    )
    assert b <= a and h <= a and not (b & h)
    assert set(r["C_rejection"]["failed_id"]) <= set(r["B_failed"]["key"])
    assert set(r["D_shift"]["failed_id"]) <= set(r["C_rejection"]["failed_id"])
    assert set(r["E_retest"]["failed_id"]) <= set(r["D_shift"]["failed_id"])
    # C shares B's availability (rejection is decided on bars up to the failure bar)
    bc = r["B_failed"].set_index("key")["available_ns"].reindex(r["C_rejection"]["failed_id"])
    assert (bc.to_numpy() == r["C_rejection"]["available_ns"].to_numpy()).all()


def test_entry_return_cost_and_funding_by_hand():
    t0 = pd.Timestamp("2026-03-02 00:00", tz="UTC")
    rows = [(100, 101, 99, 100), (100, 103, 99, 102), (102, 104, 101, 103), (103, 106, 102, 105),
            (105, 107, 104, 106)]  # fmt: skip
    df = pd.DataFrame({"open_time": [t0 + i * H for i in range(5)], "close_time": [t0 + (i + 1) * H for i in range(5)],
                       "open": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows],
                       "close": [r[3] for r in rows], "volume": 1.0})  # fmt: skip
    s = BarSeries.from_frame(df, venue="v", coin="X", timeframe="1h", availability=assumed(60))
    # event on bar 0, available at its close + 60 s -> entry at bar 2's open (102), 2-bar hold -> close of bar 3 (105)
    after = np.array([int(s.ready_at[0])])
    o = pop.outcomes(s, after, np.array([1]), 2)
    assert o.e_idx[0] == 2 and o.entry_price[0] == 102.0
    assert o.gross[0] == pytest.approx(105 / 102 - 1)
    short = pop.outcomes(s, after, np.array([-1]), 2)
    assert short.gross[0] == pytest.approx(-(105 / 102 - 1))  # one sign convention
    assert np.isnan(pop.outcomes(s, after, np.array([1]), 4).gross[0])  # past the data: NaN
    # trade_path_v1 measures the same entry and terminal return
    paths, _ = tp.trade_paths(s, pd.DataFrame({"event_id": ["e"], "entry_after_ns": after, "direction": [1]}),
                              TradePathParams(horizons=(2,), pct_levels=(1.0,), atr_levels=(1.0,)))  # fmt: skip
    assert paths["terminal_return"].iloc[0] == pytest.approx(o.gross[0])
    # funding over (entry, exit]: settlements at 03:00 and 04:00 (exit at 04:00), longs pay
    ft = np.array([int((t0 + k * H).value) for k in range(6)], dtype=np.int64)
    rate = np.array([1, 1, 1, 2, 3, 9], dtype=float) * 1e-4
    paid = pop.funding_paid(ft, rate, o.entry_ns, o.exit_ns, np.array([1]))
    assert paid[0] == pytest.approx(5e-4)
    assert pop.funding_paid(ft, rate, o.entry_ns, o.exit_ns, np.array([-1]))[0] == pytest.approx(
        -5e-4
    )
    assert np.isnan(pop.funding_paid(ft[:3], rate[:3], o.entry_ns, o.exit_ns, np.array([1]))[0])


def test_net_is_gross_minus_two_sides_of_frozen_costs(collected):
    defn, _, _, col = collected
    ev = col.frame("events")
    ev = ev[np.isfinite(ev["gross"])]
    per_side = defn.cost("hyperliquid", "BTC").per_side
    assert per_side == pytest.approx((4.5 + 2.0) / 1e4)
    btc = ev[ev["coin"] == "BTC"]
    assert np.allclose(btc["net"], btc["gross"] - 2 * per_side)
    assert np.allclose(btc["net_f"], btc["net"] - btc["fund"], equal_nan=True)


def test_reversal_and_continuation_signs():
    side = np.array(["high", "low"])
    assert list(pop.direction(side, "reversal")) == [-1, 1]
    assert list(pop.direction(side, "continuation")) == [1, -1]


def test_mirrored_market_gives_mirrored_reversal_returns():
    """Price mirror p -> k - p turns high-side events into low-side ones with the same
    reversal outcome magnitude in ATR terms; bullish and bearish cannot diverge."""
    s = series("1h", "2026-02-01", "2026-03-01", 3)
    k = 400.0
    m = BarSeries.from_frame(pd.DataFrame({
        "open_time": pd.to_datetime(s.open_time, utc=True), "close_time": pd.to_datetime(s.close_time, utc=True),
        "open": k - s.o, "high": k - s.l, "low": k - s.h, "close": k - s.c, "volume": s.v}),
        venue="hyperliquid", coin="BTC", timeframe="1h", availability=assumed(60.0), dataset_key="dataset_test")  # fmt: skip
    c = small_manifest().architecture("primary").central
    spec = ChainSpec.model_validate(
        {
            **small_manifest()
            .architecture("primary")
            .chain_spec("hyperliquid", "swing", c)
            .model_dump(),
            "structure_tf": "1h",
        }
    )
    a, b = pop.rung_frames(run_chain(spec, s, s, s)), pop.rung_frames(run_chain(spec, m, m, m))
    fa = a["B_failed"].sort_values("available_ns")
    fb = b["B_failed"].sort_values("available_ns")
    assert len(fa) == len(fb) and len(fa)
    assert (
        fa["side"].map({"high": "low", "low": "high"}).to_numpy() == fb["side"].to_numpy()
    ).all()
    assert (fa["available_ns"].to_numpy() == fb["available_ns"].to_numpy()).all()
    da, db = (
        pop.direction(fa["side"].to_numpy(), "reversal"),
        pop.direction(fb["side"].to_numpy(), "reversal"),
    )
    assert (da == -db).all()
    ga = pop.outcomes(s, fa["available_ns"].to_numpy(), da, 4)
    gb = pop.outcomes(m, fb["available_ns"].to_numpy(), db, 4)
    # in price units the mirrored move is identical (returns differ only by the price level)
    move_a = da * (s.c[ga.e_idx + 3] - s.o[ga.e_idx])
    move_b = db * (m.c[gb.e_idx + 3] - m.o[gb.e_idx])
    assert np.allclose(move_a, move_b)


# --------------------------------------------------------------------------- statistics


def test_independent_events_decluster_per_coin_and_direction(collected):
    _, _, _, col = collected
    ev = col.frame("events")
    for (_k, _r, _w, h, _c, _d), g in ev.groupby(
        ["kind", "rung", "direction", "horizon", "coin", "d"]
    ):
        ind = np.sort(g[g["independent"]]["e_idx"].to_numpy())
        assert len(np.unique(ind)) == len(ind)  # one event per entry bar
        assert (np.diff(ind) >= h).all()  # no overlapping holding windows
        assert g[g["independent"]]["evaluable"].all()


def test_decluster_hand_example():
    from market_signal.backtest.events import decluster

    assert list(decluster(np.array([0, 3, 10, 30, 33, 60]), 24)) == [0, 30, 60]


def test_matched_null_and_bh_hand_examples():
    rng = np.random.default_rng(0)
    pool = {("X", 1, 0): np.array([-1.0, 0.0, 1.0, -0.5, 0.5])}
    ind = pd.DataFrame({"coin": ["X"] * 5, "d": [1] * 5, "vol": [0] * 5})
    null = an.null_means(ind, pool, ("coin", "d", "vol"), 50, rng)
    assert np.allclose(null, 0.0)  # all 5 of 5 drawn without replacement: the centred mean
    assert an.null_means(pd.DataFrame({"coin": ["X"] * 6, "d": [1] * 6, "vol": [0] * 6}), pool,
                         ("coin", "d", "vol"), 5, rng) is None  # never padded  # fmt: skip
    assert an.p_greater(0.5, np.array([0.0, 0.1, 0.6])) == pytest.approx(2 / 4)
    assert an.p_two_sided(0.0, np.zeros(9)) == 1.0
    from market_signal.research.lab.batch import benjamini_hochberg

    q = benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
    assert q == pytest.approx({"a": 0.04, "b": 0.04 * 4 / 3, "c": 0.04 * 4 / 3, "d": 0.5})


def test_sparse_stage_is_insufficient_never_tested():
    st = StatisticsSpec()
    ev = pd.DataFrame({"coin": ["A", "B", "C"], "evaluable": True, "independent": True, "gross": 0.01,
                       "net": 0.01, "fund": 0.0, "net_f": 0.01, "excess": 0.02})  # fmt: skip
    s = an.summary(ev, st, np.random.default_rng(0))
    assert not s["testable"] and not s["sample_gates"]["min_independent_events"]
    s["untestable_reason"] = "sample gate"
    assert vd.verdict(s, None, None, None, st)[0] == vd.INSUFFICIENT


def test_families_are_per_venue_and_architecture(collected):
    defn, arch, _coins, col = collected
    out = an.analyse_venue(defn, arch, col)
    fam = out["family_primary"]
    assert fam["preregistered"] == family_sizes(arch)["primary"]
    members = fam["members"]
    assert all(m["hypothesis"].startswith("hyperliquid|primary|") for m in members)
    tested = [m for m in members if m["in_family"]]
    assert fam["m"] == len(tested) == sum(m["p_value"] is not None for m in members)
    assert all((m["q_value"] is None) == (not m["in_family"]) for m in members)
    assert out["family_contrasts"]["preregistered"] == 6
    assert out["family_subgroups"]["preregistered"] == 54


def test_venues_are_never_pooled():
    """Two venues -> two separate analyses and families; a series refuses mixed sources."""
    man = small_manifest()
    d = man.model_dump(mode="json")
    d["venues"].append({**d["venues"][0], "venue": "binance", "start_4h": "2025-09-01T00:00:00Z",
                        "start_1h": "2025-10-01T00:00:00Z", "start_15m": "2025-11-01T00:00:00Z",
                        "start_funding": "2025-10-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"})  # fmt: skip
    d["architectures"][0]["windows"].append({"venue": "binance", "event_start": "2025-11-15T00:00:00Z",
                                             "event_end": "2026-01-01T00:00:00Z"})  # fmt: skip
    d["architectures"][0]["axes"] = []
    man2 = StudyManifest.model_validate(d)
    from market_signal.research.structure.study.run import evaluate

    loads = []

    def load(venue, coin):
        loads.append((venue, coin))
        return coin_data(man2, venue, coin, seed=len(loads))

    payload, _ = evaluate(definition(man2), load)
    venues = payload["architectures"]["primary"]["venues"]
    assert set(venues) == {"hyperliquid", "binance"}
    for v, out in venues.items():
        assert all(m["hypothesis"].startswith(f"{v}|") for m in out["family_primary"]["members"])
        assert set(out["inputs"]) == {"BTC", "ETH"}
    assert all(r["label"] in ("SIMILAR", "MIXED", "OPPOSITE", "INSUFFICIENT")
               for r in payload["architectures"]["primary"]["cross_venue"])  # fmt: skip
    mixed = pd.DataFrame({"open_time": pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC"),
                          "close_time": pd.date_range("2026-01-01 01:00", periods=2, freq="1h", tz="UTC"),
                          "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "source": ["binance", "hyperliquid"]})  # fmt: skip
    with pytest.raises(ValueError, match="never mix venues"):
        BarSeries.from_frame(
            mixed, venue="binance", coin="X", timeframe="1h", availability=assumed(60)
        )


def test_result_payload_is_deterministic_and_carries_no_clock():
    """Two evaluations of the same inputs give the same digest; timings live in meta only."""
    from market_signal.research.structure.study.run import digest, evaluate

    man = small_manifest(architectures=[{**small_manifest().architecture("primary").model_dump(mode="json"),
                                         "axes": [], "subgroups": False}])  # fmt: skip
    defn = definition(man)

    def load(venue, coin):
        return coin_data(man, venue, coin, seed=7 if coin == "BTC" else 8)

    (p1, m1), (p2, _) = evaluate(defn, load), evaluate(defn, load)
    assert digest(p1) == digest(p2)
    assert "seconds" in canonical_json(m1) and "seconds" not in canonical_json(p1)


def test_unresolved_same_bar_order_is_never_an_outcome():
    ev_ind = pd.DataFrame({"rung": "B_failed", "direction": "reversal", "key": ["k1", "k2", "k3"],
                           "coin": "X"})  # fmt: skip
    pid = ["B_failed|reversal|k1", "B_failed|reversal|k2", "B_failed|reversal|k3"]
    paths = pd.DataFrame({"event_id": pid, "complete": True, "risk_status": "OK", "risk_pct": 1.0,
                          "risk_atr": 1.0, "r_terminal": [0.2, 0.3, -0.4], "r_mfe": 1.0})  # fmt: skip
    thr = pd.DataFrame({"event_id": pid, "criterion": "r_1",
                        "order": [tp.FAV, tp.AMBIGUOUS, tp.FAV],
                        "resolution": [None, "INCOMPLETE_COVERAGE", "RESOLVED"]})  # fmt: skip
    thr = pd.concat([thr] + [thr.assign(criterion=f"r_{k}", order=tp.NEITHER, resolution=None)
                             for k in (2, 3, 5)])  # fmt: skip
    r = an.r_summary(ev_ind, paths, thr, pd.Series({"X": 0.0}))["targets"]["1R"]
    assert r["favourable_first"] == 2 and r["stop_first"] == 0 and r["unresolved"] == 1
    assert r["ambiguous_raw"] == 2 and r["resolved_by_child_bars"] == 1
    assert r["p_target_first_lower"] == pytest.approx(2 / 3)
    assert r["p_target_first_upper"] == pytest.approx(1.0)
    assert r["p_target_first_excl_unresolved"] == pytest.approx(1.0)
    assert r["net_r_mean_lower"] == pytest.approx((1 - 1 + 1) / 3)
    assert r["net_r_mean_upper"] == pytest.approx(1.0)


def test_transition_entry_cost_by_hand():
    """B entered long at 100, D entered at 102 two hours later: +200 bps worse, +2 ATR."""
    st = StatisticsSpec()
    rows = []
    for rung, price, ns in (("B_failed", 100.0, 0), ("D_shift", 102.0, int(2 * 3.6e12))):
        rows.append({"kind": "swing", "rung": rung, "direction": "reversal", "coin": "X", "breach_id": "b1",
                     "key": rung, "d": 1, "entry_price": price, "entry_ns": ns, "atr": 1.0,
                     "independent": True, "evaluable": True, "excess": 0.0})  # fmt: skip
    evp = pd.DataFrame(rows)
    blank = {"independent_events": 1, "events_in_window": 1, "excess_mean": 0.0, "hit_rate": 0.0,
             "net_p10": 0.0, "paths": {}}  # fmt: skip
    ladder = {k: {w: {r: blank for r in (*LADDER, "H_held")} for w in ("reversal", "continuation")}
              for k in ("swing", "prior_extreme", "cluster")}  # fmt: skip
    rows = an.transitions(evp, ladder, st)
    assert not [r for r in rows if r.get("linked_chains")]  # B->D is not an adjacent pair
    evp2 = pd.concat([evp, evp.iloc[[0]].assign(rung="C_rejection", key="C")])
    rows = an.transitions(evp2, ladder, st)
    cd = next(
        r
        for r in rows
        if r["kind"] == "swing" and r["direction"] == "reversal" and r["to"] == "D_shift"
    )
    assert cd["entry_delay_hours_median"] == pytest.approx(2.0)
    assert cd["entry_cost_bps_median"] == pytest.approx(200.0)
    assert cd["entry_cost_atr_median"] == pytest.approx(2.0)
    short = an.transitions(evp2.assign(d=-1), ladder, st)
    cd = next(
        r
        for r in short
        if r["kind"] == "swing" and r["direction"] == "reversal" and r["to"] == "D_shift"
    )
    assert cd["entry_cost_bps_median"] == pytest.approx(
        -200.0
    )  # a higher price is BETTER for a short


def test_verdict_policy():
    st = StatisticsSpec()
    base = {"testable": True, "q_value": 0.01, "p_value": 0.001, "excess_mean": 0.003, "net_mean": 0.002,
            "positive_asset_share": 0.8, "leave_largest_asset_out": {"sign_survives": True},
            "excess_ci95": [0.001, 0.005]}  # fmt: skip
    other = {"testable": True, "excess_mean": 0.002, "p_value": 0.01}
    assert vd.verdict(base, None, "PLATEAU", other, st)[0] == vd.ROBUST
    assert vd.verdict(base, None, "MIXED", other, st)[0] == vd.PROMISING
    assert vd.verdict({**base, "net_mean": -0.001}, None, "PLATEAU", other, st)[0] == vd.WEAK
    ns = {
        **base,
        "q_value": 0.5,
        "p_value": 0.3,
        "excess_mean": -0.001,
        "excess_ci95": [-0.003, 0.0005],
    }
    assert vd.verdict(ns, None, None, None, st)[0] == vd.REJECTED  # a 10 bps effect is excluded
    assert (
        vd.verdict({**ns, "excess_ci95": [-0.003, 0.004]}, None, None, None, st)[0]
        == vd.NO_EVIDENCE
    )
    assert (
        vd.verdict({**ns, "excess_ci95": [-0.003, 0.004]}, base, None, None, st)[0] == vd.REJECTED
    )
    assert "VALIDATED" not in vd.VERDICTS and not any("proven" in v.lower() for v in vd.VERDICTS)


# --------------------------------------------------------------------------- future immunity


def test_appending_future_bars_never_changes_earlier_signals_or_outcomes():
    man = small_manifest()
    defn = definition(man)
    arch = man.architecture("primary")
    full = {
        "BTC": coin_data(man, "hyperliquid", "BTC", seed=5),
        "ETH": coin_data(man, "hyperliquid", "ETH", seed=6),
    }
    cut = pd.Timestamp("2026-03-15", tz="UTC").value
    short = {}
    for c, cd in full.items():
        s = {
            tf: x.head(int(np.searchsorted(x.close_time, cut, side="right")))
            for tf, x in cd.series.items()
        }
        short[c] = CoinData(cd.venue, c, cd.dataset_id, s, cd.funding_ns, cd.funding_rate)
    a = collect(defn, arch, "hyperliquid", short).frame("events")
    b = collect(defn, arch, "hyperliquid", full).frame("events")
    a = a[np.isfinite(a["gross"])]
    key = ["kind", "rung", "direction", "horizon", "coin", "key"]
    j = a.merge(b, on=key, suffixes=("_a", "_b"), how="left")
    assert len(j) == len(a) and len(a)
    for c in ("available_ns", "entry_ns", "entry_price", "gross", "net"):
        assert np.allclose(j[f"{c}_a"].astype(float), j[f"{c}_b"].astype(float)), c


# --------------------------------------------------------------------------- governance


def _bars_store(store, man: StudyManifest):
    from market_signal.intraday import bars as ib
    from market_signal.models.domain import Timeframe

    seen = datetime(2026, 10, 5, tzinfo=UTC)
    for v in man.venues:
        for i, coin in enumerate(man.coins):
            for j, tf in enumerate(("4h", "1h", "15m")):
                s = series(
                    tf,
                    pd.Timestamp(v.start(tf)) - pd.Timedelta(tf),
                    v.end,
                    10 * i + j,
                    venue=v.venue,
                    coin=coin,
                )
                df = pd.DataFrame({"open_time": pd.to_datetime(s.open_time, utc=True), "open": s.o, "high": s.h,
                                   "low": s.l, "close": s.c, "volume": s.v, "trades": 1})  # fmt: skip
                ib.upsert_bars(
                    store, v.venue, coin, Timeframe(tf), df, observed_at=seen, run_id="run_test"
                )
            t = pd.date_range(v.start_funding, v.end, freq="1h", inclusive="left")
            store.con.executemany(
                "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
                [
                    [
                        coin,
                        v.venue,
                        x.to_pydatetime(),
                        1e-5,
                        None,
                        x.to_pydatetime(),
                        "test",
                        "run_test",
                    ]
                    for x in t
                ],
            )


SOFT = SoftwareIdentity(label="test", python_version="3.12")


def test_governed_lifecycle_preregistration_and_rerun(store):
    from market_signal.research.lab import structure_study as ss

    man = small_manifest()
    ledger = Ledger(store)
    with pytest.raises(ss.StudyError, match="unknown study"):
        ss.run(ledger, "sstudy_" + "0" * 64, software=SOFT)
    _bars_store(store, man)
    cfg = {"costs": {"taker_fee_bps": 4.5, "slippage_bps": {"default": 8}}}
    defn = ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="test")
    assert {c.fee_bps for c in defn.costs} == {4.5}
    with pytest.raises(ss.StudyError, match="already frozen"):
        ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="again")
    seen = {}

    def spy(d, load):
        # the run row is committed BEFORE evaluation reads anything
        seen["runs"] = store.con.execute("SELECT count(*) FROM lab_structure_runs").fetchone()[0]
        seen["results"] = store.con.execute(
            "SELECT count(*) FROM lab_structure_results"
        ).fetchone()[0]
        cd = load("hyperliquid", "BTC")
        seen["dataset_key"] = cd.series["1h"].prov.dataset_key
        return {"ok": 1}, {"wall_seconds": 0.0}

    out = ss.run(ledger, defn.study_id, software=SOFT, evaluate=spy)
    assert out["status"] == "COMPLETED" and seen["runs"] == 1 and seen["results"] == 0
    assert seen["dataset_key"] == defn.dataset("hyperliquid", "BTC")
    with pytest.raises(ss.StudyError, match="explicit rerun"):
        ss.run(ledger, defn.study_id, software=SOFT, evaluate=spy)
    again = ss.run(ledger, defn.study_id, software=SOFT, evaluate=spy, rerun_of=out["run_id"],
                   rerun_reason="reproducibility")  # fmt: skip
    assert again["result_digest"] == out["result_digest"]
    info = ss.inspect(ledger, defn.study_id)
    assert [r["attempt"] for r in info["runs"]] == [1, 2] and info[
        "evidence_class"
    ] == "EXPLORATORY"
    # a result row cannot exist without a run of a frozen study (FK), nor claim validation
    import duckdb

    with pytest.raises(duckdb.Error):
        store.con.execute("INSERT INTO lab_structure_results VALUES ('r','srun_missing',now(),'COMPLETED',"
                          "'EXPLORATORY','x','{}','{}')")  # fmt: skip
    with pytest.raises(duckdb.Error):
        store.con.execute("INSERT INTO lab_structure_results VALUES ('r2',?,now(),'COMPLETED','VALIDATED',"
                          "'x','{}','{}')", [out["run_id"]])  # fmt: skip


def test_failed_evaluation_is_recorded_not_hidden(store):
    from market_signal.research.lab import structure_study as ss

    man = small_manifest(name="p17_fail")
    ledger = Ledger(store)
    _bars_store(store, man)
    cfg = {"costs": {"taker_fee_bps": 4.5}}
    defn = ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="test")

    def boom(d, load):
        raise RuntimeError("boom")

    out = ss.run(ledger, defn.study_id, software=SOFT, evaluate=boom)
    assert out["status"] == "FAILED" and out["result_digest"] is None
    assert "boom" in ss.result_payload(ledger, out["run_id"])["error"]["message"]


def test_dataset_windows_are_verified_against_the_frozen_manifest(store):
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.datasets import capture_dataset

    man = small_manifest(name="p17_ds")
    ledger = Ledger(store)
    _bars_store(store, man)
    defn = ss.register(ledger, man, perps_cfg={}, software=SOFT, origin="test", reason="test")
    sel = ss.selections(man, "hyperliquid", "BTC")
    shifted = tuple(s.model_copy(update={"start": s.start + pd.Timedelta(hours=4)}) for s in sel)
    wrong = ledger.register_dataset(capture_dataset(store, shifted))
    bad = defn.model_copy(update={"datasets": tuple(
        r.model_copy(update={"dataset_id": wrong}) if (r.venue, r.coin) == ("hyperliquid", "BTC") else r
        for r in defn.datasets)})  # fmt: skip
    with pytest.raises(ss.StudyError, match="frozen"):
        ss.verify_datasets(ledger, bad)
    ss.verify_datasets(ledger, defn)
    # retained rows are what evaluation reads: identical to the store's bars at capture
    from market_signal.research.structure.study.data import load_coin

    cd = load_coin(
        ledger, defn.dataset("hyperliquid", "ETH"), venue="hyperliquid", coin="ETH", latency_s=60.0
    )
    assert cd.series["1h"].prov.availability_mode == "assumed" and len(cd.funding_ns)
    assert canonical_json(sorted(cd.series)) == canonical_json(["15m", "1h", "4h"])


# --------------------------------------------------------------------------- isolation


def test_no_live_consumer_reads_phase17():
    for pkg in ("paper", "copilot"):
        for f in (SRC / pkg).rglob("*.py"):
            text = f.read_text()
            assert "structure_study" not in text and "structure.study" not in text, f
    for f in (SRC / "research" / "lab" / "forward.py", SRC / "ops" / "runtime.py",
              SRC / "research" / "lab" / "corroboration.py", SRC / "research" / "lab" / "validation.py",
              SRC / "research" / "lab" / "evidence.py"):  # fmt: skip
        text = f.read_text()
        assert "structure_study" not in text and "research.structure" not in text, f
    for f in (SRC / "research" / "structure" / "study").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "market_signal.research.lab.forward", "telegram", "httpx", "INSERT ", "UPDATE "):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(
        "study" in " ".join(map(str, args)) for job in rt.JOBS.values() for _, args in job
    )


def test_central_chain_rejects_delayed_rejection():
    with pytest.raises(ValueError, match="within_bars"):
        CentralChain(rejection={"min_wick_range": 0.5, "within_bars": 1})
    arch = small_manifest().architecture("primary")
    assert isinstance(arch, Architecture)
