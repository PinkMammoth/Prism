"""Robustness tooling: walk-forward, parameter sensitivity (plateaus), regime splits.

All functions consume *event tables* (from ``events.run_event_study``) plus the baseline
bar returns, so selection and evaluation use the same definitions.

Walk-forward details:
  - Folds: train [t0, t_k), test [t_k, t_k + test_years); anchored (expanding) by default.
  - Purging: training events whose forward window could reach into the test period are
    dropped (signal_time > train_end - purge).
  - Excess returns are recomputed *inside each window* from that window's baseline only,
    so no full-sample information leaks into parameter selection.
  - At most two parameters are calibrated at once (enforced), in line with the
    anti-overfitting rules.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.backtest.events import decluster
from market_signal.backtest.metrics import summarise_returns


@dataclass(frozen=True)
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_folds(
    start: pd.Timestamp,
    end: pd.Timestamp,
    train_years: float,
    test_years: float,
    anchored: bool = True,
) -> list[Fold]:
    folds = []
    train_len = pd.DateOffset(months=int(train_years * 12))
    test_len = pd.DateOffset(months=int(test_years * 12))
    t = start + train_len
    while t + pd.DateOffset(months=6) <= end:
        test_end = min(t + test_len, end)
        folds.append(Fold(start if anchored else t - train_len, t, t, test_end))
        t = t + test_len
    return folds


def window_excess(
    events: pd.DataFrame,
    baseline_bars: pd.DataFrame,
    horizon: str,
    start,
    end,
    gap: int | dict[str, int],
) -> pd.DataFrame:
    """Independent events in [start, end) with excess vs the baseline of the same window."""
    ev = events[
        (events["horizon"] == horizon)
        & (events["signal_time"] >= start)
        & (events["signal_time"] < end)
    ]
    base = baseline_bars[(baseline_bars["horizon"] == horizon) & (baseline_bars["signal_time"] >= start)
                         & (baseline_bars["signal_time"] < end)]  # fmt: skip
    if ev.empty:
        return ev.assign(w_excess=pd.Series(dtype=float))
    bmean = base.groupby("symbol")["ret"].mean()
    ev = ev[ev["symbol"].isin(bmean.index)].copy()
    ev["w_excess"] = ev["ret"] - ev["symbol"].map(bmean)
    kept = []
    for sym, g in ev.groupby("symbol"):
        g_gap = gap[sym] if isinstance(gap, dict) else gap
        idx = set(decluster(g["bar"].to_numpy(), g_gap).tolist())
        kept.append(g[g["bar"].isin(idx)])
    return pd.concat(kept) if kept else ev.iloc[0:0]


def _objective(df: pd.DataFrame, kind: str) -> float:
    if df.empty:
        return np.nan
    return float(df["w_excess"].median() if kind == "median_excess" else df["w_excess"].mean())


def walk_forward(
    events_for: Callable[[dict], pd.DataFrame],
    baseline_bars: pd.DataFrame,
    grid: list[dict],
    default: dict,
    folds: list[Fold],
    horizon: str,
    gap: int | dict[str, int],
    purge: pd.Timedelta,
    min_train_events: int = 30,
    objective: str = "median_excess",
) -> tuple[pd.DataFrame, dict]:
    varying = {k for p in grid for k, v in p.items() if v != default.get(k)}
    if len(varying) > 2:
        raise ValueError(
            f"walk-forward calibrates at most 2 parameters at once, got {sorted(varying)}"
        )
    cache = {}

    def ev(params: dict) -> pd.DataFrame:
        key = tuple(sorted(params.items()))
        if key not in cache:
            cache[key] = events_for(params)
        return cache[key]

    rows, oos_chosen, oos_default = [], [], []
    for k, f in enumerate(folds):
        best, best_score, best_n = None, -np.inf, 0
        for params in grid:
            tr = window_excess(
                ev(params), baseline_bars, horizon, f.train_start, f.train_end - purge, gap
            )
            score = _objective(tr, objective)
            if len(tr) >= min_train_events and np.isfinite(score) and score > best_score:
                best, best_score, best_n = params, score, len(tr)
        chosen = best or default
        te = window_excess(ev(chosen), baseline_bars, horizon, f.test_start, f.test_end, gap)
        te_def = window_excess(ev(default), baseline_bars, horizon, f.test_start, f.test_end, gap)
        oos_chosen.append(te)
        oos_default.append(te_def)
        s, sd = summarise_returns(te["w_excess"]), summarise_returns(te_def["w_excess"])
        rows.append({
            "fold": k, "train": f"{f.train_start:%Y-%m}→{f.train_end:%Y-%m}", "test": f"{f.test_start:%Y-%m}→{f.test_end:%Y-%m}",
            "chosen": ("default (no param met min events)" if best is None else
                       ", ".join(f"{p}={chosen[p]}" for p in sorted(varying)) or "default"),
            "train_n": best_n, "train_objective": best_score if best is not None else np.nan,
            "test_n": s["n"], "test_excess_mean": s["mean"], "test_excess_median": s["median"],
            "test_hit": summarise_returns(te["ret"])["hit_rate"] if len(te) else np.nan,
            "default_test_n": sd["n"], "default_test_excess_mean": sd["mean"],
        })  # fmt: skip
    table = pd.DataFrame(rows)
    pooled = pd.concat(oos_chosen) if oos_chosen else pd.DataFrame(columns=["w_excess"])
    pooled_def = pd.concat(oos_default) if oos_default else pd.DataFrame(columns=["w_excess"])
    ps, pd_ = summarise_returns(pooled["w_excess"]), summarise_returns(pooled_def["w_excess"])
    summary = {
        "folds": len(folds),
        "oos_n": ps["n"], "oos_excess_mean": ps["mean"], "oos_excess_median": ps["median"], "oos_excess_t": ps["t_stat"],
        "oos_default_n": pd_["n"], "oos_default_excess_mean": pd_["mean"], "oos_default_excess_t": pd_["t_stat"],
        "folds_positive": int((table["test_excess_mean"] > 0).sum()) if len(table) else 0,
        "folds_with_events": int((table["test_n"] > 0).sum()) if len(table) else 0,
        "is_mean_objective": float(table["train_objective"].mean()) if len(table) else np.nan,
    }  # fmt: skip
    return table, summary


def sensitivity_grid(
    events_for: Callable[[dict], pd.DataFrame],
    default: dict,
    axes: dict[str, list],
    horizon: str,
    baseline_bars: pd.DataFrame,
    gap: int | dict[str, int],
) -> pd.DataFrame:
    if len(axes) > 2:
        raise ValueError("sensitivity grids are limited to 2 parameters at a time")
    names = list(axes)
    start = baseline_bars["signal_time"].min()
    end = baseline_bars["signal_time"].max() + pd.Timedelta(days=1)
    rows = []
    for combo in itertools.product(*[axes[n] for n in names]):
        params = {**default, **dict(zip(names, combo, strict=True))}
        ex = window_excess(events_for(params), baseline_bars, horizon, start, end, gap)
        s = summarise_returns(ex["w_excess"])
        rows.append({**dict(zip(names, combo, strict=True)), "n_indep": s["n"], "excess_mean": s["mean"],
                     "excess_median": s["median"], "excess_t": s["t_stat"],
                     "hit_rate": summarise_returns(ex["ret"])["hit_rate"] if len(ex) else np.nan})  # fmt: skip
    return pd.DataFrame(rows)


def plateau_verdict(
    table: pd.DataFrame,
    default: dict,
    axes: dict[str, list],
    min_events: int,
    min_ratio: float = 0.5,
    fragile_share: float = 0.5,
) -> dict:
    """Classify the default parameter point:
    INSUFFICIENT  default has < min_events independent events
    NO_EDGE       default excess <= 0
    FRAGILE       >= fragile_share of neighbours have opposite-sign excess (a 'magic number')
    PLATEAU       neighbours mostly keep >= min_ratio of the default's excess
    MIXED         otherwise
    """
    names = list(axes)

    def pos(name: str, value) -> int:
        return axes[name].index(value)

    centre = table
    for n in names:
        centre = centre[centre[n] == default[n]]
    if centre.empty:
        return {"verdict": "UNDEFINED", "detail": "default not in grid"}
    c = centre.iloc[0]
    if c["n_indep"] < min_events:
        return {
            "verdict": "INSUFFICIENT",
            "centre_excess": c["excess_mean"],
            "centre_n": int(c["n_indep"]),
        }
    neigh = []
    for _, r in table.iterrows():
        d = [abs(pos(n, r[n]) - pos(n, default[n])) for n in names]
        if max(d) == 1:
            neigh.append(r)
    nb = pd.DataFrame(neigh)
    ce = c["excess_mean"]
    out = {"centre_excess": float(ce), "centre_n": int(c["n_indep"]), "neighbours": len(nb)}
    if not np.isfinite(ce) or ce <= 0:
        out["verdict"] = "NO_EDGE"
        return out
    opposite = float((nb["excess_mean"] <= 0).mean()) if len(nb) else np.nan
    keeps = float((nb["excess_mean"] >= min_ratio * ce).mean()) if len(nb) else np.nan
    out["neighbours_opposite_sign"] = opposite
    out["neighbours_keep_ratio"] = keeps
    if len(nb) and opposite >= fragile_share:
        out["verdict"] = "FRAGILE"
    elif len(nb) and keeps >= 0.6:
        out["verdict"] = "PLATEAU"
    else:
        out["verdict"] = "MIXED"
    return out


def label_asof(times: pd.Series, labels: pd.Series) -> pd.Series:
    """Backward as-of lookup of a label series (indexed by UTC time) at ``times``."""
    if labels is None or labels.empty:
        return pd.Series("UNKNOWN", index=times.index)
    left = pd.DataFrame(
        {"t": pd.to_datetime(times, utc=True).to_numpy(), "_pos": np.arange(len(times))}
    )
    left["t"] = pd.to_datetime(left["t"], utc=True)
    right = pd.DataFrame(
        {"t": pd.DatetimeIndex(labels.index).tz_convert("UTC"), "label": labels.to_numpy()}
    )
    right["t"] = right["t"].astype(left["t"].dtype)
    m = pd.merge_asof(
        left.sort_values("t"), right.sort_values("t"), on="t", direction="backward"
    ).sort_values("_pos")
    return pd.Series(m["label"].fillna("UNKNOWN").to_numpy(), index=times.index)


def split_by_label(events: pd.DataFrame, label_col: str, horizon: str) -> pd.DataFrame:
    ev = events[(events["horizon"] == horizon) & events["independent"]]
    rows = []
    for lab, g in ev.groupby(label_col):
        s, ex = summarise_returns(g["ret"]), summarise_returns(g["excess"])
        rows.append({label_col: lab, "n_indep": s["n"], "mean": s["mean"], "median": s["median"],
                     "hit_rate": s["hit_rate"], "excess_mean": ex["mean"], "excess_t": ex["t_stat"],
                     "avg_mae": float(g["mae"].mean())})  # fmt: skip
    return pd.DataFrame(rows)
