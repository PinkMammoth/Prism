"""Pooled (per venue, never across venues) statistics, tests, families and verdicts.

Primary metric (preregistered): mean NET excess forward return at the primary horizon over
the matched baseline cell (same coin, direction, horizon and volatility tercile), on
independent events. Net means after two sides of fee + slippage; funding is reported
beside it. The excess of a cost-paying event over a cost-paying random entry is what the
signal's TIMING adds; whether the trade itself pays is the separate ``net_mean`` gate.

Tests:

- one-sided ``matched_random_entry_mean_excess_v1``: the observed mean excess versus the
  means of the same per-cell counts drawn without replacement from each cell's centred
  eligible baseline pool; p = (hits + 1) / (draws + 1);
- two-sided contrasts (failed vs held, rejection vs none): label permutation of the
  difference in mean excess between two disjoint sets of independent events;
- subgroups: the same matched null restricted to baseline bars with the same regime
  label, two-sided.

BH (``batch.benjamini_hochberg``, the Lab's implementation) runs within one family:
one venue x one architecture x one family kind. Untestable members (sample gates on counts
and coverage only, never on the statistic) are excluded from m and stay visible.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from market_signal.backtest.robustness import plateau_verdict
from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.structure import path as tp
from market_signal.research.structure.study.collect import VOL_NAMES, Collected
from market_signal.research.structure.study.spec import (
    DIRECTIONS,
    HELD,
    LADDER,
    LEVEL_KINDS,
    R_RUNGS,
    STRETCH,
    SUBGROUP_CELLS,
    SUBGROUP_RUNGS,
    Architecture,
    StatisticsSpec,
    StudyDefinition,
    axes_for,
)


def _rng(seed: int, *key) -> np.random.Generator:
    h = hashlib.sha256("\0".join(map(str, key)).encode()).digest()
    return np.random.default_rng([seed, int.from_bytes(h[:8], "little")])


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _q(s: pd.Series, q: float) -> float | None:
    s = pd.Series(s).dropna()
    return _f(s.quantile(q)) if len(s) else None


# --------------------------------------------------------------------------- nulls


def pools(base: pd.DataFrame, horizon: int, keys: tuple[str, ...]) -> dict:
    """Centred eligible pools per baseline cell (``keys`` among coin, d, vol, regime)."""
    b = base[base["horizon"] == horizon]
    return {k: (g["net"] - g["net"].mean()).to_numpy() for k, g in b.groupby(list(keys))}


def null_means(ind: pd.DataFrame, pool: dict, keys: tuple[str, ...], draws: int,
               rng: np.random.Generator) -> np.ndarray | None:  # fmt: skip
    """Means of matched random entries: per cell, as many draws (without replacement) from
    the cell's centred pool as the population has independent events in that cell."""
    counts = ind.groupby(list(keys)).size()
    total = int(counts.sum())
    if not total:
        return None
    sums = np.zeros(draws)
    for cell, k in counts.items():
        cell = cell if isinstance(cell, tuple) else (cell,)
        p = pool.get(cell)
        if p is None or len(p) < k:
            return None  # a cell without enough eligible bars: untestable, never padded
        for i in range(draws):
            sums[i] += p[rng.choice(len(p), size=k, replace=False)].sum()
    return sums / total


def p_greater(obs: float, null: np.ndarray) -> float:
    return float((np.sum(null >= obs) + 1) / (len(null) + 1))


def p_two_sided(obs: float, null: np.ndarray) -> float:
    lo = (np.sum(null <= obs) + 1) / (len(null) + 1)
    hi = (np.sum(null >= obs) + 1) / (len(null) + 1)
    return float(min(1.0, 2 * min(lo, hi)))


def bootstrap_ci(x: np.ndarray, b: int, rng: np.random.Generator) -> tuple:
    x = np.asarray(x, float)
    if len(x) < 2:
        return None, None
    m = x[rng.integers(0, len(x), (b, len(x)))].mean(axis=1)
    return _f(np.quantile(m, 0.025)), _f(np.quantile(m, 0.975))


def permutation_p(a: np.ndarray, b: np.ndarray, n: int, rng: np.random.Generator) -> float:
    obs = abs(a.mean() - b.mean())
    allx = np.concatenate([a, b])
    na = len(a)
    hits = 0
    for _ in range(n):
        perm = rng.permutation(allx)
        hits += abs(perm[:na].mean() - perm[na:].mean()) >= obs - 1e-15
    return float((hits + 1) / (n + 1))


# --------------------------------------------------------------------------- summaries


