"""Phase 18 relative-strength study: primitives, inference, governance and isolation.

Synthetic fixtures only (hand-built prices and a correlated random market). Nothing here
reads real market data, and no test result says anything about whether a signal works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.relative import primitives as rp
from market_signal.research.relative.study import analysis as an
from market_signal.research.relative.study import calibration as cal
from market_signal.research.relative.study import collect as cl
from market_signal.research.relative.study import stats
from market_signal.research.relative.study import verdicts as vd
from market_signal.research.relative.study.spec import (
    FAMILIES,
    Member,
    RelativeStudyDefinition,
    StatisticsSpec,
    StudyManifest,
    families_spec,
    load_manifest,
)
from market_signal.research.structure.series import BarSeries, assumed
from market_signal.research.structure.study.data import CoinData

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_signal"
MANIFEST = ROOT / "config" / "relative" / "phase18_relative_strength.v1.yaml"
STEP = pd.Timedelta("4h")
SOFT = SoftwareIdentity(label="test", python_version="3.12")


# --------------------------------------------------------------------------- fixtures


def bars(closes, coin: str, start="2025-01-01", venue="hyperliquid", skip=(), tf="4h") -> BarSeries:
    step = pd.Timedelta(tf)
    c = np.asarray(closes, float)
    st = pd.Timestamp(start)
    st = st.tz_localize("UTC") if st.tzinfo is None else st.tz_convert("UTC")
    ot = pd.date_range(st, periods=len(c), freq=step)
    o = np.concatenate([[c[0]], c[:-1]])
    df = pd.DataFrame({"open_time": ot, "close_time": ot + step, "open": o,
                       "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "volume": 1.0})  # fmt: skip
    df = df.drop(index=list(skip)).reset_index(drop=True)
    return BarSeries.from_frame(df, venue=venue, coin=coin, timeframe=tf, availability=assumed(60.0),
                                dataset_key="dataset_test")  # fmt: skip


def walk(n: int, seed: int, beta: float = 1.0, base=None, sig=0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.normal(0, sig, n)
    if base is not None:
        x = beta * base + x
    return x


def prices(x: np.ndarray) -> np.ndarray:
    return 100 * np.exp(np.cumsum(x))


def tiny_manifest(**over) -> StudyManifest:
    d = {
        "name": "p18_test", "description": "test",
        "funding": [{"venue": "hyperliquid", "start": "2025-01-01T00:00:00Z", "end": "2025-08-01T00:00:00Z"},
                    {"venue": "binance", "start": "2024-06-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"}],
        "architectures": [{
            "name": "primary", "timeframe": "4h", "horizons": [1, 6], "primary_horizon": 6,
            "windows": [{"venue": "hyperliquid", "data_start": "2025-01-01T00:00:00Z",
                         "event_start": "2025-03-15T00:00:00Z", "event_end": "2025-08-01T00:00:00Z",
                         "coins": ["BTC", "ETH", "SOL", "LINK", "AAVE"]}],
            "axes": [{"name": "z_threshold", "values": [1.5, 2.0, 2.5]}],
        }],
    }  # fmt: skip
    d.update(over)
    return StudyManifest.model_validate(d)


def two_venue_manifest() -> StudyManifest:
    m = tiny_manifest()
    a = m.architectures[0].model_dump(mode="json")
    a["windows"].append({"venue": "binance", "data_start": "2024-06-01T00:00:00Z",
                         "event_start": "2024-08-15T00:00:00Z", "event_end": "2025-01-01T00:00:00Z",
                         "coins": ["BTC", "ETH", "SOL", "LINK", "AAVE"],
                         "excluded": {"HYPE": "test exclusion"}})  # fmt: skip
    return tiny_manifest(architectures=[a])


@pytest.fixture(scope="module")
def synthetic():
    """One full synthetic evaluation (two venues) plus its collected frames."""
    man = two_venue_manifest()
    out = cal.run_synthetic(man, seed=7)
    return man, out


def _ctx_for(closes: dict[str, np.ndarray], man: StudyManifest | None = None, venue="hyperliquid"):
    man = man or tiny_manifest()
    defn = cal.definition(man)
    arch = man.architecture("primary")
    w = arch.window(venue)
    coins = {c: CoinData(venue, c, "dataset_" + "0" * 64, {"4h": bars(closes[c], c, start=w.data_start)},
                         np.array([], dtype=np.int64), np.array([], dtype=float)) for c in w.coins}  # fmt: skip
    return defn, arch, cl.context(defn, arch, venue, coins)


# --------------------------------------------------------------------------- primitives


def test_rolling_beta_and_correlation_match_hand_ols_and_never_read_future_rows():
    xb = walk(120, 1)
    xa = walk(120, 2, beta=1.5, base=xb)
    m = rp.rolling_moments(xa, xb, 20)
    a, b = xa[31:51], xb[31:51]
    assert m.beta[50] == pytest.approx(np.cov(a, b)[0, 1] / np.var(b, ddof=1), rel=1e-12)
    assert m.corr[50] == pytest.approx(np.corrcoef(a, b)[0, 1], rel=1e-12)
    assert m.sd_residual[50] == pytest.approx(np.std(a - m.beta[50] * b, ddof=1), rel=1e-10)
    assert np.isnan(m.beta[:19]).all() and np.isfinite(m.beta[19:]).all()
    xa2 = xa.copy()
    xa2[60:] = 0.5  # rewrite the future
    m2 = rp.rolling_moments(xa2, xb, 20)
    assert np.array_equal(m.beta[:60], m2.beta[:60], equal_nan=True)
    assert np.array_equal(m.corr[:60], m2.corr[:60], equal_nan=True)
    assert not np.allclose(m.beta[60:], m2.beta[60:])
    # a missing pair makes every window that contains it undefined
    xa3 = xa.copy()
    xa3[40] = np.nan
    m3 = rp.rolling_moments(xa3, xb, 20)
    assert np.isnan(m3.beta[40:60]).all() and np.isfinite(m3.beta[60])


def test_zero_btc_variance_is_undefined_not_infinite():
    xb = np.zeros(50)
    xa = walk(50, 3)
    with np.errstate(all="raise"):
        m = rp.rolling_moments(xa, xb, 10)
        assert np.isnan(m.beta[9:]).all() and np.isnan(m.corr[9:]).all()
    xb[:5] = 1e-7  # below MIN_REF_STD as well
    assert np.isnan(rp.rolling_moments(xa, xb, 10).beta).all()
    # in a panel: a flat BTC makes no alt eligible (never a division by zero)
    n = 300
    p = rp.Panel.from_series({"BTC": bars(np.full(n, 100.0), "BTC"),
                              "ETH": bars(prices(walk(n, 4)), "ETH")})  # fmt: skip
    pf = rp.pair_features(p, "ETH", 6, 90)
    assert not pf.eligible.any() and np.isnan(pf.beta).all()


def test_relative_return_and_residual_by_hand():
    cb = np.array([100, 101, 99, 102, 104, 103, 105, 107, 106.0])
    ca = np.array([50, 51, 50.5, 52, 53.5, 52, 54, 56, 55.0])
    p = rp.Panel.from_series({"BTC": bars(cb, "BTC"), "ETH": bars(ca, "ETH")})
    L, W, t = 2, 3, 8
    pf = rp.pair_features(p, "ETH", L, W)
    ra, rb = ca[t] / ca[t - L] - 1, cb[t] / cb[t - L] - 1
    assert pf.r[t] == pytest.approx(ra) and pf.r_ref[t] == pytest.approx(rb)
    assert pf.rel[t] == pytest.approx(ra - rb)
    xa, xb = ca[1:] / ca[:-1] - 1, cb[1:] / cb[:-1] - 1  # x[i] is the return INTO bar i+1
    wa, wb = xa[t - L - W : t - L], xb[t - L - W : t - L]  # the W returns ending at bar t-L
    beta_prior = np.cov(wa, wb)[0, 1] / np.var(wb, ddof=1)
    assert pf.beta_prior[t] == pytest.approx(beta_prior)
    assert pf.res[t] == pytest.approx(ra - beta_prior * rb)
    sd_rel = np.std(wa - wb, ddof=1)
    assert pf.z_rel[t] == pytest.approx((ra - rb) / (sd_rel * np.sqrt(L)))
    sd_res = np.std(wa - beta_prior * wb, ddof=1)
    assert pf.z_res[t] == pytest.approx((ra - beta_prior * rb) / (sd_res * np.sqrt(L)))
    assert pf.z_raw[t] == pytest.approx(ra / (np.std(wa, ddof=1) * np.sqrt(L)))
    # the forward-target hedge ratio is the beta of the W returns ending AT t
    wa2, wb2 = xa[t - W : t], xb[t - W : t]
    assert pf.beta[t] == pytest.approx(np.cov(wa2, wb2)[0, 1] / np.var(wb2, ddof=1))
    # not eligible before W + L + 1 bars of history
    assert not pf.eligible[: L + W].any() and pf.eligible[t]


def test_cross_sectional_ranks_use_only_eligible_alts():
    vals = np.array([[0.3, 0.1, 0.2, np.nan], [0.1, 0.1, 0.5, 0.4], [0.2, 0.9, 0.1, 0.0]])
    elig = np.array([[True, True, True, True], [True, True, True, True], [True, False, True, True]])
    xs = rp.rank_cross_section(vals, elig, ("A", "B", "C", "D"))
    assert xs.rank.tolist() == [[1, 3, 2, 0], [3, 4, 1, 2], [1, 0, 2, 3]]
    assert xs.n.tolist() == [3, 4, 3]  # NaN and ineligible alts never occupy a rank
    assert xs.top(3)[2].tolist() == [True, False, False, False]
    assert xs.bottom(3)[2].tolist() == [False, False, False, True]
    assert not xs.top(4)[0].any()  # universe below the minimum: no leader
    assert xs.percentile()[1].tolist() == pytest.approx([1 / 3, 0.0, 1.0, 2 / 3])
    # ranking by raw, BTC-relative or market-relative return is identical at a timestamp
    common = np.array([[0.05], [-0.2], [0.1]])
    assert np.array_equal(rp.rank_cross_section(vals - common, elig, xs.alts).rank, xs.rank)


def test_future_listing_never_changes_earlier_ranks_or_features():
    n, late = 700, 400
    xb = walk(n, 10)
    close = {
        c: prices(walk(n, 11 + i, 1.2, xb)) for i, c in enumerate(("ETH", "SOL", "LINK", "AAVE"))
    }
    base = {"BTC": bars(prices(xb), "BTC"), **{c: bars(v, c) for c, v in close.items()}}
    hype = bars(prices(walk(n, 20, 1.4, xb)), "HYPE", skip=range(late))
    without, with_ = rp.Panel.from_series(base), rp.Panel.from_series({**base, "HYPE": hype})
    ctx_args = {"lookback": 18, "beta_window": 90}
    fa = {
        a: rp.pair_features(without, a, ctx_args["lookback"], ctx_args["beta_window"])
        for a in without.alts
    }
    fb = {
        a: rp.pair_features(with_, a, ctx_args["lookback"], ctx_args["beta_window"])
        for a in with_.alts
    }
    for a in without.alts:  # before HYPE is listed nothing about the others changes
        for name in ("rel", "res", "z_rel", "z_res", "z_mkt", "beta"):
            assert np.array_equal(
                getattr(fa[a], name)[:late], getattr(fb[a], name)[:late], equal_nan=True
            )

    def ranks(panel, f):
        alts = panel.alts
        e = np.column_stack([f[a].eligible for a in alts])
        v = np.column_stack([f[a].rel for a in alts])
        return rp.rank_cross_section(v, e, alts)

    ra, rb = ranks(without, fa), ranks(with_, fb)
    first = int(np.flatnonzero(fb["HYPE"].eligible)[0])
    assert first > late + 90  # HYPE needs W + L + 1 bars of its own history
    cols = [with_.alts.index(a) for a in without.alts]
    assert np.array_equal(ra.rank[:first], rb.rank[:first][:, cols])
    assert np.array_equal(ra.n[:first], rb.n[:first]) and rb.n[first] == ra.n[first] + 1


def test_missing_bar_excludes_the_asset_from_every_window_containing_it():
    n, gap = 400, 250
    xb = walk(n, 30)
    s = {"BTC": bars(prices(xb), "BTC"), "ETH": bars(prices(walk(n, 31, 1, xb)), "ETH"),
         "SOL": bars(prices(walk(n, 32, 1, xb)), "SOL", skip=[gap])}  # fmt: skip
    p = rp.Panel.from_series(s)
    assert not p.present["SOL"][gap] and p.present["SOL"][gap - 1]
    L, W = 6, 90
    pf = rp.pair_features(p, "SOL", L, W)
    # the one-bar returns into and out of the hole are undefined, so every W window that
    # contains them (and every L window spanning the hole) is undefined
    assert not pf.eligible[gap : gap + W + L + 1].any()
    assert pf.eligible[gap - 1] and pf.eligible[gap + W + L + 1]
    eth = rp.pair_features(p, "ETH", L, W)
    xs = rp.rank_cross_section(np.column_stack([eth.rel, pf.rel]),
                               np.column_stack([eth.eligible, pf.eligible]), ("ETH", "SOL"))  # fmt: skip
    assert xs.rank[gap + 5, 1] == 0 and xs.n[gap + 5] == 1


def test_correlation_breakdown_fires_once_when_observable():
    n, g = 400, 250
    xb = walk(n, 40)
    xa = xb + walk(n, 41, sig=0.002)
    xa[g:] = walk(n - g, 42, sig=0.01) + 0.004  # decoupled, drifting up vs BTC
    p = rp.Panel.from_series({"BTC": bars(prices(xb), "BTC"), "ETH": bars(prices(xa), "ETH")})
    W, S, hi, drop = 60, 20, 0.7, 0.3
    b = rp.breakdown(p, "ETH", W, S, hi, drop)
    # brute-force reference with np.corrcoef on explicit windows
    x_a, x_b = rp.one_bar_returns(p.c["ETH"]), rp.one_bar_returns(p.c["BTC"])
    cond = np.zeros(n, bool)
    for t in range(W + S, n):
        rp_ = np.corrcoef(x_a[t - S - W + 1 : t - S + 1], x_b[t - S - W + 1 : t - S + 1])[0, 1]
        rs = np.corrcoef(x_a[t - S + 1 : t + 1], x_b[t - S + 1 : t + 1])[0, 1]
        cond[t] = rp_ >= hi and rs <= rp_ - drop
    want = cond & ~np.concatenate([[False], cond[:-1]])
    want[: W + S + 1] = False
    got = b.event
    assert np.array_equal(got, want & (b.sign != 0))
    first = int(np.flatnonzero(got)[0])
    assert g < first <= g + S  # observable only once the short window holds the divergence
    assert b.sign[first] == 1  # the residual moved up: continuation = long the alt


def test_edge_trigger_counts_a_persistent_excursion_once():
    z = np.array([np.nan, 0.0, 1.0, 2.5, 3.0, 2.2, 1.0, 2.1, 2.4, -2.5, -3.0, -1.0])
    assert np.flatnonzero(rp.cross_up(z, 2.0)).tolist() == [3, 7]
    assert np.flatnonzero(rp.cross_down(z, 2.0)).tolist() == [9]
    z2 = np.array([np.nan, 2.5, 2.6])  # no defined bar below the threshold: not a crossing
    assert not rp.cross_up(z2, 2.0).any()


def test_btc_consolidation_uses_the_frozen_phase17_range_rule():
    n = 500
    rng = np.random.default_rng(5)
    trend = 100 * np.exp(np.cumsum(np.full(n, 0.004) + rng.normal(0, 0.001, n)))
    zig = trend[-1] * (1 + 0.01 * np.where(np.arange(n) % 2 == 0, 1, -1) + rng.normal(0, 0.001, n))
    btc = np.concatenate([trend, zig])
    closes = {
        "BTC": btc,
        **{c: btc * (1 + 0.01 * i) for i, c in enumerate(("ETH", "SOL", "LINK", "AAVE"))},
    }
    _, _, ctx = _ctx_for(closes)
    c = ctx.panel.c["BTC"]
    for t in (300, 450, 800, 950):
        er = abs(c[t] - c[t - 20]) / np.abs(np.diff(c[t - 20 : t + 1])).sum()
        assert ctx.consolidation[t] == (er < 1 / np.sqrt(20))
    assert not ctx.consolidation[300:500].any() and ctx.consolidation[800:1000].all()
    assert (ctx.trend[300:500] == 1).all()  # close above EMA(50) while trending up


def test_rank_persistence_statistics_by_hand():
    xs = pd.DataFrame({
        "t": [0] * 4 + [6] * 4, "k": [2] * 4 + [8] * 4, "coin": ["A", "B", "C", "D"] * 2,
        "n": 4, "rel": [0.3, 0.1, 0.2, -0.1, 0.0, 0.5, 0.2, 0.1],
        "res": [0.3, 0.1, 0.2, -0.1, 0.0, 0.5, 0.2, 0.1],
        "fwd_rel": [0.05, -0.02, 0.01, -0.03, 0.01, -0.04, 0.02, 0.03],
        "fwd_res": [0.05, -0.02, 0.01, -0.03, 0.01, -0.04, 0.02, 0.03],
        "cost": [0.001, 0.002, 0.001, 0.003] * 2, "third": 0, "trend": 1, "consolidation": False,
        "vol": 0, "pullback": False,
    })  # fmt: skip
    ic = an._xs_values(Member("x", "ic", "ic", "rel", "rel"), xs, 4)
    assert ic["value"].tolist() == pytest.approx([1.0, rp.spearman(np.array([0.0, 0.5, 0.2, 0.1]),
                                                                   np.array([0.01, -0.04, 0.02, 0.03]))])  # fmt: skip
    top = an._xs_values(Member("x", "top", "bucket", "rel", "top"), xs, 4)
    # t=0: leader A finishes 1st (top half: +1 - 1/2); t=6: leader B finishes last (0 - 1/2)
    assert top["value"].tolist() == [0.5, -0.5] and top["coin"].tolist() == ["A", "B"]
    bot = an._xs_values(Member("x", "bot", "bucket", "rel", "bottom"), xs, 4)
    # t=0: laggard D finishes last; t=6: laggard A finishes 3rd of 4 (still bottom half)
    assert bot["value"].tolist() == [0.5, 0.5] and bot["coin"].tolist() == ["D", "A"]
    sp = an._xs_values(Member("x", "s", "spread", "rel", "top"), xs, 4)
    assert sp["value"].tolist() == pytest.approx([0.08, -0.05])
    assert sp["net"].tolist() == pytest.approx(
        [0.08 - 2 * (0.001 + 0.003), -0.05 - 2 * (0.002 + 0.001)]
    )
    assert an._xs_values(Member("x", "ic", "ic", "rel", "rel"), xs, 5).empty  # universe gate


def test_entry_is_the_first_open_after_availability_and_spread_costs_charge_both_legs():
    n = 400
    xb = walk(n, 50)
    closes = {
        c: prices(walk(n, 51 + i, 1.2, xb)) for i, c in enumerate(("ETH", "SOL", "LINK", "AAVE"))
    }
    closes["BTC"] = prices(xb)
    defn, arch, ctx = _ctx_for(closes)
    f = cl.features(ctx, arch.central)
    t = np.array([300, 310])
    d = np.array([1, -1])
    o = cl.outcomes(ctx, f, "SOL", t, d, f.pairs["SOL"].ready, (6,), close_entry=True)
    # a 4h bar closing at T is available at T + 60 s: the first bar opening after that is t + 2
    assert o["k"].tolist() == [302, 312]
    p = ctx.panel
    ra = p.c["SOL"][t + 7] / p.o["SOL"][t + 2] - 1
    rb = p.c["BTC"][t + 7] / p.o["BTC"][t + 2] - 1
    beta = f.pairs["SOL"].beta[t]
    ca, cb = defn.cost("hyperliquid", "SOL").per_side, defn.cost("hyperliquid", "BTC").per_side
    assert o["g_rel"].to_numpy() == pytest.approx(d * (ra - rb))
    assert o["g_res"].to_numpy() == pytest.approx(d * (ra - beta * rb))
    assert o["g_usd"].to_numpy() == pytest.approx(d * ra)
    assert o["n_rel"].to_numpy() == pytest.approx(d * (ra - rb) - 2 * (ca + cb))  # both legs
    assert o["n_res"].to_numpy() == pytest.approx(
        d * (ra - beta * rb) - 2 * (ca + np.abs(beta) * cb)
    )
    assert o["n_usd"].to_numpy() == pytest.approx(d * ra - 2 * ca)  # the alt alone
    rc = p.c["SOL"][t + 6] / p.c["SOL"][t] - 1 - (p.c["BTC"][t + 6] / p.c["BTC"][t] - 1)
    assert o["nc_rel"].to_numpy() == pytest.approx(d * rc - 2 * (ca + cb))  # signal-close reference


def test_absolute_and_relative_targets_are_never_mixed():
    for fam, ms in FAMILIES.items():  # one declared target per family
        assert len({m.target for m in ms}) == 1, fam
    assert {m.target for m in FAMILIES["absolute"]} == {"usd"}
    assert {m.target for m in FAMILIES["relative_momentum"]} == {"rel"}
    assert {m.target for m in FAMILIES["residual"]} == {"res"}
    n = 400
    xb = walk(n, 60)
    closes = {
        c: prices(walk(n, 61 + i, 1.2, xb)) for i, c in enumerate(("ETH", "SOL", "LINK", "AAVE"))
    }
    closes["BTC"] = prices(xb)
    _, arch, ctx = _ctx_for(closes)
    f = cl.features(ctx, arch.central)
    t, d = np.array([300]), np.array([1])
    o1 = cl.outcomes(ctx, f, "ETH", t, d, f.pairs["ETH"].ready, (6,), close_entry=False)
    closes2 = dict(closes)
    b2 = closes["BTC"].copy()
    b2[305:] *= 1.2  # BTC jumps during the forward window only
    closes2["BTC"] = b2
    _, _, ctx2 = _ctx_for(closes2)
    o2 = cl.outcomes(ctx2, f, "ETH", t, d, f.pairs["ETH"].ready, (6,), close_entry=False)
    assert o1["g_usd"].iloc[0] == pytest.approx(o2["g_usd"].iloc[0])  # USD ignores BTC
    assert o2["g_rel"].iloc[0] < o1["g_rel"].iloc[0] - 0.1  # relative does not
    # an event member reads only its own target's columns
    rows = o1.assign(pop="rel+")
    base = o1.copy()
    poisoned = rows.assign(n_usd=np.nan, g_usd=np.nan, f_usd=np.nan)
    a = an.with_excess(rows, base, "rel", None)
    b = an.with_excess(poisoned, base, "rel", None)
    assert a["excess"].tolist() == b["excess"].tolist()


# --------------------------------------------------------------------------- inference


def test_student_t_and_clustered_mean_by_hand():
    assert stats.t_sf(2.0, 10) == pytest.approx(0.036694, abs=1e-6)
    assert stats.t_sf(1.959964, 1e7) == pytest.approx(0.025, abs=1e-5)
    assert stats.t_sf(-1.0, 5) == pytest.approx(1 - stats.t_sf(1.0, 5))
    r = stats.clustered_mean([1, 2, 3, 4], [0, 0, 2, 2])
    # S = (3, 7), n = (2, 2), m = 2.5, u = (-0.5, 0.5); blocks 0 and 2 are not adjacent
    assert r["mean"] == 2.5 and r["se"] == pytest.approx(np.sqrt(0.5)) and r["df"] == 1
    adj = stats.clustered_mean([1, 2, 3, 4], [0, 0, 1, 1])
    assert adj["se"] == pytest.approx(np.sqrt(0.5))  # negative adjacent covariance: floored
    pos = stats.clustered_mean([3, 3, 4, 4, 1, 1], [0, 0, 1, 1, 5, 5])
    m = 16 / 6
    u = (np.array([6, 8, 2]) - m * np.array([2, 2, 2])) / 6  # blocks 0 and 1 co-move
    assert u[0] * u[1] > 0
    assert pos["se"] == pytest.approx(np.sqrt((u * u).sum() + 2 * u[0] * u[1]))
    d = stats.clustered_difference([1, 3], [0, 1], [0, 0], [0, 1])
    assert d["mean"] == pytest.approx(2.0)


def test_cross_asset_co_movement_is_one_cluster_not_many_events():
    """Five alts firing on the same bar with the same outcome are one observation."""
    rng = np.random.default_rng(1)
    common = rng.normal(0, 1, 60)
    v = np.repeat(common, 5)
    b = np.repeat(np.arange(60) * 3, 5)  # non-adjacent blocks
    naive_se = v.std(ddof=1) / np.sqrt(len(v))
    r = stats.clustered_mean(v, b)
    assert r["se"] > 2 * naive_se and r["clusters"] == 60


def test_verdict_policy():
    st = StatisticsSpec()
    base = {"testable": True, "stat": 0.003, "ci95": [0.001, 0.005], "p_value": 0.001,
            "q_value": 0.02, "net_mean": 0.002, "positive_asset_share": 0.8, "loo_all_same_sign": True}  # fmt: skip
    other = {"testable": True, "stat": 0.002, "p_value": 0.01}
    assert vd.verdict(base, other, "PLATEAU", 0.001, "event", st)[0] == vd.ROBUST
    assert vd.verdict(base, None, "PLATEAU", 0.001, "event", st)[0] == vd.PROMISING
    assert vd.verdict(base, other, "FRAGILE", 0.001, "event", st)[0] == vd.PROMISING
    assert (
        vd.verdict({**base, "net_mean": -0.001}, other, "PLATEAU", 0.001, "event", st)[0] == vd.WEAK
    )
    assert (
        vd.verdict({**base, "loo_all_same_sign": False}, other, "PLATEAU", 0.001, "event", st)[0]
        == vd.WEAK
    )
    assert vd.verdict({**base, "stat": 0.0005}, other, "PLATEAU", 0.001, "event", st)[0] == vd.WEAK
    assert vd.verdict({**base, "stat": -0.003}, other, None, 0.001, "event", st)[0] == vd.REJECTED
    tight = {**base, "q_value": 0.5, "stat": 0.0002, "ci95": [-0.0003, 0.0008], "p_value": 0.3}
    assert vd.verdict(tight, None, None, 0.001, "event", st)[0] == vd.REJECTED  # floor excluded
    weak = {**base, "q_value": 0.3, "ci95": [-0.001, 0.007], "p_value": 0.03}
    assert vd.verdict(weak, None, None, 0.001, "event", st)[0] == vd.WEAK
    none = {**weak, "p_value": 0.2}
    assert vd.verdict(none, None, None, 0.001, "event", st)[0] == vd.NO_EVIDENCE
    assert (
        vd.verdict({**base, "testable": False}, None, None, 0.001, "event", st)[0]
        == vd.INSUFFICIENT
    )
    ic = {
        **base,
        "stat": 0.05,
        "ci95": [0.02, 0.08],
        "net_mean": None,
        "positive_asset_share": None,
    }
    assert vd.verdict(ic, None, None, 0.03, "ic", st)[0] == vd.PROMISING  # no net gate for an IC


def test_directional_reading_mirrors_continuation_and_reversal():
    s = {"testable": True, "stat": 0.002, "ci95": [-0.001, 0.005], "t": 1.5, "df": 100,
         "net_mean": 0.001, "net_mean_reversal": -0.004, "q_value": 0.4,
         "breadth": {"per_asset": {"A": {"excess_mean": 0.003}, "B": {"excess_mean": -0.001}},
                     "leave_one_out_excess": {"A": -0.001, "B": 0.003}}}  # fmt: skip
    c, r = an.directional(s, "continuation"), an.directional(s, "reversal")
    assert c["stat"] == -r["stat"] and c["ci95"] == [-0.001, 0.005] and r["ci95"] == [-0.005, 0.001]
    assert c["p_value"] + r["p_value"] == pytest.approx(1.0) and c["p_value"] < 0.1
    assert c["net_mean"] == 0.001 and r["net_mean"] == -0.004
    assert c["positive_asset_share"] == 0.5 and c["loo_all_same_sign"] is False


# --------------------------------------------------------------------------- pipeline


def test_families_bh_and_venue_isolation(synthetic):
    _, out = synthetic
    a = out["payload"]["architectures"]["primary"]
    assert set(a["venues"]) == {"hyperliquid", "binance"}
    for v, vv in a["venues"].items():
        assert set(vv["families"]) == set(FAMILIES)
        for fam, fd in vv["families"].items():
            ms = fd["members"]
            assert fd["preregistered"] == len(FAMILIES[fam])
            assert all(r["hypothesis"].startswith(f"{v}|primary|{fam}|") for r in ms)
            pv = {r["hypothesis"]: r["p_value"] for r in ms if r["testable"]}
            assert fd["m"] == len(pv)  # untestable members are excluded from m
            q = benjamini_hochberg(pv) if pv else {}
            for r in ms:
                assert r["q_value"] == q.get(r["hypothesis"])
                if not r["testable"]:
                    assert r["q_value"] is None and r["untestable_reason"]
    # verdicts exist for both directions of every member on every venue
    keys = {(r["venue"], r["family"], r["member"], r["direction"]) for r in a["verdicts"]}
    assert len(keys) == 2 * 2 * sum(len(ms) for ms in FAMILIES.values())


def test_venues_are_never_pooled(synthetic):
    man, _ = synthetic
    with pytest.raises(ValueError, match="ONE venue"):
        rp.Panel.from_series({"BTC": bars(np.full(10, 1.0), "BTC"),
                              "ETH": bars(np.full(10, 1.0), "ETH", venue="binance")})  # fmt: skip
    # changing every Binance bar leaves every Hyperliquid number unchanged
    defn = cal.definition(man)
    from market_signal.research.relative.study.run import evaluate

    base_load = _loader(man, seed=7)
    other_load = _loader(man, seed=7, binance_seed=99)
    p1, _ = evaluate(defn, base_load)
    p2, _ = evaluate(defn, other_load)
    h1 = p1["architectures"]["primary"]["venues"]["hyperliquid"]
    h2 = p2["architectures"]["primary"]["venues"]["hyperliquid"]
    assert h1 == h2
    assert (
        p1["architectures"]["primary"]["venues"]["binance"]
        != p2["architectures"]["primary"]["venues"]["binance"]
    )


def _loader(man: StudyManifest, seed: int, binance_seed: int | None = None):
    cache: dict = {}

    def load(venue: str, coin: str):
        if venue not in cache:
            a = man.architecture("primary")
            w = a.window(venue)
            s = binance_seed if (venue == "binance" and binance_seed) else seed
            cache[venue] = cal.coin_data(venue, tuple(w.coins), "4h", w.data_start, w.event_end, s)
        return cache[venue][coin]

    return load


def test_payload_is_deterministic_and_carries_no_clock(synthetic):
    man, out = synthetic
    from market_signal.research.relative.study.run import digest

    again = cal.run_synthetic(man, seed=7)
    assert digest(out["payload"]) == digest(again["payload"])
    text = str(out["payload"])
    assert "wall_seconds" not in text and "peak_rss" not in text
    assert out["meta"]["wall_seconds"] > 0


def test_sensitivity_is_descriptive_and_one_at_a_time(synthetic):
    _, out = synthetic
    vv = out["payload"]["architectures"]["primary"]["venues"]["hyperliquid"]
    rows = {r["member"]: r for r in vv["sensitivity"]}
    assert rows["rel_strong"]["axes"] == ["z_threshold"]
    assert [n["variant"] for n in rows["rel_strong"]["neighbours"]] == [
        "z_threshold=1.5",
        "z_threshold=2.5",
    ]
    assert "breakdown" not in rows  # the threshold axis cannot change a correlation breakdown
    assert all(r.get("p_value") is None for r in rows["rel_strong"]["neighbours"])


def test_appending_future_bars_never_changes_earlier_signals_or_outcomes():
    man = tiny_manifest()
    arch = man.architecture("primary")
    defn = cal.definition(man)
    w = arch.window("hyperliquid")
    full = cal.coin_data("hyperliquid", tuple(w.coins), "4h", w.data_start, w.event_end, 3)
    cut = pd.Timestamp("2025-06-01", tz="UTC").value
    short = {}
    for c, d in full.items():
        s = d.series["4h"]
        k = s.open_time < cut
        df = pd.DataFrame({"open_time": pd.to_datetime(s.open_time[k], utc=True),
                           "close_time": pd.to_datetime(s.close_time[k], utc=True), "open": s.o[k],
                           "high": s.h[k], "low": s.l[k], "close": s.c[k], "volume": s.v[k]})  # fmt: skip
        bs = BarSeries.from_frame(df, venue="hyperliquid", coin=c, timeframe="4h",
                                  availability=assumed(60.0), dataset_key="dataset_synthetic")  # fmt: skip
        short[c] = CoinData(
            "hyperliquid", c, d.dataset_id, {"4h": bs}, d.funding_ns, d.funding_rate
        )
    a = cl.collect(defn, arch, "hyperliquid", short).central.events
    b = cl.collect(defn, arch, "hyperliquid", full).central.events
    a = a[np.isfinite(a["n_rel"])]
    key = ["pop", "coin", "t", "d", "horizon"]
    j = a.merge(b, on=key, suffixes=("_a", "_b"), how="left")
    assert len(j) == len(a) and len(a) > 50
    for col in ("k", "g_rel", "n_res", "n_usd", "beta", "vol", "consolidation", "pullback"):
        assert np.allclose(j[f"{col}_a"].astype(float), j[f"{col}_b"].astype(float)), col
    xa, xb = (
        cl.collect(defn, arch, "hyperliquid", short).central.xs,
        cl.collect(defn, arch, "hyperliquid", full).central.xs,
    )
    xa = xa[np.isfinite(xa["fwd_rel"])]
    jx = xa.merge(xb, on=["t", "coin"], suffixes=("_a", "_b"))
    assert len(jx) == len(xa) and (jx["rank_rel_a"] == jx["rank_rel_b"]).all()


def test_decomposition_separates_conditional_from_executable(synthetic):
    _, out = synthetic
    dec = out["payload"]["architectures"]["primary"]["venues"]["hyperliquid"]["decomposition"]
    assert "NOT executable" in dec["conditional_from_h6_entry"]["note"]
    assert dec["h6_events"] >= dec["confirmed"]
    if dec["confirmed"]:
        assert dec["delay_bars_median"] >= 1  # confirmation is always observed later


# --------------------------------------------------------------------------- manifest / governance


def test_shipped_manifest_is_valid_and_bounded():
    man = load_manifest(MANIFEST)
    prim = man.architecture("primary")
    assert prim.timeframe == "4h" and prim.primary_horizon == 6 and len(prim.variants()) == 13
    assert man.architecture("secondary").axes == ()
    hl, bn = prim.window("hyperliquid"), prim.window("binance")
    assert bn.event_end <= hl.event_start  # no shared outcome hour
    assert "HYPE" not in bn.coins and "HYPE" in bn.excluded
    assert max(w.event_end for a in man.architectures for w in a.windows) <= datetime(
        2026, 10, 1, tzinfo=UTC
    )
    assert sum(len(ms) for ms in FAMILIES.values()) == 30


def test_manifest_refuses_overlap_drift_and_unfrozen_families():
    man = tiny_manifest()
    a = man.architectures[0].model_dump(mode="json")
    a["windows"].append({"venue": "binance", "data_start": "2024-06-01T00:00:00Z",
                         "event_start": "2024-08-15T00:00:00Z", "event_end": "2025-04-01T00:00:00Z",
                         "coins": ["BTC", "ETH"]})  # fmt: skip
    with pytest.raises(ValueError, match="overlap"):
        tiny_manifest(architectures=[a])
    b = man.architectures[0].model_dump(mode="json")
    b["axes"] = [{"name": "lookback", "values": [6, 42, 18]}]
    with pytest.raises(ValueError):
        tiny_manifest(architectures=[b])
    b["axes"] = [{"name": "lookback", "values": [6, 18, 42]}]
    b["central"] = {"lookback": 42}
    with pytest.raises(ValueError, match="middle"):
        tiny_manifest(architectures=[b])
    c = man.architectures[0].model_dump(mode="json")
    c["windows"][0]["coins"] = ["ETH", "SOL"]
    with pytest.raises(ValueError, match="reference"):
        tiny_manifest(architectures=[c])
    defn = cal.definition(man)
    assert defn.study_id.startswith("sstudy_") and defn.evidence_class == "EXPLORATORY"
    tampered = families_spec()
    tampered["absolute"] = tampered["absolute"][:2]
    with pytest.raises(ValueError, match="family membership"):
        RelativeStudyDefinition(**{**defn.model_dump(), "families": tampered})


def _bars_store(store, man: StudyManifest):
    from market_signal.intraday import bars as ib
    from market_signal.models.domain import Timeframe

    seen = datetime(2026, 10, 5, tzinfo=UTC)
    for venue, coin in man.venue_coins():
        for a in man.architectures:
            w = next((w for w in a.windows if w.venue == venue), None)
            if w is None or coin not in w.coins:
                continue
            cd = cal.coin_data(venue, tuple(w.coins), a.timeframe,
                               pd.Timestamp(w.data_start) - pd.Timedelta(a.timeframe), w.event_end, 5)[coin]  # fmt: skip
            s = cd.series[a.timeframe]
            df = pd.DataFrame({"open_time": pd.to_datetime(s.open_time, utc=True), "open": s.o, "high": s.h,
                               "low": s.l, "close": s.c, "volume": s.v, "trades": 1})  # fmt: skip
            ib.upsert_bars(
                store, venue, coin, Timeframe(a.timeframe), df, observed_at=seen, run_id="run_test"
            )
        f = next(x for x in man.funding if x.venue == venue)
        t = pd.date_range(f.start, f.end, freq="8h", inclusive="left")
        store.con.executemany("INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
                              [[coin, venue, x.to_pydatetime(), 1e-5, None, x.to_pydatetime(), "test", "run_test"]
                               for x in t])  # fmt: skip


def test_governed_lifecycle_freezes_before_any_result(store):
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.relative.study.spec import RelativeStudyDefinition as RSD

    man = tiny_manifest(name="p18_gov")
    ledger = Ledger(store)
    _bars_store(store, man)
    cfg = {"costs": {"taker_fee_bps": 4.5, "slippage_bps": {"default": 8}}}
    defn = ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="test")
    assert isinstance(defn, RSD) and {c.fee_bps for c in defn.costs} == {4.5}
    # registration evaluates nothing
    assert store.con.execute("SELECT count(*) FROM lab_structure_runs").fetchone()[0] == 0
    assert store.con.execute("SELECT count(*) FROM lab_structure_results").fetchone()[0] == 0
    got, row = ss.get_study(ledger, defn.study_id)
    assert (
        isinstance(got, RSD)
        and got.study_id == defn.study_id
        and row["evidence_class"] == "EXPLORATORY"
    )
    with pytest.raises(ss.StudyError, match="already frozen"):
        ss.register(ledger, man, perps_cfg=cfg, software=SOFT, origin="test", reason="again")
    seen = {}

    def spy(d, load):
        seen["runs"] = store.con.execute("SELECT count(*) FROM lab_structure_runs").fetchone()[0]
        seen["results"] = store.con.execute(
            "SELECT count(*) FROM lab_structure_results"
        ).fetchone()[0]
        cd = load("hyperliquid", "SOL")
        seen["dataset_key"] = cd.series["4h"].prov.dataset_key
        seen["tfs"] = sorted(cd.series)
        return {"ok": 1}, {"wall_seconds": 0.0}

    out = ss.run(ledger, defn.study_id, software=SOFT, evaluate=spy)
    assert out["status"] == "COMPLETED" and seen["runs"] == 1 and seen["results"] == 0
    assert seen["dataset_key"] == defn.dataset("hyperliquid", "SOL") and seen["tfs"] == ["4h"]
    with pytest.raises(ss.StudyError, match="explicit rerun"):
        ss.run(ledger, defn.study_id, software=SOFT, evaluate=spy)
    assert ss.inspect(ledger, defn.study_id)["study_version"] == "relative_strength_v1"
    # the retained datasets select exactly the frozen windows
    ss.verify_datasets(ledger, defn)
    sel = man.selections("hyperliquid", "SOL")
    assert [s.kind for s in sel] == ["perp_intraday_bars", "perp_funding"]
    assert sel[0].end == man.architecture("primary").window("hyperliquid").event_end


def test_governed_real_evaluation_on_retained_snapshots(store):
    """The default evaluator of a Phase 18 study runs from the retained rows end to end."""
    from market_signal.research.lab import structure_study as ss

    man = tiny_manifest(name="p18_gov_eval")
    man = man.model_copy(
        update={"architectures": (man.architectures[0].model_copy(update={"axes": ()}),)}
    )
    ledger = Ledger(store)
    _bars_store(store, man)
    defn = ss.register(ledger, man, perps_cfg={}, software=SOFT, origin="test", reason="test")
    out = ss.run(ledger, defn.study_id, software=SOFT)
    assert out["status"] == "COMPLETED", out["payload"].get("error")
    p = ss.result_payload(ledger, out["run_id"])
    assert p["study_version"] == "relative_strength_v1" and p["evidence_class"] == "EXPLORATORY"
    from market_signal.research.relative.study.report import render

    assert "Phase 18 result" in render(p)


def test_null_calibration_runs_on_synthetic_data_only():
    man = tiny_manifest()
    res = cal.calibrate(man, seeds=(1,))
    assert res["tests"] > 0 and 0 <= res["share_p_below_0.05"] <= 1
    assert {r["venue"] for r in res["rows"]} == {"hyperliquid"}


# --------------------------------------------------------------------------- isolation


def test_no_live_consumer_reads_phase18():
    for pkg in ("paper", "copilot", "ops"):
        for f in (SRC / pkg).rglob("*.py"):
            text = f.read_text()
            assert "research.relative" not in text and "relative_cmds" not in text, f
    for f in (SRC / "research" / "lab" / "forward.py", SRC / "research" / "lab" / "corroboration.py",
              SRC / "research" / "lab" / "validation.py", SRC / "research" / "lab" / "evidence.py",
              SRC / "research" / "lab" / "families.py"):  # fmt: skip
        assert "research.relative" not in f.read_text(), f
    for f in (SRC / "research" / "relative").rglob("*.py"):
        text = f.read_text()
        for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.ops",
                       "market_signal.research.lab.forward", "telegram", "httpx", "INSERT ", "UPDATE "):  # fmt: skip
            assert banned not in text, (f, banned)
    from market_signal.ops import runtime as rt

    assert not any(
        "relative" in " ".join(map(str, args)) for job in rt.JOBS.values() for _, args in job
    )
