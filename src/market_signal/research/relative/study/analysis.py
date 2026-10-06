"""Per-venue (never pooled across venues) statistics, families, breadth and stability.

Primary metric of an ``event`` member (preregistered): the mean NET excess of its
continuation-oriented target at the primary horizon over the matched baseline cell (same
coin, orientation, horizon and BTC volatility tercile; plus the condition's own label for
conditional members), on independent events (greedy, gap = horizon, per coin and
orientation). Both the event and the baseline pay the same costs, so the excess measures
what the signal's TIMING adds; whether the position itself pays is the separate
``net_mean`` gate. Continuation and reversal are the two signs of one two-sided test.

BH (``batch.benjamini_hochberg``, the Lab's implementation) runs within one family of one
venue x architecture. Untestable members (sample gates on counts and coverage only) are
excluded from m and stay visible as INSUFFICIENT.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.backtest.events import decluster
from market_signal.backtest.robustness import plateau_verdict
from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.relative.study import stats
from market_signal.research.relative.study.collect import TREND_NAMES, VOL_NAMES, Collected, Frames
from market_signal.research.relative.study.spec import (
    AXIS_POPS,
    FAMILIES,
    TARGETS,
    Architecture,
    Member,
    RelativeStudyDefinition,
    StatisticsSpec,
)
from market_signal.research.structure.study import analysis as p17

_f, _q, _rng = p17._f, p17._q, p17._rng


# --------------------------------------------------------------------------- building blocks


def independent(df: pd.DataFrame, gap: int) -> np.ndarray:
    """Greedy declustering per (coin, orientation) on the entry bar, gap = ``gap`` bars, at
    most one event per entry bar (Phase 17's rule)."""
    out = np.zeros(len(df), dtype=bool)
    if not len(df):
        return out
    k = df["k"].to_numpy()
    for (_, _), idx in df.groupby(["coin", "d"]).indices.items():
        keep = set(decluster(k[idx], gap).tolist())
        seen: set[int] = set()
        for i in idx[np.argsort(k[idx], kind="stable")]:
            if k[i] in keep and k[i] not in seen:
                out[i] = True
                seen.add(k[i])
    return out


def cell_keys(cond: str | None) -> list[str]:
    return ["coin", "d", "vol"] + ([cond] if cond else [])


def with_excess(rows: pd.DataFrame, base: pd.DataFrame, target: str, cond: str | None,
                col: str = "n") -> pd.DataFrame:  # fmt: skip
    """Attach ``excess`` = net - its matched baseline cell mean (same horizon)."""
    keys = cell_keys(cond)
    c = f"{col}_{target}"
    b = base[np.isfinite(base[c])]
    means = b.groupby(["horizon", *keys])[c].mean().rename("base_mean")
    r = rows.join(means, on=["horizon", *keys])
    r = r.assign(net=r[c], gross=r[f"g_{target}"], fund=r[f"f_{target}"])
    r["excess"] = r["net"] - r["base_mean"]
    r["evaluable"] = np.isfinite(r["excess"].to_numpy(float)) & (r["vol"] >= 0)
    return r


def population(ev: pd.DataFrame, m: Member, h: int, inside: bool | None = True) -> pd.DataFrame:
    if ev.empty:
        return ev
    r = ev[(ev["pop"] == m.pop) & (ev["horizon"] == h)]
    if m.condition and inside is not None:
        r = r[r[m.condition].astype(bool) == inside]
    return r


def _assets(ind: pd.DataFrame, sign: float) -> dict:
    """Per-asset breadth of independent events, oriented so that ``sign`` > 0 is the effect."""
    if ind.empty:
        return {}
    g = ind.groupby("coin")
    per = pd.DataFrame({"n": g.size(), "excess_mean": g["excess"].mean(), "net_mean": g["net"].mean(),
                        "excess_sum": g["excess"].sum()})  # fmt: skip
    eff = per["excess_mean"] * sign
    contrib = per["excess_sum"].abs()
    loo = {}
    for coin in per.index:
        rest = ind[ind["coin"] != coin]["excess"]
        loo[coin] = _f(rest.mean()) if len(rest) else None
    return {
        "per_asset": {c: {"n": int(r["n"]), "excess_mean": _f(r["excess_mean"]),
                          "net_mean": _f(r["net_mean"])} for c, r in per.iterrows()},
        "positive": int((eff > 0).sum()), "negative": int((eff < 0).sum()),
        "median_asset_effect": _f(eff.median()),
        "largest_share_of_abs_contribution": _f(contrib.max() / contrib.sum()) if contrib.sum() else None,
        "leave_one_out_excess": loo,
    }  # fmt: skip


def _cells(ind: pd.DataFrame, col: str = "excess") -> dict:
    """Descriptive excess by chronological third, BTC regime and volatility tercile."""
    if ind.empty:
        return {}

    def agg(key, names):
        out = {}
        for k, g in ind.groupby(key):
            out[names.get(k, str(k)) if names else str(k)] = {"n": len(g), "excess_mean": _f(g[col].mean()),
                                                             "assets": int(g["coin"].nunique())}  # fmt: skip
        return out

    reg = np.where(ind["consolidation"].astype(bool), "btc_consolidation",
                   ind["trend"].map(TREND_NAMES).to_numpy())  # fmt: skip
    return {"thirds": agg("third", {0: "early", 1: "middle", 2: "late"}),
            "btc_regime": agg(reg, None), "btc_vol": agg("vol", VOL_NAMES)}  # fmt: skip


# --------------------------------------------------------------------------- members


def event_member(m: Member, fr: Frames, hp: int, st: StatisticsSpec, hid: str, *,
                 full: bool) -> dict:  # fmt: skip
    """``event`` members: matched net excess of the continuation-oriented target."""
    rows = population(fr.events, m, hp)
    if rows.empty:
        return _empty(m, hid, "no events")
    r = with_excess(rows, fr.baseline, m.target, m.condition)
    ok = r[r["evaluable"]].copy()
    ok["independent"] = independent(ok, hp)
    ind = ok[ok["independent"]]
    n_window = len(r)
    res = stats.clustered_mean(ind["excess"].to_numpy(), (ind["k"] // hp).to_numpy())
    rev_net = (ind["net"] - 2 * ind["gross"]) if len(ind) else pd.Series(dtype=float)
    out = {
        "hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
        "condition": m.condition, "events_in_window": n_window, "events_evaluable": len(ok),
        "independent_events": len(ind), "assets_with_events": int(ind["coin"].nunique()),
        "evaluable_fraction": _f(len(ok) / n_window) if n_window else None,
        "stat": res["mean"], "ci95": res["ci95"], "se": res["se"], "t": res["t"],
        "p_value": res["p_value"], "clusters": res["clusters"], "df": res.get("df"),
        "net_mean": _f(ind["net"].mean()) if len(ind) else None,
        "net_mean_reversal": _f(rev_net.mean()) if len(ind) else None,
        "gross_mean": _f(ind["gross"].mean()) if len(ind) else None,
        "funding_mean": _f(ind["fund"].mean()) if len(ind) else None,
        "funding_covered": int(ind["fund"].notna().sum()),
        "hit_rate": _f((ind["net"] > 0).mean()) if len(ind) else None,
    }  # fmt: skip
    gates = {
        "min_independent_events": len(ind) >= st.min_independent_events,
        "min_assets_with_events": ind["coin"].nunique() >= st.min_assets_with_events,
        "min_evaluable_fraction": bool(n_window) and len(ok) / n_window >= st.min_evaluable_fraction,
        "min_clusters": res["clusters"] >= st.min_clusters,
    }  # fmt: skip
    out["sample_gates"] = gates
    out["testable"] = all(gates.values()) and res["p_value"] is not None
    if not full:
        return out
    out["breadth"] = _assets(ind, 1.0)
    out["stability"] = _cells(ind)
    keys = tuple(cell_keys(m.condition))
    if out["testable"]:
        # Phase 17's matched random-entry null (independent draws), for comparison only.
        b = fr.baseline[fr.baseline["horizon"] == hp]
        b = b.assign(net=b[f"n_{m.target}"])[np.isfinite(b[f"n_{m.target}"])]
        pool = p17.pools(b, hp, keys)
        null = p17.null_means(ind, pool, keys, st.draws, _rng(st.seed, hid, "naive"))
        out["p_naive_independent_draws"] = (
            p17.p_two_sided(float(ind["excess"].mean()), null) if null is not None else None
        )
    # the same independent events under every target (absolute vs relative)
    out["targets"] = {}
    for tg in TARGETS:
        rt = with_excess(ind.drop(columns=["base_mean", "net", "gross", "fund", "excess",
                                           "evaluable"]), fr.baseline, tg, m.condition)  # fmt: skip
        rt = rt[rt["evaluable"]]
        out["targets"][tg] = {"n": len(rt), "excess_mean": _f(rt["excess"].mean()) if len(rt) else None,
                              "net_mean": _f(rt["net"].mean()) if len(rt) else None,
                              "hit_rate": _f((rt["net"] > 0).mean()) if len(rt) else None}  # fmt: skip
    if len(ind):
        alt = ind["d"] * ind["r_alt"]
        rel = ind["d"] * (ind["r_alt"] - ind["r_btc"])
        out["btc_and_alt"] = {
            "btc_forward_mean": _f(ind["r_btc"].mean()),
            "alt_forward_oriented_mean": _f(alt.mean()),
            "relative_won_but_alt_lost_share": _f(((rel > 0) & (alt < 0)).mean()),
            "relative_won_share": _f((rel > 0).mean()),
        }  # fmt: skip
    if "nc_rel" in fr.events:
        rc = with_excess(ind.drop(columns=["base_mean", "net", "gross", "fund", "excess",
                                           "evaluable"]), fr.baseline, m.target, m.condition, col="nc")  # fmt: skip
        out["signal_close_entry"] = {
            "excess_mean": _f(rc["excess"].mean()) if len(rc) else None,
            "note": "non-executable: entered at the signal bar's close, before availability",
        }  # fmt: skip
    return out


def contrast_member(m: Member, fr: Frames, hp: int, st: StatisticsSpec, hid: str, *,
                    full: bool) -> dict:  # fmt: skip
    """Inside vs outside the condition, each against its own condition-matched baseline."""
    sides = {}
    for label, inside in (("inside", True), ("outside", False)):
        rows = population(fr.events, m, hp, inside)
        if rows.empty:
            sides[label] = rows
            continue
        r = with_excess(rows, fr.baseline, m.target, m.condition)
        ok = r[r["evaluable"]].copy()
        ok["independent"] = independent(ok, hp)
        sides[label] = ok[ok["independent"]]
    a, b = sides["inside"], sides["outside"]
    if a.empty or b.empty:
        return _empty(m, hid, "no events on one side")
    res = stats.clustered_difference(a["excess"], a["k"] // hp, b["excess"], b["k"] // hp)
    out = {
        "hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
        "condition": m.condition, "independent_events": int(min(len(a), len(b))),
        "inside_events": len(a), "outside_events": len(b),
        "inside_excess": _f(a["excess"].mean()), "outside_excess": _f(b["excess"].mean()),
        "inside_net": _f(a["net"].mean()), "outside_net": _f(b["net"].mean()),
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


def _xs_values(m: Member, xs: pd.DataFrame, min_n: int, drop: str | None = None) -> pd.DataFrame:
    """Per sampled timestamp, the statistic of an ``ic`` / ``bucket`` / ``spread`` member.

    Vectorised over [timestamp x alt]. Ranking ties break by alt order (as in
    ``rank_cross_section``): the leader is the first maximum, the laggard the last minimum."""
    cols = [
        "t",
        "k",
        "value",
        "net",
        "third",
        "trend",
        "consolidation",
        "vol",
        "pullback",
        "n",
        "coin",
    ]
    if xs.empty:
        return pd.DataFrame(columns=cols)
    x = xs if drop is None else xs[xs["coin"] != drop]
    sig = m.pop if m.kind == "ic" else "rel"
    tgt = f"fwd_{m.target}"
    s = x.pivot(index="t", columns="coin", values=sig)
    f = x.pivot(index="t", columns="coin", values=tgt).reindex_like(s)
    c = x.pivot(index="t", columns="coin", values="cost").reindex_like(s)
    valid = np.isfinite(s.to_numpy(float)) & np.isfinite(f.to_numpy(float))
    n = valid.sum(axis=1)
    keep = n >= (min_n if drop is None else min_n - 1)
    meta = x.drop_duplicates("t").set_index("t").reindex(s.index)
    sv = np.where(valid, s.to_numpy(float), np.nan)[keep]
    fv = np.where(valid, f.to_numpy(float), np.nan)[keep]
    cv = c.to_numpy(float)[keep]
    vk = valid[keep]
    nn = n[keep]
    coins = np.asarray(s.columns)
    out = pd.DataFrame({"t": s.index[keep], "k": meta["k"].to_numpy()[keep],
                        "third": meta["third"].to_numpy()[keep], "trend": meta["trend"].to_numpy()[keep],
                        "consolidation": meta["consolidation"].to_numpy(bool)[keep],
                        "vol": meta["vol"].to_numpy()[keep], "pullback": meta["pullback"].to_numpy(bool)[keep],
                        "n": nn})  # fmt: skip
    if not len(out):
        return pd.DataFrame(columns=cols)
    if m.kind == "ic":
        rs = pd.DataFrame(sv).rank(axis=1).to_numpy()
        rf = pd.DataFrame(fv).rank(axis=1).to_numpy()
        rs, rf = (
            rs - np.nanmean(rs, axis=1, keepdims=True),
            rf - np.nanmean(rf, axis=1, keepdims=True),
        )
        num = np.nansum(rs * rf, axis=1)
        den = np.sqrt(np.nansum(rs * rs, axis=1) * np.nansum(rf * rf, axis=1))
        with np.errstate(invalid="ignore", divide="ignore"):
            out["value"] = np.where(den > 0, num / den, np.nan)
        out["net"] = np.nan
        out["coin"] = None
        return out
    rows = np.arange(len(out))
    top = np.nanargmax(np.where(vk, sv, -np.inf), axis=1)
    rev = np.where(vk, sv, np.inf)[:, ::-1]
    bot = sv.shape[1] - 1 - np.nanargmin(rev, axis=1)
    j = np.arange(sv.shape[1])[None, :]

    def fpos(i: np.ndarray) -> np.ndarray:  # forward position, 1 = best (stable ties)
        fi = fv[rows, i][:, None]
        better = vk & ((fv > fi) | ((fv == fi) & (j < i[:, None])))
        return better.sum(axis=1) + 1

    half = nn // 2
    if m.kind == "bucket":
        if m.pop == "top":
            hit = (fpos(top) <= half).astype(float)
            pick = top
        else:
            hit = (fpos(bot) > nn - half).astype(float)
            pick = bot
        out["value"] = hit - half / nn
        out["net"] = np.nan
        out["coin"] = coins[pick]
        return out
    gross = fv[rows, top] - fv[rows, bot]  # spread: long the leader, short the laggard
    out["value"] = gross
    out["net"] = gross - 2 * (cv[rows, top] + cv[rows, bot])
    out["coin"] = coins[top]
    return out


def xs_member(m: Member, fr: Frames, hp: int, st: StatisticsSpec, hid: str, min_n: int, *,
              full: bool) -> dict:  # fmt: skip
    v = _xs_values(m, fr.xs, min_n)
    v = v[np.isfinite(v["value"])] if len(v) else v
    if v.empty:
        return _empty(m, hid, "no cross-sections")
    j = np.arange(len(v))  # consecutive samples are adjacent blocks
    res = stats.clustered_mean(v["value"].to_numpy(), j)
    out = {"hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
           "condition": None, "timestamps": len(v), "independent_events": len(v),
           "universe_size_mean": _f(v["n"].mean()), "stat": res["mean"], "ci95": res["ci95"],
           "se": res["se"], "t": res["t"], "p_value": res["p_value"], "clusters": res["clusters"],
           "df": res.get("df"), "assets_with_events": int(fr.xs["coin"].nunique())}  # fmt: skip
    if m.kind == "spread":
        out["net_mean"] = _f(v["net"].mean())
        out["net_mean_reversal"] = _f((-v["value"] - (v["value"] - v["net"])).mean())
    gates = {"min_timestamps": len(v) >= st.min_timestamps,
             "min_assets_with_events": out["assets_with_events"] >= st.min_assets_with_events}  # fmt: skip
    out["sample_gates"] = gates
    out["testable"] = all(gates.values()) and res["p_value"] is not None
    if not full:
        return out
    loo = {}
    for coin in sorted(fr.xs["coin"].unique()):
        w = _xs_values(m, fr.xs, min_n, drop=coin)
        w = w[np.isfinite(w["value"])] if len(w) else w
        loo[coin] = _f(w["value"].mean()) if len(w) else None
    sign = np.sign(res["mean"] or 0.0)
    out["breadth"] = {"leave_one_out": loo,
                      "loo_same_sign_share": _f(np.mean([np.sign(x) == sign for x in loo.values() if x is not None]))
                      if loo else None}  # fmt: skip
    if v["coin"].notna().any():
        out["breadth"]["by_selected_asset"] = {c: {"n": len(g), "mean": _f(g["value"].mean())}
                                               for c, g in v.groupby("coin")}  # fmt: skip
    w = v.assign(coin="all", excess=v["value"])
    out["stability"] = _cells(w)
    return out


def _empty(m: Member, hid: str, why: str) -> dict:
    return {"hypothesis": hid, "member": m.name, "kind": m.kind, "target": m.target, "pop": m.pop,
            "condition": m.condition, "independent_events": 0, "stat": None, "p_value": None,
            "testable": False, "untestable_reason": why, "sample_gates": {}}  # fmt: skip


def evaluate_member(m: Member, fr: Frames, hp: int, st: StatisticsSpec, hid: str, min_n: int,
                    *, full: bool = True) -> dict:  # fmt: skip
    if m.kind == "event":
        out = event_member(m, fr, hp, st, hid, full=full)
    elif m.kind == "contrast":
        out = contrast_member(m, fr, hp, st, hid, full=full)
    else:
        out = xs_member(m, fr, hp, st, hid, min_n, full=full)
    if not out.get("testable") and "untestable_reason" not in out:
        failed = [g for g, ok in out.get("sample_gates", {}).items() if not ok]
        out["untestable_reason"] = "sample gate: " + ", ".join(failed) if failed else "no test"
    return out


def floor_of(m: Member, st: StatisticsSpec) -> float:
    return {"ic": st.ic_floor, "bucket": st.bucket_floor}.get(m.kind, st.economic_floor)


def directional(s: dict, direction: str) -> dict:
    """One member's statistics read in one direction (continuation = as oriented)."""
    sign = 1.0 if direction == "continuation" else -1.0
    stat = s.get("stat")
    lo, hi = (s.get("ci95") or [None, None])[:2]
    up, down = stats.one_sided(s)
    net = s.get("net_mean") if direction == "continuation" else s.get("net_mean_reversal")
    out = {"testable": bool(s.get("testable")), "stat": None if stat is None else sign * stat,
           "ci95": [None, None] if lo is None else sorted([sign * lo, sign * hi]),
           "p_value": up if direction == "continuation" else down, "q_value": s.get("q_value"),
           "net_mean": net}  # fmt: skip
    br = s.get("breadth") or {}
    if "per_asset" in br:
        eff = [sign * (v["excess_mean"] or 0) for v in br["per_asset"].values()]
        out["positive_asset_share"] = float(np.mean([e > 0 for e in eff])) if eff else None
        rest = [sign * v for v in br.get("leave_one_out_excess", {}).values() if v is not None]
        out["loo_all_same_sign"] = bool(rest) and all(r > 0 for r in rest)
    elif "per_asset_difference" in br:
        eff = [sign * v for v in br["per_asset_difference"].values() if v is not None]
        out["positive_asset_share"] = float(np.mean([e > 0 for e in eff])) if eff else None
        out["loo_all_same_sign"] = None
    elif "leave_one_out" in br:
        rest = [sign * v for v in br["leave_one_out"].values() if v is not None]
        out["positive_asset_share"] = None
        out["loo_all_same_sign"] = bool(rest) and all(r > 0 for r in rest)
    return out


# --------------------------------------------------------------------------- venue analysis


def member_id(venue: str, arch: str, m: Member) -> str:
    return f"{venue}|{arch}|{m.family}|{m.name}"


def analyse_venue(defn: RelativeStudyDefinition, arch: Architecture, col: Collected) -> dict:
    st = defn.manifest.statistics
    hp = arch.primary_horizon
    min_n = arch.central.min_universe
    venue = col.venue
    fr = col.central
    out: dict = {"venue": venue, "inputs": col.inputs, "counts": col.counts,
                 "universe": col.universe, "families": {}}  # fmt: skip
    if fr is None or fr.baseline.empty:
        out["empty"] = True
        return out
    for fam, ms in FAMILIES.items():
        rows = [evaluate_member(m, fr, hp, st, member_id(venue, arch.name, m), min_n) for m in ms]
        pv = {r["hypothesis"]: r["p_value"] for r in rows if r.get("testable")}
        qv = benjamini_hochberg(pv) if pv else {}
        for r in rows:
            r["q_value"] = qv.get(r["hypothesis"])
            r["in_family"] = r["hypothesis"] in qv
        out["families"][fam] = {"m": len(qv), "preregistered": len(rows), "members": rows}
    out["horizons"] = horizon_profile(fr, arch, st)
    out["decomposition"] = decomposition_summary(col, hp)
    out["continuous"] = continuous(fr.xs)
    out["leaders"] = leader_table(fr.xs, min_n)
    if col.variants:
        out["sensitivity"] = sensitivity(col, arch, st, out)
    out["counts"] = {**col.counts, **raw_counts(fr, hp)}
    return out


def raw_counts(fr: Frames, hp: int) -> dict:
    """Persistent bars vs edge-triggered events vs independent events per population."""
    ev = fr.events
    if ev.empty:
        return {}
    e = ev[(ev["horizon"] == hp) & np.isfinite(ev["n_rel"])]
    out = {}
    for pop, g in e.groupby("pop"):
        out[pop] = {"edge_events": len(g), "independent": int(independent(g, hp).sum())}
    return {"populations": out}


def horizon_profile(fr: Frames, arch: Architecture, st: StatisticsSpec) -> list[dict]:
    rows = []
    for ms in FAMILIES.values():
        for m in ms:
            if m.kind != "event":
                continue
            for h in arch.horizons:
                r = population(fr.events, m, h)
                if r.empty:
                    continue
                r = with_excess(r, fr.baseline, m.target, m.condition)
                ok = r[r["evaluable"]].copy()
                ind = ok[independent(ok, h)]
                rows.append({"family": m.family, "member": m.name, "horizon": h,
                             "independent_events": len(ind),
                             "excess_mean": _f(ind["excess"].mean()) if len(ind) else None,
                             "net_mean": _f(ind["net"].mean()) if len(ind) else None,
                             "hit_rate": _f((ind["net"] > 0).mean()) if len(ind) else None})  # fmt: skip
    return rows


def decomposition_summary(col: Collected, hp: int) -> dict:
    """H6 -> breakdown: conditional (H6 entry, selected by later confirmation) vs executable
    (entered when the breakdown is observable). Residual target, primary horizon."""
    dec, base = col.decomposition, col.central.baseline
    if dec.empty:
        return {"h6_events": 0}
    b = base[(base["horizon"] == hp) & np.isfinite(base["n_res"])]
    means = b.groupby(["coin", "d", "vol"])["n_res"].mean()

    def ex(df, ncol, kcol, vcol):
        x = df[np.isfinite(df[ncol])].copy()
        x["base"] = means.reindex(
            pd.MultiIndex.from_arrays([x["coin"], x["d"], x[vcol]])
        ).to_numpy()
        x["excess"] = x[ncol] - x["base"]
        x = x[np.isfinite(x["excess"])]
        x["k"] = x[kcol]
        x = x[independent(x, hp)]
        return x

    conf = dec[dec["confirmed"]]
    c6 = ex(conf, "n6", "k6", "vol6")
    c5 = ex(conf, "n5", "k5", "vol5")
    un = ex(dec[~dec["confirmed"]], "n6", "k6", "vol6")
    return {
        "h6_events": len(dec), "confirmed": int(dec["confirmed"].sum()),
        "confirmed_share": _f(dec["confirmed"].mean()),
        "delay_bars_median": _q(conf["k5"] - conf["k6"], 0.5) if len(conf) else None,
        "entry_move_bps_median": _q(conf["entry_move"] * 1e4, 0.5) if len(conf) else None,
        "conditional_from_h6_entry": {"independent": len(c6), "excess_mean": _f(c6["excess"].mean()) if len(c6) else None,
                                      "note": "selection on later information: NOT executable"},
        "executable_from_breakdown": {"independent": len(c5), "excess_mean": _f(c5["excess"].mean()) if len(c5) else None},
        "never_confirmed_from_h6_entry": {"independent": len(un), "excess_mean": _f(un["excess"].mean()) if len(un) else None},
    }  # fmt: skip


def continuous(xs: pd.DataFrame) -> list[dict]:
    """Descriptive gradients on non-overlapping cross-section samples (pooled over alts):
    Spearman and quintile means of the forward target by signal quintile. Never used to
    choose a threshold."""
    rows = []
    if xs.empty:
        return rows
    pairs = [("rel", "fwd_rel"), ("res", "fwd_res"), ("rel", "fwd_res"), ("res", "fwd_rel")]
    if "dcorr" in xs:
        pairs.append(("dcorr", "fwd_res_oriented"))
    for sig, tgt in pairs:
        x = xs[[sig, tgt]].replace([np.inf, -np.inf], np.nan).dropna()
        row = {"signal": sig, "target": tgt, "n": len(x)}
        if len(x) >= 25:
            row["spearman"] = _f(x[sig].rank().corr(x[tgt].rank()))
            qs = pd.qcut(x[sig].rank(method="first"), 5, labels=False)
            means = x.groupby(qs)[tgt].mean().to_numpy()
            row["quintile_means"] = [_f(v) for v in means]
            row["quintile_upper_bounds"] = [_f(v) for v in x.groupby(qs)[sig].max().to_numpy()]
            row["monotonicity"] = _f(pd.Series(means).rank().corr(pd.Series(range(5)).rank()))
        rows.append(row)
    return rows


def leader_table(xs: pd.DataFrame, min_n: int) -> dict:
    """Top / middle / bottom by BTC-relative rank: forward USD, BTC-relative and residual
    returns (gross), in and out of broad pullbacks. Descriptive."""
    if xs.empty:
        return {}
    x = xs[xs["n"] >= min_n].copy()
    x["bucket"] = np.where(
        x["rank_rel"] == 1, "top", np.where(x["rank_rel"] == x["n"], "bottom", "middle")
    )
    out = {}
    for label, g0 in (
        ("all", x),
        ("pullback", x[x["pullback"]]),
        ("no_pullback", x[~x["pullback"]]),
    ):
        out[label] = {b: {"n": len(g), "fwd_usd_mean": _f(g["r_alt"].mean()), "fwd_rel_mean": _f(g["fwd_rel"].mean()),
                          "fwd_res_mean": _f(g["fwd_res"].mean()), "btc_fwd_mean": _f(g["r_btc"].mean())}
                      for b, g in g0.groupby("bucket")}  # fmt: skip
    return out


def sensitivity(col: Collected, arch: Architecture, st: StatisticsSpec, out: dict) -> list[dict]:
    """One-at-a-time neighbours: each member's statistic per variant and Prism's
    ``plateau_verdict`` per direction. Descriptive only: never tested or selected."""
    hp, min_n = arch.primary_horizon, arch.central.min_universe
    vals = {a.name: list(a.values) for a in arch.axes}
    keyed = {key: (params, moved) for key, params, moved in arch.variants()}
    rows = []
    for fam, ms in FAMILIES.items():
        central = {r["member"]: r for r in out["families"][fam]["members"]}
        for m in ms:
            relevant = set()
            for ax, pops in AXIS_POPS.items():
                if (
                    m.pop in pops
                    or (m.kind == "ic" and "ic" in pops)
                    or (m.condition and m.condition in pops)
                ):
                    relevant.add(ax)
            axes = [a for a in vals if a in relevant]
            if not axes:
                continue
            c = central[m.name]
            default = {a: arch.central.value(a) for a in axes}
            n_c = c.get("independent_events") or 0
            stats_by_variant = []
            for key, fr in col.variants.items():
                _, moved = keyed[key]
                ax = next(iter(moved))
                if ax not in axes:
                    continue
                s = evaluate_member(m, fr, hp, st, key, min_n, full=False)
                stats_by_variant.append({**default, ax: moved[ax], "variant": key,
                                         "n_indep": s.get("independent_events") or 0, "stat": s.get("stat")})  # fmt: skip
            row = {"family": fam, "member": m.name, "axes": axes, "neighbours": [
                {"variant": v["variant"], "independent_events": v["n_indep"], "stat": _f(v["stat"])}
                for v in stats_by_variant]}  # fmt: skip
            for direction, sign in (("continuation", 1.0), ("reversal", -1.0)):
                ce = c.get("stat")
                table = pd.DataFrame([{**default, "n_indep": n_c, "excess_mean": np.nan if ce is None else sign * ce,
                                       "variant": "central"}] + [
                    {**{a: v[a] for a in axes}, "n_indep": v["n_indep"],
                     "excess_mean": np.nan if v["stat"] is None else sign * v["stat"], "variant": v["variant"]}
                    for v in stats_by_variant])  # fmt: skip
                min_ev = (
                    st.min_timestamps
                    if m.kind in ("ic", "bucket", "spread")
                    else st.min_independent_events
                )
                verdict = plateau_verdict(
                    table, default, {a: vals[a] for a in axes}, min_ev, 0.5, 0.5
                )
                row[f"plateau_{direction}"] = verdict.get("verdict")
            sv = [v["stat"] for v in stats_by_variant if v["stat"] is not None]
            if c.get("stat") is not None:
                sv.append(c["stat"])
            row["stat_range"] = [_f(min(sv)), _f(max(sv))] if sv else [None, None]
            row["sign_agreement"] = (_f(np.mean([np.sign(v) == np.sign(c["stat"]) for v in sv]))
                                     if sv and c.get("stat") is not None else None)  # fmt: skip
            rows.append(row)
    return rows


__all__ = [
    "analyse_venue",
    "directional",
    "evaluate_member",
    "floor_of",
    "independent",
    "member_id",
]
