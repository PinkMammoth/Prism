"""Phase 19 open interest x price x funding study: primitives, inference, governance, isolation.

Synthetic fixtures only (hand-built series and a synthetic market with OI and funding).
Nothing here reads real market data, and no test result says anything about whether a
signal works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.oiprice import primitives as op
from market_signal.research.oiprice.study import analysis as an
from market_signal.research.oiprice.study import calibration as cal
from market_signal.research.oiprice.study import collect as cl
from market_signal.research.oiprice.study import verdicts as vd
from market_signal.research.oiprice.study.run import digest, evaluate
from market_signal.research.oiprice.study.spec import (
    FAMILIES,
    Member,
    OiStudyDefinition,
    OiStudyManifest,
    StatisticsSpec,
    families_spec,
    load_manifest,
)
from market_signal.research.relative.study import stats
from market_signal.research.structure.series import NS, BarSeries, assumed

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
MANIFEST = ROOT / "config" / "oiprice" / "phase19_oi_price.v1.yaml"
SOFT = SoftwareIdentity(label="test", python_version="3.12")
H = pd.Timedelta("1h")
T0 = pd.Timestamp("2026-01-01", tz="UTC")


# --------------------------------------------------------------------------- fixtures


def bars(closes, coin="BTC", start=T0, venue="binance", skip=()) -> BarSeries:
    c = np.asarray(closes, float)
    ot = pd.date_range(start, periods=len(c), freq=H)
    o = np.concatenate([[c[0]], c[:-1]])
    df = pd.DataFrame({"open_time": ot, "close_time": ot + H, "open": o, "high": np.maximum(o, c) * 1.001,
                       "low": np.minimum(o, c) * 0.999, "close": c, "volume": 10.0})  # fmt: skip
    df = df.drop(index=list(skip)).reset_index(drop=True)
    return BarSeries.from_frame(df, venue=venue, coin=coin, timeframe="1h", availability=assumed(60.0),
                                dataset_key="dataset_test")  # fmt: skip


def oi_series(levels, coin="BTC", start=T0, latency=1800.0, px=None, skip=()) -> op.OiSeries:
    lv = np.asarray(levels, float)
    t = pd.date_range(start + H, periods=len(lv), freq=H)  # stamped at each bar's close
    df = pd.DataFrame({"source": "binance", "coin": coin, "period": "1h", "observed_at": t,
                       "open_interest": lv, "oi_notional": lv * (px if px is not None else 1.0),
                       "oi_notional_method": "provider:sumOpenInterestValue"})  # fmt: skip
    return op.binance_oi(df.drop(index=list(skip)), coin=coin, latency_s=latency)


def grid(closes: dict, ois: dict, funding=None) -> op.Grid:
    return op.Grid.build({c: bars(v, c) for c, v in closes.items()},
                         {c: ois[c] for c in ois}, funding or {})  # fmt: skip


def tiny_manifest(**over) -> OiStudyManifest:
    d = {
        "name": "p19_test", "description": "test",
        "window": {"venue": "binance", "data_start": "2026-04-01T00:00:00Z",
                   "oi_start": "2026-05-01T00:00:00Z", "event_start": "2026-05-06T00:00:00Z",
                   "event_end": "2026-06-10T00:00:00Z", "funding_start": "2026-04-01T00:00:00Z",
                   "coins": ["BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE"]},
        "comparison": {"venue": "hyperliquid", "coins": ["BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE"]},
        "horizons": [1, 6], "primary_horizon": 6,
        "axes": [{"name": "oi_large", "values": [1.5, 2.0, 2.5]}],
    }  # fmt: skip
    d.update(over)
    return OiStudyManifest.model_validate(d)


@pytest.fixture(scope="module")
def synthetic():
    man = tiny_manifest()
    return man, cal.run_synthetic(man, seed=3)


# --------------------------------------------------------------------------- alignment


def test_oi_is_matched_to_the_bar_whose_close_it_is_stamped_at_and_never_filled():
    closes = {"BTC": 100 + np.arange(10.0)}
    s = oi_series(1000 + 10 * np.arange(10.0), skip=(4,))
    g = grid(closes, {"BTC": s})
    # bar t closes at T0 + (t+1)h, which is exactly the observed_at of OI row t
    assert g.oi["BTC"][0] == 1000 and g.oi["BTC"][3] == 1030
    assert np.isnan(g.oi["BTC"][4]) and g.oi["BTC"][5] == 1050  # missing stays missing
    # an off-grid timestamp (e.g. a snapshot at :20) is never matched to a bar
    off = op.OiSeries("binance", "BTC", np.array([(T0 + pd.Timedelta("80min")).value]),
                      np.array([5.0]), np.array([5.0]), "x", np.array([0]))  # fmt: skip
    assert not np.isfinite(grid(closes, {"BTC": off}).oi["BTC"]).any()


def test_signal_cannot_exist_before_oi_is_known_and_entry_follows_it():
    n = 200
    closes = {"BTC": 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, n)))}
    s = oi_series(1e6 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.01, n))))
    g = grid(closes, {"BTC": s})
    f = op.coin_features(g, "BTC", 6, 48)
    t = np.flatnonzero(f.eligible)
    assert len(t)
    oi_known = g.close_time[t] + int(1800 * NS)
    assert (f.ready[t] >= oi_known).all() and (f.ready[t] >= g.ready["BTC"][t]).all()
    k = op.entry_index(g, f.ready[t])
    ok = k < len(g)
    assert (g.open_time[k[ok]] >= f.ready[t][ok]).all()
    assert (k[ok] == t[ok] + 2).all()  # any latency in (0, 1h] enters one full bar after the close
    # a longer OI latency delays the entry: the signal is the later of price and OI
    g2 = grid(closes, {"BTC": oi_series(s.oi, latency=5400.0)})
    f2 = op.coin_features(g2, "BTC", 6, 48)
    k2 = op.entry_index(g2, f2.ready[t])
    assert (k2[ok] == t[ok] + 3).all()


def test_oi_change_delta_relative_scale_z_and_percentile_by_hand():
    n, L, W = 40, 2, 5
    lv = 100.0 * (1.01 ** np.arange(n)) + np.sin(np.arange(n))  # strictly positive, varied
    g = grid({"BTC": 100 + 0 * np.arange(n) + np.arange(n) * 0.1}, {"BTC": oi_series(lv)})
    f = op.coin_features(g, "BTC", L, W)
    t = 30
    chg = np.log(lv / np.roll(lv, L))
    assert f.oi_chg[t] == pytest.approx(np.log(lv[t] / lv[t - L]))
    assert f.oi_delta[t] == pytest.approx(lv[t] - lv[t - L])
    assert f.oi_rel[t] == pytest.approx((lv[t] - lv[t - L]) / lv[t - L - W + 1 : t - L + 1].mean())
    prior = chg[t - L - W + 1 : t - L + 1]  # the W changes ending at t - L
    assert f.oi_s[t] == pytest.approx(chg[t] / np.sqrt((prior**2).mean()))
    assert f.oi_z[t] == pytest.approx((chg[t] - prior.mean()) / prior.std(ddof=1))
    prior_pct = chg[t - L - W + 1 : t - L + 1]
    assert f.oi_pct[t] == pytest.approx(
        ((prior_pct < chg[t]).sum() + 0.5 * (prior_pct == chg[t]).sum()) / W
    )
    assert f.oi_accel[t] == pytest.approx(chg[t] - chg[t - L])
    y = np.log(lv[t - L : t + 1])
    xs = np.arange(L + 1) - L / 2
    assert f.oi_trend[t] == pytest.approx((y * xs).sum() / (xs * xs).sum())
    # undefined until W + 2L observations exist
    assert np.isnan(f.oi_s[: L + W + L - 1]).all() and np.isfinite(f.oi_s[2 * L + W - 1])


def test_usd_and_coin_oi_are_separate_measures_and_never_mixed():
    n = 60
    px = 100 * 1.002 ** np.arange(n)
    coin = np.full(n, 1000.0)  # positioning flat, price rising
    g = grid({"BTC": px}, {"BTC": oi_series(coin, px=px)})
    f = op.coin_features(g, "BTC", 4, 10)
    t = 40
    assert f.oi_chg[t] == 0.0
    assert f.usd_chg[t] == pytest.approx(np.log(px[t] / px[t - 4]))  # USD moves with price alone
    assert g.usd_method["BTC"] == "provider:sumOpenInterestValue"
    bad = pd.DataFrame({"source": "binance", "coin": "BTC", "period": "1h",
                        "observed_at": pd.date_range(T0, periods=2, freq=H), "open_interest": [1.0, 2.0],
                        "oi_notional": [1.0, 2.0], "oi_notional_method": ["a", "b"]})  # fmt: skip
    with pytest.raises(ValueError, match="mixed USD OI"):
        op.binance_oi(bad, coin="BTC", latency_s=0)


def test_oi_z_is_causal_rewriting_the_future_changes_nothing_earlier():
    rng = np.random.default_rng(5)
    n = 300
    lv = 1e6 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    f1 = op.coin_features(grid({"BTC": px}, {"BTC": oi_series(lv)}), "BTC", 6, 72)
    lv2, px2 = lv.copy(), px.copy()
    lv2[200:] *= 3.0
    px2[200:] *= 0.5
    f2 = op.coin_features(grid({"BTC": px2}, {"BTC": oi_series(lv2)}), "BTC", 6, 72)
    for name in ("oi_s", "oi_z", "oi_pct", "pz", "oi_chg", "usd_s"):
        a, b = getattr(f1, name)[:199], getattr(f2, name)[:199]
        assert np.array_equal(np.isnan(a), np.isnan(b)) and np.allclose(
            a[~np.isnan(a)], b[~np.isnan(b)]
        ), name


# --------------------------------------------------------------------------- states


def _feat(pz, oi_s, oi_z=None, ret=None, fund_pct=None, usd_s=None):
    n = len(pz)
    nan = np.full(n, np.nan)
    f = op.CoinFeatures(coin="X", L=6, W=72, ret=np.asarray(ret if ret is not None else pz, float),
                        pz=np.asarray(pz, float), oi_chg=np.asarray(oi_s, float), usd_chg=nan,
                        oi_delta=nan, usd_delta=nan, oi_rel=nan, oi_s=np.asarray(oi_s, float),
                        usd_s=np.asarray(usd_s if usd_s is not None else nan, float),
                        oi_z=np.asarray(oi_z if oi_z is not None else oi_s, float), oi_pct=nan,
                        oi_accel=nan, oi_trend=nan, oi_to_volume=nan, ready=np.zeros(n, np.int64))  # fmt: skip
    fp = np.asarray(fund_pct if fund_pct is not None else np.full(n, 0.5), float)
    fund = op.Funding(nan, nan, nan, fp, np.zeros(n, int))
    return f, fund


def test_quadrant_classification_uses_both_dead_zones():
    p = tiny_manifest().central
    pz = [1.0, 1.0, -1.0, -1.0, 0.3, 1.0, np.nan]
    oi = [1.0, -1.0, 1.0, -1.0, 1.0, 0.2, 1.0]
    st = cl.states(*_feat(pz, oi), p, None)
    assert list(np.flatnonzero(st["P+"] & st["O+"])) == [0]
    assert list(np.flatnonzero(st["P+"] & st["O-"])) == [1]
    assert list(np.flatnonzero(st["P-"] & st["O+"])) == [2]
    assert list(np.flatnonzero(st["P-"] & st["O-"])) == [3]
    # inside a dead zone (price 0.3 < 0.5, OI 0.2 < 0.5) or undefined: no quadrant
    quad = st["P"] & (st["O+"] | st["O-"])
    assert not quad[4] and not quad[5] and not quad[6]
    assert list(np.flatnonzero(st["DIV"])) == [1, 2]


def test_flat_price_with_oi_expansion_and_progress_split():
    p = tiny_manifest().central
    pz = [0.1, -0.4, 0.6, 0.9, 1.5, 0.1]
    oi = [2.5, 2.1, 2.2, 2.0, 3.0, 1.9]
    st = cl.states(*_feat(pz, oi), p, None)
    assert list(np.flatnonzero(cl.state_mask(st, "F0&OL+"))) == [0, 1]
    assert list(np.flatnonzero(cl.state_mask(st, "OL+&PS"))) == [0, 1, 2, 3]
    assert list(np.flatnonzero(cl.state_mask(st, "OL+&PB"))) == [4]
    # ordinary vs large expansion partition the moves beyond the dead zone
    assert list(np.flatnonzero(st["OO+"])) == [5] and not (st["OO+"] & st["OL+"]).any()


def test_oi_shock_event_fires_once_on_the_first_bar_of_the_excursion():
    z = np.array([0.0, 1.0, 2.1, 2.5, 3.0, 1.0, 2.2])
    st = cl.states(*_feat(np.zeros(7), np.zeros(7), oi_z=z), tiny_manifest().central, None)
    ev = op.edge(st["SZ+"], np.ones(7, bool))
    assert list(np.flatnonzero(ev)) == [2, 6]
    # an undefined previous bar never manufactures an edge
    d = np.ones(7, bool)
    d[5] = False
    assert list(np.flatnonzero(op.edge(st["SZ+"], d))) == [2]


def test_funding_features_are_causal_and_cadence_free():
    n = 24 * 60
    closes = {"BTC": np.full(n, 100.0), "ETH": np.full(n, 100.0)}
    t8 = pd.date_range(T0, periods=n // 8, freq="8h")
    t4 = pd.date_range(T0, periods=n // 4, freq="4h")
    fund = {"BTC": (pd.DatetimeIndex(t8).as_unit("ns").asi8, np.full(len(t8), 3e-4)),
            "ETH": (pd.DatetimeIndex(t4).as_unit("ns").asi8, np.full(len(t4), 1.5e-4))}  # fmt: skip
    g = op.Grid.build({c: bars(v, c) for c, v in closes.items()}, {}, fund)
    fb, fe = op.funding_features(g, "BTC", 720), op.funding_features(g, "ETH", 720)
    t = 24 * 40
    # one day of carry regardless of cadence: 3 x 3e-4 == 6 x 1.5e-4
    assert fb.fund_24h[t] == pytest.approx(9e-4) and fe.fund_24h[t] == pytest.approx(9e-4)
    assert fb.settlements_24h[t] == 3 and fe.settlements_24h[t] == 6
    assert np.isnan(fb.fund_24h[:22]).all()  # not a full day of history: undefined, not zero
    # a settlement exactly at the bar close counts; one after it never does
    i = int(np.flatnonzero(g.close_time == pd.DatetimeIndex(t8).as_unit("ns").asi8[100])[0])
    assert fb.fund_last[i] == 3e-4
    rate2 = np.full(len(t8), 3e-4)
    rate2[101:] = 9e-3
    g2 = op.Grid.build({c: bars(v, c) for c, v in closes.items()}, {},
                       {"BTC": (fund["BTC"][0], rate2), "ETH": fund["ETH"]})  # fmt: skip
    f2 = op.funding_features(g2, "BTC", 720)
    assert np.allclose(f2.fund_24h[: i + 1], fb.fund_24h[: i + 1], equal_nan=True)
    assert np.allclose(f2.fund_pct[: i + 1], fb.fund_pct[: i + 1], equal_nan=True)


def test_volatility_target_reads_exactly_the_forward_window():
    c = np.array([100, 101, 102, 104, 103, 105, 107, 106, 108, 110.0])
    g = grid({"BTC": c}, {})
    k, h = np.array([3]), 4
    p = op.forward_paths(g, "BTC", k, h)
    o = g.o["BTC"][3]
    assert p.ret[0] == pytest.approx(c[6] / o - 1)
    assert p.absret[0] == pytest.approx(abs(np.log(c[6] / o)))
    hi, lo = g.h["BTC"][3:7].max(), g.l["BTC"][3:7].min()
    assert p.rng[0] == pytest.approx((hi - lo) / o)
    assert p.mfe_long[0] == pytest.approx(hi / o - 1) and p.mae_short[0] == pytest.approx(
        1 - hi / o
    )
    assert np.isnan(op.forward_paths(g, "BTC", np.array([8]), 4).ret[0])  # runs past the data


# --------------------------------------------------------------------------- controls, inference


def test_price_matched_control_compares_bars_with_the_same_price_move():
    m = Member("x", "q", "event", "dir", "P+&O+", "price", "price")
    bars_ = pd.DataFrame({
        "coin": "X", "t": [1, 2, 3, 4, 5, 6], "vol": 1, "psign": [1, 1, 1, -1, -1, 1],
        "pzb": [2, 2, 2, 2, 2, 3], "fundb": 1, "ret": [0.03, 0.01, 0.02, -0.05, 0.04, 0.5],
        "absret": 0.0, "fund_long": 0.0, "ret_close": 0.0, "range": 0.0, "k": [3, 4, 5, 6, 7, 8],
        "mfe_long": 0.0, "mae_long": 0.0, "mfe_short": 0.0, "mae_short": 0.0,
    })  # fmt: skip
    ev = pd.DataFrame({"coin": ["X"], "t": [1]})
    r = an.scored(ev, bars_, m, {"X": 0.0005})
    # the cell is (coin, vol, psign=+1, pzb=2): bars 1, 2, 3 only
    assert r["excess"].iloc[0] == pytest.approx(0.03 - np.mean([0.03, 0.01, 0.02]))
    assert r["net"].iloc[0] == pytest.approx(0.03 - 2 * 0.0005)
    ru = an.scored(ev, bars_, m, {"X": 0.0005}, baseline="uncond")
    assert ru["excess"].iloc[0] == pytest.approx(0.03 - bars_["ret"].mean())
    # a falling-price event is oriented short: continuation = the price keeps falling
    r4 = an.scored(pd.DataFrame({"coin": ["X"], "t": [4]}), bars_, m, {"X": 0.0})
    assert r4["d"].iloc[0] == -1 and r4["excess"].iloc[0] == pytest.approx(
        -(-0.05 - np.mean([-0.05, 0.04]))
    )


def test_simultaneous_signals_across_assets_are_one_time_block():
    hp = 6
    k = np.array([60, 60, 61, 62, 63, 64])  # six coins entering on one shock
    v = np.array([0.01, 0.012, 0.011, 0.009, 0.013, 0.01])
    blocked = stats.clustered_mean(v, k // hp)
    iid = stats.clustered_mean(v, np.arange(len(v)) * 2)
    assert blocked["clusters"] == 1 and iid["clusters"] == 6


def test_bh_runs_within_families_and_untestable_members_are_excluded(synthetic):
    _, out = synthetic
    fams = out["payload"]["primary"]["families"]
    assert set(fams) == set(FAMILIES)
    for fam, fd in fams.items():
        assert fd["preregistered"] == len(FAMILIES[fam])
        testable = [r for r in fd["members"] if r.get("testable")]
        assert fd["m"] == len(testable)
        for r in fd["members"]:
            assert (r["q_value"] is None) == (not r.get("testable"))


def test_verdict_policy_and_volatility_skips_the_position_gate():
    st = StatisticsSpec()
    base = {"testable": True, "stat": 0.004, "ci95": [0.002, 0.006], "p_value": 0.001, "q_value": 0.02,
            "positive_asset_share": 0.8, "loo_all_same_sign": True}  # fmt: skip
    assert (
        vd.verdict({**base, "net_mean": -0.001}, None, "PLATEAU", 0.001, "event", st)[0] == vd.WEAK
    )
    assert (
        vd.verdict({**base, "net_mean": None}, None, "PLATEAU", 0.001, "volatility", st)[0]
        == vd.PROMISING
    )
    assert (
        vd.verdict({**base, "net_mean": 0.002}, None, "PLATEAU", 0.001, "event", st)[0]
        == vd.PROMISING
    )
    assert vd.verdict({"testable": False}, None, None, 0.001, "event", st)[0] == vd.INSUFFICIENT
    neg = {**base, "stat": -0.004, "ci95": [-0.006, -0.002]}
    assert vd.verdict(neg, None, None, 0.001, "event", st)[0] == vd.REJECTED


def test_directions_are_labelled_by_orientation_never_by_folklore(synthetic):
    _, out = synthetic
    vs = out["payload"]["primary"]["verdicts"]
    by = {}
    for r in vs:
        by.setdefault(r["member"] + r["family"], set()).add(r["direction"])
    assert by["q_up_oi_upquadrants"] == {"continuation", "reversal"}
    assert by["oi_large_upoi_only"] == {"up", "down"}
    assert by["vol_oi_large_upvolatility"] == {"larger", "smaller"}
    assert not any(
        "liquidation" in str(r).lower() or "short covering" in str(r).lower() for r in vs
    )


# --------------------------------------------------------------------------- venues, coverage


def test_venues_are_never_pooled_or_filled_from_each_other():
    s = oi_series([1.0, 2.0])
    hl = op.OiSeries(
        "hyperliquid", "BTC", s.observed_ns, s.oi, s.oi_usd, "open_interest*mark_px", s.observed_ns
    )
    with pytest.raises(ValueError, match="ONE venue"):
        grid({"BTC": [100.0, 101.0]}, {"BTC": hl})
    with pytest.raises(ValueError, match="venue/coin"):
        op.binance_oi(pd.DataFrame({"source": ["hyperliquid"], "coin": ["BTC"], "period": ["1h"],
                                    "observed_at": [T0], "open_interest": [1.0], "oi_notional": [1.0],
                                    "oi_notional_method": ["x"]}), coin="BTC", latency_s=0)  # fmt: skip
    # a Binance gap stays a gap even with Hyperliquid snapshots available at that hour
    g = grid({"BTC": 100 + np.arange(20.0)}, {"BTC": oi_series(1000 + np.arange(20.0), skip=(10,))})
    snaps = op.OiSeries("hyperliquid", "BTC", g.close_time.copy(), np.full(20, 5.0), np.full(20, 5.0),
                        "open_interest*mark_px", g.close_time.copy())  # fmt: skip
    a = op.align_snapshots(g, snaps, 2, 7200.0)
    assert np.isnan(g.oi["BTC"][10]) and a["oi"][10] == 5.0


def test_hyperliquid_alignment_uses_elapsed_time_and_staleness():
    g = grid({"BTC": 100 + np.arange(30.0)}, {})
    t = g.close_time
    # snapshots every 3 hours, captured 10 minutes before a bar close
    obs = t[::3] - 600 * NS
    s = op.OiSeries(
        "hyperliquid", "BTC", obs, np.arange(len(obs)) + 100.0, np.ones(len(obs)), "m", obs
    )
    a = op.align_snapshots(g, s, 3, 7200.0)
    assert a["age_s"][3] == 600 and np.isfinite(a["chg"][3])
    # 2h50m old at bar 5: beyond the 2h limit, so no value (never interpolated)
    assert np.isnan(a["oi"][5])
    # two snapshots: a value stays usable for up to 2h, so bars 3 and 4 (vs bars 0 and 1)
    # each have a change over 3h of elapsed time, and nothing else does
    sparse = op.OiSeries(
        "hyperliquid", "BTC", obs[:2], np.array([1.0, 2.0]), np.ones(2), "m", obs[:2]
    )
    assert list(np.flatnonzero(np.isfinite(op.align_snapshots(g, sparse, 3, 7200.0)["chg"]))) == [
        3,
        4,
    ]


def test_sparse_hyperliquid_history_is_insufficient_not_forced():
    man = tiny_manifest(axes=[])
    sparse = cal.run_synthetic(man, seed=4, hl="sparse")["payload"]["primary"]
    assert sparse["cross_venue_gate"].startswith("comparison venue coverage")
    for r in sparse["families"]["cross_venue"]["members"]:
        assert not r["testable"] and "comparison venue coverage" in r["untestable_reason"]
    assert {v["verdict"] for v in sparse["verdicts"] if v["family"] == "cross_venue"} == {
        vd.INSUFFICIENT
    }
    dense = cal.run_synthetic(man, seed=4, hl="dense")["payload"]["primary"]
    assert dense["cross_venue_gate"] == "passed"
    assert all(v["aligned_hours"] >= 336 for v in dense["comparison_coverage"].values())


# --------------------------------------------------------------------------- determinism, immunity


def test_payload_is_deterministic_and_carries_no_clock():
    man = tiny_manifest(axes=[])
    a = cal.run_synthetic(man, seed=9)
    b = cal.run_synthetic(man, seed=9)
    assert digest(a["payload"]) == digest(b["payload"])
    text = str(a["payload"])
    assert "wall_seconds" not in text and "peak_rss" not in text


def test_appending_future_rows_never_changes_earlier_features_or_events():
    man = tiny_manifest(axes=[])
    defn = cal.definition(man)
    prim, comp = cal.coin_data(defn, seed=11)
    cut = pd.Timestamp("2026-05-25", tz="UTC").value
    short = {}
    for c, d in prim.items():
        n = int(np.searchsorted(d.bars.close_time, cut, side="right"))
        m = int(np.searchsorted(d.oi.observed_ns, cut, side="right"))
        f = int(np.searchsorted(d.funding_ns, cut, side="right"))
        oi = op.OiSeries(d.oi.venue, c, d.oi.observed_ns[:m], d.oi.oi[:m], d.oi.oi_usd[:m],
                         d.oi.usd_method, d.oi.ready_ns[:m])  # fmt: skip
        short[c] = type(d)(
            d.venue, c, d.dataset_id, d.bars.head(n), d.funding_ns[:f], d.funding_rate[:f], oi, m
        )
    full_ctx = cl.context(defn, prim, comp)
    short_ctx = cl.context(defn, short, comp)
    fv, sv = cl.variant(full_ctx, defn, man.central), cl.variant(short_ctx, defn, man.central)
    n = len(short_ctx.grid)
    for c in prim:
        for name in ("pz", "oi_s", "oi_z", "usd_s", "oi_pct"):
            a, b = getattr(fv.feats[c], name)[:n], getattr(sv.feats[c], name)
            assert np.allclose(a, b, equal_nan=True), (c, name)
        assert np.array_equal(full_ctx.vol_own[c][:n], short_ctx.vol_own[c])
        assert np.allclose(
            full_ctx.funding[c].fund_pct[:n], short_ctx.funding[c].fund_pct, equal_nan=True
        )
    for pop in ("P+&O+", "SZ+", "FH&CE+", "F0&OL+"):
        e_full = cl.events(fv, full_ctx, pop)
        e_short = cl.events(sv, short_ctx, pop)
        early = e_full[e_full["t"] < n - 1]
        assert (
            early.sort_values(["coin", "t"])
            .reset_index(drop=True)
            .equals(e_short[e_short["t"] < n - 1].sort_values(["coin", "t"]).reset_index(drop=True))
        )


# --------------------------------------------------------------------------- calibration


def test_null_calibration_false_positive_rate_is_in_range():
    res = cal.calibrate(tiny_manifest(), seeds=(1, 2, 3))
    assert res["tests"] > 40
    assert res["share_p_below_0.05"] <= 0.12  # nominal 5%; the blocked t-test is conservative
    assert {r["family"] for r in res["rows"]} <= set(FAMILIES)


def test_planted_oi_effect_is_detected():
    res = cal.calibrate(tiny_manifest(), seeds=(1, 2), planted=0.4)
    ic = [r for r in res["rows"] if r["member"] == "ic_oi"]
    assert ic and all(r["p"] < 0.05 for r in ic)


# --------------------------------------------------------------------------- manifest, governance


def test_shipped_manifest_is_valid_bounded_and_ends_before_untouched_data():
    man = load_manifest(MANIFEST)
    assert man.primary_horizon == 6 and man.timeframe == "1h"
    assert man.window.event_end == datetime(2026, 10, 1, tzinfo=UTC)
    assert man.assumed_oi_latency_s == 1800.0 and man.comparison.venue == "hyperliquid"
    assert len(man.variants()) == 1 + 2 * len(man.axes)
    for v, c in man.venue_coins():
        for s in man.selections(v, c):
            assert s.end == man.window.event_end
    kinds = {s.kind for s in man.selections("binance", "BTC")}
    assert kinds == {"perp_intraday_bars", "perp_funding", "perp_oi_history"}
    assert [s.kind for s in man.selections("hyperliquid", "BTC")] == ["perp_snapshots"]


def test_manifest_refuses_drift_and_unfrozen_families():
    with pytest.raises(ValueError, match="middle"):
        tiny_manifest(axes=[{"name": "oi_large", "values": [1.5, 2.5, 3.0]}])
    with pytest.raises(ValueError, match="primary horizon"):
        tiny_manifest(primary_horizon=12)
    defn = cal.definition(tiny_manifest())
    tampered = families_spec()
    tampered["quadrants"] = tampered["quadrants"][:2]
    with pytest.raises(ValueError, match="family membership"):
        OiStudyDefinition(**{**defn.model_dump(), "families": tampered})


def test_oi_dataset_kinds_select_the_period_and_venue():
    from market_signal.research.lab.datasets import SeriesSelection

    with pytest.raises(ValueError, match="1h statistics period"):
        SeriesSelection(kind="perp_oi_history", symbol="BTC", source="binance",
                        start=T0, end=T0 + H)  # fmt: skip


def _store_inputs(store, man: OiStudyManifest):
    from market_signal.intraday import bars as ib
    from market_signal.models.domain import Timeframe

    defn = cal.definition(man)
    prim, _ = cal.coin_data(defn, seed=5)
    seen = datetime(2026, 10, 5, tzinfo=UTC)
    for c, d in prim.items():
        s = d.bars
        df = pd.DataFrame({"open_time": pd.to_datetime(s.open_time, utc=True), "open": s.o, "high": s.h,
                           "low": s.l, "close": s.c, "volume": s.v, "trades": 1})  # fmt: skip
        ib.upsert_bars(store, "binance", c, Timeframe.H1, df, observed_at=seen, run_id="run_test")
        store.con.executemany("INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
                              [[c, "binance", pd.Timestamp(t, tz="UTC").to_pydatetime(), float(r), None,
                                pd.Timestamp(t, tz="UTC").to_pydatetime(), "test", "run_test"]
                               for t, r in zip(d.funding_ns, d.funding_rate, strict=True)])  # fmt: skip
        store.con.executemany("INSERT INTO perp_oi_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [["binance", c, c + "USDT", "usdm_perpetual", "1h",
                                pd.Timestamp(t, tz="UTC").to_pydatetime(), float(a), float(b),
                                "provider:sumOpenInterestValue", seen, seen, "run_test"]
                               for t, a, b in zip(d.oi.observed_ns, d.oi.oi, d.oi.oi_usd, strict=True)])  # fmt: skip
        # a 4h-period row and a Hyperliquid snapshot must NOT enter the Binance 1h selection
        store.con.execute("INSERT INTO perp_oi_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          ["binance", c, c + "USDT", "usdm_perpetual", "4h",
                           pd.Timestamp(d.oi.observed_ns[5], tz="UTC").to_pydatetime(), 1.0, 1.0,
                           "provider:sumOpenInterestValue", seen, seen, "run_test"])  # fmt: skip
        store.con.execute("INSERT INTO perp_snapshots (coin, source, snapshot_at, open_interest, "
                          "oi_notional, ingest_run_id) VALUES (?,?,?,?,?,?)",
                          [c, "hyperliquid", pd.Timestamp(d.oi.observed_ns[50], tz="UTC").to_pydatetime(),
                           123.0, 456.0, "run_test"])  # fmt: skip


def test_governed_lifecycle_freezes_then_evaluates_from_retained_oi(store):
    from market_signal.research.lab import structure_study as ss

    man = tiny_manifest(name="p19_gov", axes=[])
    ledger = Ledger(store)
    _store_inputs(store, man)
    defn = ss.register(ledger, man, perps_cfg={}, software=SOFT, origin="test", reason="test")
    assert isinstance(defn, OiStudyDefinition)
    assert store.con.execute("SELECT count(*) FROM lab_structure_runs").fetchone()[0] == 0
    got, row = ss.get_study(ledger, defn.study_id)
    assert isinstance(got, OiStudyDefinition) and row["evidence_class"] == "EXPLORATORY"
    ds = ledger.get_dataset(defn.dataset("binance", "SOL"))
    assert sorted(s.selection.kind for s in ds.series) == [
        "perp_funding",
        "perp_intraday_bars",
        "perp_oi_history",
    ]
    oi_fp = next(s for s in ds.series if s.selection.kind == "perp_oi_history")
    assert oi_fp.row_count == len(
        cal.coin_data(defn, seed=5)[0]["SOL"].oi
    )  # the 4h row is excluded
    hl = ledger.get_dataset(defn.dataset("hyperliquid", "SOL"))
    assert [s.selection.kind for s in hl.series] == ["perp_snapshots"] and hl.series[
        0
    ].row_count == 1
    out = ss.run(ledger, defn.study_id, software=SOFT)
    assert out["status"] == "COMPLETED", out["payload"].get("error")
    p = ss.result_payload(ledger, out["run_id"])
    assert p["study_version"] == "oi_price_v1" and p["primary"]["cross_venue_gate"] != "passed"
    with pytest.raises(ss.StudyError, match="explicit rerun"):
        ss.run(ledger, defn.study_id, software=SOFT)
    again = ss.run(
        ledger, defn.study_id, software=SOFT, rerun_of=out["run_id"], rerun_reason="repro"
    )
    assert again["result_digest"] == out["result_digest"]
    from market_signal.research.oiprice.study.report import render

    assert "Phase 19 result" in render(p)


def test_coverage_audit_lists_absent_venues_explicitly(store):
    from market_signal.research.oiprice.coverage import oi_coverage

    man = tiny_manifest()
    _store_inputs(store, man)
    df = oi_coverage(
        store, pd.Timestamp("2026-05-01", tz="UTC"), pd.Timestamp("2026-06-10", tz="UTC")
    )
    bn = df[(df["venue"] == "binance") & (df["coin"] == "BTC")].iloc[0]
    assert bn["missing"] == 0 and bn["max_gap_h"] == 1.0 and bn["price_bars_1h"] > 0
    late = oi_coverage(store, pd.Timestamp("2026-04-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC"),
                       coins=["BTC"])  # fmt: skip
    assert set(late["venue"]) == {"binance", "hyperliquid"} and (late["observations"] == 0).all()


# --------------------------------------------------------------------------- isolation


def test_no_live_consumer_reads_phase19():
    for pkg in ("paper", "copilot", "ops", "perps"):
        for f in (SRC / pkg).rglob("*.py"):
            text = f.read_text()
            assert "research.oiprice" not in text and "oiprice_cmds" not in text, f
    for f in (SRC / "research" / "lab" / "forward.py", SRC / "research" / "lab" / "corroboration.py",
              SRC / "research" / "lab" / "validation.py", SRC / "research" / "lab" / "evidence.py",
              SRC / "research" / "lab" / "families.py", SRC / "cli" / "oi_cmds.py"):  # fmt: skip
        assert "research.oiprice" not in f.read_text(), f
    for f in (SRC / "research" / "oiprice").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "market_signal.research.lab.forward", "market_signal.perps.open_interest",
                       "telegram", "httpx", "INSERT ", "UPDATE ", "DELETE "):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(
        "oiprice" in " ".join(map(str, args)) for job in rt.JOBS.values() for _, args in job
    )


def test_live_oi_collection_semantics_are_untouched():
    """Phase 19 reads OI; it does not change how it is collected."""
    from market_signal.perps import open_interest as oi

    assert oi.BINANCE == "binance" and oi.HYPERLIQUID == "hyperliquid"
    assert oi.NOTIONAL_METHOD == "provider:sumOpenInterestValue"
    text = (ROOT / "config" / "perps.yaml").read_text()
    assert (
        "period: 1h" in text and "HYPE" not in text.split("venues:")[1].split("open_interest:")[0]
    )


def test_evaluate_refuses_nothing_silently_on_missing_oi():
    """A coin whose OI is entirely absent contributes no events (never zero-filled)."""
    man = tiny_manifest(axes=[])
    defn = cal.definition(man)
    prim, comp = cal.coin_data(defn, seed=2)
    d = prim["AAVE"]
    prim["AAVE"] = type(d)(d.venue, "AAVE", d.dataset_id, d.bars, d.funding_ns, d.funding_rate,
                           op.OiSeries.empty("binance", "AAVE"), 0)  # fmt: skip
    payload, _ = evaluate(defn, lambda v, c: prim[c] if v == "binance" else comp[c])
    q = payload["primary"]["families"]["quadrants"]["members"]
    for r in q:
        per = (r.get("breadth") or {}).get("per_asset") or {}
        assert "AAVE" not in per
    ctx = cl.context(defn, prim, comp)
    assert (cl.variant(ctx, defn, man.central).labels["coin"] != "AAVE").all()
    assert (ctx.entry["AAVE"] == len(ctx.grid)).all()  # no OI -> never an entry
