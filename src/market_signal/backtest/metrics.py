"""Performance metrics for trades and equity curves.

Conventions:
- Equity curves are sampled per *calendar day* (UTC); annualisation uses 365.
- Returns are simple returns, net of modelled costs.
- Metrics that are undefined for the sample (e.g. Sortino with no losing days, CAGR over
  < 90 days) are NaN, not 0 or inf.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ANNUAL_DAYS = 365


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    if equity.empty:
        return np.nan, None, None
    peak = equity.cummax()
    dd = equity / peak - 1
    trough = dd.idxmin()
    peak_t = equity.loc[:trough].idxmax()
    return float(dd.min()), peak_t, trough


def curve_metrics(
    equity: pd.Series, exposure: pd.Series | None = None, turnover_notional: float = 0.0
) -> dict:
    """equity: daily equity indexed by UTC date. exposure: daily gross exposure fraction."""
    out: dict = {}
    if equity.empty or len(equity) < 2:
        return {
            "cagr": np.nan,
            "sharpe": np.nan,
            "sortino": np.nan,
            "max_drawdown": np.nan,
            "days": len(equity),
        }
    rets = equity.pct_change().dropna()
    days = (equity.index[-1] - equity.index[0]).days
    total = equity.iloc[-1] / equity.iloc[0] - 1
    out["total_return"] = float(total)
    out["days"] = int(days)
    out["cagr"] = (
        float((1 + total) ** (ANNUAL_DAYS / days) - 1) if days >= 90 and total > -1 else np.nan
    )
    sd = rets.std(ddof=1)
    out["ann_vol"] = float(sd * np.sqrt(ANNUAL_DAYS))
    out["sharpe"] = float(rets.mean() / sd * np.sqrt(ANNUAL_DAYS)) if sd > 0 else np.nan
    downside = np.sqrt((np.minimum(rets, 0) ** 2).mean())
    out["sortino"] = (
        float(rets.mean() / downside * np.sqrt(ANNUAL_DAYS)) if downside > 0 else np.nan
    )
    mdd, peak_t, trough_t = max_drawdown(equity)
    out["max_drawdown"] = mdd
    out["max_drawdown_peak"] = str(peak_t.date()) if peak_t is not None else None
    out["max_drawdown_trough"] = str(trough_t.date()) if trough_t is not None else None
    if exposure is not None and not exposure.empty:
        out["time_in_market"] = float((exposure > 1e-9).mean())
        out["avg_gross_exposure"] = float(exposure.mean())
    avg_eq = float(equity.mean())
    years = max(days / ANNUAL_DAYS, 1e-9)
    out["turnover_per_year"] = float(turnover_notional / avg_eq / years) if avg_eq > 0 else np.nan
    return out


def trade_metrics(trades: pd.DataFrame) -> dict:
    """Per-trade statistics independent of sizing."""
    if trades is None or trades.empty:
        return {"trades": 0}
    r = trades["ret"].astype(float)
    wins, losses = r[r > 0], r[r <= 0]
    out = {
        "trades": len(r),
        "hit_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "median_return": float(r.median()),
        "avg_win": float(wins.mean()) if len(wins) else np.nan,
        "avg_loss": float(losses.mean()) if len(losses) else np.nan,
        "payoff_ratio": float(wins.mean() / -losses.mean())
        if len(wins) and len(losses) and losses.mean() < 0
        else np.nan,
        "expectancy": float(r.mean()),
        "avg_holding_bars": float(trades["bars_held"].mean()),
        "avg_mae": float(trades["mae"].mean()),
        "worst_mae": float(trades["mae"].min()),
        "avg_mfe": float(trades["mfe"].mean()),
        "best_trade": float(r.max()),
        "worst_trade": float(r.min()),
    }
    if "r_multiple" in trades and trades["r_multiple"].notna().any():
        out["expectancy_r"] = float(trades["r_multiple"].mean())
    out["exit_reasons"] = trades["exit_reason"].value_counts().to_dict()
    return out


def summarise_returns(x: pd.Series) -> dict:
    x = x.dropna().astype(float)
    n = len(x)
    if n == 0:
        return {
            "n": 0,
            "mean": np.nan,
            "median": np.nan,
            "hit_rate": np.nan,
            "std": np.nan,
            "t_stat": np.nan,
        }
    sd = x.std(ddof=1) if n > 1 else np.nan
    return {
        "n": n,
        "mean": float(x.mean()),
        "median": float(x.median()),
        "hit_rate": float((x > 0).mean()),
        "std": float(sd) if n > 1 else np.nan,
        "t_stat": float(x.mean() / (sd / np.sqrt(n))) if n > 1 and sd > 0 else np.nan,
        "p10": float(x.quantile(0.10)),
        "p90": float(x.quantile(0.90)),
    }