def summary(ev: pd.DataFrame, st: StatisticsSpec, rng) -> dict:
    """Stats of one population at one horizon. ``ev`` = all in-window events of it."""
    n_window = len(ev)
    ok = ev[ev["evaluable"]]
    ind = ok[ok["independent"]]
    n = len(ind)
    coins = ind.groupby("coin")["excess"].mean() if n else pd.Series(dtype=float)
    out = {
        "events_in_window": n_window,
        "events_evaluable": len(ok),
        "independent_events": n,
        "evaluable_fraction": _f(len(ok) / n_window) if n_window else None,
        "assets_with_events": len(coins),
        "gross_mean": _f(ind["gross"].mean()) if n else None,
        "net_mean": _f(ind["net"].mean()) if n else None,
        "net_median": _f(ind["net"].median()) if n else None,
        "net_p10": _q(ind["net"], 0.1),
        "net_p90": _q(ind["net"], 0.9),
        "funding_mean": _f(ind["fund"].mean()) if n else None,
        "funding_covered": int(ind["fund"].notna().sum()),
        "net_incl_funding_mean": _f(ind["net_f"].mean()) if n else None,
        "excess_mean": _f(ind["excess"].mean()) if n else None,
        "excess_median": _f(ind["excess"].median()) if n else None,
        "hit_rate": _f((ind["net"] > 0).mean()) if n else None,
        "positive_asset_share": _f((coins > 0).mean()) if len(coins) else None,
    }
    lo, hi = bootstrap_ci(ind["excess"].to_numpy(), st.bootstrap, rng)
    out["excess_ci95"] = [lo, hi]
    if n and len(coins) > 1:
        sign = np.sign(out["excess_mean"] or 0.0)
        contrib = ind.groupby("coin")["excess"].sum() * sign
        drop = contrib.idxmax()
        rest = ind[ind["coin"] != drop]["excess"]
        out["leave_largest_asset_out"] = {"dropped": drop, "excess_mean": _f(rest.mean()),
                                          "sign_survives": bool(np.sign(rest.mean()) == sign)}  # fmt: skip
    gates = {
        "min_independent_events": n >= st.min_independent_events,
        "min_assets_with_events": len(coins) >= st.min_assets_with_events,
        "min_evaluable_fraction": bool(n_window) and len(ok) / n_window >= st.min_evaluable_fraction,
    }  # fmt: skip
    out["sample_gates"] = gates
    out["testable"] = all(gates.values())
    return out


def path_summary(ev_ind: pd.DataFrame, paths: pd.DataFrame, thr: pd.DataFrame) -> dict:
    """MFE/MAE and threshold ordering of the independent events (central variant)."""
    if ev_ind.empty or paths.empty:
        return {}
    pid = ev_ind["rung"] + "|" + ev_ind["direction"] + "|" + ev_ind["key"].astype(str)
    p = paths[paths["event_id"].isin(set(pid)) & paths["complete"]]
    p = p.drop_duplicates(["event_id", "coin"])
    out = {"paths": len(p)}
    for c in ("mfe", "mae", "mfe_close", "mae_close"):
        out[c] = {"median": _q(p[c], 0.5), "p25": _q(p[c], 0.25), "p75": _q(p[c], 0.75),
                  "p90": _q(p[c], 0.9)}  # fmt: skip
    out["bars_to_mfe_median"] = _q(p["bars_to_mfe"], 0.5)
    out["bars_to_mae_median"] = _q(p["bars_to_mae"], 0.5)
    out["mfe_before_mae_share"] = _f((p["mfe_mae_order"] == tp.FAV).mean()) if len(p) else None
    t = thr[thr["event_id"].isin(set(p["event_id"]))]
    for crit in ("atr_1", "pct_1"):
        sub = t[t["criterion"] == crit]
        cnt = sub["order"].value_counts()
        fav, adv = int(cnt.get(tp.FAV, 0)), int(cnt.get(tp.ADV, 0))
        out[f"{crit}_order"] = {"favourable_first": fav, "adverse_first": adv,
                                "neither": int(cnt.get(tp.NEITHER, 0)),
                                "ambiguous": int(cnt.get(tp.AMBIGUOUS, 0)),
                                "favourable_share_of_decided": _f(fav / (fav + adv)) if fav + adv else None}  # fmt: skip
    return out


