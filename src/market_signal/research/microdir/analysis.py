"""``microdir_inference_v1`` / ``microdir_verdicts_v1`` / ``microdir_maturity_v1``.

``evaluate(frames, defn, as_of=...)`` turns per-coin 1-minute frames into the full Phase 24B
result: events, independent episodes, side-signed net outcomes after the frozen costs,
block-clustered inference, BH within families, contrasts, candle twins, the candle-only
baseline ladder, matched random-entry excess, maturity, verdicts, frequency, breakdowns,
examples and the failure analysis. Pure: no database access (``data.py`` loads the frames
and the context providers), so the synthetic calibration runs exactly this code.

Units: returns are fractions of notional (``0.001`` = 10 bp). The unit of inference is the
independent episode (greedy refractory of ``DEDUP_MINUTES`` per coin, hypothesis and side).
Blocks are UTC ``BLOCK_HOURS`` intervals of the entry time, shared by all coins, with
adjacent-block covariance (``relative.study.stats``): six coins reacting to one BTC move
are one cluster, not six confirmations.
"""

from __future__ import annotations

import hashlib
import math
import warnings
from collections.abc import Callable

import numpy as np
import pandas as pd

from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.microdir import features as ft
from market_signal.research.microdir import outcomes as oc
from market_signal.research.microdir import spec as sp
from market_signal.research.relative.study.stats import (
    betainc,
    clustered_difference,
    clustered_mean,
)

H = sp.PRIMARY_HORIZON
CrowdingFn = Callable[[str, pd.Timestamp], dict]
ContextFn = Callable[[str, pd.Timestamp], str]


# --------------------------------------------------------------------------- helpers


def clean(o):
    """JSON-safe, deterministic: numpy -> python, NaN/inf -> None, sorted-free structures."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [clean(v) for v in o.tolist()]
    if isinstance(o, np.bool_ | bool):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating | float):
        v = float(o)
        return round(v, 10) if math.isfinite(v) else None
    if isinstance(o, pd.Timestamp):
        return None if pd.isna(o) else o.isoformat()
    if o is pd.NaT:
        return None
    return o


def event_id(coin: str, window_open: pd.Timestamp, member: str) -> str:
    blob = f"{sp.STUDY_NAME}|{coin}|{pd.Timestamp(window_open).isoformat()}|{member}"
    return "mdevt_" + hashlib.sha256(blob.encode()).hexdigest()[:24]


def one_sided_p(t: float | None, p: float | None) -> float:
    """P(mean <= 0) evidence against, for the hypothesized (positive) direction."""
    if t is None or (p is None and math.isfinite(t)):
        return 1.0
    if not math.isfinite(t):
        return 0.0 if t > 0 else 1.0
    return p / 2 if t > 0 else 1 - p / 2


def ns(times) -> np.ndarray:
    """Epoch nanoseconds, independent of the datetime resolution pandas chose."""
    t = pd.to_datetime(pd.Series(times), utc=True)
    return ((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(1, "ns")).to_numpy(np.int64)


def _blocks(entry: pd.Series) -> np.ndarray:
    return ns(entry) // (sp.BLOCK_HOURS * 3600 * 10**9)


def cm(values, entry: pd.Series) -> dict:
    v = np.asarray(values, float)
    r = clustered_mean(v, _blocks(entry)) if len(v) else {"mean": None, "t": None,
                                                         "p_value": None, "clusters": 0}  # fmt: skip
    r["n"] = int(np.isfinite(v).sum())
    r["p_one_sided"] = one_sided_p(r.get("t"), r.get("p_value"))
    return r


def f_sf(F: float, d1: int, d2: int) -> float:
    if not math.isfinite(F) or F <= 0:
        return 1.0 if not (math.isfinite(F) and F > 0) else 0.0
    return float(betainc(d2 / 2, d1 / 2, d2 / (d2 + d1 * F)))


# --------------------------------------------------------------------------- panel


def panel(frames: dict[str, pd.DataFrame], *, crowding: CrowdingFn | None = None,
          context: ContextFn | None = None) -> pd.DataFrame:  # fmt: skip
    """All coins' 15m windows with features, flags inputs and long-direction outcomes."""
    parts = []
    for coin in sp.COINS:
        if coin not in frames or frames[coin] is None or frames[coin].empty:
            continue
        f, g = ft.windows(frames[coin], coin)
        if f.empty:
            continue
        parts.append(oc.compute(f, g))
    if not parts:
        return pd.DataFrame()
    P = pd.concat(parts, ignore_index=True)
    btc = P[P["coin"] == "BTC"].set_index("window_open")["ret_mid"]
    P["btc_dir"] = np.sign(P["window_open"].map(btc)).where(P["coin"] != "BTC")
    P["btc_alignment"] = np.select(
        [P["coin"] == "BTC", P["btc_dir"].isna() | (P["flow_side"] == 0),
         P["btc_dir"] == P["flow_side"]], ["self", "unknown", "same"], "opposite")  # fmt: skip
    # crowding / context: only for one-sided eligible windows (the event universe)
    ev = (P["flow_side"] != 0) & P["eligible"]
    P["crowd_skew"] = None
    P["crowd_aligned"] = P["funding_aligned"]
    P["crowd_known"] = np.isfinite(P["funding_pct"].to_numpy(float))
    P["context"] = "none" if context is None else None
    for i in np.flatnonzero(ev.to_numpy()):
        coin, t = P.at[i, "coin"], P.at[i, "available_at"]
        if crowding is not None:
            c = crowding(coin, t) or {}
            skew = c.get("skew")
            P.at[i, "crowd_skew"] = skew
            s = P.at[i, "flow_side"]
            aligned = skew == ("crowded_long_like" if s > 0 else "crowded_short_like")
            P.at[i, "crowd_aligned"] = bool(P.at[i, "funding_aligned"] or aligned)
            P.at[i, "crowd_known"] = bool(P.at[i, "crowd_known"] or skew not in (None, "unknown"))
        if context is not None:
            P.at[i, "context"] = context(coin, t)
    P["context"] = P["context"].fillna("none")
    P["block"] = _blocks(P["entry_time"].fillna(P["window_close"]))
    return P


