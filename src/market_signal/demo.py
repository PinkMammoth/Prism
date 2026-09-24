"""SYNTHETIC demonstration dataset.

Used only to exercise the pipeline when real providers are unreachable (e.g. in a
network-restricted build environment). Every bar is stored with ``source='synthetic'``
in a *separate* database file (default ``data/demo.duckdb``). The dashboard and CLI show a
prominent SYNTHETIC banner whenever this database is in use. Nothing here is market data,
and no research conclusion may be drawn from it.

Series are geometric random walks with regime-switching drift/vol, so setups fire and
regimes change, but there is no exploitable structure by construction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.config import Settings
from market_signal.data.calendars import close_times, nyse_open_utc, nyse_sessions
from market_signal.data.providers.base import normalise_bars
from market_signal.data.store import Store
from market_signal.models.domain import Calendar, Timeframe, utcnow

SYNTHETIC_SOURCE = "synthetic"

_START_PRICE = {
    "BTC": 20000, "ETH": 1500, "SOL": 30, "HYPE": 20, "LINK": 8, "AAVE": 90,
    "SPY": 300, "QQQ": 250, "MSFT": 200, "GOOGL": 90, "AMZN": 100, "META": 150,
    "NVDA": 30, "JPM": 110, "GOLD": 160, "SILVER": 20, "COPPER": 22, "OIL": 60,
}  # fmt: skip


def _path(
    n: int, rng: np.random.Generator, ann_vol: float, bars_per_year: int, start: float
) -> np.ndarray:
    # two-state drift/vol regime switching; no autocorrelation to exploit
    state = np.zeros(n, dtype=int)
    for i in range(1, n):
        state[i] = state[i - 1] if rng.random() > 1 / 120 else 1 - state[i - 1]
    drift = np.where(state == 0, 0.25, -0.20) / bars_per_year
    vol = ann_vol * np.where(state == 0, 0.8, 1.3) / np.sqrt(bars_per_year)
    rets = rng.normal(drift - 0.5 * vol**2, vol)
    return start * np.exp(np.cumsum(rets))


def _bars_from_close(
    ts: pd.DatetimeIndex, close: np.ndarray, rng: np.random.Generator, vol: float
) -> pd.DataFrame:
    open_ = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, vol * 0.2, len(close)))
    wick = np.abs(rng.normal(0, vol * 0.6, len(close)))
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol * 0.6, len(close))))
    volume = rng.lognormal(10, 0.4, len(close))
    return normalise_bars(
        pd.DataFrame(
            {"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
        )
    )


def build_demo_db(
    settings: Settings, store: Store, years: int = 8, seed: int = 7
) -> dict[str, int]:
    rng = np.random.default_rng(seed)
    end = pd.Timestamp(utcnow()).normalize() - pd.Timedelta(days=1)
    counts: dict[str, int] = {}
    store.upsert_assets(settings.universe.values())
    for asset in settings.universe.values():
        start_price = float(_START_PRICE.get(asset.symbol, 100))
        if asset.calendar == Calendar.CRYPTO_24_7:
            n_years = 2 if asset.symbol == "HYPE" else years
            ts = pd.date_range(end - pd.Timedelta(days=365 * n_years), end, freq="1D", tz="UTC")
            ann_vol, bpy = (0.9 if asset.symbol != "BTC" else 0.6), 365
        else:
            sessions = nyse_sessions((end - pd.Timedelta(days=365 * years)).date(), end.date())
            ts = pd.DatetimeIndex([pd.Timestamp(nyse_open_utc(d)) for d in sessions])
            ann_vol, bpy = (0.18 if asset.asset_class.value == "etf" else 0.32), 252
        close = _path(len(ts), rng, ann_vol, bpy, start_price)
        bars = _bars_from_close(ts, close, rng, ann_vol / np.sqrt(bpy))
        bars["close_time"] = close_times(bars["ts"], asset.calendar, Timeframe.D1)
        bars = bars[bars["close_time"] <= pd.Timestamp(utcnow())]
        run_id = store.start_run(SYNTHETIC_SOURCE, "bars_1d", asset.symbol, {"seed": seed})
        c = store.upsert_bars(asset.symbol, Timeframe.D1, SYNTHETIC_SOURCE, bars, run_id)
        store.finish_run(run_id, status="ok", rows_received=len(bars), rows_written=c["inserted"])
        counts[asset.symbol] = c["inserted"]
    _demo_macro(store, rng, end)
    _demo_fundamentals(settings, store, rng, end)
    _demo_crypto_metrics(store, rng, end)
    return counts


def _demo_crypto_metrics(store: Store, rng: np.random.Generator, end: pd.Timestamp) -> None:
    """Synthetic protocol revenue histories and HYPE snapshots (supply, USDC, AF balance)."""
    from market_signal.fundamentals.crypto_data import _rows, _snapshot, upsert_metrics

    fetched = pd.Timestamp(utcnow()).to_pydatetime()
    frames = []
    for sym, base in {
        "HYPE": 2.0e6,
        "ETH": 3.0e6,
        "SOL": 1.5e6,
        "AAVE": 0.3e6,
        "LINK": 0.05e6,
    }.items():
        days = pd.date_range(end - pd.Timedelta(days=720), end, freq="D")
        level = base * np.exp(np.cumsum(rng.normal(0.0005, 0.04, len(days))))
        hist = pd.DataFrame(
            {"obs_date": days.date, "value": level * rng.lognormal(0, 0.25, len(days))}
        )
        frames.append(_rows(sym, SYNTHETIC_SOURCE, "daily_revenue", hist, fetched))
    for metric, value in {"circulating_supply": 336e6, "total_supply": 962e6, "future_emissions": 380e6,
                          "usdc_on_hyperliquid": 5.2e9, "af_hype_balance": 31e6}.items():  # fmt: skip
        frames.append(_snapshot("HYPE", SYNTHETIC_SOURCE, metric, value, fetched))
    upsert_metrics(store, pd.concat(frames, ignore_index=True), "demo")


def _demo_fundamentals(
    settings: Settings, store: Store, rng: np.random.Generator, end: pd.Timestamp
) -> None:
    """Synthetic EDGAR-shaped facts (10-Q quarters + YTD, 10-K years) for equities."""
    from market_signal.fundamentals.equity import FACT_COLUMNS, upsert_facts

    rows = []
    for a in settings.universe.values():
        if not a.provider_ids.get("sec_cik"):
            continue
        rev_q = 10e9 * rng.uniform(0.5, 2.0)
        growth = rng.uniform(0.01, 0.04)
        margin = rng.uniform(0.15, 0.40)
        shares = 1e9 * rng.uniform(0.5, 3.0)
        first_year = end.year - 9
        for year in range(first_year, end.year + 1):
            q_vals = []
            for q in range(4):
                q_start = pd.Timestamp(year, 3 * q + 1, 1).date()
                q_end = (pd.Timestamp(year, 3 * q + 1, 1) + pd.offsets.QuarterEnd(0)).date()
                if pd.Timestamp(q_end) + pd.Timedelta(days=40) > end.tz_localize(None):
                    break
                rev_q *= 1 + growth + rng.normal(0, 0.03)
                m = float(np.clip(margin + rng.normal(0, 0.03), 0.02, 0.6))
                vals = {
                    "revenue": rev_q,
                    "operating_income": rev_q * m,
                    "net_income": rev_q * m * 0.8,
                    "cfo": rev_q * m * 0.95,
                    "capex": rev_q * 0.08,
                }
                q_vals.append((q_start, q_end, vals))
                filed = (pd.Timestamp(q_end) + pd.Timedelta(days=35)).date()
                if q < 3:  # 10-Q: quarter, plus YTD for Q2/Q3
                    for metric, v in vals.items():
                        rows.append(
                            (a.symbol, metric, metric, "USD", q_start, q_end, v, "10-Q", filed)
                        )
                        if q > 0:
                            ytd = sum(x[2][metric] for x in q_vals)
                            rows.append(
                                (
                                    a.symbol,
                                    metric,
                                    metric,
                                    "USD",
                                    q_vals[0][0],
                                    q_end,
                                    ytd,
                                    "10-Q",
                                    filed,
                                )
                            )
                    rows.append(
                        (
                            a.symbol,
                            "diluted_shares",
                            "diluted_shares",
                            "shares",
                            q_start,
                            q_end,
                            shares,
                            "10-Q",
                            filed,
                        )
                    )
            if len(q_vals) == 4:  # 10-K
                filed = (pd.Timestamp(q_vals[-1][1]) + pd.Timedelta(days=60)).date()
                for metric in ("revenue", "operating_income", "net_income", "cfo", "capex"):
                    total = sum(x[2][metric] for x in q_vals)
                    rows.append(
                        (
                            a.symbol,
                            metric,
                            metric,
                            "USD",
                            q_vals[0][0],
                            q_vals[-1][1],
                            total,
                            "10-K",
                            filed,
                        )
                    )
                rows.append(
                    (
                        a.symbol,
                        "equity",
                        "equity",
                        "USD",
                        None,
                        q_vals[-1][1],
                        shares * rng.uniform(5, 30),
                        "10-K",
                        filed,
                    )
                )
    df = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "concept",
            "metric",
            "unit",
            "period_start",
            "period_end",
            "value",
            "form",
            "filed",
        ],
    )
    # use the first concept name of each metric so the PIT loader recognises it
    from market_signal.fundamentals.equity import CONCEPTS

    df["concept"] = df["metric"].map({k: v[0] for k, v in CONCEPTS.items()})
    df["fy"], df["fp"] = None, None
    df["accn"] = [f"synthetic-{i}" for i in range(len(df))]
    df["available_at"] = pd.to_datetime(df["filed"]).dt.tz_localize("UTC") + pd.Timedelta(days=1)
    df["pit_method"] = "filing_date"
    upsert_facts(store, df[FACT_COLUMNS], "demo")


def _demo_macro(store: Store, rng: np.random.Generator, end: pd.Timestamp) -> None:
    from market_signal.data.updaters import upsert_macro

    dates = pd.bdate_range(end - pd.Timedelta(days=365 * 8), end)
    specs = {"VIXCLS": (18, 4, 10, 60), "BAA10Y": (2.0, 0.3, 1.2, 4.0), "T10Y2Y": (0.5, 0.6, -1.2, 2.5),
             "DTWEXBGS": (115, 4, 100, 130), "DGS10": (3.0, 0.8, 0.6, 5.0), "DTB3": (3.0, 1.5, 0.0, 5.5),
             "DFII10": (1.0, 0.8, -1.2, 2.6), "T10YIE": (2.3, 0.3, 1.5, 3.0), "DFF": (3.0, 1.6, 0.05, 5.4)}  # fmt: skip
    for sid, (mu, sd, lo, hi) in specs.items():
        x = np.empty(len(dates))
        x[0] = mu
        for i in range(1, len(x)):
            x[i] = np.clip(x[i - 1] + 0.02 * (mu - x[i - 1]) + rng.normal(0, sd * 0.05), lo, hi)
        df = pd.DataFrame({"series_id": sid, "obs_date": dates.date, "value": x, "realtime_start": dates.date,
                           "realtime_end": None, "available_at": dates.tz_convert("UTC") + pd.Timedelta(days=2),
                           "pit_method": "market_close"})  # fmt: skip
        upsert_macro(store, df, SYNTHETIC_SOURCE, "demo")


def is_synthetic(store: Store) -> bool:
    try:
        row = store.con.execute(
            "SELECT count(*) FROM bars WHERE source=?", [SYNTHETIC_SOURCE]
        ).fetchone()
    except Exception:
        return False
    return bool(row and row[0])
