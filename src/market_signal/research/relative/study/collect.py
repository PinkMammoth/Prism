"""Collect every event, baseline bar and cross-section sample of one venue and architecture.

Nothing here aggregates across venues; ``analysis`` pools coins within one venue only.

1. a ``Panel`` of the venue's coins on BTC's grid (retained snapshot bars only);
2. BTC's regime vocabulary (Phase 17 ``regime``: trend, range = consolidation, volatility
   tercile), read at the signal bar;
3. per parameter variant: pair features per alt, the cross-section, the populations
   (threshold crossings, leader/laggard entries, correlation breakdowns), and for each
   event and every baseline bar the forward outcome of all three targets:

   - ``rel``: d x (r_alt - r_BTC), costs on both legs, funding on both legs;
   - ``res``: d x (r_alt - beta(t) r_BTC), costs on the alt and |beta| x BTC;
   - ``usd``: d x r_alt, costs and funding on the alt only;

   entered at the open of the first bar opening at/after the signal's availability
   (assumed latency) and exited at the close of the H-th bar. ``d`` is the CONTINUATION
   orientation (+1 long the alt side); reversal is its exact negative.
4. central variant only: every horizon, the non-executable signal-close entry and the
   H6 -> breakdown confirmation decomposition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.research.relative import primitives as rp
from market_signal.research.relative.study.spec import (
    Architecture,
    Central,
    RelativeStudyDefinition,
)
from market_signal.research.structure.series import NAT
from market_signal.research.structure.study import populations as sp
from market_signal.research.structure.study.data import CoinData

VOL_NAMES = {0: "vol_low", 1: "vol_mid", 2: "vol_high"}
TREND_NAMES = {1: "btc_up", -1: "btc_down", 0: "btc_unknown"}


@dataclass
class Frames:
    events: pd.DataFrame
    baseline: pd.DataFrame
    xs: pd.DataFrame


@dataclass
class Collected:
    venue: str
    arch: str
    central: Frames | None = None
    variants: dict[str, Frames] = field(default_factory=dict)
    decomposition: pd.DataFrame = field(default_factory=pd.DataFrame)
    inputs: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)


@dataclass(frozen=True, eq=False)
class Context:
    """Everything a variant needs that does not depend on its parameters."""

    panel: rp.Panel
    in_window: np.ndarray  # signal bars closing inside the event window
    trend: np.ndarray
    consolidation: np.ndarray
    vol: np.ndarray
    third: np.ndarray  # chronological thirds of the event window (0/1/2, -1 outside)
    cost: dict[str, float]  # per side, per coin
    funding: dict[str, tuple[np.ndarray, np.ndarray]]


def context(defn: RelativeStudyDefinition, arch: Architecture, venue: str,
            coins: dict[str, CoinData]) -> Context:  # fmt: skip
    tf = arch.timeframe
    w = arch.window(venue)
    series = {c: coins[c].series[tf] for c in w.coins}
    panel = rp.Panel.from_series(series, reference=defn.manifest.reference)
    btc = series[defn.manifest.reference]
    reg = sp.regime(btc, arch.regime)
    pos = np.searchsorted(btc.open_time, panel.open_time)
    ok = (pos < len(btc)) & (btc.open_time[np.minimum(pos, len(btc) - 1)] == panel.open_time)
    pc = np.minimum(pos, max(len(btc) - 1, 0))
    trend = np.where(ok, reg.trend[pc], 0) if len(btc) else np.zeros(len(panel), int)
    cons = np.where(ok, reg.is_range[pc], False) if len(btc) else np.zeros(len(panel), bool)
    vol = np.where(ok, reg.vol[pc], -1) if len(btc) else np.full(len(panel), -1)
    s, e = (pd.Timestamp(x).value for x in (w.event_start, w.event_end))
    inw = (panel.close_time >= s) & (panel.close_time < e)
    third = np.where(inw, np.minimum(((panel.close_time - s) * 3) // (e - s), 2), -1)
    cost = {c: defn.cost(venue, c).per_side for c in w.coins}
    funding = {c: (coins[c].funding_ns, coins[c].funding_rate) for c in w.coins}
    return Context(panel, inw, trend.astype(int), cons.astype(bool), vol.astype(int),
                   third.astype(int), cost, funding)  # fmt: skip


# --------------------------------------------------------------------------- features


@dataclass(frozen=True, eq=False)
class Features:
    params: Central
    pairs: dict[str, rp.PairFeatures]
    xs_rel: rp.CrossSection
    xs_res: rp.CrossSection
    xs_ready: np.ndarray
    pullback: np.ndarray
    breakdown: dict[str, rp.Breakdown]


def features(ctx: Context, p: Central) -> Features:
    panel = ctx.panel
    bask = rp.basket(panel, p.lookback)
    pairs = {a: rp.pair_features(panel, a, p.lookback, p.beta_window, bask) for a in panel.alts}
    alts = panel.alts
    elig = (
        np.column_stack([pairs[a].eligible for a in alts])
        if alts
        else np.zeros((len(panel), 0), bool)
    )
    rel = np.column_stack([pairs[a].rel for a in alts]) if alts else elig.astype(float)
    res = np.column_stack([pairs[a].res for a in alts]) if alts else elig.astype(float)
    xs_rel = rp.rank_cross_section(rel, elig, alts)
    xs_res = rp.rank_cross_section(res, elig, alts)
    rd = np.vstack([panel.ready[c] for c in panel.coins])
    xs_ready = np.where((rd != NAT).any(axis=0), rd.max(axis=0), NAT)
    r_btc = rp.window_return(panel.c[panel.reference], p.lookback)
    pull = np.isfinite(r_btc) & np.isfinite(bask[0]) & (r_btc < 0) & (bask[0] < 0)
    bd = {a: rp.breakdown(panel, a, p.beta_window, p.corr_short, p.corr_high, p.corr_drop)
          for a in alts}  # fmt: skip
    return Features(p, pairs, xs_rel, xs_res, xs_ready, pull, bd)


def populations(f: Features, ctx: Context) -> dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """pop -> coin -> (signal bar indices inside the window, continuation orientation d)."""
    p = f.params
    out: dict[str, dict] = {k: {} for k in ("raw+", "raw-", "rel+", "rel-", "mkt+", "mkt-",
                                             "res+", "res-", "top", "bottom", "bd")}  # fmt: skip
    top = f.xs_rel.top(p.min_universe)
    bot = f.xs_rel.bottom(p.min_universe)
    defined = f.xs_rel.n >= p.min_universe
    for j, a in enumerate(ctx.panel.alts):
        pf = f.pairs[a]
        for m in ("raw", "rel", "mkt", "res"):
            z = pf.z(m)
            for sign, fn in (("+", rp.cross_up), ("-", rp.cross_down)):
                t = np.flatnonzero(fn(z, p.z_threshold) & ctx.in_window)
                out[m + sign][a] = (t, np.full(len(t), 1 if sign == "+" else -1))
        for name, mask, d in (("top", top[:, j], 1), ("bottom", bot[:, j], -1)):
            t = np.flatnonzero(rp.edge(mask, defined) & ctx.in_window)
            out[name][a] = (t, np.full(len(t), d))
        b = f.breakdown[a]
        t = np.flatnonzero(b.event & ctx.in_window)
        out["bd"][a] = (t, b.sign[t])
    return out


# --------------------------------------------------------------------------- outcomes


def _forward_close(panel: rp.Panel, coin: str, t: np.ndarray, h: int) -> np.ndarray:
    """Non-executable reference: close of the signal bar to the close of bar t + h."""
    wr = rp.window_return(panel.c[coin], h)
    j = t + h
    out = np.full(len(t), np.nan)
    ok = j < len(panel)
    out[ok] = wr[j[ok]]
    return out


def outcomes(ctx: Context, f: Features, coin: str, t: np.ndarray, d: np.ndarray,
             ready: np.ndarray, horizons, *, close_entry: bool) -> pd.DataFrame:  # fmt: skip
    """Rows (t, horizon) with every target's gross / net / funding for orientation ``d``."""
    panel, ref = ctx.panel, ctx.panel.reference
    pf = f.pairs[coin]
    k = rp.entry_index(panel, ready[t])
    beta = pf.beta[t]
    ca, cb = ctx.cost[coin], ctx.cost[ref]
    parts = []
    for h in horizons:
        ra, rb = rp.forward_return(panel, coin, k, h), rp.forward_return(panel, ref, k, h)
        kc = np.minimum(k, len(panel) - 1)
        last = np.minimum(k + h - 1, len(panel) - 1)
        ent = np.where(k < len(panel), panel.open_time[kc], NAT)
        ext = np.where(np.isfinite(ra), panel.close_time[last], NAT)
        fa = sp.funding_paid(*ctx.funding[coin], ent, ext, np.ones(len(t)))
        fb = sp.funding_paid(*ctx.funding[ref], ent, ext, np.ones(len(t)))
        g_rel, g_res, g_usd = d * (ra - rb), d * (ra - beta * rb), d * ra
        n_rel = g_rel - 2 * (ca + cb)
        n_res = g_res - 2 * (ca + np.abs(beta) * cb)
        n_usd = g_usd - 2 * ca
        row = {
            "coin": coin, "t": t, "k": k, "d": d, "horizon": h, "r_alt": ra, "r_btc": rb,
            "beta": beta, "g_rel": g_rel, "g_res": g_res, "g_usd": g_usd,
            "n_rel": n_rel, "n_res": n_res, "n_usd": n_usd,
            "f_rel": d * (fa - fb), "f_res": d * (fa - beta * fb), "f_usd": d * fa,
        }  # fmt: skip
        if close_entry:
            rac, rbc = _forward_close(panel, coin, t, h), _forward_close(panel, ref, t, h)
            row["nc_rel"] = d * (rac - rbc) - 2 * (ca + cb)
            row["nc_res"] = d * (rac - beta * rbc) - 2 * (ca + np.abs(beta) * cb)
            row["nc_usd"] = d * rac - 2 * ca
        parts.append(pd.DataFrame(row))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(out):
        tt = out["t"].to_numpy()
        out["vol"] = ctx.vol[tt]
        out["trend"] = ctx.trend[tt]
        out["consolidation"] = ctx.consolidation[tt]
        out["pullback"] = f.pullback[tt]
        out["tight"] = pf.corr_prior[tt] >= f.params.corr_high
        out["third"] = ctx.third[tt]
        out["signal_ns"] = panel.close_time[tt]
    return out


