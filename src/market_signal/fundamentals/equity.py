"""Point-in-time equity fundamentals from SEC EDGAR XBRL ``companyfacts``.

Availability: every fact carries its filing date. A fact becomes usable at
``filed + 1 day 00:00 UTC`` (acceptance can be after the US close on the filing date).
Restatements are later filings for the same period; the as-of state keeps, for each
period, the latest value filed *before* the as-of time. Nothing here uses today's
restated numbers for past dates.

TTM construction (for flows): with the latest fiscal period end E known at t,
  - if a ~12-month fact ends at E, TTM = that fact;
  - otherwise TTM = YTD(E) + FY(previous year) − YTD(E − 1y) using facts known at t;
  - failing that, the sum of four contiguous quarterly facts; else NaN.
Valuation uses split-adjusted close × diluted shares (share count converted to today's
split basis via splits after the filing date — a unit conversion, not information).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from market_signal.config import Settings
from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.data.providers.base import HttpProvider
from market_signal.data.store import Store
from market_signal.indicators.technical import rolling_percentile
from market_signal.models.domain import Asset, PitMethod

# concept groups: first match per period wins (companies switch tags over time)
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense",
    ],
    "net_income": ["NetIncomeLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "equity": ["StockholdersEquity"],
}  # fmt: skip
FLOW_METRICS = ("revenue", "net_income", "operating_income", "cfo", "capex")
FACT_COLUMNS = ["symbol", "concept", "metric", "unit", "period_start", "period_end", "value", "form", "fy", "fp",
                "filed", "accn", "available_at", "pit_method"]  # fmt: skip


class EdgarProvider(HttpProvider):
    name = "sec_edgar"

    def __init__(self, http: HttpClient):
        super().__init__(http)

    @staticmethod
    def parse(symbol: str, payload: Any) -> pd.DataFrame:
        try:
            gaap = payload["facts"]["us-gaap"]
        except (KeyError, TypeError) as exc:
            raise SchemaError(f"edgar companyfacts for {symbol}: missing facts.us-gaap") from exc
        rows = []
        for metric, concepts in CONCEPTS.items():
            for concept in concepts:
                node = gaap.get(concept)
                if not node:
                    continue
                for unit, facts in (node.get("units") or {}).items():
                    if unit not in ("USD", "shares", "USD/shares"):
                        continue
                    for f in facts:
                        if "filed" not in f or "end" not in f or "val" not in f:
                            raise SchemaError(f"edgar fact missing keys in {concept}: {sorted(f)}")
                        rows.append({
                            "symbol": symbol, "concept": concept, "metric": metric, "unit": unit,
                            "period_start": f.get("start"), "period_end": f["end"], "value": float(f["val"]),
                            "form": f.get("form"), "fy": f.get("fy"), "fp": f.get("fp"), "filed": f["filed"],
                            "accn": f.get("accn"),
                        })  # fmt: skip
        if not rows:
            return pd.DataFrame(columns=FACT_COLUMNS)
        df = pd.DataFrame(rows)
        df["period_start"] = pd.to_datetime(df["period_start"]).dt.date
        df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
        df["filed"] = pd.to_datetime(df["filed"]).dt.date
        df["available_at"] = pd.to_datetime(df["filed"]).dt.tz_localize("UTC") + pd.Timedelta(
            days=1
        )
        df["pit_method"] = PitMethod.FILING_DATE.value
        return df[FACT_COLUMNS]

    def get_fundamentals(self, entity: str, cik: str | None = None) -> pd.DataFrame:
        if not cik:
            raise ProviderError(f"{entity}: no SEC CIK configured")
        payload = self.http.get_json(f"/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json")
        return self.parse(entity, payload)


def fact_key(r: pd.Series) -> str:
    raw = f"{r.symbol}|{r.concept}|{r.unit}|{r.period_start}|{r.period_end}|{r.accn}|{r.filed}"
    return hashlib.sha1(raw.encode()).hexdigest()


def upsert_facts(store: Store, df: pd.DataFrame, run_id: str) -> int:
    if df.empty:
        return 0
    stage = df.copy()
    stage["fact_key"] = stage.apply(fact_key, axis=1)
    stage["source"] = "sec_edgar"
    stage["run_id"] = run_id
    stage["fy"] = pd.to_numeric(stage["fy"], errors="coerce").astype("Int64")
    store.con.register("_facts", stage)
    try:
        before = store.con.execute("SELECT count(*) FROM fundamental_facts").fetchone()[0]
        store.con.execute(
            """INSERT OR REPLACE INTO fundamental_facts
               SELECT fact_key, symbol, source, concept, unit, period_start, period_end, value, form, fy, fp,
                      filed, accn, available_at, pit_method, run_id FROM _facts"""
        )
        after = store.con.execute("SELECT count(*) FROM fundamental_facts").fetchone()[0]
    finally:
        store.con.unregister("_facts")
    return int(after - before)


def load_facts(store: Store, symbol: str) -> pd.DataFrame:
    df = store.query(
        """SELECT symbol, concept, unit, period_start, period_end, value, filed, available_at
           FROM fundamental_facts WHERE symbol=? AND pit_method='filing_date'""",
        [symbol],
    )
    if df.empty:
        return df
    metric_of = {c: m for m, cs in CONCEPTS.items() for c in cs}
    df["metric"] = df["concept"].map(metric_of)
    df["priority"] = df.apply(lambda r: CONCEPTS[r["metric"]].index(r["concept"]), axis=1)
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    for c in ("period_start", "period_end", "filed"):
        df[c] = pd.to_datetime(df[c]).dt.date
    return df


# --------------------------------------------------------------------------- PIT state


def _known(facts: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """Facts known at t: per (metric, start, end) the best-priority concept, latest filing."""
    k = facts[facts["available_at"] <= t]
    if k.empty:
        return k
    k = k.sort_values(["metric", "period_start", "period_end", "priority", "filed"],
                      ascending=[True, True, True, True, False], na_position="first")  # fmt: skip
    return k.drop_duplicates(["metric", "period_start", "period_end"], keep="first")


def _near(a: date, b: date, tol: int) -> bool:
    return abs((a - b).days) <= tol


def ttm_at(known: pd.DataFrame, metric: str, end: date | None = None) -> tuple[float, date | None]:
    """TTM flow value for the latest period end (or a given end) from known facts."""
    m = known[(known["metric"] == metric) & known["period_start"].notna()]
    if m.empty:
        return np.nan, None
    m = m.assign(
        days=[(e - s).days for s, e in zip(m["period_start"], m["period_end"], strict=True)]
    )
    e = end or max(m["period_end"])
    at_e = m[[_near(x, e, 3 if end is None else 8) for x in m["period_end"]]]  # 52/53-week years
    if at_e.empty:
        return np.nan, e
    fy = at_e[at_e["days"].between(350, 380)]
    if len(fy):
        return float(fy.iloc[0]["value"]), e
    ytd = at_e[at_e["days"] < 350].sort_values("days").iloc[-1]
    s = ytd["period_start"]
    ends, starts = m["period_end"].to_numpy(), m["period_start"].to_numpy()
    near_prev_end = np.array([_near(x, s - timedelta(days=1), 5) for x in ends], dtype=bool)
    prev_fy = m[m["days"].between(350, 380).to_numpy() & near_prev_end]
    prev_ytd = m[np.array([_near(x, e - timedelta(days=365), 7) for x in ends], dtype=bool)
                 & np.array([_near(x, s - timedelta(days=365), 7) for x in starts], dtype=bool)]  # fmt: skip
    if len(prev_fy) and len(prev_ytd):
        return float(ytd["value"] + prev_fy.iloc[0]["value"] - prev_ytd.iloc[0]["value"]), e
    # fallback: four contiguous quarters
    q = m[m["days"].between(80, 100)].sort_values("period_end")
    chain, cur = [], e
    for _ in range(4):
        hit = q[[_near(x, cur, 5) for x in q["period_end"]]]
        if hit.empty:
            return np.nan, e
        chain.append(float(hit.iloc[0]["value"]))
        cur = hit.iloc[0]["period_start"] - timedelta(days=1)
    return float(sum(chain)), e


def latest_instant(known: pd.DataFrame, metric: str) -> tuple[float, date | None, date | None]:
    m = known[known["metric"] == metric].sort_values("period_end")
    if m.empty:
        return np.nan, None, None
    r = m.iloc[-1]
    return float(r["value"]), r["period_end"], r["filed"]


def latest_quarter_shares(known: pd.DataFrame) -> tuple[float, date | None]:
    m = known[(known["metric"] == "diluted_shares") & known["period_start"].notna()]
    if m.empty:
        return np.nan, None
    m = m.assign(
        days=[(e - s).days for s, e in zip(m["period_start"], m["period_end"], strict=True)]
    )
    q = m[m["days"] < 120].sort_values(["period_end", "filed"])
    r = (q if len(q) else m.sort_values(["period_end", "filed"])).iloc[-1]
    return float(r["value"]), r["filed"]


def snapshot(facts: pd.DataFrame, t: pd.Timestamp) -> dict[str, Any]:
    """All derived fundamentals as known at time t."""
    k = _known(facts, t)
    out: dict[str, Any] = {"asof": t}
    if k.empty:
        return out
    for metric in FLOW_METRICS:
        v, e = ttm_at(k, metric)
        out[f"{metric}_ttm"] = v
        out[f"{metric}_end"] = e
        if e is not None:
            prev, _ = ttm_at(k, metric, e - timedelta(days=365))
            out[f"{metric}_ttm_prev"] = prev
    shares, shares_filed = latest_quarter_shares(k)
    out["diluted_shares"], out["shares_filed"] = shares, shares_filed
    eq, _, _ = latest_instant(k, "equity")
    out["equity"] = eq
    return out


def fundamentals_timeline(facts: pd.DataFrame) -> pd.DataFrame:
    """One snapshot per distinct availability time (state only changes on filings)."""
    if facts.empty:
        return pd.DataFrame()
    times = sorted(facts["available_at"].unique())
    rows = [snapshot(facts, pd.Timestamp(t)) for t in times]
    tl = pd.DataFrame(rows)
    tl["asof"] = pd.to_datetime(tl["asof"], utc=True)
    return tl


def derive_ratios(tl: pd.DataFrame) -> pd.DataFrame:
    d = tl.copy()

    def growth(cur: pd.Series, prev: pd.Series) -> pd.Series:
        return (cur / prev - 1).where((prev > 0) & cur.notna())

    d["rev_growth"] = growth(d.get("revenue_ttm"), d.get("revenue_ttm_prev"))
    d["ni_growth"] = growth(d.get("net_income_ttm"), d.get("net_income_ttm_prev"))
    d["op_margin"] = d.get("operating_income_ttm") / d.get("revenue_ttm")
    d["fcf_ttm"] = d.get("cfo_ttm") - d.get("capex_ttm")
    d["fcf_margin"] = d["fcf_ttm"] / d.get("revenue_ttm")
    d["roe"] = d.get("net_income_ttm") / d.get("equity").where(d.get("equity") > 0)
    return d


def split_factor_after(actions: pd.DataFrame, filed: date | None) -> float:
    if actions is None or actions.empty or filed is None:
        return 1.0
    a = actions[(actions["date"] > filed) & (actions["split_factor"] != 1.0)]
    return float(np.prod(a["split_factor"].to_numpy())) if len(a) else 1.0


def pit_fundamental_features(
    store: Store, settings: Settings, asset: Asset, feat: pd.DataFrame
) -> pd.DataFrame | None:
    """Fundamental features aligned to ``feat`` rows, each as known at the bar's close."""
    facts = load_facts(store, asset.symbol)
    if facts.empty:
        return None
    tl = derive_ratios(fundamentals_timeline(facts))
    from market_signal.data.registry import source_label
    from market_signal.models.domain import Timeframe

    actions = store.get_actions(
        asset.symbol, source_label(asset.series[Timeframe.D1].provider, Timeframe.D1)
    )
    tl["shares_today_basis"] = [
        s * split_factor_after(actions, f)
        for s, f in zip(tl["diluted_shares"], tl["shares_filed"], strict=True)
    ]
    left = pd.DataFrame(
        {"t": pd.to_datetime(feat["close_time"], utc=True), "_pos": np.arange(len(feat))}
    )
    tl = tl.sort_values("asof")
    tl["asof"] = tl["asof"].astype(left["t"].dtype)
    m = pd.merge_asof(
        left.sort_values("t"), tl, left_on="t", right_on="asof", direction="backward"
    ).sort_values("_pos")
    m.index = feat.index
    mcap = feat["close"] * m["shares_today_basis"]
    out = pd.DataFrame(index=feat.index)
    out["market_cap"] = mcap
    out["pe"] = (mcap / m["net_income_ttm"]).where(m["net_income_ttm"] > 0)
    out["earnings_yield"] = m["net_income_ttm"] / mcap
    out["fcf_yield"] = m["fcf_ttm"] / mcap
    for c in (
        "rev_growth",
        "ni_growth",
        "op_margin",
        "fcf_margin",
        "roe",
        "revenue_ttm",
        "net_income_ttm",
        "fcf_ttm",
    ):
        out[c] = m[c]
    out["fund_period_end"] = m.get("revenue_end")
    out["fund_known_since"] = m["asof"]
    out["fundamental_score"] = quality_score(out, is_bank="bank" in asset.tags)
    bpy = 252
    out["pe_pct_5y"] = rolling_percentile(out["pe"], window=5 * bpy, min_periods=2 * bpy)
    out["fcf_yield_pct_5y"] = rolling_percentile(
        out["fcf_yield"], window=5 * bpy, min_periods=2 * bpy
    )
    return out


