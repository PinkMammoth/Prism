"""Per-venue statistics, BH families, controls, breadth, stability and sensitivity.

Primary metric of an ``event`` member (preregistered): the mean excess of its oriented
target at the primary horizon over its declared baseline cell, on independent events
(greedy, gap = horizon, per coin and orientation). For the direction target the excess is
``d x (r - cell mean r)``: event and baseline pay the same costs, so the excess measures what
the STATE adds; whether the position pays after fees, slippage and funding is the separate
``net_mean`` gate. For the volatility target it is ``|log r| - cell mean`` (no costs, no
direction).

The primary test is Phase 18's block-clustered t-test: calendar blocks of ``hp`` entry bars
shared by ALL coins, so six alts entering on one market shock are one block, not six
observations. An event-level iid t-test is reported beside it (``p_naive_iid``).

BH (``batch.benjamini_hochberg``) runs within one family. Untestable members (sample gates
on counts and coverage only) are excluded from m and stay visible as INSUFFICIENT.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.backtest.robustness import plateau_verdict
from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.oiprice.study import collect as cl
from market_signal.research.oiprice.study.spec import (
    AXIS_TOKENS,
    DIRECTION_LABELS,
    FAMILIES,
    Member,
    OiStudyDefinition,
    StatisticsSpec,
)
from market_signal.research.relative.study import analysis as rsa
from market_signal.research.relative.study import stats
from market_signal.research.structure.study import analysis as p17

_f, _q = p17._f, p17._q
independent = rsa.independent
BASE_KEYS = {
    "uncond": ["coin", "vol"],
    "price": ["coin", "vol", "psign", "pzb"],
    "fund": ["coin", "vol", "fundb"],
    "vol": ["coin", "vol", "pzb"],
}


# --------------------------------------------------------------------------- tables


def bar_table(var: cl.Variant, ctx: cl.Context, h: int) -> pd.DataFrame:
    """Every eligible in-window bar with its labels and its horizon-``h`` long outcome."""
    o = ctx.outcomes[ctx.outcomes["horizon"] == h]
    return var.labels.merge(o, on=["coin", "t"], how="left")


def _target_col(m: Member) -> str:
    return "absret" if m.target == "absret" else "ret"


def cell_means(bars: pd.DataFrame, m: Member) -> pd.Series:
    keys = BASE_KEYS[m.baseline]
    col = _target_col(m)
    b = bars[np.isfinite(bars[col])]
    if m.baseline == "fund":
        b = b[b["fundb"] >= 0]
    return b.groupby(keys)[col].mean().rename("base_mean")


def orient(rows: pd.DataFrame, m: Member) -> pd.DataFrame:
    if m.orient == "price":
        d = rows["psign"].to_numpy()
    else:
        d = np.ones(len(rows), dtype=int)
    rows = rows.assign(d=d)
    return rows[rows["d"] != 0]


def scored(ev: pd.DataFrame, bars: pd.DataFrame, m: Member, cost: dict[str, float],
           baseline: str | None = None) -> pd.DataFrame:  # fmt: skip
    """Events with outcome, net and excess against ``baseline`` (default: the member's)."""
    mm = m if baseline is None else Member(m.family, m.name, m.kind, m.target, m.pop, m.orient,
                                           baseline, m.pop_b)  # fmt: skip
    r = ev.merge(bars, on=["coin", "t"], how="inner")
    r = orient(r, mm)
    means = cell_means(bars, mm)
    r = r.join(means, on=BASE_KEYS[mm.baseline])
    d = r["d"].to_numpy(float)
    c = r["coin"].map(cost).to_numpy(float)
    if mm.target == "absret":
        r["excess"] = r["absret"] - r["base_mean"]
        r["gross"] = r["absret"]
        r["net"] = np.nan
        r["fund"] = np.nan
    else:
        r["excess"] = d * (r["ret"] - r["base_mean"])
        r["gross"] = d * r["ret"]
        r["net"] = r["gross"] - 2 * c
        r["fund"] = d * r["fund_long"]
    r["net_f"] = r["net"] - r["fund"]
    r["mfe"] = np.where(d > 0, r["mfe_long"], r["mfe_short"])
    r["mae"] = np.where(d > 0, r["mae_long"], r["mae_short"])
    r["close_excess"] = d * (r["ret_close"] - r["base_mean"]) if mm.target == "dir" else np.nan
    ok = np.isfinite(r["excess"].to_numpy(float)) & (r["vol"] >= 0)
    if mm.baseline == "fund":
        ok &= r["fundb"].to_numpy() >= 0
    r["evaluable"] = ok
    return r


def independent_rows(r: pd.DataFrame, h: int) -> pd.DataFrame:
    ok = r[r["evaluable"]].copy()
    ok["independent"] = independent(ok, h)
    return ok[ok["independent"]]


# --------------------------------------------------------------------------- members


def _empty(m: Member, hid: str, why: str) -> dict:
    return {"hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
            "orient": m.orient, "baseline": m.baseline, "independent_events": 0, "stat": None,
            "p_value": None, "testable": False, "untestable_reason": why, "sample_gates": {}}  # fmt: skip


def _cells(ind: pd.DataFrame) -> dict:
    if ind.empty:
        return {}

    def agg(key, names):
        return {names.get(k, str(k)): {"n": len(g), "excess_mean": _f(g["excess"].mean()),
                                       "assets": int(g["coin"].nunique())}
                for k, g in ind.groupby(key)}  # fmt: skip

    return {"thirds": agg("third", {0: "early", 1: "middle", 2: "late"}),
            "btc_trend": agg("btc_trend", cl.TREND_NAMES),
            "btc_vol": agg("btc_vol", cl.BTC_VOL_NAMES), "own_vol": agg("vol", cl.VOL_NAMES)}  # fmt: skip


def event_member(m: Member, var: cl.Variant, ctx: cl.Context, bars: pd.DataFrame, hp: int,
                 st: StatisticsSpec, hid: str, *, full: bool) -> dict:  # fmt: skip
    ev = cl.events(var, ctx, m.pop)
    if ev.empty:
        return _empty(m, hid, "no events")
    r = scored(ev, bars, m, ctx.cost)
    ind = independent_rows(r, hp)
    n_window = len(r)
    res = stats.clustered_mean(ind["excess"].to_numpy(), (ind["k"] // hp).to_numpy())
    naive = stats.clustered_mean(ind["excess"].to_numpy(), np.arange(len(ind)) * 2)
    out = {
        "hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
        "orient": m.orient, "baseline": m.baseline, "events_in_window": n_window,
        "events_evaluable": int(r["evaluable"].sum()), "independent_events": len(ind),
        "assets_with_events": int(ind["coin"].nunique()),
        "evaluable_fraction": _f(r["evaluable"].mean()) if n_window else None,
        "stat": res["mean"], "ci95": res["ci95"], "se": res["se"], "t": res["t"],
        "p_value": res["p_value"], "p_naive_iid": naive["p_value"], "clusters": res["clusters"],
        "df": res.get("df"),
        "net_mean": _f(ind["net"].mean()) if len(ind) and m.target == "dir" else None,
        "net_mean_reversal": _f((ind["net"] - 2 * ind["gross"]).mean()) if len(ind) and m.target == "dir" else None,
        "gross_mean": _f(ind["gross"].mean()) if len(ind) else None,
        "funding_mean": _f(ind["fund"].mean()) if len(ind) and m.target == "dir" else None,
        "net_incl_funding_mean": _f(ind["net_f"].mean()) if len(ind) and m.target == "dir" else None,
        "hit_rate": _f((ind["gross"] > 0).mean()) if len(ind) and m.target == "dir" else None,
    }  # fmt: skip
    gates = {
        "min_independent_events": len(ind) >= st.min_independent_events,
        "min_assets_with_events": ind["coin"].nunique() >= st.min_assets_with_events,
        "min_evaluable_fraction": bool(n_window) and r["evaluable"].mean() >= st.min_evaluable_fraction,
        "min_clusters": res["clusters"] >= st.min_clusters,
    }  # fmt: skip
    out["sample_gates"] = gates
    out["testable"] = all(gates.values()) and res["p_value"] is not None
    if not full:
        return out
    ind = ind.assign(net=ind["net"] if m.target == "dir" else ind["excess"])
    out["breadth"] = rsa._assets(ind, 1.0)
    out["stability"] = _cells(ind)
    if len(ind):
        out["paths"] = {"mfe_median": _q(ind["mfe"], 0.5), "mae_median": _q(ind["mae"], 0.5),
                        "mfe_p75": _q(ind["mfe"], 0.75), "mae_p25": _q(ind["mae"], 0.25),
                        "absret_median": _q(ind["absret"], 0.5),
                        "range_median": _q(ind["range"], 0.5)}  # fmt: skip
    if m.target == "dir" and len(ind):
        # The same independent events against the other controls (descriptive): does the
        # OI condition add anything beyond the matching price move / unconditional drift?
        out["controls"] = {}
        for b in ("uncond", "price", "fund"):
            rb = scored(ind[["coin", "t"]], bars, m, ctx.cost, baseline=b)
            rb = rb[rb["evaluable"]]
            out["controls"][b] = {
                "n": len(rb),
                "excess_mean": _f(rb["excess"].mean()) if len(rb) else None,
            }
        out["signal_close_entry"] = {
            "excess_mean": _f(ind["close_excess"].mean()),
            "note": "non-executable: entered at the signal bar's close, before OI availability",
        }  # fmt: skip
    return out


def contrast_member(m: Member, var: cl.Variant, ctx: cl.Context, bars: pd.DataFrame, hp: int,
                    st: StatisticsSpec, hid: str, *, full: bool) -> dict:  # fmt: skip
    sides = {}
    for label, pop in (("a", m.pop), ("b", m.pop_b)):
        ev = cl.events(var, ctx, pop)
        sides[label] = (
            independent_rows(scored(ev, bars, m, ctx.cost), hp) if len(ev) else pd.DataFrame()
        )
    a, b = sides["a"], sides["b"]
    if a.empty or b.empty:
        return _empty(m, hid, "no events on one side")
    res = stats.clustered_difference(a["excess"], a["k"] // hp, b["excess"], b["k"] // hp)
    out = {
        "hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
        "pop_b": m.pop_b, "orient": m.orient, "baseline": m.baseline,
        "independent_events": int(min(len(a), len(b))), "a_events": len(a), "b_events": len(b),
        "a_excess": _f(a["excess"].mean()), "b_excess": _f(b["excess"].mean()),
        "net_mean": _f(a["net"].mean()), "net_mean_reversal": _f((a["net"] - 2 * a["gross"]).mean()),
        "stat": res["mean"], "ci95": res["ci95"], "se": res["se"], "t": res["t"],
        "p_value": res["p_value"], "clusters": res["clusters"], "df": res.get("df"),
        "assets_with_events": int(min(a["coin"].nunique(), b["coin"].nunique())),
    }  # fmt: skip
    gates = {
        "min_independent_events": min(len(a), len(b)) >= st.min_independent_events,
        "min_assets_with_events": out["assets_with_events"] >= st.min_assets_with_events,
        "min_clusters": res["clusters"] >= st.min_clusters,
    }
    out["sample_gates"] = gates
    out["testable"] = all(gates.values()) and res["p_value"] is not None
    if full:
        per = {}
        for coin in sorted(set(a["coin"]) | set(b["coin"])):
            x, y = a[a["coin"] == coin]["excess"], b[b["coin"] == coin]["excess"]
            per[coin] = _f(x.mean() - y.mean()) if len(x) and len(y) else None
        vals = pd.Series({k: v for k, v in per.items() if v is not None}, dtype=float)
        out["breadth"] = {"per_asset_difference": per, "positive": int((vals > 0).sum()),
                          "negative": int((vals < 0).sum()), "median_asset_effect": _f(vals.median())}  # fmt: skip
    return out


def ic_samples(var: cl.Variant, ctx: cl.Context, bars: pd.DataFrame, hp: int) -> pd.DataFrame:
    """Non-overlapping cross-sections every ``hp`` bars from the window start."""
    idx = np.flatnonzero(ctx.in_window)
    if not len(idx) or bars.empty:
        return pd.DataFrame()
    ts = set(idx[(idx - idx[0]) % hp == 0].tolist())
    x = bars[bars["t"].isin(ts) & np.isfinite(bars["ret"])].copy()
    x["oi_s_resid"] = np.nan
    for _, g in x.groupby("t"):
        ok = np.isfinite(g["oi_s"]) & np.isfinite(g["pz"])
        if ok.sum() >= 3:
            A = np.column_stack([np.ones(ok.sum()), g.loc[ok, "pz"]])
            beta, *_ = np.linalg.lstsq(A, g.loc[ok, "oi_s"].to_numpy(), rcond=None)
            x.loc[g.index[ok], "oi_s_resid"] = g.loc[ok, "oi_s"] - A @ beta
    return x


def ic_member(m: Member, samples: pd.DataFrame, min_n: int, hp: int, st: StatisticsSpec, hid: str,
              *, full: bool, drop: str | None = None) -> dict:  # fmt: skip
    x = samples if drop is None else samples[samples["coin"] != drop]
    vals = []
    if len(x):
        for t, g in x.groupby("t"):
            g = g[np.isfinite(g[m.pop]) & np.isfinite(g["ret"])]
            if len(g) >= (min_n if drop is None else min_n - 1):
                s, f = g[m.pop].rank(), g["ret"].rank()
                if s.std() > 0 and f.std() > 0:
                    vals.append((t, float(s.corr(f)), int(g["third"].iloc[0])))
    if not vals:
        return _empty(m, hid, "no cross-sections")
    v = pd.DataFrame(vals, columns=["t", "value", "third"])
    res = stats.clustered_mean(v["value"].to_numpy(), np.arange(len(v)))
    out = {"hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
           "orient": m.orient, "baseline": m.baseline, "timestamps": len(v),
           "independent_events": len(v), "stat": res["mean"], "ci95": res["ci95"], "se": res["se"],
           "t": res["t"], "p_value": res["p_value"], "clusters": res["clusters"], "df": res.get("df"),
           "assets_with_events": int(x["coin"].nunique()) if len(x) else 0}  # fmt: skip
    gates = {"min_timestamps": len(v) >= st.min_timestamps,
             "min_assets_with_events": out["assets_with_events"] >= st.min_assets_with_events}  # fmt: skip
    out["sample_gates"] = gates
    out["testable"] = all(gates.values()) and res["p_value"] is not None
    if full:
        loo = {c: ic_member(m, samples, min_n, hp, st, hid, full=False, drop=c).get("stat")
               for c in sorted(samples["coin"].unique())}  # fmt: skip
        out["breadth"] = {"leave_one_out": loo}
        out["stability"] = {"thirds": {str(k): {"n": len(g), "mean": _f(g["value"].mean())}
                                       for k, g in v.groupby("third")}}  # fmt: skip
    return out


def evaluate_member(m: Member, var, ctx, bars, samples, hp, st, hid, min_n, *, full=True,
                    xv_gate: str | None = None) -> dict:  # fmt: skip
    if m.family == "cross_venue" and xv_gate:
        out = _empty(m, hid, xv_gate)
    elif m.kind == "event":
        out = event_member(m, var, ctx, bars, hp, st, hid, full=full)
    elif m.kind == "contrast":
        out = contrast_member(m, var, ctx, bars, hp, st, hid, full=full)
    else:
        out = ic_member(m, samples, min_n, hp, st, hid, full=full)
    if not out.get("testable") and "untestable_reason" not in out:
        failed = [g for g, ok in out.get("sample_gates", {}).items() if not ok]
        out["untestable_reason"] = "sample gate: " + ", ".join(failed) if failed else "no test"
    return out


def floor_of(m: Member, st: StatisticsSpec) -> float:
    return st.ic_floor if m.kind == "ic" else st.economic_floor


def directional(s: dict, sign: str) -> dict:
    """One member read in one direction (``positive`` = as oriented)."""
    out = rsa.directional(s, "continuation" if sign == "positive" else "reversal")
    if s.get("target") == "absret":
        out["net_mean"] = None
    return out


def verdict_kind(m: Member) -> str:
    """The Phase 18 verdict policy's position gate applies to direction positions only."""
    return "volatility" if m.target == "absret" else m.kind


def member_id(venue: str, m: Member) -> str:
    return f"{venue}|{m.family}|{m.name}"


# --------------------------------------------------------------------------- venue analysis


def xv_gate(var: cl.Variant, ctx: cl.Context, defn: OiStudyDefinition) -> tuple[str | None, dict]:
    cmp_ = defn.manifest.comparison
    cov = cl.comparison_coverage(var, ctx)
    good = [c for c, v in cov.items() if v["aligned_hours"] >= cmp_.min_aligned_hours]
    if len(good) >= cmp_.min_assets:
        return None, cov
    return (f"comparison venue coverage: {len(good)} coin(s) with >= {cmp_.min_aligned_hours} "
            f"aligned {cmp_.venue} hours (need {cmp_.min_assets})"), cov  # fmt: skip


def analyse(defn: OiStudyDefinition, ctx: cl.Context, var: cl.Variant,
            variants: dict[str, cl.Variant]) -> dict:  # fmt: skip
    man = defn.manifest
    st, hp, venue = man.statistics, man.primary_horizon, man.window.venue
    min_n = man.central.min_universe
    bars = bar_table(var, ctx, hp)
    samples = ic_samples(var, ctx, bars, hp)
    gate, cov = xv_gate(var, ctx, defn)
    out: dict = {"venue": venue, "families": {}, "comparison_coverage": cov,
                 "cross_venue_gate": gate or "passed"}  # fmt: skip
    for fam, ms in FAMILIES.items():
        rows = [evaluate_member(m, var, ctx, bars, samples, hp, st, member_id(venue, m), min_n,
                                xv_gate=gate) for m in ms]  # fmt: skip
        pv = {r["hypothesis"]: r["p_value"] for r in rows if r.get("testable")}
        qv = benjamini_hochberg(pv) if pv else {}
        for r in rows:
            r["q_value"] = qv.get(r["hypothesis"])
            r["in_family"] = r["hypothesis"] in qv
        out["families"][fam] = {"m": len(qv), "preregistered": len(rows), "members": rows}
    out["horizons"] = horizon_profile(var, ctx, man.horizons)
    out["descriptive"] = descriptive(var, ctx, bars, samples, hp, man.central)
    if variants:
        out["sensitivity"] = sensitivity(defn, ctx, variants, out)
    pops = sorted({m.pop for f in FAMILIES.values() for m in f if m.kind != "ic"}
                  | {m.pop_b for f in FAMILIES.values() for m in f if m.pop_b})  # fmt: skip
    out["counts"] = {"grid_bars": len(ctx.grid), "window_bars": int(ctx.in_window.sum()),
                     "eligible_bar_rows": len(var.labels), "outcome_rows": len(ctx.outcomes),
                     "persistent_bars": cl.persistent_bars(var, ctx, pops)}  # fmt: skip
    return out


def horizon_profile(var: cl.Variant, ctx: cl.Context, horizons) -> list[dict]:
    rows = []
    for ms in FAMILIES.values():
        for m in ms:
            if m.kind != "event" or m.family == "cross_venue":
                continue
            ev = cl.events(var, ctx, m.pop)
            for h in horizons:
                if ev.empty:
                    continue
                ind = independent_rows(scored(ev, bar_table(var, ctx, h), m, ctx.cost), h)
                rows.append({"family": m.family, "member": m.name, "horizon": h,
                             "independent_events": len(ind),
                             "excess_mean": _f(ind["excess"].mean()) if len(ind) else None,
                             "net_mean": _f(ind["net"].mean()) if len(ind) and m.target == "dir" else None,
                             "hit_rate": _f((ind["gross"] > 0).mean()) if len(ind) and m.target == "dir" else None})  # fmt: skip
    return rows


def _quintiles(x: pd.DataFrame, sig: str, tgt: str) -> dict:
    x = x[[sig, tgt]].replace([np.inf, -np.inf], np.nan).dropna()
    row = {"signal": sig, "target": tgt, "n": len(x)}
    if len(x) >= 25:
        row["spearman"] = _f(x[sig].rank().corr(x[tgt].rank()))
        qs = pd.qcut(x[sig].rank(method="first"), 5, labels=False)
        means = x.groupby(qs)[tgt].mean().to_numpy()
        row["quintile_means"] = [_f(v) for v in means]
        row["quintile_upper_bounds"] = [_f(v) for v in x.groupby(qs)[sig].max().to_numpy()]
        row["monotonicity"] = _f(pd.Series(means).rank().corr(pd.Series(range(5)).rank()))
    return row


def descriptive(var: cl.Variant, ctx: cl.Context, bars: pd.DataFrame, samples: pd.DataFrame,
                hp: int, p) -> dict:  # fmt: skip
    """Never tested, never used to choose a threshold."""
    out: dict = {}
    s = samples
    out["continuous"] = [_quintiles(s, a, b) for a, b in (
        ("oi_s", "ret"), ("usd_s", "ret"), ("pz", "ret"), ("oi_z", "ret"), ("fund_pct", "ret"),
        ("oi_s", "absret"), ("oi_z", "absret"), ("oi_to_volume", "absret"), ("pz", "absret"))]  # fmt: skip
    b = bars
    tot = max(len(b), 1)
    po, pm = b["pz"] >= p.price_thr, b["pz"] <= -p.price_thr
    oo, om = b["oi_s"] >= p.oi_thr, b["oi_s"] <= -p.oi_thr
    out["quadrant_occupancy"] = {
        "price_up_oi_up": _f((po & oo).sum() / tot), "price_up_oi_down": _f((po & om).sum() / tot),
        "price_down_oi_up": _f((pm & oo).sum() / tot), "price_down_oi_down": _f((pm & om).sum() / tot),
        "dead_zone_either": _f((~(po | pm) | ~(oo | om)).sum() / tot),
    }  # fmt: skip
    moved = np.abs(b["oi_s"]) >= p.oi_thr
    out["usd_vs_coin"] = {
        "corr_oi_chg_usd_chg": _f(b["oi_chg"].corr(b["usd_chg"])),
        "corr_usd_minus_coin_vs_log_price_return": _f((b["usd_chg"] - b["oi_chg"]).corr(np.log1p(b["ret_lb"]))),
        "direction_disagreement_share": _f((np.sign(b["oi_s"]) != np.sign(b["usd_s"]))[moved].mean()) if moved.any() else None,
        "note": "USD OI = coin OI x price, so its change carries the price return mechanically",
    }  # fmt: skip
    # Lead / lag (Spearman per coin on non-overlapping samples, then the mean across coins):
    # does OI move BEFORE price (OI now vs the next executable return) or AFTER it (the price
    # move now vs the OI change over the next hp bars)?
    ll = {"oi_now_vs_next_return": [], "price_now_vs_next_oi_change": [],
          "contemporaneous_oi_vs_price": []}  # fmt: skip
    g = ctx.grid
    for c, f in var.feats.items():
        x = s[s["coin"] == c]
        if len(x) < 10:
            continue
        t = x["t"].to_numpy()
        nxt = np.full(len(t), np.nan)
        ok = t + hp < len(g)
        with np.errstate(invalid="ignore", divide="ignore"):
            nxt[ok] = np.log(g.oi[c][t[ok] + hp] / g.oi[c][t[ok]])
        df = pd.DataFrame({"oi": x["oi_s"].to_numpy(), "ret": x["ret"].to_numpy(), "pz": x["pz"].to_numpy(),
                           "nxt": nxt, "chg": f.oi_chg[t], "r": f.ret[t]}).dropna()  # fmt: skip
        if len(df) >= 10:
            ll["oi_now_vs_next_return"].append(df["oi"].rank().corr(df["ret"].rank()))
            ll["price_now_vs_next_oi_change"].append(df["pz"].rank().corr(df["nxt"].rank()))
            ll["contemporaneous_oi_vs_price"].append(df["chg"].rank().corr(df["r"].rank()))
    out["lead_lag"] = {k: {"mean_spearman": _f(np.mean(v)) if v else None, "coins": len(v),
                           "per_coin": [_f(x) for x in v]} for k, v in ll.items()}  # fmt: skip
    cov = {}
    for c in g.coins:
        fu = ctx.funding[c]
        w = ctx.in_window
        cov[c] = {"fund_pct_defined_share": _f(np.isfinite(fu.fund_pct[w]).mean()) if w.any() else None,
                  "settlements_per_24h_median": _f(np.median(fu.settlements_24h[w])) if w.any() else None,
                  "fund_24h_positive_share": _f((fu.fund_24h[w] > 0).mean()) if w.any() else None}  # fmt: skip
    out["funding"] = cov
    if var.hl:
        agree = {}
        for c, a in var.hl.items():
            chg = a["chg"][ctx.in_window]
            bn = var.feats[c].oi_chg[ctx.in_window]
            ok = np.isfinite(chg) & np.isfinite(bn)
            agree[c] = {"aligned_hours": int(ok.sum()),
                        "sign_agreement": _f((np.sign(chg[ok]) == np.sign(bn[ok])).mean()) if ok.sum() >= 10 else None,
                        "spearman": _f(pd.Series(chg[ok]).rank().corr(pd.Series(bn[ok]).rank())) if ok.sum() >= 10 else None}  # fmt: skip
        out["cross_venue"] = agree
    return out


def _tokens(m: Member) -> set[str]:
    pops = [m.pop] + ([m.pop_b] if m.pop_b else [])
    return {t.rstrip("+-") for p in pops for t in p.split("&")}


def sensitivity(defn: OiStudyDefinition, ctx: cl.Context, variants: dict[str, cl.Variant],
                out: dict) -> list[dict]:  # fmt: skip
    """One-at-a-time neighbours per member and Prism's ``plateau_verdict`` per direction.
    Descriptive only: never tested or selected."""
    man = defn.manifest
    st, hp, min_n = man.statistics, man.primary_horizon, man.central.min_universe
    vals = {a.name: list(a.values) for a in man.axes}
    keyed = {key: moved for key, _, moved in man.variants()}
    cache = {}
    rows = []
    for fam, ms in FAMILIES.items():
        central = {r["member"]: r for r in out["families"][fam]["members"]}
        for m in ms:
            c = central[m.name]
            if not c.get("testable"):
                continue
            toks = _tokens(m) if m.kind != "ic" else set()
            axes = [a for a in vals if "*" in AXIS_TOKENS[a] or toks & set(AXIS_TOKENS[a])]
            if not axes:
                continue
            default = {a: man.central.value(a) for a in axes}
            by_variant = []
            for key, var in variants.items():
                ax = next(iter(keyed[key]))
                if ax not in axes:
                    continue
                if key not in cache:
                    bars = bar_table(var, ctx, hp)
                    cache[key] = (bars, ic_samples(var, ctx, bars, hp))
                bars, samples = cache[key]
                s = evaluate_member(m, var, ctx, bars, samples, hp, st, key, min_n, full=False)
                by_variant.append({**default, ax: keyed[key][ax], "variant": key,
                                   "n_indep": s.get("independent_events") or 0, "stat": s.get("stat")})  # fmt: skip
            row = {"family": fam, "member": m.name, "axes": axes,
                   "neighbours": [{"variant": v["variant"], "independent_events": v["n_indep"],
                                   "stat": _f(v["stat"])} for v in by_variant]}  # fmt: skip
            for direction, sign in (("positive", 1.0), ("negative", -1.0)):
                ce = c.get("stat")
                table = pd.DataFrame(
                    [{**default, "n_indep": c.get("independent_events") or 0,
                      "excess_mean": np.nan if ce is None else sign * ce, "variant": "central"}]
                    + [{**{a: v[a] for a in axes}, "n_indep": v["n_indep"],
                        "excess_mean": np.nan if v["stat"] is None else sign * v["stat"],
                        "variant": v["variant"]} for v in by_variant])  # fmt: skip
                min_ev = st.min_timestamps if m.kind == "ic" else st.min_independent_events
                row[f"plateau_{direction}"] = plateau_verdict(
                    table, default, {a: vals[a] for a in axes}, min_ev, 0.5, 0.5
                ).get("verdict")
            sv = [v["stat"] for v in by_variant if v["stat"] is not None]
            if c.get("stat") is not None:
                sv.append(c["stat"])
            row["stat_range"] = [_f(min(sv)), _f(max(sv))] if sv else [None, None]
            row["sign_agreement"] = (_f(np.mean([np.sign(v) == np.sign(c["stat"]) for v in sv]))
                                     if sv and c.get("stat") is not None else None)  # fmt: skip
            rows.append(row)
    return rows


def labels_for(m: Member) -> tuple[str, str]:
    return DIRECTION_LABELS[m.orient]


__all__ = ["analyse", "directional", "evaluate_member", "floor_of", "labels_for", "member_id",
           "verdict_kind"]  # fmt: skip