def _costs(defn, coins: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fee = coins.map({c.coin: c.fee_bps for c in defn.costs}).to_numpy(float)
    slip = coins.map({c.coin: c.slippage_bps for c in defn.costs}).to_numpy(float)
    return fee + slip, fee, slip


def dedup(times: pd.Series, coins: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Greedy refractory per coin: keep an event, suppress the same (coin, member, side)
    events whose window opens within ``DEDUP_MINUTES`` of the last kept one. Returns (kept
    mask, episode index = position of the kept event each raw event belongs to)."""
    t = ns(times)
    gap = sp.DEDUP_MINUTES * 60 * 10**9
    keep = np.zeros(len(t), bool)
    ep = np.full(len(t), -1)
    order = np.lexsort((t, coins.to_numpy()))
    last: dict = {}
    for i in order:
        c = coins.iat[i]
        if c in last and t[i] - t[last[c]] < gap:
            ep[i] = last[c]
            continue
        keep[i] = True
        last[c] = i
        ep[i] = i
    return keep, ep


def events(P: pd.DataFrame, defn, h: sp.Hypothesis, side: str) -> pd.DataFrame:
    """Raw events of one member with side-signed outcomes; ``independent`` marks episodes."""
    fl = ft.flags(P)
    m = ft.member_mask(P, fl, h, side)
    E = P[m].copy()
    if E.empty:
        return E
    keep, ep = dedup(E["window_open"], E["coin"])
    E["independent"] = keep
    E["episode_of"] = E.index.to_numpy()[ep]
    cost, fee, slip = _costs(defn, E["coin"])
    sv = oc.side_view(E, side, cost, fee, slip)
    for c in sv.columns:
        E[c] = sv[c]
    E["member"] = sp.member_key(h.key, side)
    E["event_id"] = [event_id(c, w, E["member"].iat[0]) for c, w in
                     zip(E["coin"], E["window_open"], strict=True)]  # fmt: skip
    return E


def twin_events(P: pd.DataFrame, defn, twin: str, side: str) -> pd.DataFrame:
    d = 1 if side == "long" else -1
    E = P[P["eligible"] & (P[f"twin_{twin}"] == d)].copy()
    if E.empty:
        return E
    keep, _ = dedup(E["window_open"], E["coin"])
    E["independent"] = keep
    cost, fee, slip = _costs(defn, E["coin"])
    sv = oc.side_view(E, side, cost, fee, slip)
    for c in sv.columns:
        E[c] = sv[c]
    return E


# --------------------------------------------------------------------------- baselines


def candle_model(P: pd.DataFrame) -> tuple[np.ndarray, dict]:
    """OLS of the next-1h long return (in sigma(1h) units) on the candle-only terms, fitted on
    every eligible window. Returns (prediction per row of P, fit summary)."""
    X, y, ok = ladder_matrix(P, ("candle",))
    pred = np.full(len(P), np.nan)
    if ok.sum() < 20:
        return pred, {"n": int(ok.sum()), "beta": None}
    beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
    Xa = np.nan_to_num(X)
    pred[:] = Xa @ beta
    names = ["const", *sp.LADDER[0][1]]
    return pred, {"n": int(ok.sum()), "beta": dict(zip(names, beta.tolist(), strict=True))}


def _terms(P: pd.DataFrame) -> dict[str, np.ndarray]:
    fl = ft.flags(P)
    s = P["flow_side"].to_numpy(float) * fl["one_sided"]
    clip = sp.Z_CLIP

    def c(x):
        return np.clip(np.asarray(x, float), -clip, clip)

    f_abs = s * fl["resp_weak"]
    return {
        "c_ret": c(P["c_ret_z"]), "c_clv": P["clv"].to_numpy(float) - 0.5,
        "c_mom": P["twin_candle_momentum"].to_numpy(float),
        "c_absorb": P["twin_candle_absorption"].to_numpy(float),
        "f_flow": c(P["flow_z"]), "f_cont": s * fl["resp_efficient"], "f_abs": f_abs,
        "b_imb": P["imb5_end"].to_numpy(float), "b_res": s * fl["opp_resilient"],
        "o_flow": s * fl["oi_rising"], "o_abs": f_abs * fl["oi_rising"],
        "p_fund": P["funding_pct"].to_numpy(float) - 0.5,
        "p_abs_crowd": f_abs * fl["crowd_aligned"],
    }  # fmt: skip


def ladder_matrix(P: pd.DataFrame, layers: tuple[str, ...]):
    T = _terms(P)
    names = [n for lay, ns in sp.LADDER if lay in layers for n in ns]
    X = np.column_stack([np.ones(len(P)), *[T[n] for n in names]])
    y = P[f"gross_long_{H}"].to_numpy(float) / (2 * P["sigma15"].to_numpy(float))
    ok = P["eligible"].to_numpy() & np.isfinite(y) & np.isfinite(X).all(1)
    return X, y, ok


def _robust_ols(X: np.ndarray, y: np.ndarray, blocks: np.ndarray) -> tuple:
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    u = y - X @ beta
    meat = np.zeros((X.shape[1], X.shape[1]))
    ids, inv = np.unique(blocks, return_inverse=True)
    S = np.zeros((len(ids), X.shape[1]))
    np.add.at(S, inv, X * u[:, None])
    meat = S.T @ S
    # adjacent blocks share overlapping forward windows (as in ``clustered_mean``); keep the
    # cross terms when the result stays positive semi-definite
    adj = np.diff(ids) == 1
    if adj.any():
        cross = S[:-1][adj].T @ S[1:][adj]
        wide = meat + cross + cross.T
        if np.linalg.eigvalsh((wide + wide.T) / 2).min() >= 0:
            meat = wide
    G = len(ids)
    V = XtX_inv @ meat @ XtX_inv * (G / max(G - 1, 1))
    r2 = 1 - (u @ u) / max(((y - y.mean()) ** 2).sum(), 1e-300)
    return beta, V, r2, G


def ladder(P: pd.DataFrame) -> dict:
    """Nested candle -> +flow -> +book -> +OI -> +positioning OLS on identical rows, with a
    block-clustered Wald F test of each layer's added terms. ``y`` = next-1h long return
    in sigma(1h) units; a layer "adds information" if its F test rejects."""
    _, y, ok = ladder_matrix(P, tuple(lay for lay, _ in sp.LADDER))
    n = int(ok.sum())
    out: dict = {"n": n, "y": "next-1h long mid return / (2 * trailing sigma15)", "layers": []}
    if n < 50:
        out["note"] = "too few eligible windows"
        return out
    blocks = P["block"].to_numpy()[ok]
    yy = y[ok]
    prev_r2 = None
    for k in range(1, len(sp.LADDER) + 1):
        layers = tuple(lay for lay, _ in sp.LADDER[:k])
        X, _, _ = ladder_matrix(P, layers)
        X = X[ok]
        names = ["const", *[nm for lay, ns in sp.LADDER[:k] for nm in ns]]
        live = np.ones(X.shape[1], bool)
        live[1:] = X[:, 1:].std(0) > 0  # a term that never varies is not estimable
        beta, V, r2, G = _robust_ols(X[:, live], yy, blocks)
        b = dict.fromkeys(names)
        t = dict.fromkeys(names)
        for j, nm in enumerate(np.array(names)[live]):
            b[nm] = float(beta[j])
            t[nm] = float(beta[j] / math.sqrt(V[j, j])) if V[j, j] > 0 else None
        added = [nm for nm in sp.LADDER[k - 1][1]]
        idx = [list(np.array(names)[live]).index(nm) for nm in added if nm in np.array(names)[live]]
        test = {"F": None, "df1": len(idx), "df2": G - 1, "p_value": None}
        if k > 1 and idx:
            bb = beta[idx]
            Vs = V[np.ix_(idx, idx)]
            try:
                W = float(bb @ np.linalg.solve(Vs, bb))
                F = W / len(idx)
                test = {"F": F, "df1": len(idx), "df2": G - 1, "p_value": f_sf(F, len(idx), G - 1)}
            except np.linalg.LinAlgError:
                pass
        out["layers"].append({"layer": sp.LADDER[k - 1][0], "terms": added, "r2": r2,
                              "delta_r2": None if prev_r2 is None else r2 - prev_r2,
                              "added_test": test if k > 1 else None,
                              "coef": {nm: b[nm] for nm in names[1:]},
                              "t": {nm: t[nm] for nm in names[1:]}})  # fmt: skip
        prev_r2 = r2
    return out


def random_expectation(P: pd.DataFrame, defn, side: str) -> pd.Series:
    """Matched random-entry expectation: mean side-signed net over ALL eligible windows of the
    same coin, volatility tercile and UTC session (deterministic: every window is a draw)."""
    B = P[P["eligible"]].copy()
    cost, fee, slip = _costs(defn, B["coin"])
    sv = oc.side_view(B, side, cost, fee, slip)
    B["net"] = sv["net"]
    key = ["coin", "vol_state", "session"]
    mean = B.groupby(key)["net"].mean()
    return mean


# --------------------------------------------------------------------------- stats


def frequency(E: pd.DataFrame, span_days: int) -> dict:
    if E.empty or span_days <= 0:
        return {"raw_per_day": 0.0, "independent_per_day": 0.0, "per_asset_day": {},
                "median_gap_hours": None, "days_zero": 1.0, "days_1plus": 0.0,
                "days_3plus": 0.0, "days_5plus": 0.0, "overlap": None}  # fmt: skip
    ind = E[E["independent"]]
    per_day = ind.groupby(ind["window_open"].dt.floor("D")).size()
    counts = np.concatenate([per_day.to_numpy(), np.zeros(max(span_days - len(per_day), 0))])
    t = np.sort(ns(ind["window_open"]))
    gaps = np.diff(t) / 3.6e12
    return {
        "raw_per_day": len(E) / span_days, "independent_per_day": len(ind) / span_days,
        "per_asset_day": {c: n / span_days for c, n in ind.groupby("coin").size().items()},
        "median_gap_hours": float(np.median(gaps)) if len(gaps) else None,
        "days_zero": float((counts == 0).mean()), "days_1plus": float((counts >= 1).mean()),
        "days_3plus": float((counts >= 3).mean()), "days_5plus": float((counts >= 5).mean()),
        "raw": len(E), "independent": len(ind), "overlap": 1 - len(ind) / len(E),
    }  # fmt: skip


def _by(S: pd.DataFrame, col: str) -> dict:
    out = {}
    for k, g in S.groupby(col, dropna=False):
        out[str(k)] = {"n": len(g), "net_mean": float(g["net"].mean()),
                       "gross_mean": float(g["gross"].mean()),
                       "win_rate": float((g["gross"] > 0).mean())}  # fmt: skip
    return out


def member_stats(E: pd.DataFrame, P: pd.DataFrame, defn, side: str, cand_pred: np.ndarray,
                 rand: pd.Series, span_days: int, end: pd.Timestamp) -> dict:  # fmt: skip
    raw = len(E)
    if E.empty:
        return {
            "raw_events": 0,
            "episodes": 0,
            "evaluable": 0,
            "frequency": frequency(E, span_days),
        }
    ind = E[E["independent"]]
    pending = int(ind[f"pending_{H}"].sum())
    missing = int(ind[f"missing_{H}"].sum())
    S = ind[np.isfinite(ind["net"].to_numpy(float))].copy()
    out: dict = {"raw_events": raw, "episodes": len(ind), "evaluable": len(S),
                 "pending": pending, "missing": missing, "frequency": frequency(E, span_days)}  # fmt: skip
    if S.empty:
        return out
    d = 1.0 if side == "long" else -1.0
    ent = S["entry_time"]
    out["net"] = cm(S["net"], ent)
    out["gross"] = cm(S["gross"], ent)
    out["net_passive"] = cm(S["net_passive"], ent)
    out["costs"] = {"fees": float(S["fees"].mean()), "slippage": float(S["slippage"].mean()),
                    "funding": float(S["funding"].mean())}  # fmt: skip
    g = out["gross"]["mean"]
    out["break_even_round_trip_bps"] = None if g is None else g * 1e4
    out["frozen_round_trip_bps"] = float((S["fees"] + S["slippage"]).mean() * 1e4)
    out["horizons"] = {str(h): {"gross_mean": float(S[f"gross_{h}"].mean()),
                                "net_mean": float(S[f"net_{h}"].mean()),
                                "sign_accuracy": float((S[f"gross_{h}"] > 0).mean()),
                                "n": int(S[f"gross_{h}"].notna().sum())}
                       for h in sp.HORIZONS_MIN}  # fmt: skip
    out["sign_accuracy"] = float((S["gross"] > 0).mean())
    # matched random entry
    key = list(zip(S["coin"], S["vol_state"], S["session"], strict=True))
    exp = np.array([rand.get(k, np.nan) for k in key], float)
    out["excess_vs_random"] = cm(S["net"].to_numpy(float) - exp, ent)
    # candle residual (sigma(1h) units and bps)
    sig1h = 2 * S["sigma15"].to_numpy(float)
    y = S[f"gross_long_{H}"].to_numpy(float) / sig1h
    resid = d * (y - cand_pred[S.index.to_numpy()])
    out["candle_residual"] = cm(resid, ent)
    out["candle_residual_bps"] = float(np.nanmean(resid * sig1h) * 1e4)
    # path
    out["path"] = {"mfe_mean": float(S["mfe"].mean()), "mfe_median": float(S["mfe"].median()),
                   "mae_mean": float(S["mae"].mean()), "mae_median": float(S["mae"].median()),
                   "t_mfe_median_min": float(S["t_mfe"].median()),
                   "t_mae_median_min": float(S["t_mae"].median()),
                   "favourable_first": float((S["favourable_first"] > 0).mean()),
                   "adverse_first": float((S["favourable_first"] < 0).mean()),
                   "rv_path_mean": float(S["rv_path"].mean())}  # fmt: skip
    # concentration
    by_asset = S.groupby("coin")["net"].agg(["size", "mean", "sum"])
    out["by_asset"] = {c: {"n": int(r["size"]), "net_mean": float(r["mean"])}
                       for c, r in by_asset.iterrows()}  # fmt: skip
    out["assets"] = len(by_asset)
    out["assets_positive_share"] = float((by_asset["mean"] > 0).mean())
    best = by_asset["sum"].idxmax()
    loo = S[S["coin"] != best]["net"]
    out["leave_best_asset_out"] = {
        "asset": best,
        "net_mean": float(loo.mean()) if len(loo) else None,
    }
    tot = S["net"].sum()
    top5 = np.sort(S["net"].to_numpy())[-5:].sum()
    out["top5_share"] = float(top5 / tot) if tot > 0 else None
    share = by_asset["sum"].clip(lower=0)
    out["top_asset_share_of_positive"] = (
        float(share.max() / share.sum()) if share.sum() > 0 else None
    )
    for col in (
        "session",
        "vol_state",
        "btc_alignment",
        "context",
        "flow_bucket",
        "response",
        "book_state",
        "oi_state",
        "persistence",
        "lp_state",
    ):
        out[f"by_{col}"] = _by(S, col)
    # recent window (the temporary-edge route)
    R = S[S["entry_time"] >= end - pd.Timedelta(days=sp.RECENT_DAYS)]
    out["recent"] = cm(R["net"], R["entry_time"])
    out["recent"]["assets"] = int(R["coin"].nunique())
    if len(R):
        rb = R.groupby("coin")["net"].agg(["mean", "sum"])
        rbest = rb["sum"].idxmax()
        rl = R[R["coin"] != rbest]["net"]
        out["recent"].update({
            "leave_best_asset_out": float(rl.mean()) if len(rl) else None,
            "top5_share": float(np.sort(R["net"].to_numpy())[-5:].sum() / R["net"].sum())
            if R["net"].sum() > 0 else None,
            "assets_positive_share": float((rb["mean"] > 0).mean()),
            "gross_mean": float(R["gross"].mean()),
            "excess": cm(R["net"].to_numpy(float) - exp[S.index.get_indexer(R.index)],
                         R["entry_time"]),
            "candle_residual": cm(resid[S.index.get_indexer(R.index)], R["entry_time"]),
            "independent_per_day": len(R) / sp.RECENT_DAYS,
        })  # fmt: skip
    return out


# --------------------------------------------------------------------------- maturity


def coverage(P: pd.DataFrame) -> dict:
    if P.empty:
        return {"coverage_days": 0, "days": 0}
    day = P["window_open"].dt.floor("D")
    share = P.groupby(day)["complete"].mean()
    elig = P.groupby(day)["eligible"].sum()
    return {"coverage_days": int((share >= sp.COVERAGE_DAY_MIN).sum()), "days": len(share),
            "eligible_days": int((elig > 0).sum()),
            "first_window": P["window_open"].min(), "last_window": P["window_open"].max()}  # fmt: skip


def maturity(cov_days: int, st: dict) -> str:
    n = st.get("evaluable", 0)
    per_asset = [v["n"] for v in (st.get("by_asset") or {}).values()]
    level = "WARMUP"
    for lv in ("EARLY", "DEVELOPING", "ADEQUATE"):
        dmin, nmin, amin, emin = sp.MATURITY[lv]
        if cov_days >= dmin and n >= nmin and sum(x >= emin for x in per_asset) >= amin:
            level = lv
    return level


# --------------------------------------------------------------------------- verdicts


def _t(x: dict | None) -> float:
    v = (x or {}).get("t")
    return -math.inf if v is None else v


def _m(x: dict | None) -> float:
    v = (x or {}).get("mean")
    return -math.inf if v is None else v


def guards(st: dict, recent: bool = False) -> dict[str, bool]:
    if recent:
        r = st.get("recent") or {}
        return {
            "assets>=3": r.get("assets", 0) >= 3,
            "leave_best_asset_out>0": (r.get("leave_best_asset_out") or -1) > 0,
            "top5<50%": r.get("top5_share") is not None and r["top5_share"] < 0.5,
            "half_assets_positive": (r.get("assets_positive_share") or 0) >= 0.5,
            "gross>0": (r.get("gross_mean") or -1) > 0,
            "excess_t>=1": _t(r.get("excess")) >= 1,
            "candle_residual_t>=1": _t(r.get("candle_residual")) >= 1,
            "frequency>=0.25/day": (r.get("independent_per_day") or 0) >= 0.25,
        }
    return {
        "assets>=3": st.get("assets", 0) >= 3,
        "leave_best_asset_out>0": ((st.get("leave_best_asset_out") or {}).get("net_mean") or -1) > 0,
        "top5<50%": st.get("top5_share") is not None and st["top5_share"] < 0.5,
        "half_assets_positive": (st.get("assets_positive_share") or 0) >= 0.5,
        "gross>0": _m(st.get("gross")) > 0,
        "excess_t>=1": _t(st.get("excess_vs_random")) >= 1,
        "candle_residual_t>=1": _t(st.get("candle_residual")) >= 1,
        "frequency>=0.25/day": st["frequency"]["independent_per_day"] >= 0.25,
    }  # fmt: skip


def verdict(st: dict, level: str, q: float | None) -> dict:
    """First match: IMMATURE (WARMUP) -> a candidate route (full sample, or the recent window,
    which an earlier sample never vetoes) with all guards -> a route that failed a guard
    (INTERESTING) -> REJECTED (net t <= -1, or an edge only before costs) -> INTERESTING
    (net > 0, t >= 1; EARLY caps here) -> NO_EVIDENCE."""
    floor = sp.FLOOR_BPS / 1e4
    if level == "WARMUP" or "net" not in st:
        return {"verdict": "IMMATURE", "reasons": ["maturity WARMUP: no verdict"], "route": None}
    net, gross = st["net"], st["gross"]
    routes: list[tuple[str, dict]] = []
    if level in ("DEVELOPING", "ADEQUATE"):
        if _m(net) >= floor and _t(net) >= 2.0:
            routes.append(("full", guards(st)))
        r = st.get("recent") or {}
        if r.get("n", 0) >= 30 and _m(r) >= floor and _t(r) >= 2.5:
            routes.append(("recent", guards(st, recent=True)))
    for route, g in routes:
        if all(g.values()):
            strong = (route == "full" and level == "ADEQUATE" and q is not None and q <= sp.BH_Q
                      and _t(net) >= 2.5 and _t(st.get("candle_residual")) >= 2
                      and _m(st.get("recent")) > 0)  # fmt: skip
            v = "STRONG_INCUBATION_CANDIDATE" if strong else "INCUBATION_CANDIDATE"
            return {"verdict": v, "reasons": [f"route {route}"], "route": route, "guards": g}
    if routes:
        route, g = routes[0]
        failed = [k for k, ok in g.items() if not ok]
        return {"verdict": "INTERESTING", "route": route, "guards": g,
                "reasons": [f"route {route} failed guards: {', '.join(failed)}"]}  # fmt: skip
    if _t(net) <= -1:
        return {"verdict": "REJECTED", "reasons": ["net t <= -1"], "route": None}
    if _t(gross) >= 2 and _m(net) <= 0:
        return {"verdict": "REJECTED", "reasons": ["edge only before costs"], "route": None}
    if _m(net) > 0 and _t(net) >= 1:
        reasons = ["net > 0, t >= 1"]
        if level == "EARLY":
            reasons.append("maturity EARLY caps the verdict at INTERESTING")
        return {"verdict": "INTERESTING", "reasons": reasons, "route": None}
    return {"verdict": "NO_EVIDENCE", "reasons": ["no route"], "route": None}


# --------------------------------------------------------------------------- examples


def explain_row(r: pd.Series, member: str | None = None) -> dict:
    """Machine-readable explanation of one window/event."""
    side = None
    if member:
        side = member.split(":")[1]
    return clean({
        "event_id": event_id(r["coin"], r["window_open"], member) if member else None,
        "member": member, "coin": r["coin"], "window_open": r["window_open"],
        "signal_available_at": r["available_at"], "entry_time": r.get("entry_time"),
        "entry_delay_s": r.get("entry_delay_s"), "expected_direction": side,
        "why": {"flow_bucket": r["flow_bucket"], "flow_pct": r["flow_pct"],
                "flow_z": r["flow_z"], "signed_notional_delta": r["delta_ntl"],
                "buy_frac": r["buy_frac"], "response": r["response"], "resp_z": r["resp_z"],
                "response_efficiency": r["response_efficiency"],
                "book_state": r["book_state"], "opp_replenish_pct": r["opp_rep_pct"],
                "opp_depth_ratio": r["opp_depth_ratio"], "imb5_end": r["imb5_end"],
                "imb20_end": r["imb20_end"], "spread_bps": r["spread_bps"],
                "oi_state": r["oi_state"], "oi_z": r["oi_z"], "funding": r["funding"],
                "funding_pct": r["funding_pct"], "crowd_skew": r.get("crowd_skew"),
                "crowd_aligned": r.get("crowd_aligned"), "persistence": r["persistence"],
                "lp_state": r["lp_state"], "vol_state": r["vol_state"],
                "session": r["session"], "btc_alignment": r.get("btc_alignment"),
                "context": r.get("context"),
                "candle": {"ret_z": r["c_ret_z"], "clv": r["clv"], "volume_pct": r["vol_pct"]}},
        "outcome": {k: r.get(k) for k in ("gross", "fees", "slippage", "funding", "net",
                                           "net_passive", "mfe", "mae", "t_mfe", "t_mae",
                                           "favourable_first")},
        "versions": {"study": sp.STUDY_NAME, "features": sp.FEATURE_VERSION,
                     "source": sp.SOURCE_FEATURE_VERSION},
    })  # fmt: skip


def examples(E: pd.DataFrame) -> dict:
    """Favourable / unfavourable / ambiguous: the MOST RECENT matured episode of each kind
    (a neutral rule: never the best or worst)."""
    floor = sp.FLOOR_BPS / 1e4
    S = E[E["independent"] & np.isfinite(E["net"].to_numpy(float))].sort_values("window_open")
    out = {}
    for name, m in (("favourable", S["net"] > floor), ("unfavourable", S["net"] < -floor),
                    ("ambiguous", S["net"].abs() <= floor)):  # fmt: skip
        if m.any():
            out[name] = explain_row(S[m].iloc[-1], S[m].iloc[-1]["member"])
    return out


# --------------------------------------------------------------------------- failure analysis


def failure_analysis(res: dict) -> dict:
    """Which layer failed, from frozen rules (only meaningful once maturity >= EARLY)."""
    mem = res["members"]

    def st(k):
        return mem.get(k, {}).get("stats", {})

    def any_(keys, f):
        return any(f(st(k)) for k in keys)

    ff = ("flow_follow:long", "flow_follow:short")
    cont = ("continuation_core:long", "continuation_core:short")
    absr = ("absorption_core:long", "absorption_core:short")
    lad = {x["layer"]: x for x in res["ladder"].get("layers", [])}

    def lp(name):
        t = (lad.get(name) or {}).get("added_test") or {}
        return t.get("p_value")

    immature = res["maturity"]["study_level"] in ("WARMUP",)
    flags = {
        "sample_immature": immature,
        "flow_has_no_edge": not any_(ff + cont + absr, lambda s: _t(s.get("gross")) >= 2),
        "flow_works_but_costs_kill_it": any_(ff + cont + absr, lambda s: _t(s.get("gross")) >= 2
                                             and _m(s.get("net")) <= 0),
        "absorption_too_rare": all(st(k).get("frequency", {}).get("independent_per_day", 0)
                                   < 0.25 for k in absr),
        "oi_adds_nothing": (lp("oi") is None or lp("oi") > 0.10),
        "book_adds_nothing": (lp("book") is None or lp("book") > 0.10),
        "candles_already_explain": (lp("flow") is None or lp("flow") > 0.10)
        and not any_(ff + cont + absr, lambda s: _t(s.get("candle_residual")) >= 2),
    }  # fmt: skip
    return {"flags": flags, "note": "frozen diagnostic rules; WARMUP makes every other flag "
                                    "uninformative"}  # fmt: skip


# --------------------------------------------------------------------------- evaluate


def evaluate(frames: dict[str, pd.DataFrame], defn, *, as_of: pd.Timestamp | None = None,
             crowding: CrowdingFn | None = None, context: ContextFn | None = None,
             data_meta: dict | None = None) -> dict:  # fmt: skip
    """The full Phase 24B result (JSON-safe). NaN arithmetic on gaps is expected and silent."""
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return _evaluate(frames, defn, as_of=as_of, crowding=crowding, context=context,
                         data_meta=data_meta)  # fmt: skip


def _evaluate(frames, defn, *, as_of, crowding, context, data_meta) -> dict:
    P = panel(frames, crowding=crowding, context=context)
    cov = coverage(P)
    end = pd.Timestamp(as_of) if as_of is not None else (
        cov.get("last_window") or pd.Timestamp.now(tz="UTC"))  # fmt: skip
    res: dict = {"study": sp.STUDY_NAME, "study_version": sp.STUDY_VERSION,
                 "definition_digest": sp.definition_digest(), "as_of": end,
                 "data": {**(data_meta or {}), "coverage": cov}}  # fmt: skip
    if P.empty:
        res.update(members={}, contrasts={}, twins={}, ladder={"layers": []},
                   maturity={"study_level": "WARMUP", "coverage_days": 0},
                   bh={}, examples={})  # fmt: skip
        res["failure_analysis"] = failure_analysis(res)
        return clean(res)
    res["data"]["windows"] = {
        "total": len(P), "complete": int(P["complete"].sum()), "eligible": int(P["eligible"].sum()),
        "one_sided": int(((P["flow_side"] != 0) & P["eligible"]).sum()),
        "lp_available_share": float(P["lp_available"].mean()),
        "by_coin": {c: {"windows": len(g), "complete": int(g["complete"].sum()),
                        "eligible": int(g["eligible"].sum())} for c, g in P.groupby("coin")},
    }  # fmt: skip
    span = max(cov.get("eligible_days", 0), 1)
    cand_pred, cand_fit = candle_model(P)
    rand = {s: random_expectation(P, defn, s) for s in sp.SIDES}
    members: dict = {}
    allE = []
    for h in sp.HYPOTHESES:
        for side in sp.SIDES:
            key = sp.member_key(h.key, side)
            E = events(P, defn, h, side)
            stt = member_stats(E, P, defn, side, cand_pred, rand[side], span, end)
            members[key] = {"hypothesis": h.key, "family": h.family, "side": side,
                            "action": h.action, "requires": list(h.requires),
                            "candle_twin": h.candle_twin, "statement": h.statement,
                            "thesis": h.thesis(side), "stats": stt}  # fmt: skip
            if h.key in ("continuation_core", "absorption_core") and not E.empty:
                allE.append(E)
    # BH within each verdict family (one-sided p of net; untestable -> 1, m never shrinks)
    bh: dict = {}
    for fam in sp.FAMILIES:
        keys = [k for k, v in members.items() if v["family"] == fam]
        p = {k: (members[k]["stats"].get("net") or {}).get("p_one_sided", 1.0) or 1.0 for k in keys}
        q = benjamini_hochberg({k: min(max(v, 0.0), 1.0) for k, v in p.items()}) if p else {}
        bh[fam] = {"p": p, "q": q, "verdict_family": fam in sp.VERDICT_FAMILIES}
        for k in keys:
            members[k]["bh_q"] = q.get(k)
    cov_days = cov["coverage_days"]
    for v in members.values():
        lvl = maturity(cov_days, v["stats"])
        v["maturity"] = lvl
        if v["family"] in sp.VERDICT_FAMILIES:
            v["verdict"] = verdict(v["stats"], lvl, v.get("bh_q"))
        else:
            v["verdict"] = {"verdict": "DESCRIPTIVE", "reasons": ["descriptive family"],
                            "route": None}  # fmt: skip
    # contrasts
    contrasts: dict = {}
    for name, fam, base, flag, other in sp.CONTRASTS:
        h = sp.hypothesis(base)
        for side in sp.SIDES:
            E = events(P, defn, h, side)
            if E.empty:
                contrasts[f"{name}:{side}"] = {"family": fam, "n1": 0, "n2": 0}
                continue
            fl = ft.flags(E)
            S = E[E["independent"] & np.isfinite(E["net"].to_numpy(float))]
            a, b = (
                S[fl[flag][E.index.get_indexer(S.index)]],
                S[fl[other][E.index.get_indexer(S.index)]],
            )
            dres = clustered_difference(a["net"], _blocks(a["entry_time"]), b["net"],
                                        _blocks(b["entry_time"]))  # fmt: skip
            dres["p_one_sided"] = one_sided_p(dres.get("t"), dres.get("p_value"))
            contrasts[f"{name}:{side}"] = {"family": fam, "base": base, "flag": flag,
                                           "other": other, "n1": len(a), "n2": len(b),
                                           "mean1": float(a["net"].mean()) if len(a) else None,
                                           "mean2": float(b["net"].mean()) if len(b) else None,
                                           "difference": dres}  # fmt: skip
    cq = benjamini_hochberg({k: min(max(v["difference"]["p_one_sided"], 0.0), 1.0)
                             for k, v in contrasts.items() if "difference" in v})  # fmt: skip
    for k, v in contrasts.items():
        v["bh_q_all_contrasts"] = cq.get(k)
    # candle twins (OHLCV-only rules, same costs, same stats)
    twins = {}
    for tw in sp.CANDLE_TWINS:
        for side in sp.SIDES:
            E = twin_events(P, defn, tw, side)
            st = member_stats(E, P, defn, side, cand_pred, rand[side], span, end) if not E.empty \
                else {"raw_events": 0, "episodes": 0, "evaluable": 0}  # fmt: skip
            twins[f"{tw}:{side}"] = {k: st.get(k) for k in ("raw_events", "episodes", "evaluable",
                                                             "net", "gross", "frequency",
                                                             "sign_accuracy")}  # fmt: skip
    for v in members.values():
        tw = twins.get(f"{v['candle_twin']}:{v['side']}") or {}
        v["vs_candle_twin"] = {"twin": v["candle_twin"],
                               "twin_net_mean": (tw.get("net") or {}).get("mean"),
                               "member_net_mean": (v["stats"].get("net") or {}).get("mean")}  # fmt: skip
    res["members"] = members
    res["bh"] = bh
    res["contrasts"] = contrasts
    res["twins"] = twins
    res["candle_model"] = cand_fit
    res["ladder"] = ladder(P)
    levels = [v["maturity"] for k, v in members.items() if k.split(":")[0] in
              ("absorption_core", "continuation_core")]  # fmt: skip
    rank = {lv: i for i, lv in enumerate(sp.MATURITY_LEVELS)}
    study_level = "WARMUP"
    for lv in ("EARLY", "DEVELOPING", "ADEQUATE"):
        if cov_days >= sp.MATURITY[lv][0]:
            study_level = lv
    primary = min(levels, key=lambda x: rank[x]) if levels else "WARMUP"
    res["maturity"] = {"study_level": study_level, "coverage_days": cov_days,
                       "primary_members_min": primary,
                       "thresholds": sp.MATURITY}  # fmt: skip
    res["examples"] = examples(pd.concat(allE)) if allE else {}
    res["failure_analysis"] = failure_analysis(res)
    res["summary"] = summary(res)
    return clean(res)


def summary(res: dict) -> dict:
    out = {"verdicts": {}, "candidates": []}
    for k, v in res["members"].items():
        vd = v["verdict"]["verdict"]
        out["verdicts"][vd] = out["verdicts"].get(vd, 0) + 1
        if vd in sp.CANDIDATE_VERDICTS:
            out["candidates"].append({"member": k, "verdict": vd, "thesis": v["thesis"]})
    return out