def r_summary(ev_ind: pd.DataFrame, paths: pd.DataFrame, thr: pd.DataFrame,
              per_side_cost: pd.Series) -> dict:  # fmt: skip
    """+kR before -1R on independent reversal events with an objective invalidation.

    Ambiguous same-bar orderings are never assigned: raw ambiguous counts, how many complete
    15m child bars resolved, and the rest reported as unresolved. Probabilities and the
    net R expectancy are given as bounds (unresolved = all stop / all target) and excluding
    the unresolved. Cost in R = round-trip fee + slippage as a fraction of entry / risk."""
    if ev_ind.empty or paths.empty:
        return {}
    pid = ev_ind["rung"] + "|" + ev_ind["direction"] + "|" + ev_ind["key"].astype(str)
    cost = pd.Series(per_side_cost.reindex(ev_ind["coin"]).to_numpy(), index=pid.to_numpy())
    p = paths[paths["event_id"].isin(set(pid)) & paths["complete"]].drop_duplicates("event_id")
    status = p["risk_status"].value_counts().to_dict()
    ok = p[p["risk_status"] == "OK"].set_index("event_id")
    out = {"risk_status": {k: int(v) for k, v in status.items()},
           "risk_pct_median": _q(ok["risk_pct"], 0.5), "risk_atr_median": _q(ok["risk_atr"], 0.5),
           "r_terminal_median": _q(ok["r_terminal"], 0.5), "r_mfe_median": _q(ok["r_mfe"], 0.5),
           "targets": {}}  # fmt: skip
    if ok.empty:
        return out
    cost_r = (2 * cost.reindex(ok.index).to_numpy()) / (ok["risk_pct"].to_numpy() / 100)
    t = thr[thr["event_id"].isin(set(ok.index))]
    for k in (1, 2, 3, 5):
        sub = t[t["criterion"] == f"r_{k}"].set_index("event_id").reindex(ok.index)
        order = sub["order"].to_numpy()
        res = sub["resolution"].to_numpy()
        raw_amb = (order == tp.AMBIGUOUS) | pd.notna(res)
        fav, adv = order == tp.FAV, order == tp.ADV
        unres = order == tp.AMBIGUOUS
        nei = order == tp.NEITHER
        base_r = np.where(fav, k, np.where(adv, -1.0, np.where(nei, ok["r_terminal"], np.nan)))
        lo = np.where(unres, -1.0, base_r) - cost_r
        hi = np.where(unres, float(k), base_r) - cost_r
        ex = (base_r - cost_r)[~unres]
        dec = int(fav.sum() + adv.sum())
        n = len(order)
        out["targets"][f"{k}R"] = {
            "events": n, "favourable_first": int(fav.sum()), "stop_first": int(adv.sum()),
            "neither": int(nei.sum()), "ambiguous_raw": int(raw_amb.sum()),
            "resolved_by_child_bars": int((res == "RESOLVED").sum()),
            "unresolved": int(unres.sum()),
            "unresolved_reasons": {str(k_): int(v) for k_, v in
                                   pd.Series(res[unres]).value_counts(dropna=False).items()},
            "p_target_first_lower": _f(fav.sum() / n) if n else None,
            "p_target_first_upper": _f((fav.sum() + unres.sum()) / n) if n else None,
            "p_target_first_excl_unresolved": _f(fav.sum() / (n - unres.sum())) if n > unres.sum() else None,
            "p_target_first_of_decided": _f(fav.sum() / dec) if dec else None,
            "net_r_mean_lower": _f(np.nanmean(lo)), "net_r_mean_upper": _f(np.nanmean(hi)),
            "net_r_mean_excl_unresolved": _f(np.nanmean(ex)) if len(ex) else None,
        }  # fmt: skip
    return out


# --------------------------------------------------------------------------- venue analysis


def _primary_rows(ev: pd.DataFrame, hp: int) -> pd.DataFrame:
    return ev[ev["horizon"] == hp]


def analyse_venue(defn: StudyDefinition, arch: Architecture, col: Collected) -> dict:
    st = defn.manifest.statistics
    hp = arch.primary_horizon
    ev = col.frame("events")
    base = col.frame("baseline")
    paths, thr = col.frame("paths"), col.frame("thresholds")
    chains = col.frame("chains")
    nb = col.frame("neighbours")
    bm = col.frame("bmetrics")
    venue = col.venue
    cost = pd.Series({c.coin: c.per_side for c in defn.costs if c.venue == venue})
    keys = ("coin", "d", "vol")
    pool = pools(base, hp, keys) if len(base) else {}
    # Wall-clock timings (col.seconds) are run metadata, never part of the result digest.
    out: dict = {"venue": venue, "inputs": col.inputs, "counts": dict(col.counts)}
    if ev.empty:
        out["empty"] = True
        return out
    evp = _primary_rows(ev, hp)

    def pop_(kind, rung, which, frame=evp):
        return frame[
            (frame["kind"] == kind) & (frame["rung"] == rung) & (frame["direction"] == which)
        ]

    # ---- the ladder and the primary family
    tested = []
    ladder: dict = {}
    combos = [(k, r) for k in LEVEL_KINDS for r in (*LADDER, HELD)]
    if arch.stretch_control is not None:
        combos.append(("none", STRETCH))
    for kind, rung in combos:
        for which in DIRECTIONS:
            e = pop_(kind, rung, which)
            hid = f"{venue}|{arch.name}|{kind}|{rung}|{which}"
            s = summary(e, st, _rng(st.seed, hid, "boot"))
            s.update(kind=kind, rung=rung, direction=which, hypothesis=hid)
            ind = e[e["evaluable"] & e["independent"]]
            if s["testable"]:
                null = null_means(ind, pool, keys, st.draws, _rng(st.seed, hid, "null"))
                if null is None:
                    s["testable"] = False
                    s["untestable_reason"] = "a baseline cell has fewer eligible bars than events"
                else:
                    s["p_value"] = p_greater(s["excess_mean"], null)
                    tested.append(hid)
            else:
                s["untestable_reason"] = "sample gate: " + ", ".join(
                    g for g, ok in s["sample_gates"].items() if not ok
                )
            s["paths"] = path_summary(ind, paths, thr)
            if which == "reversal" and rung in R_RUNGS:
                s["r_paths"] = r_summary(ind, paths, thr, cost)
            ladder.setdefault(kind, {}).setdefault(which, {})[rung] = s
    flat = [s for k in ladder.values() for w in k.values() for s in w.values()]
    pv = {s["hypothesis"]: s["p_value"] for s in flat if "p_value" in s}
    qv = benjamini_hochberg(pv) if pv else {}
    for s in flat:
        s["q_value"] = qv.get(s["hypothesis"])
        s["in_family"] = s["hypothesis"] in qv
    out["ladder"] = ladder
    out["family_primary"] = {"m": len(qv), "preregistered": len(flat),
                             "members": [{k: s.get(k) for k in ("hypothesis", "kind", "rung",
                                          "direction", "independent_events", "excess_mean",
                                          "p_value", "q_value", "in_family", "untestable_reason")}
                                         for s in flat]}  # fmt: skip

    # ---- horizon profile (descriptive)
    out["horizons"] = []
    for kind, rung in combos:
        for which in DIRECTIONS:
            for h in arch.horizons:
                e = ev[(ev["kind"] == kind) & (ev["rung"] == rung) & (ev["direction"] == which)
                       & (ev["horizon"] == h)]  # fmt: skip
                ind = e[e["evaluable"] & e["independent"]]
                out["horizons"].append({"kind": kind, "rung": rung, "direction": which,
                                        "horizon": h, "independent_events": len(ind),
                                        "net_mean": _f(ind["net"].mean()) if len(ind) else None,
                                        "excess_mean": _f(ind["excess"].mean()) if len(ind) else None,
                                        "hit_rate": _f((ind["net"] > 0).mean()) if len(ind) else None})  # fmt: skip

    # ---- transitions / incremental value, chain-linked entry economics
    out["transitions"] = transitions(evp, ladder, st)
    out["entry_delay"] = entry_delay(evp, chains)
    out["conditional_vs_executable"] = conditional(evp, pool, keys)
    if arch.name == "primary":
        out["family_contrasts"] = contrasts(evp, st, venue, arch.name)
        out["shift_ablation"] = shift_ablation(evp, chains, ladder)
        out["retest_ablation"] = retest_ablation(evp, chains)
        out["rejection_continuous"] = rejection_continuous(evp, bm)
        out["level_comparison"] = level_comparison(evp, st, venue)
        out["sensitivity"] = sensitivity(nb, ladder, arch, st)
    if arch.subgroups:
        out["family_subgroups"] = subgroups(evp, base, hp, st, venue)
    out["counts"].update(raw_and_independent(evp))
    return out


