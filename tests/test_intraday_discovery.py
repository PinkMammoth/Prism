"""Phase 22 intraday strategy discovery: primitives, catalogue, timing, symmetry, statistics,
verdicts, clustering, ensemble, calibration, governance and isolation.

Synthetic fixtures only. Nothing here reads real market data, and no test result says
anything about whether a strategy works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.research.discovery import analysis as an
from market_signal.research.discovery import catalogue as cat
from market_signal.research.discovery import collect as co
from market_signal.research.discovery import primitives as pr
from market_signal.research.discovery import signals as sg
from market_signal.research.discovery import synthetic as sy
from market_signal.research.discovery.spec import DiscoveryManifest, load_manifest
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.structure.series import BarSeries, assumed
from market_signal.research.structure.study.data import CoinData

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
MANIFEST = ROOT / "config" / "discovery" / "phase22_intraday_discovery.v1.yaml"
SOFT = SoftwareIdentity(label="test", python_version="3.12")
DAY = 86_400 * 10**9
H = 3600 * 10**9


# --------------------------------------------------------------------------- fixtures


def series(c, *, tf="1h", start="2026-01-01", h=None, l=None, o=None, v=None, coin="BTC",  # noqa: E741
           venue="binance", skip=()) -> BarSeries:  # fmt: skip
    c = np.asarray(c, float)
    o = np.concatenate([[c[0]], c[:-1]]) if o is None else np.asarray(o, float)
    h = np.maximum(o, c) * 1.001 if h is None else np.asarray(h, float)
    l = np.minimum(o, c) * 0.999 if l is None else np.asarray(l, float)  # noqa: E741
    v = np.full(len(c), 100.0) if v is None else np.asarray(v, float)
    h, l = np.maximum(h, np.maximum(o, c)), np.minimum(l, np.minimum(o, c))  # noqa: E741
    step = pd.Timedelta(tf)
    t = pd.date_range(start, periods=len(c) + len(skip), freq=step, tz="UTC")
    keep = np.array([i not in set(skip) for i in range(len(t))])
    t = t[keep]
    df = pd.DataFrame({"open_time": t, "close_time": t + step, "open": o, "high": h, "low": l,
                       "close": c, "volume": v})  # fmt: skip
    return BarSeries.from_frame(df, venue=venue, coin=coin, timeframe=tf, availability=assumed(60))


def mini_manifest(**over) -> DiscoveryManifest:
    raw = {
        "name": "p22_mini", "description": "synthetic test manifest",
        "venues": [{"venue": "binance", "role": "discovery", "coins": list(sy.COINS),
                    "data_start": {"15m": "2026-04-20T00:00:00Z", "1h": "2026-03-01T00:00:00Z",
                                   "4h": "2026-03-01T00:00:00Z"},
                    "funding_start": "2026-02-01T00:00:00Z",
                    "event_start": "2026-05-20T00:00:00Z", "event_end": "2026-10-01T00:00:00Z"}],
        "windows": {"contemporary_start": "2026-05-20T00:00:00Z",
                    "recent_180_start": "2026-06-01T00:00:00Z",
                    "recent_90_start": "2026-07-03T00:00:00Z",
                    "recent_30_start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    }  # fmt: skip
    raw.update(over)
    return DiscoveryManifest.model_validate(raw)


def market(seed=7, man=None, funding_mean=1e-4):
    man = man or mini_manifest()
    v = man.discovery
    starts = {"15m": v.data_start.m15, "1h": v.data_start.h1, "4h": v.data_start.h4}
    raw = sy.generate(v.data_start.h1, v.event_end, seed, funding_mean=funding_mean)
    return raw, {c: sy.coin_data(r, "binance", starts) for c, r in raw.items()}, starts


def venue(data, per_side=0.001):
    return co.VenueData("binance", {c: co.CoinInputs(c, d.series, d.funding_ns, d.funding_rate, per_side)
                                    for c, d in data.items()})  # fmt: skip


@pytest.fixture(scope="module")
def synthetic():
    _, data, _ = market(seed=7)
    return data


# --------------------------------------------------------------------------- primitives


def test_range_width_and_position_by_hand():
    h = np.array([10, 12, 11, 13, 12, 14.0])
    lo = np.array([9, 10, 10, 11, 10, 12.0])
    seg = np.zeros(6, int)
    rh, rl = pr.roll_high(h, seg, 3), pr.roll_low(lo, seg, 3)
    # the range of bar i is the PRIOR three bars i-3..i-1
    assert np.isnan(rh[:3]).all() and rh[3] == 12 and rl[3] == 9 and rh[5] == 13 and rl[5] == 10
    f = pr.Frame(
        o=h, h=h, l=lo, c=np.array([9.5, 11, 10.5, 12, 11, 13.0]), v=np.ones(6), segment=seg
    )
    assert f.range_width(3)[3] == pytest.approx(np.log(12 / 9))
    assert f.range_pos(3)[3] == pytest.approx(np.log(12 / 9) / np.log(12 / 9))  # close == high
    assert f.range_pos(3)[4] == pytest.approx(np.log(11 / 10) / np.log(13 / 10))
    assert f.range_mid(3)[3] == pytest.approx(np.sqrt(12 * 9))
    # a gap inside the window makes the range undefined
    seg2 = np.array([0, 0, 1, 1, 1, 1])
    assert np.isnan(pr.roll_high(h, seg2, 3)[3]) and pr.roll_high(h, seg2, 3)[5] == 13


def test_compression_percentile_by_hand():
    x = np.array([5.0, 4, 3, 2, 1, 6])
    r = pr.pct_rank(x, 4)
    assert np.isnan(r[0]) and r[1] == pytest.approx(0.5)  # min_periods w/2
    assert (
        r[3] == pytest.approx(0.25) and r[4] == pytest.approx(0.25) and r[5] == pytest.approx(1.0)
    )
    assert np.isnan(pr.pct_rank(np.array([1.0, np.nan, 2.0]), 2)[1])


def test_relative_volume_and_placeholder_bars():
    v = np.array([10, 10, 10, 10, 40, 0, 10, 10, 10, 10, 10.0])
    seg = np.zeros(len(v), int)
    rv = pr.rel_volume(v, seg, 4)
    assert np.isnan(rv[:4]).all() and rv[4] == pytest.approx(4.0)
    # the zero (placeholder) bar and every window containing it are NaN
    assert np.isnan(rv[5:10]).all() and rv[10] == pytest.approx(1.0)
    z = pr.vol_z(v, seg, 4)
    assert np.isnan(z[5:10]).all()


def test_volume_slope_and_acceleration_by_hand():
    v = np.exp(0.1 * np.arange(20.0))
    seg = np.zeros(20, int)
    s = pr.vol_slope(v, seg, 5)
    assert np.isnan(s[:4]).all() and np.allclose(s[4:], 0.1)
    v2 = np.exp(0.05 * np.arange(20.0) ** 2)
    f = pr.Frame(o=np.ones(20), h=np.ones(20), l=np.ones(20), c=np.ones(20), v=v2, segment=seg)
    acc = f.vol_accel(5)
    assert np.allclose(acc[9:], 0.05 * 2 * 5)  # slope of x^2 grows by 2*n per n bars


def test_efficiency_close_location_and_return_z():
    lc = np.log(np.array([100, 101, 102, 101, 103.0]))
    seg = np.zeros(5, int)
    er = pr.efficiency(lc, seg, 4)
    path = np.sum(np.abs(np.diff(lc)))
    assert er[4] == pytest.approx((lc[4] - lc[0]) / path)
    assert pr.close_location(np.log([10.0]), np.log([8.0]), np.log([9.0]))[0] == pytest.approx(
        np.log(9 / 8) / np.log(10 / 8)
    )
    c = 100 * np.exp(np.cumsum(np.r_[0, np.tile([0.01, -0.01], 30), 0.05, 0.05]))
    z = pr.ret_z(np.log(c), np.zeros(len(c), int), 2, 20)
    vol = np.std(np.diff(np.log(c))[-24:-2], ddof=1)  # the 20 returns BEFORE the move
    assert z[-1] == pytest.approx(0.1 / (np.std(np.diff(np.log(c))[-22:-2], ddof=1) * np.sqrt(2)))
    assert vol > 0


def test_features_are_causal_truncation_invariant(synthetic):
    """No future bar is used: every primitive and signal on the first n bars is identical
    whether or not later bars exist."""
    s = synthetic["ETH"].series["1h"]
    n = len(s) - 500
    full, head = pr.Frame.from_series(s), pr.Frame.from_series(s.head(n))
    for name, fn in (("ret_z", lambda f: f.ret_z(4)), ("ema", lambda f: f.ema(50)),
                     ("comp", lambda f: f.compression(24, 720)), ("rel", lambda f: f.rel_volume(20)),
                     ("vs", lambda f: f.vol_slope_pct(12, 720)), ("pos", lambda f: f.range_pos(24)),
                     ("eff", lambda f: f.efficiency(12)), ("tr", lambda f: f.tr_atr)):  # fmt: skip
        np.testing.assert_array_equal(fn(full)[:n], fn(head), err_msg=name)


# --------------------------------------------------------------------------- catalogue


def test_catalogue_is_frozen_versioned_balanced_and_small():
    ss = cat.strategies()
    assert len(ss) == 125 and len({s.strategy_id for s in ss}) == 125
    assert sum(s.side == "long" for s in ss) == 61 and sum(s.side == "short" for s in ss) == 64
    assert sorted(s.rule for s in ss if not s.symmetric) == sorted(cat.SHORT_ONLY)
    assert all(s.side == "short" for s in ss if not s.symmetric)
    assert cat.strategies() == ss  # deterministic
    for s in ss:
        allowed = cat.HORIZONS_1H if s.timeframe == "1h" else cat.HORIZONS_15M
        assert set(s.horizons) <= set(allowed) and s.primary_horizon in s.horizons
        assert len(s.horizons) == 3 and s.complexity.conditions <= 4
        assert s.entry == cat.ENTRY_RULE and s.feature_version == pr.FEATURES_VERSION
        assert s.bh_family in cat.BH_FAMILIES
    sym = [s for s in ss if s.symmetric]
    pairs = {(s.rule, s.params, s.timeframe, s.context_timeframe) for s in sym}
    assert len(pairs) * 2 == len(sym)  # every symmetric variant has its mirror
    assert (
        cat.catalogue_id()
        == "icat_2444a9f947887c2a99e0bffab941556df1aca0741c485199ebb3c43a5353a397"
    )
    # no tiny correction family manufactures survivors
    fams = cat.summary()["bh_families"]
    assert min(f["long"] + f["short"] for f in fams.values()) >= 4


def test_neighbours_are_one_step_on_one_axis():
    ss = cat.strategies()
    s = next(x for x in ss if x.key == "breakout[n=24]:1h:long")
    assert sorted(n.key for n in cat.neighbours(s, ss)) == ["breakout[n=12]:1h:long",
                                                            "breakout[n=48]:1h:long"]  # fmt: skip
    s = next(x for x in ss if x.key == "breakout[n=12]:1h:long")
    assert [n.key for n in cat.neighbours(s, ss)] == ["breakout[n=24]:1h:long"]


# --------------------------------------------------------------------------- signals


def _ctx(s: BarSeries, **kw):
    return sg.SignalContext(
        tf=s.prov.timeframe, f=pr.Frame.from_series(s), ready_at=s.ready_at, **kw
    )


def _strat(key):
    return next(x for x in cat.strategies() if x.key == key)


def test_breakout_fires_once_on_the_close_above_the_prior_high():
    c = np.r_[np.full(30, 100.0), 101, 102, 103, 100, 100]
    s = series(c, h=np.r_[np.full(30, 100.5), 101.2, 102.2, 103.2, 100.5, 100.5])
    m = sg.signal_mask(_ctx(s), _strat("breakout[n=12]:1h:long"))
    assert np.flatnonzero(m).tolist() == [30]  # edge-triggered: 31, 32 continue the state
    assert not sg.signal_mask(_ctx(s), _strat("breakout[n=12]:1h:short")).any()


def test_range_reversion_needs_the_bottom_of_an_intact_range():
    rng = np.random.default_rng(1)
    c = 100 + rng.uniform(-1, 1, 80)
    c[60] = 99.05  # near the low of the prior 24 bars but inside
    lo = np.minimum(c, np.r_[c[0], c[:-1]]) - 0.01
    lo[:60] = np.minimum(lo[:60], 99.0)
    lo[37] = 98.95
    s = series(c, l=lo, h=np.maximum(c, np.r_[c[0], c[:-1]]) + 0.01)
    f = pr.Frame.from_series(s)
    pos = f.range_pos(24)
    m = sg.signal_mask(_ctx(s), _strat("range_reversion[n=24,max_width_atr=1000.0]:1h:long"))
    for i in np.flatnonzero(m):
        assert 0 <= pos[i] <= 0.1 and not (0 <= pos[i - 1] <= 0.1)


def test_momentum_fires_on_a_strong_move_in_pre_move_vol_units():
    r = np.tile([0.002, -0.002], 40)
    r = np.r_[r, 0.01, 0.01, 0.01, 0.01, 0.0]
    c = 100 * np.exp(np.cumsum(r))
    s = series(c)
    m = sg.signal_mask(_ctx(s), _strat("momentum_z[n=4,k=1.5]:1h:long"))
    z = pr.Frame.from_series(s).ret_z(4)
    assert m.any() and all(z[i] >= 1.5 for i in np.flatnonzero(m))
    assert not sg.signal_mask(_ctx(s), _strat("momentum_z[n=4,k=1.5]:1h:short")).any()


def _mirror_coin(cd: CoinData, ref: float) -> CoinData:
    out = {}
    for tf, s in cd.series.items():
        o, h, l, c = pr.mirror_bars(s.o, s.h, s.l, s.c, ref)  # noqa: E741
        df = pd.DataFrame({"open_time": pd.to_datetime(s.open_time, utc=True),
                           "close_time": pd.to_datetime(s.close_time, utc=True),
                           "open": o, "high": h, "low": l, "close": c, "volume": s.v})  # fmt: skip
        out[tf] = BarSeries.from_frame(df, venue="binance", coin=cd.coin, timeframe=tf,
                                       availability=assumed(60))  # fmt: skip
    return CoinData(cd.venue, cd.coin, cd.dataset_id, out, cd.funding_ns, cd.funding_rate)


def test_long_and_short_rules_are_exact_mirrors(synthetic):
    """The short rule on a price-mirrored market fires exactly where the long rule fires on
    the original (every symmetric rule except the funding rules, whose percentile of a
    negated series is 1 - p only up to ties)."""
    vd = venue(synthetic)
    vm = venue({c: _mirror_coin(d, 100.0) for c, d in synthetic.items()})
    checked = 0
    for s in cat.strategies():
        if not s.symmetric or s.side != "long" or s.bh_family == "funding":
            continue
        short = next(x for x in cat.strategies() if x.key == s.key.replace(":long", ":short"))
        for coin in ("BTC", "SOL"):
            a = sg.signal_mask(vd.context(coin, s.timeframe), s)
            b = sg.signal_mask(vm.context(coin, s.timeframe), short)
            assert (a == b).mean() > 0.9999, (s.key, coin, int((a != b).sum()))
            checked += 1
    assert checked > 100


def test_short_only_rules_read_the_real_downside(synthetic):
    vd = venue(synthetic)
    s = _strat("volume_downside_expansion[r=2.5,body=1.5]:1h:short")
    ctx = vd.context("SOL", "1h")
    m = sg.signal_mask(ctx, s)
    f = ctx.f
    for i in np.flatnonzero(m):
        assert f.body_atr[i] <= -1.5 and f.close_location[i] <= 0.25 and f.rel_volume(20)[i] >= 2.5


# --------------------------------------------------------------------------- timing / outcomes / costs


def test_entry_is_the_next_15m_open_after_availability_and_outcomes_by_hand():
    c15 = 100 * np.exp(np.cumsum(np.r_[0, np.full(40, 0.001)]))
    s15 = series(c15, tf="15m")
    ready = np.array([s15.close_time[3] + 60 * 10**9])  # a 1H bar closing at bar 3's close
    ex = co.execute(s15, ready, 60)
    assert ex.e[0] == 5  # bar 4 opened at the close (before availability); bar 5 is next
    assert ex.entry_ns[0] - s15.close_time[3] == 15 * 60 * 10**9
    assert ex.gross_long[0] == pytest.approx(s15.c[8] / s15.o[5] - 1)
    assert ex.exit_ns[0] == s15.close_time[8]
    # incomplete paths are NaN, never truncated
    assert np.isnan(co.execute(s15, np.array([s15.close_time[-2]]), 60).gross_long[0])
    # a gap inside the holding window -> NaN
    g = series(c15[:20], tf="15m", skip=(10,))
    assert np.isnan(co.execute(g, np.array([g.open_time[7]]), 60).gross_long[0])


def test_costs_and_funding_are_charged_and_signed(synthetic):
    vd = venue(synthetic, per_side=0.0007)
    s = _strat("momentum_z[n=4,k=1.5]:1h:short")
    ev = co.strategy_events(vd, s, pd.Timestamp("2026-06-01", tz="UTC").value,
                            pd.Timestamp("2026-09-01", tz="UTC").value)  # fmt: skip
    e = ev.dropna(subset=["net_4"])
    assert len(e) > 50 and (e["d"] == -1).all()
    np.testing.assert_allclose(e["net_4"], e["gross_4"] - 0.0014 - e["fund_4"])
    # positive funding: a short RECEIVES it (negative funding cost)
    assert (e["fund_4"] <= 1e-12).mean() > 0.95
    assert (e["entry_ns"] > e["signal_close_ns"]).all() and (e["delay_min"] == 15).all()


def test_signal_frequency_and_independence():
    t0 = pd.Timestamp("2026-01-01", tz="UTC").value
    ev = pd.DataFrame({"signal_close_ns": [t0 + k * H for k in (0, 1, 2, 30, 31)],
                       "signal_ns": [t0 + k * H + 60 * 10**9 for k in (0, 1, 2, 30, 31)],
                       "independent": [True, False, False, True, True]})  # fmt: skip
    f = an.frequency(ev, t0, t0 + 4 * DAY, n_coins=2)
    assert f["raw"] == 5 and f["independent"] == 3
    assert f["raw_per_day"] == pytest.approx(5 / 4) and f["independent_per_day"] == pytest.approx(
        3 / 4
    )
    assert f["per_asset_per_day"] == pytest.approx(3 / 8)
    assert f["median_hours_between"] == pytest.approx(15.5)  # gaps 30h, 1h
    assert f["zero_signal_day_share"] == pytest.approx(0.5)  # days 0 and 1 have signals


def test_independent_opportunity_dedup_across_strategies():
    t0 = pd.Timestamp("2026-01-01", tz="UTC").value

    def ev(times, coin="SOL", d=1):
        return pd.DataFrame({"coin": coin, "d": d, "signal_ns": [t0 + x * H for x in times],
                             "signal_close_ns": [t0 + x * H for x in times], "independent": True})  # fmt: skip

    a, b = ev([1, 10, 30]), ev([2, 30.5, 50])
    c = ev([2], d=-1)
    e = an.ensemble([a, b, c], t0, t0 + 4 * DAY, dedup_minutes=240, target=5.0)
    # SOL long: 1, 2(dup), 10, 30, 30.5(dup), 50 -> 4 ; SOL short: 1
    assert e["raw"] == 7 and e["independent"] == 5
    assert e["long_per_day"] == pytest.approx(4 / 4) and e["short_per_day"] == pytest.approx(1 / 4)
    assert e["days_ge1_share"] == pytest.approx(3 / 4) and e["zero_day_share"] == pytest.approx(
        1 / 4
    )


# --------------------------------------------------------------------------- inference


def test_cross_asset_cotimed_events_are_blocked_not_counted_as_independent():
    rng = np.random.default_rng(3)
    days = 60
    shock = rng.normal(0, 0.02, days)
    t = np.repeat(np.arange(days) * DAY + 12 * H, 6)
    net = np.repeat(shock, 6) + rng.normal(0, 0.002, days * 6) + 0.003
    ev = pd.DataFrame({"coin": np.tile(list(sy.COINS), days), "signal_ns": t, "signal_close_ns": t,
                       "independent": True, "net_4": net, "gross_4": net, "fund_4": 0.0,
                       "excess_4": 0.0, "cost": 0.0, "d": 1})  # fmt: skip
    s = an.stats(ev, 4)
    iid_t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))
    assert s["clusters"] <= days and abs(s["t"]) < abs(iid_t) / 1.8


def test_bh_runs_within_frozen_families_and_untestable_keeps_m():
    man = mini_manifest()
    ss = cat.strategies()
    smap = {s.key: s for s in ss}
    rows = {}
    for s in ss:
        p = 0.0001 if s.key == "breakout[n=24]:1h:long" else 0.5
        n = 0 if s.bh_family == "funding" else 100
        rows[s.key] = {"contemporary": {"n": n, "assets": 6, "clusters": 50, "p_one_sided": p}}
    bh = an.apply_bh(rows, smap, man)
    assert bh["breakout"]["m"] == 11 and bh["breakout"]["discoveries"] == ["breakout[n=24]:1h:long"]
    assert rows["breakout[n=24]:1h:long"]["q_value"] == pytest.approx(0.0001 * 11)
    assert bh["funding"]["testable"] == 0 and all(rows[k]["q_value"] == 1.0 for k in rows
                                                    if smap[k].bh_family == "funding")  # fmt: skip
    assert sum(f["m"] for f in bh.values()) == 125


# --------------------------------------------------------------------------- verdicts


def _w(n=100, mean=0.003, t=2.5, **kw):
    base = {"n": n, "assets": 5, "net_mean": mean, "t": t, "loo_mean": mean, "top5_share": 0.2,
            "positive_asset_share": 0.8, "gross_mean": mean + 0.002, "excess_mean": 0.001,
            "gross_t": 3.0}  # fmt: skip
    return {**base, **kw}


def _row(**windows):
    r = {"contemporary": _w(), "recent_180": _w(), "recent_90": _w(), "recent_30": _w(n=20),
         "frequency": {"independent_per_day": 2.0}, "q_value": 0.05}  # fmt: skip
    r.update(windows)
    return r


def test_verdict_strong_candidate_and_reasons():
    man = mini_manifest()
    r = _row()
    assert an.verdict(r, man, 0.001, [])[0] == "STRONG_INCUBATION_CANDIDATE"
    r = _row(q_value=0.5)
    assert an.verdict(r, man, 0.001, [])[0] == "INCUBATION_CANDIDATE"


def test_emerging_edge_old_null_history_does_not_veto():
    man = mini_manifest()
    r = _row(contemporary=_w(mean=0.0002, t=0.4), recent_180=_w(mean=0.004, t=2.4),
             long_context={"net_mean": -0.002, "t": -0.5})  # fmt: skip
    v, _ = an.verdict(r, man, 0.001, [])
    assert (
        v in ("INCUBATION_CANDIDATE", "STRONG_INCUBATION_CANDIDATE") and r["route"] == "recent_180"
    )
    # a 60-day edge: only the 90-day window sees it
    r = _row(contemporary=_w(mean=-0.0005, t=-0.6), recent_180=_w(mean=0.0004, t=0.8),
             recent_90=_w(mean=0.004, t=2.3))  # fmt: skip
    assert (
        an.verdict(r, man, 0.001, [])[0].endswith("INCUBATION_CANDIDATE")
        and r["route"] == "recent_90"
    )


def test_dead_recent_edge_is_not_rescued_by_a_strong_past():
    man = mini_manifest()
    r = _row(contemporary=_w(mean=0.004, t=4.0), recent_180=_w(mean=-0.001, t=-0.8),
             recent_90=_w(mean=-0.003, t=-2.0))  # fmt: skip
    v, why = an.verdict(r, man, 0.001, [])
    assert v not in ("INCUBATION_CANDIDATE", "STRONG_INCUBATION_CANDIDATE")
    assert "recent_not_dead" in why[0] and "recent_180_positive" in why[0]


def test_verdict_guards_costs_sparsity_and_parameter_support():
    man = mini_manifest()
    assert (
        an.verdict(_row(frequency={"independent_per_day": 0.01}), man, 0.001, [])[0] == "REJECTED"
    )
    weak = _row(contemporary=_w(mean=-0.0005, t=-0.4, gross_t=2.0),
                recent_180=_w(mean=-0.0004, t=-0.3), recent_90=_w(mean=-0.0001, t=-0.1),
                recent_30=_w(mean=-0.0001, t=-0.1))  # fmt: skip
    assert an.verdict(weak, man, 0.001, []) == ("REJECTED", ["edge exists only before costs"])
    lone = _row(contemporary=_w(t=1.8), recent_180=_w(t=0.5, mean=0.0005),
                recent_90=_w(t=0.5, mean=0.0005), q_value=0.5)  # fmt: skip
    v, why = an.verdict(lone, man, 0.001, [])
    assert v == "INTERESTING" and "parameter_support" in why[0]
    nb = {"contemporary": _w(t=1.2)}
    assert an.verdict(lone, man, 0.001, [nb])[0] == "INCUBATION_CANDIDATE"
    one_asset = _row(contemporary=_w(loo_mean=-0.001))
    assert "not_one_asset" in an.verdict(one_asset, man, 0.001, [])[1][0]
    none = _row(**{k: _w(n=5) for k in ("contemporary", "recent_180", "recent_90", "recent_30")})
    assert an.verdict(none, man, 0.001, [])[0] == "NO_EVIDENCE"


def test_contemporary_windows_are_frozen_by_calendar():
    m = load_manifest(MANIFEST)
    w = m.windows
    assert (w.contemporary_start, w.recent_180_start, w.recent_90_start, w.recent_30_start, w.end) == (
        datetime(2024, 11, 1, tzinfo=UTC), datetime(2026, 4, 4, tzinfo=UTC),
        datetime(2026, 7, 3, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    assert (w.end - w.recent_180_start).days == 180 and (w.end - w.recent_90_start).days == 90
    assert (
        m.discovery.venue == "binance"
        and m.discovery.long_context.event_end == w.contemporary_start
    )
    bad = mini_manifest().model_dump(mode="json", by_alias=True)
    bad["windows"]["recent_90_start"] = "2026-05-01T00:00:00Z"
    with pytest.raises(ValueError, match="nest"):
        DiscoveryManifest.model_validate(bad)
    with pytest.raises(ValueError):
        DiscoveryManifest.model_validate({**bad, "unexpected": 1})


# --------------------------------------------------------------------------- clustering


def test_duplicate_strategies_cluster_and_the_simplest_represents():
    t0 = pd.Timestamp("2026-01-01", tz="UTC").value
    base = pd.DataFrame({"coin": "ETH", "d": 1, "signal_ns": t0 + np.arange(20) * 7 * H})
    near = base.assign(signal_ns=base["signal_ns"] + H)  # within 2 h: the same trades
    other = base.assign(d=-1)
    assert an.overlap(base, near, 120) == pytest.approx(1.0) and an.overlap(base, other, 120) == 0
    ss = {s.key: s for s in cat.strategies()}
    keys = ["compression_breakout[n=24,pct=0.2]:1h:long", "breakout[n=24]:1h:long",
            "momentum_z[n=4,k=1.5]:1h:short"]  # fmt: skip
    rows = {k: {"contemporary": {"t": 2.0}} for k in keys}
    cl, _ = an.clusters(keys, {keys[0]: base, keys[1]: near, keys[2]: other}, ss, rows,
                        mini_manifest().clustering)  # fmt: skip
    big = next(c for c in cl if c["size"] == 2)
    assert big["representative"] == "breakout[n=24]:1h:long"  # lower complexity wins


# --------------------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def payload():
    from market_signal.research.discovery.run import evaluate_manifest

    man = mini_manifest()
    _, data, _ = market(seed=11)
    costs = {("binance", c): 0.0007 for c in sy.COINS}
    return evaluate_manifest(
        man, costs, lambda v, c: data[c], replication=False, long_context=False
    )


def test_payload_is_deterministic_and_complete(payload):
    from market_signal.research.discovery.run import evaluate_manifest
    from market_signal.research.structure.study.run import digest

    p, meta = payload
    man = mini_manifest()
    _, data, _ = market(seed=11)
    p2, _ = evaluate_manifest(man, {("binance", c): 0.0007 for c in sy.COINS}, lambda v, c: data[c],
                              replication=False, long_context=False)  # fmt: skip
    assert digest(p) == digest(p2)
    assert len(p["strategies"]) == 125 and sum(p["verdict_counts"].values()) == 125
    assert "wall_seconds" in meta and "seconds" not in p
    for s in p["strategies"]:
        assert s["verdict"] in ("REJECTED", "NO_EVIDENCE", "INTERESTING", "INCUBATION_CANDIDATE",
                                "STRONG_INCUBATION_CANDIDATE")  # fmt: skip
        c = s["contemporary"]
        if c["n"]:
            # gross - cost drag = net (up to funding NaN handling)
            assert c["gross_mean"] - c["cost_drag"] == pytest.approx(c["net_mean"], abs=1e-9)
    assert set(p["sides"]) == {"long", "short"}
    assert "VALIDATED" not in str(p["verdict_counts"])


def test_cook_and_eligibility_are_deterministic_and_parameter_free(payload):
    import inspect

    from market_signal.research.discovery.cook import cook, eligibility

    p, _ = payload
    a, b = cook(p), cook(p)
    assert a == b and a["question"] == "What did the Lab cook?"
    for i in a["items"]:
        assert {"hypothesis", "strategy_id", "side", "timeframe", "frequency", "recent_effect",
                "lifetime_context", "costs", "evidence_state", "caveats", "overlap_cluster",
                "recommended_incubation_policy"} <= set(i)  # fmt: skip
    e1, e2 = eligibility(p, "d1"), eligibility(p, "d1")
    assert (
        e1 == e2
        and e1["eligibility_id"].startswith("ielig_")
        and "NOT ACTIVATED" in e1["activation"]
    )
    assert eligibility(p, "d2")["eligibility_id"] != e1["eligibility_id"]
    assert set(inspect.signature(cook).parameters) == {"payload", "include_interesting"}


def test_planted_short_only_edge_is_discovered_and_the_long_side_is_not():
    from market_signal.research.discovery.run import evaluate_manifest

    man = mini_manifest()
    raw, data, starts = market(seed=5)
    vd = venue(data, 0.0)
    target = _strat("breakout[n=24]:1h:short")
    ev = co.strategy_events(vd, target, pd.Timestamp("2026-05-20", tz="UTC").value,
                            pd.Timestamp("2026-10-01", tz="UTC").value)  # fmt: skip
    entries = {}
    for coin, g in ev.groupby("coin"):
        e = ((g["entry_ns"].to_numpy(np.int64) - raw[coin].open_ns[0]) // sy.M15).astype(int)
        entries[coin] = list(zip(e.tolist(), g["d"].tolist(), strict=True))
    sy.plant_drift(raw, entries, 0.02, 16)
    data = {c: sy.coin_data(r, "binance", starts) for c, r in raw.items()}
    p, _ = evaluate_manifest(man, {("binance", c): 0.0007 for c in sy.COINS}, lambda v, c: data[c],
                             replication=False, long_context=False)  # fmt: skip
    rows = {s["key"]: s for s in p["strategies"]}
    assert rows[target.key]["verdict"] in ("INCUBATION_CANDIDATE", "STRONG_INCUBATION_CANDIDATE")
    assert rows["breakout[n=24]:1h:long"]["verdict"] not in ("INCUBATION_CANDIDATE",
                                                            "STRONG_INCUBATION_CANDIDATE")  # fmt: skip
    assert rows[target.key]["contemporary"]["net_mean"] > 0.005
    assert "path" in rows[target.key] and rows[target.key]["path"]["mfe_mean"] > 0


def test_null_calibration_false_positive_behaviour():
    """Cost-free null on the mini window: the one-sided test is not anti-conservative and
    nothing reaches STRONG."""
    from market_signal.research.discovery import calibration as cb

    r = cb.run_one((mini_manifest(), "null_costfree", 3))
    assert r["raw_p_lt_005_share"] is not None and r["raw_p_lt_005_share"] <= 0.15
    assert r["false_strong"] == 0 and r["bh_discoveries_non_target"] <= 3
    assert r["frequency_selfcheck"]


# --------------------------------------------------------------------------- governance


def _bars_store(store, man: DiscoveryManifest):
    from market_signal.intraday import bars as ib
    from market_signal.models.domain import Timeframe

    seen = datetime(2026, 10, 5, tzinfo=UTC)
    for v in man.venues:
        raw = sy.generate(v.data_start.h1, v.event_end, 2, coins=v.coins)
        for coin in v.coins:
            b15 = sy.bars_15m(raw[coin])
            for tf, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
                df = b15 if tf == "15m" else sy.aggregate(b15, minutes)
                df = df[df["open_time"] >= pd.Timestamp(v.data_start.get(tf))]
                df = df[["open_time", "open", "high", "low", "close", "volume"]].assign(trades=1)
                ib.upsert_bars(
                    store, v.venue, coin, Timeframe(tf), df, observed_at=seen, run_id="run_test"
                )
            store.con.executemany(
                "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
                [[coin, v.venue, pd.Timestamp(t, tz="UTC").to_pydatetime(), float(r), None,
                  pd.Timestamp(t, tz="UTC").to_pydatetime(), "test", "run_test"]
                 for t, r in zip(raw[coin].funding_ns, raw[coin].funding_rate, strict=True)
                 if pd.Timestamp(t, tz="UTC") >= pd.Timestamp(v.funding_start)])  # fmt: skip


def test_governed_register_and_run_on_retained_snapshots(store):
    from market_signal.research.discovery.spec import IntradayDiscoveryDefinition
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger

    man = mini_manifest(name="p22_gov")
    man = man.model_copy(
        update={"venues": (man.venues[0].model_copy(update={"coins": ("BTC", "ETH", "SOL")}),)}
    )
    ledger = Ledger(store)
    _bars_store(store, man)
    cfg = {"costs": {"taker_fee_bps": 4.5, "slippage_bps": {"default": 8}},
           "venues": {"binance": {"taker_fee_bps": 5.0}}}  # fmt: skip
    defn = ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="test")
    assert isinstance(defn, IntradayDiscoveryDefinition) and {c.fee_bps for c in defn.costs} == {
        5.0
    }
    assert store.con.execute("SELECT count(*) FROM lab_structure_runs").fetchone()[0] == 0
    got, _ = ss.get_study(ledger, defn.study_id)
    assert got.study_id == defn.study_id and got.validated_reachable is False
    out = ss.run(ledger, defn.study_id, software=SOFT)
    assert out["status"] == "COMPLETED", out["payload"].get("error")
    re = ss.run(
        ledger, defn.study_id, software=SOFT, rerun_of=out["run_id"], rerun_reason="determinism"
    )
    assert re["result_digest"] == out["result_digest"]
    with pytest.raises(ss.StudyError, match="already frozen"):
        ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="again")
    p = ss.result_payload(ledger, out["run_id"])
    from market_signal.research.discovery.report import render

    assert p["study_version"] == "intraday_discovery_v1" and "Phase 22" in render(p)


def test_definition_rejects_a_tampered_catalogue(store):
    from market_signal.research.discovery.spec import IntradayDiscoveryDefinition, catalogue_spec

    spec = catalogue_spec()
    spec["strategies"] = spec["strategies"][:-1]
    with pytest.raises(ValueError, match="catalogue"):
        IntradayDiscoveryDefinition(manifest=mini_manifest(), catalogue=spec, costs=(), datasets=(),
                                    semantics={})  # fmt: skip


# --------------------------------------------------------------------------- isolation


def test_phase21_baseline_is_unchanged():
    """Phase 22 adds a catalogue beside Phase 21; the frozen Phase 21 identities do not move."""
    import yaml

    from market_signal.research.incubation import policy as pm
    from market_signal.research.incubation import prospective as ip

    cfg = yaml.safe_load((ROOT / "config" / "perps.yaml").read_text())
    f = ip.build_freeze(cfg, ROOT / "config" / "lab" / "families")
    assert (
        f.freeze_id == "incfreeze_f34b719a715d02b2415ae056354d0c77bdf450508e981ed1f9b6ba1ce342a12f"
    )
    assert f.pool_id == "incpool_346515921d0eff2d9929f1cb2d72c99c35d2cbbb43dcf67905e9f2410411a81e"
    assert {p: x.policy_id for p, x in pm.POLICIES.items()} == {
        "CONSERVATIVE": "incpolicy_4356148786bda18d98916ca14b53caf48bd1a0d31d3e63d51cc4ea5d8048d76b",
        "BALANCED": "incpolicy_aaec7841f4b18d5222c6c50a0f34ced1c86e7c1decb77aa91da0d5608228a042",
        "AGGRESSIVE": "incpolicy_c2f2fe3583daa7203ff0e80a3d73966c192cdf5ccfdb0dda5691bd93c72f35f6",
    }
    assert len(f.members) == 128
    for p in (SRC / "research" / "incubation").rglob("*.py"):
        assert "discovery" not in p.read_text(), p


def test_no_live_consumer_or_order_path_reads_discovery():
    for pkg in ("paper", "copilot", "ops", "perps", "intraday"):
        for f in (SRC / pkg).rglob("*.py"):
            if pkg == "paper" and "v2" in f.parts:  # Phase 25A explicitly consumes frozen probes
                continue
            assert "research.discovery" not in f.read_text(), f
    for f in (SRC / "research" / "lab").rglob("*.py"):
        if f.name != "structure_study.py":
            assert "research.discovery" not in f.read_text(), f
    for f in (SRC / "research" / "discovery").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "market_signal.research.lab.forward", "telegram", "httpx", "requests",
                       "INSERT ", "UPDATE ", "DELETE "):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(
        "discovery" in " ".join(map(str, args)) for job in rt.JOBS.values() for _, args in job
    )