def frames(ctx: Context, f: Features, horizons, *, extras: bool) -> Frames:
    pops = populations(f, ctx)
    ev = []
    for pop, per in pops.items():
        for coin, (t, d) in per.items():
            if not len(t):
                continue
            ready = f.xs_ready if pop in ("top", "bottom") else f.pairs[coin].ready
            o = outcomes(ctx, f, coin, t, d, ready, horizons, close_entry=extras)
            o.insert(0, "pop", pop)
            ev.append(o)
    base = []
    for coin in ctx.panel.alts:
        t = np.flatnonzero(f.pairs[coin].eligible & ctx.in_window)
        for dd in (1, -1):
            o = outcomes(ctx, f, coin, t, np.full(len(t), dd), f.pairs[coin].ready, horizons,
                         close_entry=extras)  # fmt: skip
            base.append(o)
    events = pd.concat(ev, ignore_index=True) if ev else pd.DataFrame()
    baseline = pd.concat(base, ignore_index=True) if base else pd.DataFrame()
    return Frames(events, baseline, pd.DataFrame())


def xs_frame(ctx: Context, f: Features, hp: int) -> pd.DataFrame:
    """Non-overlapping cross-sections every ``hp`` bars from the window start: one row per
    (sample bar, ranked alt) with ranks, signal values and forward (gross) targets. Entries
    ``hp`` bars apart have disjoint forward windows."""
    panel = ctx.panel
    idx = np.flatnonzero(ctx.in_window)
    if not len(idx):
        return pd.DataFrame()
    t = idx[(idx - idx[0]) % hp == 0]
    k = rp.entry_index(panel, f.xs_ready[t])
    rb = rp.forward_return(panel, panel.reference, k, hp)
    rows = []
    for j, a in enumerate(panel.alts):
        pf = f.pairs[a]
        ra = rp.forward_return(panel, a, k, hp)
        rows.append(pd.DataFrame({
            "t": t, "k": k, "coin": a, "n": f.xs_rel.n[t], "rank_rel": f.xs_rel.rank[t, j],
            "rank_res": f.xs_res.rank[t, j], "rel": pf.rel[t], "res": pf.res[t],
            "beta": pf.beta[t], "r_alt": ra, "r_btc": rb, "fwd_rel": ra - rb,
            "fwd_res": ra - pf.beta[t] * rb, "cost": ctx.cost[a], "third": ctx.third[t],
            "trend": ctx.trend[t], "consolidation": ctx.consolidation[t], "vol": ctx.vol[t],
            "pullback": f.pullback[t],
            # correlation change (short window minus the prior long window) and the forward
            # residual oriented with the residual move over that short window
            "dcorr": f.breakdown[a].rho_short[t] - f.breakdown[a].rho_prior[t],
            "fwd_res_oriented": f.breakdown[a].sign[t] * (ra - pf.beta[t] * rb),
        }))  # fmt: skip
    out = pd.concat(rows, ignore_index=True)
    return out[out["rank_rel"] > 0].reset_index(drop=True)