def raw_and_independent(evp: pd.DataFrame) -> dict:
    g = evp.groupby(["kind", "rung", "direction"])
    return {"rung_events": {f"{k}|{r}|{d}": {"raw": len(x), "evaluable": int(x["evaluable"].sum()),
                                             "independent": int(x["independent"].sum())}
                            for (k, r, d), x in g}}  # fmt: skip


# --------------------------------------------------------------------------- increments


TRANSITIONS = (("A_breach", "B_failed"), ("B_failed", "C_rejection"), ("C_rejection", "D_shift"),
               ("D_shift", "E_retest"), ("A_breach", HELD))  # fmt: skip


def _chain_entries(evp: pd.DataFrame, kind: str, which: str) -> dict[str, pd.DataFrame]:
    e = evp[(evp["kind"] == kind) & (evp["direction"] == which)]
    return {r: g.drop_duplicates(["coin", "breach_id"]).set_index(["coin", "breach_id"])
            for r, g in e.groupby("rung")}  # fmt: skip


def transitions(evp: pd.DataFrame, ladder: dict, st: StatisticsSpec) -> list[dict]:
    rows = []
    for kind in LEVEL_KINDS:
        for which in DIRECTIONS:
            ce = _chain_entries(evp, kind, which)
            for a, b in TRANSITIONS:
                sa, sb = ladder[kind][which][a], ladder[kind][which][b]
                ia = evp[(evp["kind"] == kind) & (evp["direction"] == which) & (evp["rung"] == a)
                         & evp["independent"]]["excess"].to_numpy()  # fmt: skip
                ib = evp[(evp["kind"] == kind) & (evp["direction"] == which) & (evp["rung"] == b)
                         & evp["independent"]]["excess"].to_numpy()  # fmt: skip
                rng = _rng(st.seed, kind, which, a, b)
                diff_lo = diff_hi = None
                if len(ia) > 1 and len(ib) > 1:
                    ma = ia[rng.integers(0, len(ia), (st.bootstrap, len(ia)))].mean(axis=1)
                    mb = ib[rng.integers(0, len(ib), (st.bootstrap, len(ib)))].mean(axis=1)
                    diff_lo, diff_hi = (
                        _f(np.quantile(mb - ma, 0.025)),
                        _f(np.quantile(mb - ma, 0.975)),
                    )
                row = {"kind": kind, "direction": which, "from": a, "to": b,
                       "independent_from": sa["independent_events"], "independent_to": sb["independent_events"],
                       "retention": _f(sb["events_in_window"] / sa["events_in_window"]) if sa["events_in_window"] else None,
                       "excess_from": sa["excess_mean"], "excess_to": sb["excess_mean"],
                       "excess_change": _f((sb["excess_mean"] or np.nan) - (sa["excess_mean"] or np.nan)),
                       "excess_change_ci95": [diff_lo, diff_hi],
                       "hit_change": _f((sb["hit_rate"] or np.nan) - (sa["hit_rate"] or np.nan)),
                       "net_p10_change": _f((sb["net_p10"] or np.nan) - (sa["net_p10"] or np.nan)),
                       "mfe_median_change": _f(((sb["paths"].get("mfe") or {}).get("median") or np.nan)
                                               - ((sa["paths"].get("mfe") or {}).get("median") or np.nan)),
                       "mae_median_change": _f(((sb["paths"].get("mae") or {}).get("median") or np.nan)
                                               - ((sa["paths"].get("mae") or {}).get("median") or np.nan))}  # fmt: skip
                if a in ce and b in ce:
                    j = ce[b].join(ce[a], how="inner", lsuffix="_to", rsuffix="_from")
                    j = j[np.isfinite(j["entry_price_to"]) & np.isfinite(j["entry_price_from"])]
                    if len(j):
                        delay = (j["entry_ns_to"] - j["entry_ns_from"]) / 3.6e12
                        worse = j["d_to"] * (j["entry_price_to"] / j["entry_price_from"] - 1) * 1e4
                        atr = (
                            j["d_to"] * (j["entry_price_to"] - j["entry_price_from"]) / j["atr_to"]
                        )
                        row.update(linked_chains=len(j), entry_delay_hours_median=_q(delay, 0.5),
                                   entry_delay_hours_mean=_f(delay.mean()),
                                   entry_cost_bps_median=_q(worse, 0.5), entry_cost_bps_mean=_f(worse.mean()),
                                   entry_cost_atr_median=_q(atr, 0.5))  # fmt: skip
                rows.append(row)
    return rows