def ramp(x: pd.Series, lo: float, hi: float) -> pd.Series:
    """Linear 0→1 between lo and hi, clipped; NaN stays NaN."""
    return ((x - lo) / (hi - lo)).clip(0, 1)


QUALITY_RAMPS = {  # metric: (value scoring 0, value scoring 1) — configured assumptions
    "rev_growth": (-0.05, 0.20),
    "ni_growth": (-0.10, 0.25),
    "op_margin": (0.0, 0.35),
    "fcf_margin": (0.0, 0.30),
    "roe": (0.0, 0.25),
}
BANK_METRICS = ("rev_growth", "ni_growth", "roe")  # margins/FCF are not meaningful for banks


def quality_score(f: pd.DataFrame, is_bank: bool = False, min_metrics: int = 2) -> pd.Series:
    """Point-in-time 0..1 business-quality score: mean of available metric ramps.

    NaN when fewer than ``min_metrics`` inputs exist (missing stays missing).
    """
    metrics = BANK_METRICS if is_bank else tuple(QUALITY_RAMPS)
    parts = pd.DataFrame({m: ramp(f[m], *QUALITY_RAMPS[m]) for m in metrics if m in f})
    return parts.mean(axis=1).where(parts.notna().sum(axis=1) >= min_metrics)


def update_edgar(settings: Settings, store: Store) -> pd.DataFrame:
    from market_signal.data.registry import ProviderRegistry

    ua = settings.secret("SEC_USER_AGENT")
    results = []
    equities = [a for a in settings.active_assets() if a.provider_ids.get("sec_cik")]
    if not ua:
        return pd.DataFrame([(a.symbol, "failed", 0, 0, "SEC_USER_AGENT not set") for a in equities],
                            columns=["entity", "status", "received", "new_rows", "note"])  # fmt: skip
    prov = EdgarProvider(ProviderRegistry(settings).http("sec_edgar", user_agent=ua))
    for a in equities:
        run_id = store.start_run(
            "sec_edgar", "companyfacts", a.symbol, {"cik": a.provider_ids["sec_cik"]}
        )
        try:
            df = prov.get_fundamentals(a.symbol, a.provider_ids["sec_cik"])
            archived = store.archive_raw(prov.drain_raw(), run_id)
            n = upsert_facts(store, df, run_id)
            store.finish_run(
                run_id, status="ok", rows_received=len(df), rows_written=n, archived=archived
            )
            results.append((a.symbol, "ok", len(df), n, "filing_date PIT"))
        except ProviderError as exc:
            store.finish_run(run_id, status="failed", error=str(exc)[:500])
            results.append((a.symbol, "failed", 0, 0, str(exc)[:80]))
    return pd.DataFrame(results, columns=["entity", "status", "received", "new_rows", "note"])