# --------------------------------------------------------------------------- decomposition


def decomposition(ctx: Context, f: Features, hp: int) -> pd.DataFrame:
    """H6 (residual shock while tightly correlated) followed by a same-direction correlation
    breakdown within ``confirm_within`` bars: the H6-entry outcome of those chains (a
    selection that uses later information: NOT executable) vs the outcome entered when the
    breakdown is observable (executable). Target: residual, primary horizon."""
    p = f.params
    rows = []
    for a in ctx.panel.alts:
        pf, b = f.pairs[a], f.breakdown[a]
        tight = pf.corr_prior >= p.corr_high
        bd_t = np.flatnonzero(b.event)
        for sign, fn in ((1, rp.cross_up), (-1, rp.cross_down)):
            t6 = np.flatnonzero(fn(pf.z_res, p.z_threshold) & tight & ctx.in_window)
            for t in t6:
                later = bd_t[(bd_t > t) & (bd_t <= t + p.confirm_within)]
                later = later[b.sign[later] == sign]
                rows.append({"coin": a, "t6": int(t), "d": sign,
                             "t5": int(later[0]) if len(later) else -1})  # fmt: skip
    if not rows:
        return pd.DataFrame()
    dec = pd.DataFrame(rows)
    out = []
    for coin, g in dec.groupby("coin"):
        d = g["d"].to_numpy()
        o6 = outcomes(ctx, f, coin, g["t6"].to_numpy(), d, f.pairs[coin].ready, (hp,),
                      close_entry=False)  # fmt: skip
        conf = g["t5"].to_numpy() >= 0
        t5 = np.where(conf, g["t5"].to_numpy(), g["t6"].to_numpy())
        o5 = outcomes(ctx, f, coin, t5, d, f.pairs[coin].ready, (hp,), close_entry=False)
        out.append(pd.DataFrame({
            "coin": coin, "t6": g["t6"].to_numpy(), "t5": g["t5"].to_numpy(), "d": d,
            "confirmed": conf, "k6": o6["k"].to_numpy(), "k5": o5["k"].to_numpy(),
            "n6": o6["n_res"].to_numpy(), "n5": np.where(conf, o5["n_res"].to_numpy(), np.nan),
            "vol6": o6["vol"].to_numpy(), "vol5": o5["vol"].to_numpy(),
            "entry_move": np.where(conf, d * (ctx.panel.o[coin][np.minimum(o5["k"], len(ctx.panel) - 1)]
                                              / ctx.panel.o[coin][np.minimum(o6["k"], len(ctx.panel) - 1)] - 1), np.nan),
        }))  # fmt: skip
    return pd.concat(out, ignore_index=True)