def entry_delay(evp: pd.DataFrame, chains: pd.DataFrame) -> list[dict]:
    """Per kind and direction: for chains reaching each rung, when each rung became known
    and could be entered, relative to the breach (A) and to the failed breakout (B), in
    hours, bps and ATR (direction-signed: positive = a worse price for the trade)."""
    rows = []
    for kind in LEVEL_KINDS:
        for which in DIRECTIONS:
            ce = _chain_entries(evp, kind, which)
            if "A_breach" not in ce:
                continue
            a = ce["A_breach"]
            for rung in LADDER[1:]:
                if rung not in ce:
                    continue
                j = ce[rung].join(a, how="inner", rsuffix="_a")
                if "B_failed" in ce:
                    j = j.join(ce["B_failed"][["entry_ns", "entry_price"]].rename(
                        columns={"entry_ns": "entry_ns_b", "entry_price": "entry_price_b"}), how="left")  # fmt: skip
                j = j[np.isfinite(j["entry_price"]) & np.isfinite(j["entry_price_a"])]
                if j.empty:
                    continue
                d = j["d"]
                row = {"kind": kind, "direction": which, "rung": rung, "chains": len(j),
                       "hours_after_breach_entry_median": _q((j["entry_ns"] - j["entry_ns_a"]) / 3.6e12, 0.5),
                       "hours_after_breach_entry_p90": _q((j["entry_ns"] - j["entry_ns_a"]) / 3.6e12, 0.9),
                       "entry_cost_vs_breach_bps_median": _q(d * (j["entry_price"] / j["entry_price_a"] - 1) * 1e4, 0.5),
                       "entry_cost_vs_breach_bps_mean": _f((d * (j["entry_price"] / j["entry_price_a"] - 1) * 1e4).mean()),
                       "entry_cost_vs_breach_atr_median": _q(d * (j["entry_price"] - j["entry_price_a"]) / j["atr"], 0.5)}  # fmt: skip
                if "entry_price_b" in j and rung != "B_failed":
                    jb = j[np.isfinite(j["entry_price_b"])]
                    row.update(
                        hours_after_failed_entry_median=_q((jb["entry_ns"] - jb["entry_ns_b"]) / 3.6e12, 0.5),
                        entry_cost_vs_failed_bps_median=_q(jb["d"] * (jb["entry_price"] / jb["entry_price_b"] - 1) * 1e4, 0.5),
                        entry_cost_vs_failed_bps_mean=_f((jb["d"] * (jb["entry_price"] / jb["entry_price_b"] - 1) * 1e4).mean()),
                    )  # fmt: skip
                rows.append(row)
    # stage-bar-close deltas from the chain table (the Phase 16 entry-delay analytics)
    if len(chains):
        for kind, g in chains.groupby("kind"):
            for stage in ("rejection", "shift", "retest"):
                c = (
                    g[f"{stage}_delay_hours"].dropna()
                    if f"{stage}_delay_hours" in g
                    else pd.Series(dtype=float)
                )
                if len(c):
                    rows.append({"kind": kind, "stage_close_vs_failed": stage, "chains": len(c),
                                 "delay_hours_median": _q(c, 0.5),
                                 "reversal_entry_cost_bps_median": _q(g[f"{stage}_entry_cost_bps"], 0.5),
                                 "reversal_entry_cost_bps_mean": _f(g[f"{stage}_entry_cost_bps"].mean())})  # fmt: skip
    return rows


