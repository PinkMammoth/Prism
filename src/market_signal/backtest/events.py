"""Event study: forward returns after signals, compared with baselines.

Timing (the core no-look-ahead contract):
  A signal is evaluated on bar t using data up to and including bar t's close.
  Entry is at bar t+1's OPEN (plus costs). A horizon of h bars exits at bar t+h's CLOSE
  (minus costs). Returns use the total-return price basis (dividends included).
  If bar t+h does not exist yet, the return is NaN (never truncated or extrapolated).

Honesty devices:
  - baseline: the same forward return from *every eligible bar* of the same asset. The
    edge of a setup is its excess over that baseline, not its raw return (in a bull market
    everything is up).
  - independent events: for horizon h, events closer than h bars to the previous kept
    event are dropped (non-overlapping holding windows) before any t-stat or p-value.
  - random-entry null: p-value of the observed mean excess against random entry dates
    drawn from the same assets' eligible bars in the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.backtest.metrics import summarise_returns


@dataclass(frozen=True)
class Costs:
    fee_bps: float
    slippage_bps: float

    @property
    def per_side(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 1e4


def costs_for(cfg: dict, symbol: str, asset_class: str) -> Costs:
    c = (cfg.get("costs") or {}).get("overrides", {}).get(symbol) or (cfg.get("costs") or {}).get(
        asset_class
    )
    if not c:
        raise KeyError(f"no cost assumption for {symbol}/{asset_class}")
    return Costs(float(c["fee_bps"]), float(c["slippage_bps"]))


def forward_returns(feat: pd.DataFrame, horizons: dict[str, int], costs: Costs) -> pd.DataFrame:
    """Per-bar forward returns/MAE/MFE for each horizon, entry at next open.

    feat needs tr_open, tr_close, tr_high, tr_low (total-return basis).
    Output columns: ret_<h>, mae_<h>, mfe_<h> aligned to feat's index (signal bar t).
    """
    c = costs.per_side
    entry = feat["tr_open"].shift(-1) * (1 + c)
    out = pd.DataFrame(index=feat.index)
    for name, h in horizons.items():
        exit_ = feat["tr_close"].shift(-h) * (1 - c)
        out[f"ret_{name}"] = exit_ / entry - 1
        lo = feat["tr_low"].rolling(h, min_periods=h).min().shift(-h)
        hi = feat["tr_high"].rolling(h, min_periods=h).max().shift(-h)
        out[f"mae_{name}"] = lo / entry - 1
        out[f"mfe_{name}"] = hi / entry - 1
    return out


def decluster(idx: np.ndarray, gap: int) -> np.ndarray:
    """Keep the first event, then only events >= gap bars after the last kept one."""
    kept, last = [], -(10**12)
    for i in np.sort(idx):
        if i - last >= gap:
            kept.append(i)
            last = i
    return np.asarray(kept, dtype=int)


@dataclass
class AssetEvents:
    symbol: str
    asset_class: str
    feat: pd.DataFrame  # must contain close_time
    fwd: pd.DataFrame
    signal: pd.Series  # bool, aligned
    eligible: pd.Series  # bool, aligned: bars where the baseline is measured
    horizons: dict[str, int]


@dataclass
class EventStudyResult:
    events: pd.DataFrame  # one row per (signal, horizon) with independence flag
    baseline: pd.DataFrame  # per symbol × horizon baseline stats
    summary: pd.DataFrame  # pooled stats per horizon (all + independent)
    by_asset: pd.DataFrame
    by_class: pd.DataFrame
    notes: list[str] = field(default_factory=list)


def _events_frame(ae: AssetEvents) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, base_rows = [], []
    sig_idx = np.flatnonzero(
        ae.signal.fillna(False).to_numpy() & ae.eligible.fillna(False).to_numpy()
    )
    for hname, h in ae.horizons.items():
        ret = ae.fwd[f"ret_{hname}"]
        elig = ae.eligible.fillna(False).to_numpy() & ret.notna().to_numpy()
        base = ret[elig]
        base_mean = float(base.mean()) if len(base) else np.nan
        base_rows.append({"symbol": ae.symbol, "asset_class": ae.asset_class, "horizon": hname,
                          "base_n": len(base), "base_mean": base_mean,
                          "base_median": float(base.median()) if len(base) else np.nan,
                          "base_hit": float((base > 0).mean()) if len(base) else np.nan})  # fmt: skip
        valid = sig_idx[ret.iloc[sig_idx].notna().to_numpy()] if len(sig_idx) else sig_idx
        indep = set(decluster(valid, h).tolist())
        for i in valid:
            rows.append({
                "symbol": ae.symbol, "asset_class": ae.asset_class, "horizon": hname, "bar": int(i),
                "signal_time": ae.feat["close_time"].iloc[i],
                "ret": float(ret.iloc[i]), "excess": float(ret.iloc[i]) - base_mean,
                "mae": float(ae.fwd[f"mae_{hname}"].iloc[i]), "mfe": float(ae.fwd[f"mfe_{hname}"].iloc[i]),
                "independent": i in indep,
            })  # fmt: skip
    return pd.DataFrame(rows), pd.DataFrame(base_rows)


def random_entry_pvalue(
    indep: pd.DataFrame, assets: list[AssetEvents], horizon: str, n_samples: int, seed: int
) -> float:
    """P(mean excess of random eligible entries >= observed), matching per-asset counts.

    Random entries are also declustered-size draws (without replacement) from each asset's
    eligible bars. One-sided: tests whether the setup beats random timing.
    """
    if indep.empty:
        return np.nan
    rng = np.random.default_rng(seed)
    observed = indep["excess"].mean()
    counts = indep.groupby("symbol").size()
    pools = {}
    for ae in assets:
        if ae.symbol not in counts:
            continue
        ret = ae.fwd[f"ret_{horizon}"]
        elig = ae.eligible.fillna(False).to_numpy() & ret.notna().to_numpy()
        pool = ret[elig].to_numpy()
        pools[ae.symbol] = pool - pool.mean()
    total = int(counts.sum())
    hits = 0
    for _ in range(n_samples):
        s = 0.0
        for sym, k in counts.items():
            pool = pools[sym]
            s += rng.choice(pool, size=min(k, len(pool)), replace=False).sum()
        hits += (s / total) >= observed
    return (hits + 1) / (n_samples + 1)


def run_event_study(
    assets: list[AssetEvents],
    primary: str,
    n_boot: int = 2000,
    seed: int = 12345,
    min_events: int = 30,
) -> EventStudyResult:
    ev_frames, base_frames = (
        zip(*[_events_frame(a) for a in assets], strict=True) if assets else ([], [])
    )
    events = pd.concat(ev_frames, ignore_index=True) if ev_frames else pd.DataFrame()
    baseline = pd.concat(base_frames, ignore_index=True) if base_frames else pd.DataFrame()
    notes: list[str] = []
    horizons = list(assets[0].horizons) if assets else []

    def stats(df: pd.DataFrame) -> dict:
        s_all = (
            summarise_returns(df["ret"])
            if not df.empty
            else summarise_returns(pd.Series(dtype=float))
        )
        ind = df[df["independent"]] if not df.empty else df
        s_ind = (
            summarise_returns(ind["ret"])
            if not ind.empty
            else summarise_returns(pd.Series(dtype=float))
        )
        ex_ind = (
            summarise_returns(ind["excess"])
            if not ind.empty
            else summarise_returns(pd.Series(dtype=float))
        )
        return {
            "n_events": s_all["n"], "n_independent": s_ind["n"],
            "mean": s_all["mean"], "median": s_all["median"], "hit_rate": s_all["hit_rate"],
            "mean_indep": s_ind["mean"], "median_indep": s_ind["median"], "hit_rate_indep": s_ind["hit_rate"],
            "excess_mean_indep": ex_ind["mean"], "excess_median_indep": ex_ind["median"],
            "excess_t_indep": ex_ind["t_stat"],
            "avg_mae": float(df["mae"].mean()) if not df.empty else np.nan,
            "p10_mae": float(df["mae"].quantile(0.1)) if not df.empty else np.nan,
            "avg_mfe": float(df["mfe"].mean()) if not df.empty else np.nan,
        }  # fmt: skip

    rows = []
    for h in horizons:
        sub = events[events["horizon"] == h] if not events.empty else events
        st = stats(sub)
        b = baseline[baseline["horizon"] == h]
        w = b["base_n"].sum()
        st["baseline_mean"] = float((b["base_mean"] * b["base_n"]).sum() / w) if w else np.nan
        ind = sub[sub["independent"]] if not sub.empty else sub
        st["p_value_random_entry"] = (
            random_entry_pvalue(ind, assets, h, n_boot, seed)
            if h == primary and len(ind)
            else np.nan
        )
        st["verdict_sample"] = "OK" if st["n_independent"] >= min_events else "INSUFFICIENT"
        rows.append({"horizon": h, **st})
    summary = pd.DataFrame(rows)

    def group_table(key: str) -> pd.DataFrame:
        out = []
        if events.empty:
            return pd.DataFrame()
        for (g, h), sub in events.groupby([key, "horizon"], sort=False):
            out.append({key: g, "horizon": h, **stats(sub)})
        return pd.DataFrame(out)

    by_asset = group_table("symbol")
    by_class = group_table("asset_class")
    if not by_asset.empty:
        prim = by_asset[(by_asset["horizon"] == primary) & (by_asset["n_independent"] > 0)]
        if len(prim):
            share = float((prim["excess_mean_indep"] > 0).mean())
            notes.append(
                f"{share:.0%} of assets with events show positive independent excess at {primary}"
            )
    return EventStudyResult(events, baseline, summary, by_asset, by_class, notes)


def baseline_bars(assets: list[AssetEvents]) -> pd.DataFrame:
    """Every eligible bar's forward return per horizon (the unconditional baseline)."""
    frames = []
    for ae in assets:
        elig = ae.eligible.fillna(False).to_numpy()
        for hname in ae.horizons:
            ret = ae.fwd[f"ret_{hname}"].to_numpy()
            m = elig & np.isfinite(ret)
            idx = np.flatnonzero(m)
            frames.append(pd.DataFrame({
                "symbol": ae.symbol, "horizon": hname, "bar": idx,
                "signal_time": ae.feat["close_time"].to_numpy()[idx], "ret": ret[idx],
            }))  # fmt: skip
    if not frames:
        return pd.DataFrame(columns=["symbol", "horizon", "bar", "signal_time", "ret"])
    out = pd.concat(frames, ignore_index=True)
    out["signal_time"] = pd.to_datetime(out["signal_time"], utc=True)
    return out
