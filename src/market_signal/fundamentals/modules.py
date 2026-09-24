"""Asset-class fundamental modules for scoring. Different assets get different evidence:

  equity     — PIT EDGAR: quality (growth, margins, FCF, ROE) and own-history valuation.
  crypto     — protocol/chain revenue trend (DefiLlama, current-only); HYPE uses the
               dedicated valuation model; BTC has no cash-flow fundamentals (N/A).
  commodity  — macro/supply-demand factors (FRED, EIA). No corporate fundamentals.
  etf        — N/A (no free point-in-time constituent fundamentals).

Each module returns ``ModuleResult`` with a 0–1 ``quality`` and ``valuation`` (None = N/A)
and human-readable, provenance-tagged factor lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_signal.data.pit import asof_values, load_macro
from market_signal.regimes.engine import derived_macro_rows


@dataclass
class Factor:
    name: str
    raw: float | None
    score: float | None  # 0..1 (None = unavailable)
    kind: str  # observed | derived | assumed
    source: str
    explanation: str


@dataclass
class ModuleResult:
    module: str
    quality: float | None
    valuation: float | None
    factors: list[Factor] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def ramp(x: float | None, lo: float, hi: float) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def _mean(xs: list[float | None], min_n: int = 1) -> float | None:
    v = [x for x in xs if x is not None]
    return float(np.mean(v)) if len(v) >= min_n else None


# --------------------------------------------------------------------------- equity


def equity_module(fund_row: pd.Series | None, is_bank: bool) -> ModuleResult:
    if fund_row is None or fund_row.isna().all():
        return ModuleResult(
            "equity",
            None,
            None,
            notes=["no EDGAR fundamentals stored (run `market update --only fundamentals`)"],
        )
    f = []
    from market_signal.fundamentals.equity import BANK_METRICS, QUALITY_RAMPS

    metrics = BANK_METRICS if is_bank else tuple(QUALITY_RAMPS)
    for m in metrics:
        lo, hi = QUALITY_RAMPS[m]
        v = fund_row.get(m)
        v = None if v is None or pd.isna(v) else float(v)
        f.append(
            Factor(
                m,
                v,
                ramp(v, lo, hi),
                "derived",
                "SEC EDGAR (filing-date PIT)",
                f"{m} {'n/a' if v is None else f'{v:+.1%}'} → scored on {lo:+.0%}…{hi:+.0%}",
            )
        )
    q = _mean([x.score for x in f], min_n=2)
    pe_pct = fund_row.get("pe_pct_5y")
    fcf_pct = fund_row.get("fcf_yield_pct_5y")
    pe_pct = None if pe_pct is None or pd.isna(pe_pct) else float(pe_pct)
    fcf_pct = None if fcf_pct is None or pd.isna(fcf_pct) else float(fcf_pct)
    vparts = [
        None if pe_pct is None else 1 - pe_pct,
        None if (fcf_pct is None or is_bank) else fcf_pct,
    ]
    val = _mean(vparts)
    pe = fund_row.get("pe")
    f.append(Factor("pe_vs_own_5y", pe_pct, None if pe_pct is None else 1 - pe_pct, "derived", "price × diluted shares / TTM net income",
                    f"P/E {'n/a' if pe is None or pd.isna(pe) else f'{pe:.1f}'} at the {('n/a' if pe_pct is None else f'{pe_pct:.0%}')} percentile of its 5y range (lower = cheaper)"))  # fmt: skip
    if not is_bank:
        f.append(Factor("fcf_yield_vs_own_5y", fcf_pct, fcf_pct, "derived", "TTM FCF / market cap",
                        f"FCF yield at the {('n/a' if fcf_pct is None else f'{fcf_pct:.0%}')} percentile of its 5y range (higher = cheaper)"))  # fmt: skip
    return ModuleResult("equity", q, val, f, extra={"pe": pe, "pe_pct_5y": pe_pct})


# --------------------------------------------------------------------------- crypto (generic)


def crypto_revenue_module(store, symbol: str) -> ModuleResult:
    """Revenue trend from DefiLlama history (reconstructed → current use only)."""
    rev = store.query(
        """SELECT obs_date, value FROM crypto_metrics WHERE symbol=? AND metric='daily_revenue'
           AND pit_method='reconstructed' ORDER BY obs_date""",
        [symbol],
    )
    if rev.empty:
        return ModuleResult(
            "crypto_revenue",
            None,
            None,
            notes=["no protocol revenue data (BTC has none by design; others: run update)"],
        )
    s = pd.Series(rev["value"].to_numpy(), index=pd.to_datetime(rev["obs_date"]))
    end = s.index.max()

    def window_sum(days: int, offset: int = 0) -> float | None:
        w = s[
            (s.index > end - pd.Timedelta(days=days + offset))
            & (s.index <= end - pd.Timedelta(days=offset))
        ]
        return float(w.sum()) if len(w) >= 0.95 * days else None

    r30, r90 = window_sum(30), window_sum(90)
    r90_prev_year = window_sum(90, 365)
    mom = (r30 * 3 / r90 - 1) if r30 is not None and r90 else None
    yoy = (r90 / r90_prev_year - 1) if r90 is not None and r90_prev_year else None
    f = [
        Factor("revenue_momentum_30v90", mom, ramp(mom, -0.3, 0.3), "derived", "DefiLlama dailyRevenue (reconstructed)",
               f"30d run-rate vs 90d: {'n/a' if mom is None else f'{mom:+.0%}'}"),
        Factor("revenue_growth_yoy_90d", yoy, ramp(yoy, -0.5, 1.0), "derived", "DefiLlama dailyRevenue (reconstructed)",
               f"90d revenue vs same 90d last year: {'n/a' if yoy is None else f'{yoy:+.0%}'}"),
    ]  # fmt: skip
    return ModuleResult("crypto_revenue", _mean([x.score for x in f]), None, f,
                        notes=["current/prospective only: history is a third-party reconstruction",
                               "valuation N/A: no free market-cap/supply series wired for this asset in V1"])  # fmt: skip


def hype_module(store, settings) -> ModuleResult:
    from market_signal.fundamentals.hype import hype_valuation_from_store

    val = hype_valuation_from_store(store, settings)
    y = val.get("buyback_yield")
    band_score = {"STRONGLY_UNDERVALUED": 1.0, "UNDERVALUED": 0.8, "FAIR": 0.55, "OVERVALUED": 0.3,
                  "STRONGLY_OVERVALUED": 0.1}.get(val.signal)  # fmt: skip
    r30, r90 = val.get("revenue_30d_annualised"), val.get("revenue_90d_annualised")
    mom = (r30 / r90 - 1) if r30 and r90 else None
    net = val.get("net_structural_yield")
    f = [
        Factor("buyback_yield", y, band_score, "derived", "structural bid / market cap", f"structural buyback yield {'n/a' if y is None else f'{y:.2%}'} → {val.signal}"),
        Factor("revenue_momentum_30v90", mom, ramp(mom, -0.3, 0.3), "derived", "DefiLlama dailyRevenue", f"30d vs 90d revenue run-rate {'n/a' if mom is None else f'{mom:+.0%}'}"),
        Factor("net_structural_yield", net, ramp(net, 0.0, 0.05), "derived", "bid − contributor sells", f"net of assumed contributor selling {'n/a' if net is None else f'{net:.2%}'} (partial)"),
    ]  # fmt: skip
    quality = _mean([f[1].score, f[2].score])
    return ModuleResult(
        "hype", quality, band_score, f, notes=val.warnings, extra={"valuation": val}
    )


# --------------------------------------------------------------------------- commodity


COMMODITY_FACTORS: dict[str, list[tuple[str, str, int, float, float, str]]] = {
    # (series, transform, lookback_obs, lo, hi, meaning) — score = ramp(signed value, lo, hi)
    "gold": [("DFII10", "neg_diff", 63, -0.5, 0.5, "falling real yields support gold"),
             ("DTWEXBGS", "neg_pct", 63, -0.04, 0.04, "a weakening USD supports gold"),
             ("T10YIE", "diff", 63, -0.3, 0.3, "rising inflation expectations support gold")],
    "silver": [("DFII10", "neg_diff", 63, -0.5, 0.5, "falling real yields support silver"),
               ("DTWEXBGS", "neg_pct", 63, -0.04, 0.04, "a weakening USD supports silver")],
    "copper": [("DTWEXBGS", "neg_pct", 63, -0.04, 0.04, "a weakening USD supports copper")],
    "oil": [("WCESTUS1", "neg_seasonal", 0, -0.08, 0.08, "crude stocks below their 5y same-week average are bullish"),
            ("WCRFPUS2", "neg_pct", 13, -0.05, 0.05, "falling US production is bullish"),
            ("DTWEXBGS", "neg_pct", 63, -0.04, 0.04, "a weakening USD supports oil")],
}  # fmt: skip


def _seasonal_deviation(rows: pd.DataFrame) -> pd.DataFrame:
    """Deviation of each weekly value from the mean of the same ISO week over the prior 5y."""
    r = rows.sort_values("obs_date").reset_index(drop=True).copy()
    d = pd.to_datetime(r["obs_date"])
    r["week"] = d.dt.isocalendar().week.to_numpy()
    vals = []
    for i, row in r.iterrows():
        prior = r[
            (r["week"] == row["week"]) & (d < d[i]) & (d >= d[i] - pd.Timedelta(days=5 * 365 + 7))
        ]
        vals.append(row["value"] / prior["value"].mean() - 1 if len(prior) >= 4 else np.nan)
    r["value"] = vals
    return r


def commodity_module(store, asset_tags: tuple[str, ...], t: pd.Timestamp) -> ModuleResult:
    kind = next((k for k in COMMODITY_FACTORS if k in asset_tags), None)
    if kind is None:
        return ModuleResult("commodity", None, None, notes=["no commodity factor definition"])
    fs = []
    for series, transform, lb, lo, hi, meaning in COMMODITY_FACTORS[kind]:
        rows = load_macro(store, series, research=True)
        if rows.empty or rows["obs_date"].duplicated().any():
            fs.append(
                Factor(series, None, None, "observed", series, f"{series} unavailable — {meaning}")
            )
            continue
        if transform == "neg_seasonal":
            d = _seasonal_deviation(rows)
        else:
            d = derived_macro_rows(rows, "diff" if "diff" in transform else "pct", lb)
        v = asof_values(d, pd.DatetimeIndex([t]))["value"].iloc[0]
        v = None if pd.isna(v) else float(v)
        signed = None if v is None else (-v if transform.startswith("neg") else v)
        unit = "pp" if "diff" in transform else "%"
        shown = "n/a" if v is None else (f"{v:+.2f}{unit}" if unit == "pp" else f"{v:+.1%}")
        fs.append(
            Factor(
                series,
                v,
                ramp(signed, lo, hi),
                "derived",
                f"{series} ({transform}, {lb} obs; PIT)",
                f"{series} change {shown}: {meaning}",
            )
        )
    q = _mean([x.score for x in fs])
    return ModuleResult(
        "commodity", q, None, fs, notes=["commodities have no valuation component (N/A)"]
    )