def conditional(evp: pd.DataFrame, pool: dict, keys) -> list[dict]:
    """Conditional signal quality versus entry economics, at the primary horizon.

    For the chains that reach a later rung, the excess measured from the EARLIER rung's
    entry (B, and A) is a conditional quantity: selecting those chains used information
    that did not exist at that entry, so it is NOT executable. The executable figure is
    the excess from the rung's own entry. The gap between them is what waiting cost."""
    rows = []
    for kind in LEVEL_KINDS:
        for which in DIRECTIONS:
            ce = _chain_entries(evp, kind, which)
            for rung in ("C_rejection", "D_shift", "E_retest"):
                if rung not in ce:
                    continue
                own = ce[rung]
                ind = own[own["independent"]]
                row = {"kind": kind, "direction": which, "rung": rung, "independent_events": len(ind),
                       "executable_excess_mean": _f(ind["excess"].mean()) if len(ind) else None}  # fmt: skip
                for early in ("A_breach", "B_failed"):
                    if early not in ce:
                        continue
                    sub = ce[early].reindex(ind.index)
                    sub = sub[sub["evaluable"].fillna(False).astype(bool)]
                    row[f"conditional_excess_from_{early}_entry"] = (
                        _f(sub["excess"].mean()) if len(sub) else None
                    )
                    row[f"conditional_n_{early}"] = len(sub)
                rows.append(row)
    return rows


def contrasts(evp: pd.DataFrame, st: StatisticsSpec, venue: str, arch: str) -> dict:
    """Two-sided permutation contrasts on disjoint sets, reversal direction (continuation
    is the same contrast with the sign flipped):

    - failed vs held: B_failed vs H_held, each at its own executable entry;
    - rejection vs none: among failed breakouts, C vs B without C. Both share the failure
      entry (rejection is decided on bars up to the failure bar), so this isolates the
      information in the wick at an identical executable moment."""
    rows = []
    for kind in LEVEL_KINDS:
        e = evp[(evp["kind"] == kind) & (evp["direction"] == "reversal") & evp["independent"]]
        b = e[e["rung"] == "B_failed"]
        c_keys = set(evp[(evp["kind"] == kind) & (evp["rung"] == "C_rejection")]["failed_id"])
        for name, x, y in (("failed_vs_held", b, e[e["rung"] == HELD]),
                           ("rejection_vs_none", b[b["key"].isin(c_keys)], b[~b["key"].isin(c_keys)])):  # fmt: skip
            hid = f"{venue}|{arch}|{kind}|{name}"
            row = {"hypothesis": hid, "kind": kind, "contrast": name, "n_first": len(x), "n_second": len(y),
                   "excess_first": _f(x["excess"].mean()) if len(x) else None,
                   "excess_second": _f(y["excess"].mean()) if len(y) else None}  # fmt: skip
            if len(x) >= st.min_independent_events and len(y) >= st.min_independent_events:
                row["difference"] = _f(x["excess"].mean() - y["excess"].mean())
                row["p_value"] = permutation_p(x["excess"].to_numpy(), y["excess"].to_numpy(),
                                               st.permutations, _rng(st.seed, hid))  # fmt: skip
            else:
                row["untestable_reason"] = "fewer independent events than the minimum in a group"
            rows.append(row)
    pv = {r["hypothesis"]: r["p_value"] for r in rows if "p_value" in r}
    qv = benjamini_hochberg(pv) if pv else {}
    for r in rows:
        r["q_value"] = qv.get(r["hypothesis"])
    return {"m": len(qv), "preregistered": len(rows), "members": rows}


def shift_ablation(evp: pd.DataFrame, chains: pd.DataFrame, ladder: dict) -> list[dict]:
    rows = []
    for kind in LEVEL_KINDS:
        g = chains[chains["kind"] == kind] if len(chains) else chains
        rej = g[g["rejection_status"] == "REJECTION"] if len(g) else g
        status = rej["shift_status"].value_counts().to_dict() if len(rej) else {}
        c, d = ladder[kind]["reversal"]["C_rejection"], ladder[kind]["reversal"]["D_shift"]
        rows.append({"kind": kind, "c_chains": len(rej),
                     "shift_status": {str(k): int(v) for k, v in status.items()},
                     "retention": _f(status.get("SHIFT", 0) / len(rej)) if len(rej) else None,
                     "c_excess": c["excess_mean"], "d_excess": d["excess_mean"],
                     "c_independent": c["independent_events"], "d_independent": d["independent_events"]})  # fmt: skip
    return rows


