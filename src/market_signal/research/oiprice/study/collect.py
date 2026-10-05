"""Collect the bars, states, events and forward outcomes of the primary venue.

1. A ``Grid`` of the primary venue's coins (retained bars, OI and funding only). The
   comparison venue's snapshots are aligned separately (``align_snapshots``) and only ever
   feed the cross-venue members and descriptions.
2. ``Context``: what does not depend on the study parameters: BTC's regime (Phase 17
   vocabulary), each coin's own-volatility tercile, funding features, and for every bar and
   horizon the forward outcome of a LONG entered at the open of the first bar opening at/after
   the signal's availability (price AND OI) and exited at the close of the H-th bar.
3. ``Variant``: per parameter set, each coin's features, labels and state masks. Events are
   entries into a state (edge-triggered) inside the event window.

Because an outcome depends only on (coin, bar, horizon), events reuse the bar table:
``excess = d x (r - cell mean r)`` (both pay the same costs, which cancel), and
``net = d x r - 2 (fee + slippage)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.research.oiprice import primitives as op
from market_signal.research.oiprice.study.data import OiCoinData
from market_signal.research.oiprice.study.spec import Central, OiStudyDefinition
from market_signal.research.structure.series import NAT
from market_signal.research.structure.study import populations as sp

VOL_NAMES = {0: "vol_low", 1: "vol_mid", 2: "vol_high"}
TREND_NAMES = {1: "btc_up", -1: "btc_down", 0: "btc_unknown"}
BTC_VOL_NAMES = {0: "btc_quiet", 1: "btc_vol_mid", 2: "btc_volatile"}


@dataclass(frozen=True, eq=False)
class Context:
    grid: op.Grid
    in_window: np.ndarray
    third: np.ndarray
    btc_trend: np.ndarray
    btc_vol: np.ndarray
    vol_own: dict[str, np.ndarray]
    funding: dict[str, op.Funding]
    cost: dict[str, float]
    entry: dict[str, np.ndarray]  # k per bar (signal availability: price and OI)
    outcomes: pd.DataFrame  # (coin, t, horizon) -> long outcome columns
    comparison: dict[str, op.OiSeries]


def context(defn: OiStudyDefinition, coins: dict[str, OiCoinData],
            comparison: dict[str, OiCoinData]) -> Context:  # fmt: skip
    man = defn.manifest
    w = man.window
    series = {c: coins[c].bars for c in w.coins}
    g = op.Grid.build(series, {c: coins[c].oi for c in w.coins},
                      {c: (coins[c].funding_ns, coins[c].funding_rate) for c in w.coins})  # fmt: skip
    btc = series["BTC"]
    reg = sp.regime(btc, man.regime)
    pos = np.searchsorted(btc.open_time, g.open_time)
    pc = np.minimum(pos, max(len(btc) - 1, 0))
    ok = (pos < len(btc)) & (btc.open_time[pc] == g.open_time)
    trend = np.where(ok, reg.trend[pc], 0).astype(int)
    bvol = np.where(ok, reg.vol[pc], -1).astype(int)
    s, e = (pd.Timestamp(x).value for x in (w.event_start, w.event_end))
    inw = (g.close_time >= s) & (g.close_time < e)
    third = np.where(inw, np.minimum(((g.close_time - s) * 3) // (e - s), 2), -1).astype(int)
    vol_own = {c: op.own_vol(g, c)[1] for c in g.coins}
    fund = {c: op.funding_features(g, c, man.central.fund_window) for c in g.coins}
    cost = {c: defn.cost(w.venue, c).per_side for c in g.coins}
    entry, parts = {}, []
    idx = np.flatnonzero(inw)
    for c in g.coins:
        ready = np.where((g.ready[c] == NAT) | (g.oi_ready[c] == NAT), NAT,
                         np.maximum(g.ready[c], g.oi_ready[c]))  # fmt: skip
        k = op.entry_index(g, ready)
        entry[c] = k
        for h in man.horizons:
            p = op.forward_paths(g, c, k[idx], h)
            fpaid = sp.funding_paid(*g.funding[c], p.entry_ns, p.exit_ns, np.ones(len(idx)))
            last = np.minimum(k[idx] + h - 1, len(g) - 1)
            with np.errstate(invalid="ignore", divide="ignore"):
                rc = np.where(np.isfinite(p.ret), g.c[c][last] / g.c[c][idx] - 1, np.nan)
            parts.append(pd.DataFrame({
                "coin": c, "t": idx, "horizon": h, "k": k[idx], "ret": p.ret, "absret": p.absret,
                "range": p.rng, "mfe_long": p.mfe_long, "mae_long": p.mae_long,
                "mfe_short": p.mfe_short, "mae_short": p.mae_short, "fund_long": fpaid,
                "ret_close": rc,
            }))  # fmt: skip
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    comp = {c: d.oi for c, d in comparison.items()}
    return Context(g, inw, third, trend, bvol, vol_own, fund, cost, entry, out, comp)


# --------------------------------------------------------------------------- variants


@dataclass(frozen=True, eq=False)
class Variant:
    params: Central
    feats: dict[str, op.CoinFeatures]
    states: dict[str, dict[str, np.ndarray]]  # coin -> token -> mask
    labels: pd.DataFrame  # one row per (coin, eligible in-window bar)
    hl: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)


def states(f: op.CoinFeatures, fund: op.Funding, p: Central, hl_chg: np.ndarray | None) -> dict:
    """Every state token of one coin (see ``spec.FAMILIES``); False where undefined."""
    pz, s, z, us = f.pz, f.oi_s, f.oi_z, f.usd_s
    fin = lambda x: np.isfinite(x)  # noqa: E731
    with np.errstate(invalid="ignore"):
        st = {
            "P+": fin(pz) & (pz >= p.price_thr), "P-": fin(pz) & (pz <= -p.price_thr),
            "F0": fin(pz) & (np.abs(pz) < p.price_thr),
            "PS": fin(pz) & (np.abs(pz) < p.progress_split) & (np.sign(f.ret) != 0),
            "PB": fin(pz) & (np.abs(pz) >= p.progress_split),
            "O+": fin(s) & (s >= p.oi_thr), "O-": fin(s) & (s <= -p.oi_thr),
            "OL+": fin(s) & (s >= p.oi_large), "OL-": fin(s) & (s <= -p.oi_large),
            "OO+": fin(s) & (s >= p.oi_thr) & (s < p.oi_large),
            "OO-": fin(s) & (s <= -p.oi_thr) & (s > -p.oi_large),
            "UL+": fin(us) & (us >= p.oi_large), "UL-": fin(us) & (us <= -p.oi_large),
            "SZ+": fin(z) & (z >= p.shock_z), "SZ-": fin(z) & (z <= -p.shock_z),
            "FH": fin(fund.fund_pct) & (fund.fund_pct >= p.fund_tail),
            "FL": fin(fund.fund_pct) & (fund.fund_pct <= 1 - p.fund_tail),
            "CE+": fin(s) & (s >= p.crowd_oi), "CE-": fin(s) & (s <= -p.crowd_oi),
        }  # fmt: skip
    st["P"] = st["P+"] | st["P-"]
    st["FX"] = st["FH"] | st["FL"]
    st["DIV"] = (st["P+"] & st["O-"]) | (st["P-"] & st["O+"])
    if hl_chg is not None:
        moved = st["P"] & fin(s) & (np.abs(s) >= p.oi_thr) & fin(hl_chg) & (hl_chg != 0)
        same = np.sign(hl_chg) == np.sign(f.oi_chg)
        st["XA"], st["XD"] = moved & same, moved & ~same
    else:
        st["XA"] = st["XD"] = np.zeros(len(pz), dtype=bool)
    return st


def state_mask(st: dict[str, np.ndarray], pop: str) -> np.ndarray:
    out = None
    for tok in pop.split("&"):
        m = st[tok]
        out = m if out is None else out & m
    return out


def variant(ctx: Context, defn: OiStudyDefinition, p: Central) -> Variant:
    g = ctx.grid
    cmp_ = defn.manifest.comparison
    feats, sts, hl, rows = {}, {}, {}, []
    for c in g.coins:
        f = op.coin_features(g, c, p.lookback, p.norm_window)
        feats[c] = f
        a = None
        if c in ctx.comparison:
            a = op.align_snapshots(g, ctx.comparison[c], p.lookback, cmp_.max_age_s)
            hl[c] = a
        sts[c] = states(f, ctx.funding[c], p, a["chg"] if a is not None else None)
        fu = ctx.funding[c]
        el = f.eligible & ctx.in_window & (ctx.vol_own[c] >= 0)
        t = np.flatnonzero(el)
        fb = np.where(sts[c]["FH"], 2, np.where(sts[c]["FL"], 0, 1))
        fb = np.where(np.isfinite(fu.fund_pct), fb, -1)
        rows.append(pd.DataFrame({
            "coin": c, "t": t, "pz": f.pz[t], "ret_lb": f.ret[t], "psign": np.sign(f.ret[t]).astype(int),
            "pzb": op.pz_bucket(f.pz)[t], "oi_s": f.oi_s[t], "oi_z": f.oi_z[t],
            "usd_s": f.usd_s[t], "oi_chg": f.oi_chg[t], "usd_chg": f.usd_chg[t],
            "oi_to_volume": f.oi_to_volume[t], "fund_24h": fu.fund_24h[t],
            "fund_pct": fu.fund_pct[t], "fundb": fb[t], "vol": ctx.vol_own[c][t],
            "third": ctx.third[t], "btc_trend": ctx.btc_trend[t], "btc_vol": ctx.btc_vol[t],
        }))  # fmt: skip
    labels = pd.concat(rows, ignore_index=True)
    return Variant(p, feats, sts, labels, hl)


def events(var: Variant, ctx: Context, pop: str) -> pd.DataFrame:
    """(coin, t) of every in-window entry into ``pop`` on an eligible bar."""
    out = []
    for c, f in var.feats.items():
        el = f.eligible & (ctx.vol_own[c] >= 0)
        m = op.edge(state_mask(var.states[c], pop), el) & ctx.in_window
        t = np.flatnonzero(m)
        if len(t):
            out.append(pd.DataFrame({"coin": c, "t": t}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["coin", "t"])


def persistent_bars(var: Variant, ctx: Context, pops) -> dict:
    """In-window eligible bars in each state, before edge triggering and declustering."""
    out = {}
    for pop in pops:
        n = 0
        for c, f in var.feats.items():
            n += int((state_mask(var.states[c], pop) & f.eligible & ctx.in_window).sum())
        out[pop] = n
    return out


def comparison_coverage(var: Variant, ctx: Context) -> dict:
    """In-window hours with a defined comparison-venue OI change, per coin."""
    out = {}
    for c, a in var.hl.items():
        n = int((np.isfinite(a["chg"]) & ctx.in_window).sum())
        out[c] = {"aligned_hours": n,
                  "snapshots_in_window": int(np.isfinite(a["oi"])[ctx.in_window].sum()),
                  "snapshots_total": len(ctx.comparison[c])}  # fmt: skip
    return out


__all__ = ["Context", "Variant", "context", "events", "state_mask", "states", "variant"]