# --------------------------------------------------------------------------- orchestration


def collect(defn: RelativeStudyDefinition, arch: Architecture, venue: str,
            coins: dict[str, CoinData]) -> Collected:  # fmt: skip
    col = Collected(venue=venue, arch=arch.name)
    ctx = context(defn, arch, venue, coins)
    tf = arch.timeframe
    hp = arch.primary_horizon
    for c in arch.window(venue).coins:
        s = coins[c].series[tf]
        col.inputs[c] = {"timeframe": tf, "rows": len(s), "dataset_id": coins[c].dataset_id,
                         "first_open": int(s.open_time[0]) if len(s) else None,
                         "funding_rows": len(coins[c].funding_ns)}  # fmt: skip
    for key, params, _ in arch.variants():
        f = features(ctx, params)
        central = key == "central"
        fr = frames(ctx, f, arch.horizons if central else (hp,), extras=central)
        fr.xs = xs_frame(ctx, f, hp)
        if central:
            col.central = fr
            col.decomposition = decomposition(ctx, f, hp)
            col.universe = universe_summary(ctx, f)
            col.universe["persistent_bars"] = persistent_bars(ctx, f)
            col.counts = {"grid_bars": len(ctx.panel), "window_bars": int(ctx.in_window.sum()),
                          "baseline_rows": len(fr.baseline), "event_rows": len(fr.events),
                          "xs_rows": len(fr.xs)}  # fmt: skip
        else:
            col.variants[key] = fr
    return col