def retest_ablation(evp: pd.DataFrame, chains: pd.DataFrame) -> list[dict]:
    """Of the structure shifts (D chains): how many retest, how long it takes, what the
    entry change is, and what the moves that never retest did (the opportunity cost)."""
    rows = []
    for kind in LEVEL_KINDS:
        g = chains[(chains["kind"] == kind)] if len(chains) else chains
        dch = (
            g[(g["rejection_status"] == "REJECTION") & (g["shift_status"] == "SHIFT")]
            if len(g)
            else g
        )
        st = dch["retest_status"].value_counts().to_dict() if len(dch) else {}
        for which in DIRECTIONS:
            ce = _chain_entries(evp, kind, which)
            row = {"kind": kind, "direction": which, "d_chains": len(dch),
                   "retest_status": {str(k): int(v) for k, v in st.items()},
                   "retest_share": _f(st.get("RETEST", 0) / len(dch)) if len(dch) else None,
                   "wait_hours_median": _q(dch.get("retest_wait_hours", pd.Series(dtype=float)), 0.5)}  # fmt: skip
            if "D_shift" in ce:
                dd = ce["D_shift"]
                ids = (
                    set(dch[dch["retest_status"] == "NO_RETEST"]["breach_id"])
                    if len(dch)
                    else set()
                )
                miss = dd[dd.index.get_level_values("breach_id").isin(ids) & dd["independent"]]
                row["never_retested_independent"] = len(miss)
                row["never_retested_excess_at_d_entry"] = (
                    _f(miss["excess"].mean()) if len(miss) else None
                )
                ids_r = (
                    set(dch[dch["retest_status"] == "RETEST"]["breach_id"]) if len(dch) else set()
                )
                hit = dd[dd.index.get_level_values("breach_id").isin(ids_r) & dd["independent"]]
                row["retested_excess_at_d_entry"] = _f(hit["excess"].mean()) if len(hit) else None
            if "E_retest" in ce:
                ee = ce["E_retest"]
                ind = ee[ee["independent"]]
                row["retest_excess_at_e_entry"] = _f(ind["excess"].mean()) if len(ind) else None
                if "D_shift" in ce:
                    j = ee.join(ce["D_shift"], how="inner", rsuffix="_d")
                    j = j[np.isfinite(j["entry_price"]) & np.isfinite(j["entry_price_d"])]
                    if len(j):
                        row["e_vs_d_entry_cost_bps_median"] = _q(
                            j["d"] * (j["entry_price"] / j["entry_price_d"] - 1) * 1e4, 0.5
                        )
                        row["e_vs_d_entry_cost_bps_mean"] = _f((j["d"] * (j["entry_price"] / j["entry_price_d"] - 1) * 1e4).mean())  # fmt: skip
            rows.append(row)
    return rows


def rejection_continuous(evp: pd.DataFrame, bm: pd.DataFrame) -> list[dict]:
    """Continuous rejection metrics of failed breakouts versus their reversal excess
    (independent B events): Spearman correlation and quintile means. Descriptive only."""
    rows = []
    if bm.empty:
        return rows
    for kind in LEVEL_KINDS:
        b = evp[(evp["kind"] == kind) & (evp["rung"] == "B_failed") & (evp["direction"] == "reversal")
                & evp["independent"]]  # fmt: skip
        m = b.merge(bm[bm["kind"] == kind].drop(columns="kind"), on=["coin", "key"], how="inner")
        for metric in ("wick_range", "clv_reversal", "reversal_atr"):
            x = m[[metric, "excess"]].dropna()
            row = {"kind": kind, "metric": metric, "n": len(x)}
            if len(x) >= 10:
                row["spearman"] = _f(x[metric].rank().corr(x["excess"].rank()))
                qs = pd.qcut(x[metric].rank(method="first"), 5, labels=False)
                row["quintile_excess"] = [_f(v) for v in x.groupby(qs)["excess"].mean().to_numpy()]
                row["quintile_upper_bounds"] = [
                    _f(v) for v in x.groupby(qs)[metric].max().to_numpy()
                ]
            rows.append(row)
        sb = m.groupby("same_bar")["excess"].agg(["mean", "size"]) if len(m) else None
        if sb is not None:
            rows.append({"kind": kind, "metric": "same_bar_failure",
                         "groups": {str(k): {"excess_mean": _f(v["mean"]), "n": int(v["size"])}
                                    for k, v in sb.iterrows()}})  # fmt: skip
    return rows


def level_comparison(evp: pd.DataFrame, st: StatisticsSpec, venue: str) -> dict:
    """Does the cluster ('equal highs/lows') or swing representation add anything beyond a
    plain prior N-bar extreme? Event overlap and excess differences with bootstrap CIs."""
    out: dict = {"overlap": [], "differences": []}
    for rung in ("A_breach", "B_failed"):
        sets = {}
        for kind in LEVEL_KINDS:
            e = evp[
                (evp["kind"] == kind) & (evp["rung"] == rung) & (evp["direction"] == "reversal")
            ]
            # the first chain event of each coin/side/bar: same bar and side = same price action
            sets[kind] = set(zip(e["coin"], e["side"], e["entry_ns"], strict=True))
        for a, b in (
            ("cluster", "prior_extreme"),
            ("swing", "prior_extreme"),
            ("cluster", "swing"),
        ):
            inter = len(sets[a] & sets[b])
            out["overlap"].append({"rung": rung, "a": a, "b": b, "a_events": len(sets[a]),
                                   "b_events": len(sets[b]), "shared_entries": inter,
                                   "share_of_a_in_b": _f(inter / len(sets[a])) if sets[a] else None})  # fmt: skip
        for which in DIRECTIONS:
            for a in ("cluster", "swing"):
                x = evp[
                    (evp["kind"] == a)
                    & (evp["rung"] == rung)
                    & (evp["direction"] == which)
                    & evp["independent"]
                ]["excess"].to_numpy()
                y = evp[(evp["kind"] == "prior_extreme") & (evp["rung"] == rung) & (evp["direction"] == which) & evp["independent"]]["excess"].to_numpy()  # fmt: skip
                row = {
                    "rung": rung,
                    "direction": which,
                    "kind": a,
                    "versus": "prior_extreme",
                    "n_kind": len(x),
                    "n_prior": len(y),
                }
                if len(x) > 1 and len(y) > 1:
                    rng = _rng(st.seed, venue, rung, which, a, "level")
                    mx = x[rng.integers(0, len(x), (st.bootstrap, len(x)))].mean(axis=1)
                    my = y[rng.integers(0, len(y), (st.bootstrap, len(y)))].mean(axis=1)
                    row.update(difference=_f(x.mean() - y.mean()),
                               difference_ci95=[_f(np.quantile(mx - my, 0.025)),
                                                _f(np.quantile(mx - my, 0.975))])  # fmt: skip
                out["differences"].append(row)
    return out


