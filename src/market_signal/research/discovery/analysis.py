"""Statistics, verdicts, lifecycle context, contrasts, clustering and the ensemble view.

Units: one independent event's NET return on notional at the primary horizon (after fees,
slippage and funding), side-signed. Inference (``intraday_discovery_inference_v1``): the
Phase 18 block-clustered t with UTC-day blocks shared by every coin and the adjacent-day
covariance (co-timed, BTC-driven signals on six coins are not six observations); one-sided
for "net mean > 0" (each strategy has a direction). Event-level descriptive numbers are kept
beside it. BH runs within each frozen ``bh_family``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.discovery.catalogue import IntradayStrategy, neighbours
from market_signal.research.discovery.spec import CANDIDATE_VERDICTS, DiscoveryManifest
from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.relative.study.stats import (
    clustered_difference,
    clustered_mean,
    one_sided,
)

DAY_NS = 86_400 * 10**9
NS_MIN = 60 * 10**9


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def ns(ts) -> int:
    return int(pd.Timestamp(ts).value)


# --------------------------------------------------------------------------- per window


def evaluable(ev: pd.DataFrame, h: int) -> pd.DataFrame:
    if ev.empty:
        return ev
    return ev[ev["independent"] & np.isfinite(ev[f"net_{h}"].to_numpy(float))]


def window(ev: pd.DataFrame, lo: int, hi: int) -> pd.DataFrame:
    if ev.empty:
        return ev
    t = ev["signal_close_ns"].to_numpy(np.int64)
    return ev[(t >= lo) & (t < hi)]


def stats(ev: pd.DataFrame, h: int) -> dict:
    """Statistics of independent evaluable events at horizon ``h`` (already windowed)."""
    e = evaluable(ev, h)
    n = len(e)
    if n == 0:
        return {"n": 0, "assets": 0}
    net = e[f"net_{h}"].to_numpy(float)
    blocks = (e["signal_ns"].to_numpy(np.int64) // DAY_NS).astype(np.int64)
    cm = clustered_mean(net, blocks)
    p_up = one_sided(cm)[0] if cm.get("t") is not None else None
    coins = e["coin"].to_numpy()
    by = pd.Series(net).groupby(coins)
    sums, means = by.sum(), by.mean()
    best = sums.idxmax()
    rest = net[coins != best]
    order = np.sort(net)[::-1]
    pos_sum = net[net > 0].sum()
    exc = e[f"excess_{h}"].to_numpy(float)
    gross = e[f"gross_{h}"].to_numpy(float)
    fund = e[f"fund_{h}"].to_numpy(float)
    cum = np.cumsum(net)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum).max())
    gm = clustered_mean(gross, blocks)
    return {
        "n": n, "assets": len(sums), "clusters": cm.get("clusters"),
        "net_mean": _f(cm["mean"]), "net_se": _f(cm.get("se")), "t": _f(cm.get("t")),
        "p_one_sided": _f(p_up), "ci95": [_f(x) for x in cm.get("ci95", [None, None])],
        "gross_mean": _f(gross.mean()), "gross_t": _f(gm.get("t")),
        "cost_mean": _f(e["cost"].mean()), "funding_mean": _f(np.nanmean(fund)),
        "cost_drag": _f(e["cost"].mean() + np.nanmean(fund)),
        "excess_mean": _f(np.nanmean(exc)) if np.isfinite(exc).any() else None,
        "hit_rate": _f((net > 0).mean()),
        "iid_t": _f(net.mean() / (net.std(ddof=1) / np.sqrt(n))) if n > 1 and net.std(ddof=1) > 0 else None,
        "loo_mean": _f(rest.mean()) if len(rest) else None, "best_asset": str(best),
        "top5_share": _f(order[:5].sum() / pos_sum) if pos_sum > 0 else None,
        "positive_asset_share": _f((means > 0).mean()),
        "per_asset": {str(k): {"n": int((coins == k).sum()), "mean": _f(v)} for k, v in means.items()},
        "max_drawdown": _f(dd), "worst_event": _f(net.min()), "best_event": _f(net.max()),
        "delay_min_median": _f(np.nanmedian(e["delay_min"])) if "delay_min" in e else None,
    }  # fmt: skip


def frequency(ev: pd.DataFrame, lo: int, hi: int, n_coins: int) -> dict:
    """Raw and independent signal rates over [lo, hi) (calendar days)."""
    days = (hi - lo) / DAY_NS
    w = window(ev, lo, hi)
    if w.empty:
        return {"raw_per_day": 0.0, "independent_per_day": 0.0, "per_asset_per_day": 0.0,
                "median_hours_between": None, "zero_signal_day_share": 1.0, "raw": 0,
                "independent": 0}  # fmt: skip
    ind = w[w["independent"]]
    t = np.sort(ind["signal_ns"].to_numpy(np.int64))
    gaps = np.diff(t) / (3600 * 10**9)
    sig_days = np.unique((t - lo) // DAY_NS)
    return {
        "raw": len(w), "independent": len(ind),
        "raw_per_day": len(w) / days, "independent_per_day": len(ind) / days,
        "per_asset_per_day": len(ind) / days / max(n_coins, 1),
        "median_hours_between": _f(np.median(gaps)) if len(gaps) else None,
        "zero_signal_day_share": 1 - len(sig_days) / max(int(np.ceil(days)), 1),
    }  # fmt: skip


def weighted(ev: pd.DataFrame, h: int, as_of: int, half_life_days: float) -> dict:
    from market_signal.research.lifecycle.estimators import ess, weighted_mean_se

    e = evaluable(ev, h)
    if e.empty:
        return {"n": 0}
    age = (as_of - e["signal_ns"].to_numpy(np.int64)) / DAY_NS
    w = np.power(0.5, age / half_life_days)
    x = e[f"net_{h}"].to_numpy(float)
    m, se = weighted_mean_se(x, w, (e["signal_ns"].to_numpy(np.int64) // DAY_NS))
    return {"n": len(e), "half_life_days": half_life_days, "ess": _f(ess(w)), "net_mean": _f(m),
            "se": _f(se), "t": _f(m / se) if np.isfinite(se) and se > 0 else None}  # fmt: skip


def pattern(ev: pd.DataFrame, h: int, lo: int, hi: int) -> dict:
    """Appears / persists / decays: net means of the window's chronological thirds and of
    rolling 90-day windows every 30 days. Descriptive; never a gate."""
    thirds = []
    for k in range(3):
        a, b = lo + (hi - lo) * k // 3, lo + (hi - lo) * (k + 1) // 3
        s = stats(window(ev, a, b), h)
        thirds.append({"n": s["n"], "net_mean": s.get("net_mean"), "t": s.get("t")})
    pos = [(x["net_mean"] or 0) > 0 and (x["t"] or 0) >= 1 for x in thirds]
    neg = [(x["net_mean"] or 0) <= 0 for x in thirds]
    if all(pos):
        label = "persists"
    elif not pos[0] and pos[2]:
        label = "appears"
    elif pos[0] and neg[2]:
        label = "decays"
    elif any(pos):
        label = "intermittent"
    else:
        label = "absent"
    rolling = []
    t = lo
    while t + 90 * DAY_NS <= hi:
        s = stats(window(ev, t, t + 90 * DAY_NS), h)
        rolling.append({"start": pd.Timestamp(t, tz="UTC").date().isoformat(), "n": s["n"],
                        "net_mean": s.get("net_mean"), "t": s.get("t")})  # fmt: skip
        t += 30 * DAY_NS
    return {"label": label, "thirds": thirds, "rolling_90d": rolling}


def edge_state(ev: pd.DataFrame, h: int, lo: int, as_of: int, floor: float,
               stress_days: set) -> dict:  # fmt: skip
    """Phase 20 ``edge_state_v1`` (classify + divergence), read-only, on the contemporary
    events with the Phase 22 floor. Lifetime = the contemporary span here."""
    from market_signal.research.lifecycle import state as st
    from market_signal.research.lifecycle.estimators import EventSet
    from market_signal.research.lifecycle.policy import policy

    e = evaluable(ev, h)
    if len(e) < 2:
        return {"state": "INSUFFICIENT"}
    df = pd.DataFrame({
        "asset": e["coin"].to_numpy(),
        "signal_time": pd.to_datetime(e["signal_ns"].to_numpy(np.int64), utc=True),
        "resolved_at": pd.to_datetime(e["exit_ns"].to_numpy(np.int64), utc=True),
        "net": e[f"net_{h}"].to_numpy(float), "gross": e[f"gross_{h}"].to_numpy(float),
        "excess": e[f"excess_{h}"].to_numpy(float), "mae": np.nan, "mfe": np.nan,
        "cost": e["cost"].to_numpy(float), "funding": e[f"fund_{h}"].to_numpy(float),
        "independent": True, "evaluable": True,
        "stress": [(t // DAY_NS) in stress_days for t in e["signal_ns"].to_numpy(np.int64)],
        "regime_trend": "unknown", "regime_vol": "unknown", "regime_breadth": "unknown",
        "regime_funding": "unknown"})  # fmt: skip
    evs = EventSet.from_frame(df, history_start=pd.Timestamp(lo, tz="UTC"))
    pol = policy(1)
    core = st.core_evidence(evs, as_of / DAY_NS, pol)
    c = st.classify(core, floor, pol)
    dv = st.divergence(core, floor, pol)
    return {"state": c["state"], "reason": c["reason"], "classes": c["classes"],
            "divergence": dv["pattern"]}  # fmt: skip


# --------------------------------------------------------------------------- verdicts


ROUTES = ("contemporary", "recent_180", "recent_90", "recent_30")
NEXT_RECENT = {"contemporary": "recent_90", "recent_180": "recent_90", "recent_90": "recent_30",
               "recent_30": None}  # fmt: skip


def verdict(row: dict, man: DiscoveryManifest, floor: float, neighbour_rows: list[dict]) -> tuple[str, list[str]]:  # fmt: skip
    """``phase22_verdicts_v1`` (spec.Criteria). Returns (verdict, reasons)."""
    st, cr, fq = man.statistics, man.criteria, man.frequency
    c, r180 = row["contemporary"], row["recent_180"]
    freq = row["frequency"]["independent_per_day"]
    if all(row[w]["n"] < st.min_events for w in ROUTES):
        return "NO_EVIDENCE", [f"fewer than {st.min_events} independent events in every window"]
    tc = c.get("t") or 0.0
    if freq < fq.too_sparse_per_day and tc < fq.extraordinary_t:
        return "REJECTED", [f"too sparse: {freq * 30:.2f} independent signals per month"]
    bars = {"contemporary": cr.contemporary_t, "recent_180": cr.recent_t,
            "recent_90": cr.recent_t, "recent_30": cr.recent30_t}  # fmt: skip

    def route_ok(w: dict, need_t: float) -> bool:
        return (w["n"] >= st.min_events and (w.get("net_mean") or -1) >= floor
                and (w.get("t") or -9) >= need_t)  # fmt: skip

    route = next((r for r in ROUTES if route_ok(row[r], bars[r])), None)
    reasons: list[str] = []
    if route:
        w = row[route]
        nxt = row[NEXT_RECENT[route]] if NEXT_RECENT[route] else None
        guards = {
            "assets": w["assets"] >= st.min_assets,
            "recent_not_dead": nxt is None or nxt["n"] < cr.recent_min_events
            or (nxt.get("t") or 0) > cr.recent_min_t,
            "not_one_asset": (w.get("loo_mean") or -1) > 0,
            "not_few_trades": w.get("top5_share") is not None and w["top5_share"] < cr.max_top5_share,
            "positive_asset_share": (w.get("positive_asset_share") or 0) >= cr.min_positive_asset_share,
            "gross_positive": (w.get("gross_mean") or -1) > 0,
            "not_timing_artefact": w.get("excess_mean") is not None and w["excess_mean"] > 0,
            "frequency": freq >= fq.sparse_per_day,
        }  # fmt: skip
        if route == "contemporary":
            guards["recent_180_positive"] = (r180.get("net_mean") or -1) > 0
        if neighbour_rows:
            guards["parameter_support"] = any(
                (nb[route].get("net_mean") or -1) > 0 and (nb[route].get("t") or 0) >= 1.0
                for nb in neighbour_rows)  # fmt: skip
        else:
            guards["parameter_support"] = (w.get("t") or 0) >= cr.lone_t
        failed = [k for k, v in guards.items() if not v]
        row["guards"] = guards
        row["route"] = route
        if not failed:
            q = row.get("q_value")
            latest = row["recent_30"] if row["recent_30"]["n"] else row["recent_90"]
            strong = q is not None and q <= st.q and (latest.get("net_mean") or -1) > 0
            return ("STRONG_INCUBATION_CANDIDATE" if strong else "INCUBATION_CANDIDATE",
                    [f"route {route}", *([f"BH q <= {st.q:.2f}"] if strong else [])])  # fmt: skip
        reasons = [f"route {route} but failed: {', '.join(failed)}"]
    if any((row[r].get("net_mean") or -1) > 0 and (row[r].get("t") or 0) >= cr.interesting_t
           for r in ("contemporary", "recent_180", "recent_90")):  # fmt: skip
        return "INTERESTING", reasons or ["positive after costs with t >= 1, below candidate bar"]
    if tc <= cr.rejected_t:
        return "REJECTED", ["contemporary net effect credibly negative"]
    if (c.get("gross_t") or 0) >= 1 and (c.get("net_mean") or 0) <= 0:
        return "REJECTED", ["edge exists only before costs"]
    return "NO_EVIDENCE", reasons or ["no after-cost effect distinguishable from zero"]


def apply_bh(rows: dict[str, dict], strategies: dict[str, IntradayStrategy], man) -> dict:
    """BH within each frozen bh_family on the contemporary one-sided p (untestable -> 1)."""
    st = man.statistics
    fams: dict[str, dict[str, float]] = {}
    for key, r in rows.items():
        c = r["contemporary"]
        testable = (c["n"] >= st.min_events and c.get("assets", 0) >= st.min_assets
                    and (c.get("clusters") or 0) >= st.min_clusters and c.get("p_one_sided") is not None)  # fmt: skip
        r["testable"] = bool(testable)
        fams.setdefault(strategies[key].bh_family, {})[key] = (
            float(c["p_one_sided"]) if testable else st.untestable_p
        )
    out = {}
    for fam, ps in fams.items():
        q = benjamini_hochberg(ps)
        for k, v in q.items():
            rows[k]["q_value"] = v
        out[fam] = {"m": len(ps), "testable": sum(rows[k]["testable"] for k in ps),
                    "discoveries": sorted(k for k, v in q.items() if v <= st.q)}  # fmt: skip
    return out


def all_verdicts(rows: dict[str, dict], strategies: dict[str, IntradayStrategy], man,
                 floors: dict[str, float]) -> None:  # fmt: skip
    pool = tuple(strategies.values())
    for key, r in rows.items():
        nb = [rows[n.key] for n in neighbours(strategies[key], pool) if n.key in rows]
        r["neighbours"] = [n.key for n in neighbours(strategies[key], pool)]
        r["verdict"], r["verdict_reasons"] = verdict(r, man, floors[key], nb)


# --------------------------------------------------------------------------- contrasts


def bucket_contrast(ev: pd.DataFrame, h: int) -> dict:
    """Family J: net mean by volatility tercile and the high - low clustered difference."""
    e = evaluable(ev, h)
    if e.empty:
        return {"buckets": {}}
    blocks = e["signal_ns"].to_numpy(np.int64) // DAY_NS
    net = e[f"net_{h}"].to_numpy(float)
    vol = e["vol"].to_numpy()
    out = {}
    for b, name in ((0, "low"), (1, "normal"), (2, "high")):
        m = vol == b
        out[name] = {"n": int(m.sum()), "net_mean": _f(net[m].mean()) if m.any() else None}
    hi, lo = vol == 2, vol == 0
    d = clustered_difference(net[hi], blocks[hi], net[lo], blocks[lo])
    return {"buckets": out, "high_minus_low": _f(d.get("mean")), "t": _f(d.get("t")),
            "p_two_sided": _f(d.get("p_value"))}  # fmt: skip


def complexity_contrast(complex_ev: pd.DataFrame, base_ev: pd.DataFrame, h_c: int, h_b: int,
                        lo: int, hi: int) -> dict:  # fmt: skip
    """Does the extra condition add anything? Baseline events (its own independence) split
    by whether the complex rule also fired on that coin and bar (nested filters), plus the
    frequency the filter destroys."""
    cw, bw = window(complex_ev, lo, hi), window(base_ev, lo, hi)
    if bw.empty:
        return {"frequency_ratio": None}
    fired = set(zip(cw["coin"], cw["i"], strict=True)) if not cw.empty else set()
    be = evaluable(bw, h_b)
    infl = np.array([(c, i) in fired for c, i in zip(be["coin"], be["i"], strict=True)], dtype=bool)
    net = be[f"net_{h_b}"].to_numpy(float)
    blocks = be["signal_ns"].to_numpy(np.int64) // DAY_NS
    d = clustered_difference(net[infl], blocks[infl], net[~infl], blocks[~infl])
    sc, sb = stats(cw, h_c), stats(bw, h_b)
    adds = d.get("mean") is not None and d["mean"] > 0 and (d.get("t") or 0) >= 1.5
    return {
        "frequency_ratio": _f(len(cw) / len(bw)) if len(bw) else None,
        "complex_net_mean": sc.get("net_mean"), "complex_t": sc.get("t"), "complex_n": sc["n"],
        "baseline_net_mean": sb.get("net_mean"), "baseline_t": sb.get("t"), "baseline_n": sb["n"],
        "in_filter_minus_out": _f(d.get("mean")), "t": _f(d.get("t")),
        "p_two_sided": _f(d.get("p_value")), "in_filter_n": int(infl.sum()),
        "filter_adds_value": bool(adds),
        "prefer": "complex" if adds else "simpler",
    }  # fmt: skip


# --------------------------------------------------------------------------- clustering


def overlap(a: pd.DataFrame, b: pd.DataFrame, match_minutes: int) -> float:
    """matches / (|A| + |B| - matches): same coin and side, entries within the tolerance."""
    if a.empty or b.empty:
        return 0.0
    tol = match_minutes * NS_MIN
    m = 0
    for (coin, d), ga in a.groupby(["coin", "d"]):
        gb = b[(b["coin"] == coin) & (b["d"] == d)]
        if gb.empty:
            continue
        tb = np.sort(gb["signal_ns"].to_numpy(np.int64))
        ta = ga["signal_ns"].to_numpy(np.int64)
        j = np.searchsorted(tb, ta)
        lo = np.abs(ta - tb[np.clip(j - 1, 0, len(tb) - 1)])
        hi = np.abs(tb[np.clip(j, 0, len(tb) - 1)] - ta)
        m += int((np.minimum(lo, hi) <= tol).sum())
    m = min(m, len(a), len(b))
    return m / (len(a) + len(b) - m)


def clusters(keys: list[str], events: dict[str, pd.DataFrame], strategies, rows,
             cfg) -> tuple[list[dict], dict]:  # fmt: skip
    """Single-linkage clusters of overlapping strategies; one representative each."""
    n = len(keys)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs = {}
    for i in range(n):
        for j in range(i + 1, n):
            o = overlap(events[keys[i]], events[keys[j]], cfg.match_minutes)
            if o > 0.05:
                pairs[f"{keys[i]} | {keys[j]}"] = round(o, 4)
            if o >= cfg.threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[str]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(find(i), []).append(k)
    out = []
    for members in groups.values():
        rep = sorted(members, key=lambda k: (strategies[k].complexity.score,
                                             -(rows[k]["contemporary"].get("t") or -99), k))[0]  # fmt: skip
        out.append({"cluster_id": f"cl_{sorted(members)[0]}", "representative": rep,
                    "members": sorted(members), "size": len(members)})  # fmt: skip
    out.sort(key=lambda c: c["representative"])
    return out, pairs


# --------------------------------------------------------------------------- ensemble


def ensemble(events: list[pd.DataFrame], lo: int, hi: int, dedup_minutes: int,
             target: float) -> dict:  # fmt: skip
    """Aggregate opportunity of a set of strategies: correlated variants never count twice
    (same coin and side within ``dedup_minutes`` = one opportunity)."""
    days = (hi - lo) / DAY_NS
    parts = [
        window(e[e["independent"]], lo, hi)[["coin", "d", "signal_ns"]]
        for e in events
        if not e.empty
    ]
    if not parts or not sum(len(p) for p in parts):
        return {"raw_per_day": 0.0, "independent_per_day": 0.0, "long_per_day": 0.0,
                "short_per_day": 0.0, "zero_day_share": 1.0, "days_ge1_share": 0.0,
                "days_ge5_share": 0.0, "target_per_day": target, "strategies": len(events)}  # fmt: skip
    allv = pd.concat(parts, ignore_index=True).sort_values("signal_ns", kind="mergesort")
    keep = []
    gap = dedup_minutes * NS_MIN
    for (_, _), g in allv.groupby(["coin", "d"], sort=True):
        last = None
        for idx, t in zip(g.index, g["signal_ns"].to_numpy(np.int64), strict=True):
            if last is None or t - last >= gap:
                keep.append(idx)
                last = t
    ind = allv.loc[sorted(keep)]
    per_day = pd.Series((ind["signal_ns"].to_numpy(np.int64) - lo) // DAY_NS).value_counts()
    n_days = max(int(np.ceil(days)), 1)
    counts = np.zeros(n_days, dtype=int)
    counts[per_day.index.to_numpy().clip(0, n_days - 1)] = per_day.to_numpy()
    return {
        "strategies": len(events), "raw": len(allv), "independent": len(ind),
        "raw_per_day": len(allv) / days, "independent_per_day": len(ind) / days,
        "long_per_day": int((ind["d"] > 0).sum()) / days,
        "short_per_day": int((ind["d"] < 0).sum()) / days,
        "zero_day_share": float((counts == 0).mean()),
        "days_ge1_share": float((counts >= 1).mean()),
        "days_ge5_share": float((counts >= 5).mean()),
        "median_per_day": float(np.median(counts)),
        "target_per_day": target,
        "share_of_target": len(ind) / days / target,
    }  # fmt: skip


def shortlist(cl: list[dict], rows: dict, strategies, cfg) -> list[str]:
    """Cluster representatives with a candidate verdict, at most N per BH family (by t)."""
    reps = [c["representative"] for c in cl if rows[c["representative"]]["verdict"] in cfg.eligible]
    reps.sort(key=lambda k: (0 if rows[k]["verdict"] == "STRONG_INCUBATION_CANDIDATE" else 1,
                             -((rows[k].get(rows[k].get("route") or "contemporary") or {}).get("t") or 0), k))  # fmt: skip
    out, per = [], {}
    for k in reps:
        fam = strategies[k].bh_family
        if per.get(fam, 0) < cfg.max_per_bh_family:
            out.append(k)
            per[fam] = per.get(fam, 0) + 1
    return out


# --------------------------------------------------------------------------- Phase 21 replay


def incubation_replay(ev: pd.DataFrame, s: IntradayStrategy, h: int, lo: int, hi: int,
                      floor: float) -> dict:  # fmt: skip
    """Diagnostics only: how the frozen Phase 21 BALANCED / AGGRESSIVE policies would have
    admitted this strategy, evaluated at every trigger-bar close. The policies are imported
    unchanged; the economic floor passed is the Phase 22 floor of this horizon (the Phase 21
    floor table has no intraday horizons). No threshold is tuned."""
    from market_signal.research.incubation.machine import replay_incubation
    from market_signal.research.incubation.opportunity import opportunity_rate
    from market_signal.research.incubation.policy import POLICIES
    from market_signal.research.lifecycle.estimators import EventSet

    e = evaluable(window(ev, lo, hi), h)
    if len(e) < 2:
        return {}
    exit_ns = e["exit_ns"].to_numpy(np.int64)
    df = pd.DataFrame({
        "asset": e["coin"].to_numpy(),
        "signal_time": pd.to_datetime(e["signal_ns"].to_numpy(np.int64), utc=True),
        "resolved_at": pd.to_datetime(exit_ns, utc=True),
        "net": e[f"net_{h}"].to_numpy(float), "gross": e[f"gross_{h}"].to_numpy(float),
        "excess": e[f"excess_{h}"].to_numpy(float), "mae": np.nan, "mfe": np.nan,
        "cost": e["cost"].to_numpy(float), "funding": e[f"fund_{h}"].to_numpy(float),
        "independent": True, "evaluable": True, "stress": False,
        "regime_trend": "unknown", "regime_vol": "unknown", "regime_breadth": "unknown",
        "regime_funding": "unknown"})  # fmt: skip
    evs = EventSet.from_frame(df, history_start=pd.Timestamp(lo, tz="UTC"))
    step = (15 if s.timeframe == "15m" else 60) * NS_MIN
    times = np.arange(lo, hi, step, dtype=np.int64) / DAY_NS
    start_d, end_d = lo / DAY_NS, hi / DAY_NS
    out = {"floor": floor, "evaluation_step_minutes": step // NS_MIN}
    for name in ("BALANCED", "AGGRESSIVE"):
        rep = replay_incubation(evs, times, POLICIES[name], floor, strategy_key=s.strategy_id)
        part = rep.participate
        first = rep.episodes[0]["start"] if rep.episodes else None
        sig = pd.DataFrame({"t_sig": evs.t_sig, "asset": np.array(evs.assets)[evs.asset],
                            "side": s.side, "admitted": part})  # fmt: skip
        opp = opportunity_rate(sig, start_d, end_d)
        lv = np.array(rep.levels, dtype=object)
        out[name] = {
            "policy_id": POLICIES[name].policy_id,
            "admission_delay_days": _f(first - start_d) if first is not None else None,
            "episodes": len(rep.episodes),
            "active_share": _f(np.isin(lv, ("EXPLORATORY_PAPER", "CONFIRMED_PAPER")).mean()),
            "trades_captured": int(part.sum()), "trades_available": len(part),
            "captured_net_mean": _f(evs.net[part].mean()) if part.any() else None,
            "missed_net_mean": _f(evs.net[~part].mean()) if (~part).any() else None,
            "opportunities_per_day": opp["independent_opportunities_per_day"],
            "zero_opportunity_day_share": opp["zero_opportunity_day_share"],
        }  # fmt: skip
    return out


__all__ = [
    "CANDIDATE_VERDICTS",
    "all_verdicts",
    "apply_bh",
    "bucket_contrast",
    "clusters",
    "complexity_contrast",
    "edge_state",
    "ensemble",
    "frequency",
    "incubation_replay",
    "pattern",
    "shortlist",
    "stats",
    "verdict",
    "weighted",
    "window",
]  # fmt: skip
