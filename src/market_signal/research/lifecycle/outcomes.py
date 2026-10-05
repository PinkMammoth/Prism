"""Causal per-event outcomes of one Lab strategy: the input to every lifecycle estimate.

Same event semantics as the Phase 4 fast screen, from the same code: the Phase 3 compiler
(``compile_strategy``: eligibility, rising edge, cooldown), Prism's perp returns
(``perps.backtest.side_forward_returns``: entry at bar T+1's open, exit at bar T+h's close,
minus 2 x (fee + slippage), minus funding paid over T+1..T+h, missing funding => missing
outcome) and Prism's greedy declustering (``backtest.events.decluster``, which is causal:
it keeps the first event and then only events at least h bars after the last kept one).
A parity test checks net returns and independence flags against ``screen``.

One deliberate difference: **excess** uses a causal trailing baseline
(``causal_trailing_baseline_v1``) — the mean net of every eligible bar of the same asset and
side whose outcome had resolved by the signal close, over the trailing 365 days (at least 60
such bars, else excess is missing). Phase 4's baseline averages the whole period, which is
fine for one retrospective verdict but would leak later bars into an earlier lifecycle
decision.

Each row also carries ``resolved_at`` — the exit bar's close, the moment the outcome became
known — which is what every lifecycle estimate keys on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.backtest.events import decluster
from market_signal.perps.backtest import PerpCosts, side_forward_returns
from market_signal.research.lab.compiler import CompileCache, Snapshot, compile_strategy
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lifecycle.policy import Baseline


def _baseline(close_ns: np.ndarray, ret: np.ndarray, eligible: np.ndarray, sig_idx: np.ndarray,
              h: int, cfg: Baseline) -> np.ndarray:  # fmt: skip
    """Causal trailing baseline at each signal bar (see module docstring)."""
    b = np.flatnonzero(eligible & np.isfinite(ret))
    out = np.full(len(sig_idx), np.nan)
    if not len(b) or not len(sig_idx):
        return out
    bt = close_ns[b]
    cs = np.concatenate([[0.0], np.cumsum(ret[b])])
    look = np.int64(cfg.lookback_days) * 86_400 * 10**9
    for j, i in enumerate(sig_idx):
        hi = int(np.searchsorted(b, i - h, side="right"))  # outcome resolved by bar i's close
        lo = int(np.searchsorted(bt, close_ns[i] - look, side="right"))
        if hi - lo >= cfg.min_bars:
            out[j] = (cs[hi] - cs[lo]) / (hi - lo)
    return out


def coin_outcomes(definition: StrategyDefinition, snapshot: Snapshot, coin: str, *,
                  fee_bps: float, slippage_bps: float, horizon_bars: int, baseline: Baseline,
                  cache: CompileCache | None = None,
                  start: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.Timestamp | None]:  # fmt: skip
    """One row per eligible signal of ``definition`` on ``coin`` (perp, daily), plus the
    earliest time an outcome of this coin could have resolved (None if never eligible)."""
    if definition.market != "perp":
        raise ValueError("lifecycle outcomes are defined for perp strategies only")
    compiled = compile_strategy(definition, snapshot, coin, cache=cache)
    frame = compiled.inputs.reset_index(drop=True)
    if "funding_day" not in frame:
        raise ValueError(f"perp outcomes need the retained funding series for {coin}")
    close = pd.to_datetime(frame["close_time"], utc=True)
    window = np.ones(len(frame), dtype=bool) if start is None else (close >= start).to_numpy()
    eligible = compiled.eligible.to_numpy() & window & frame["funding_day"].notna().to_numpy()
    signal = compiled.signal.to_numpy() & eligible
    h = horizon_bars
    sign = 1 if definition.side == "long" else -1
    fwd = side_forward_returns(frame, {"h": h}, sign, PerpCosts(fee_bps, slippage_bps))
    ret = fwd["ret_h"].to_numpy(float)
    entry = frame["open"].shift(-1).to_numpy(float)
    gross = sign * (frame["close"].shift(-h).to_numpy(float) / entry - 1)
    sig_idx = np.flatnonzero(signal)
    valid = sig_idx[np.isfinite(ret[sig_idx])]
    indep = set(decluster(valid, h).tolist())
    close_ns = close.dt.tz_convert(None).to_numpy().astype("datetime64[ns]").astype(np.int64)
    base = _baseline(close_ns, ret, eligible, sig_idx, h, baseline)
    n = len(frame)
    nominal = close + pd.Timedelta(days=h)
    rows = []
    for j, i in enumerate(sig_idx):
        resolved = close.iloc[i + h] if i + h < n else nominal.iloc[i]
        ok = bool(np.isfinite(ret[i]))
        rows.append({
            "asset": coin, "signal_time": close.iloc[i], "resolved_at": resolved,
            "net": float(ret[i]) if ok else np.nan,
            "gross": float(gross[i]) if ok else np.nan,
            "excess": float(ret[i] - base[j]) if ok and np.isfinite(base[j]) else np.nan,
            "mae": float(fwd["mae_h"].iloc[i]), "mfe": float(fwd["mfe_h"].iloc[i]),
            "cost": 2 * (fee_bps + slippage_bps) / 1e4,
            "funding": float(fwd["funding_h"].iloc[i]) if ok else np.nan,
            "independent": int(i) in indep, "evaluable": ok, "bar": int(i),
        })  # fmt: skip
    first = np.flatnonzero(eligible)
    history_start = close.iloc[first[0]] + pd.Timedelta(days=h) if len(first) else None
    cols = ["asset", "signal_time", "resolved_at", "net", "gross", "excess", "mae", "mfe",
            "cost", "funding", "independent", "evaluable", "bar"]  # fmt: skip
    return pd.DataFrame(rows, columns=cols), history_start


def strategy_outcomes(definition: StrategyDefinition, snapshots: dict[str, Snapshot],
                      costs: dict[str, tuple[float, float]], *, horizon_bars: int,
                      baseline: Baseline, caches: dict[str, CompileCache] | None = None,
                      ) -> tuple[pd.DataFrame, pd.Timestamp | None]:  # fmt: skip
    """All coins of one venue, concatenated in (signal time, asset) order."""
    frames, starts = [], []
    for coin in sorted(snapshots):
        fee, slip = costs[coin]
        df, hs = coin_outcomes(definition, snapshots[coin], coin, fee_bps=fee, slippage_bps=slip,
                               horizon_bars=horizon_bars, baseline=baseline,
                               cache=(caches or {}).get(coin))  # fmt: skip
        frames.append(df)
        if hs is not None:
            starts.append(hs)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(out):
        out = out.sort_values(["signal_time", "asset"], kind="mergesort").reset_index(drop=True)
    return out, (min(starts) if starts else None)