def sensitivity(
    nb: pd.DataFrame, ladder: dict, arch: Architecture, st: StatisticsSpec
) -> list[dict]:
    """One-at-a-time neighbours around the central parameters, per (kind, rung, direction):
    Prism's ``plateau_verdict`` (frozen thresholds 0.5 / 0.5 / 60%) and sign agreement.
    Descriptive only: no neighbour is tested, selected or promoted."""
    rows = []
    vals = {a.name: list(a.values) for a in arch.axes}
    for kind in LEVEL_KINDS:
        for which in DIRECTIONS:
            for rung in (*LADDER, HELD):
                c = ladder[kind][which][rung]
                axes = [a for a in axes_for(kind, rung) if a in vals]
                if not axes:
                    continue
                default = {a: arch.central.value(a) for a in axes}
                table = [{**default, "n_indep": c["independent_events"],
                          "excess_mean": c["excess_mean"] if c["excess_mean"] is not None else np.nan,
                          "variant": "central"}]  # fmt: skip
                sub = (
                    nb[(nb["kind"] == kind) & (nb["rung"] == rung) & (nb["direction"] == which)]
                    if len(nb)
                    else nb
                )
                for a in axes:
                    col = f"ax_{a}"
                    if not len(sub) or col not in sub:
                        continue
                    for v, g in sub[sub[col].notna()].groupby(col):
                        table.append({**default, a: v, "n_indep": len(g),
                                      "excess_mean": float(g["excess"].mean()), "variant": f"{a}={v:g}"})  # fmt: skip
                t = pd.DataFrame(table)
                verdict = plateau_verdict(t, default, {a: vals[a] for a in axes},
                                          st.min_independent_events, 0.5, 0.5)  # fmt: skip
                nbs = t[t["variant"] != "central"]
                ce = c["excess_mean"]
                rows.append({"kind": kind, "rung": rung, "direction": which, "axes": axes,
                             "verdict": verdict.get("verdict"),
                             "neighbours": [{"variant": r["variant"], "independent_events": int(r["n_indep"]),
                                             "excess_mean": _f(r["excess_mean"])} for _, r in nbs.iterrows()],
                             "sign_agreement": _f((np.sign(nbs["excess_mean"]) == np.sign(ce)).mean())
                             if len(nbs) and ce is not None else None,
                             "excess_range": [_f(t["excess_mean"].min()), _f(t["excess_mean"].max())]})  # fmt: skip
    return rows


def subgroups(
    evp: pd.DataFrame, base: pd.DataFrame, hp: int, st: StatisticsSpec, venue: str
) -> dict:
    """Predefined regime / volatility cells for A, B and H, reversal direction, two-sided.
    Regime cells compare with random entries in the SAME regime (and vol tercile)."""
    rows = []
    pool_v = pools(base, hp, ("coin", "d", "vol"))
    pool_r = pools(base, hp, ("coin", "d", "vol", "regime"))
    for kind in LEVEL_KINDS:
        for rung in SUBGROUP_RUNGS:
            e = evp[(evp["kind"] == kind) & (evp["rung"] == rung) & (evp["direction"] == "reversal")
                    & evp["evaluable"] & evp["independent"]]  # fmt: skip
            for cell in SUBGROUP_CELLS:
                hid = f"{venue}|primary|{kind}|{rung}|reversal|{cell}"
                if cell.startswith("vol_"):
                    v = {n: k for k, n in VOL_NAMES.items()}[cell]
                    x, col, pool, keys = e[e["vol"] == v], "excess", pool_v, ("coin", "d", "vol")
                else:
                    x, col, pool, keys = (
                        e[e["regime"] == cell],
                        "excess_reg",
                        pool_r,
                        ("coin", "d", "vol", "regime"),
                    )
                x = x[np.isfinite(x[col])]
                row = {"hypothesis": hid, "kind": kind, "rung": rung, "cell": cell,
                       "independent_events": len(x), "assets": int(x["coin"].nunique()),
                       "excess_mean": _f(x[col].mean()) if len(x) else None,
                       "net_mean": _f(x["net"].mean()) if len(x) else None}  # fmt: skip
                if (
                    len(x) >= st.min_independent_events
                    and x["coin"].nunique() >= st.min_assets_with_events
                ):
                    null = null_means(x, pool, keys, st.draws, _rng(st.seed, hid))
                    if null is not None:
                        row["p_value"] = p_two_sided(float(x[col].mean()), null)
                if "p_value" not in row:
                    row["untestable_reason"] = "sample gate or thin baseline cell"
                rows.append(row)
    pv = {r["hypothesis"]: r["p_value"] for r in rows if "p_value" in r}
    qv = benjamini_hochberg(pv) if pv else {}
    for r in rows:
        r["q_value"] = qv.get(r["hypothesis"])
    return {"m": len(qv), "preregistered": len(rows), "members": rows}