def persistent_bars(ctx: Context, f: Features) -> dict:
    """In-window bars in each state (every bar beyond the threshold / ranked top or bottom),
    before edge triggering and declustering reduce them to events."""
    p, w = f.params, ctx.in_window
    out = {}
    for m in ("raw", "rel", "mkt", "res"):
        z = [f.pairs[a].z(m) for a in ctx.panel.alts]
        out[m + "+"] = int(sum(((x >= p.z_threshold) & w).sum() for x in z))
        out[m + "-"] = int(sum(((x <= -p.z_threshold) & w).sum() for x in z))
    out["top"] = int((f.xs_rel.top(p.min_universe) & w[:, None]).sum())
    out["bottom"] = int((f.xs_rel.bottom(p.min_universe) & w[:, None]).sum())
    return out


def universe_summary(ctx: Context, f: Features) -> dict:
    """Eligible-universe size over the event window and each alt's first eligible bar."""
    n = f.xs_rel.n[ctx.in_window]
    first = {}
    for a in ctx.panel.alts:
        e = np.flatnonzero(f.pairs[a].eligible & ctx.in_window)
        first[a] = int(ctx.panel.close_time[e[0]]) if len(e) else None
    return {"universe_size_counts": {int(k): int(v) for k, v in zip(*np.unique(n, return_counts=True), strict=True)},
            "first_eligible_close_ns": first,
            "consolidation_share": float(ctx.consolidation[ctx.in_window].mean()) if ctx.in_window.any() else None,
            "pullback_share": float(f.pullback[ctx.in_window].mean()) if ctx.in_window.any() else None}  # fmt: skip
