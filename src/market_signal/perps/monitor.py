"""Funding & open-interest monitor: describes positioning; it is NOT a tested signal.

Funding is the periodic payment between longs and shorts that keeps a perp near spot.
Positive → longs pay shorts (longs crowded); negative → shorts pay longs.

  annualised funding  = mean per-period rate × periods per year (Hyperliquid: 8760)
  current             = mean of the last ``avg_days`` (smooths hourly noise)
  percentile          = where the current N-day mean sits among the daily-sampled N-day means
                        of the last ``window_days`` (its own history, so coins are comparable)
  state               = crowded long / crowded short / neutral / not enough history
Whether extremes predict anything is a Phase 3 research question; until a strategy earns a
verdict, these are context only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.config import Settings
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.perps.data import load_funding, load_snapshots, perp_config

MIN_HISTORY_DAYS = 60
FUNDING_STALE = pd.Timedelta(hours=6)
SNAPSHOT_STALE = pd.Timedelta(hours=36)


@dataclass
class PerpView:
    coin: str
    last_funding_time: pd.Timestamp | None
    funding_now_ann: float | None  # latest settled period, annualised
    funding_1d_ann: float | None
    funding_avg_ann: float | None  # avg_days mean, annualised (the "current" reading)
    funding_30d_ann: float | None
    percentile: float | None
    state: str  # CROWDED LONG | CROWDED SHORT | NEUTRAL | NOT ENOUGH HISTORY | NO DATA
    history_days: float
    mark_px: float | None = None
    premium: float | None = None  # mark vs oracle (fraction)
    oi_notional: float | None = None
    oi_change_7d: float | None = None
    oi_history_days: float = 0.0
    max_leverage: float | None = None
    snapshot_at: pd.Timestamp | None = None
    funding_stale: bool = False
    snapshot_stale: bool = False

    @property
    def who_pays(self) -> str:
        if self.funding_avg_ann is None:
            return "–"
        if abs(self.funding_avg_ann) < 0.005:
            return "roughly balanced"
        return "longs pay shorts" if self.funding_avg_ann > 0 else "shorts pay longs"


def funding_stats(
    rates: pd.Series, now: pd.Timestamp, periods_per_year: float, avg_days: int, window_days: int,
    hi: float, lo: float,
) -> dict:  # fmt: skip
    if rates.empty:
        return {"state": "NO DATA", "history_days": 0.0}
    rates = rates[rates.index <= now]
    if rates.empty:
        return {"state": "NO DATA", "history_days": 0.0}

    def ann(window: str) -> float | None:
        r = rates[rates.index > now - pd.Timedelta(window)]
        return float(r.mean() * periods_per_year) if len(r) else None

    history_days = (rates.index[-1] - rates.index[0]).total_seconds() / 86400
    out = {
        "last_funding_time": rates.index[-1],
        "funding_now_ann": float(rates.iloc[-1] * periods_per_year),
        "funding_1d_ann": ann("1D"),
        "funding_avg_ann": ann(f"{avg_days}D"),
        "funding_30d_ann": ann("30D"),
        "history_days": history_days,
        "percentile": None,
    }
    if history_days < MIN_HISTORY_DAYS:
        out["state"] = "NOT ENOUGH HISTORY"
        return out
    rolling = rates.rolling(f"{avg_days}D").mean()
    daily = rolling.resample("1D").last().dropna()
    # only days with a full avg_days window behind them, inside the comparison window
    daily = daily[daily.index >= rates.index[0] + pd.Timedelta(days=avg_days)]
    daily = daily[daily.index > now - pd.Timedelta(days=window_days)]
    cur = out["funding_avg_ann"]
    if cur is None or len(daily) < 30:
        out["state"] = "NOT ENOUGH HISTORY"
        return out
    pct = float((daily.to_numpy() * periods_per_year <= cur + 1e-15).mean())
    out["percentile"] = pct
    out["state"] = "CROWDED LONG" if pct >= hi else "CROWDED SHORT" if pct <= lo else "NEUTRAL"
    return out


def oi_stats(snaps: pd.DataFrame, now: pd.Timestamp) -> dict:
    if snaps is None or snaps.empty:
        return {}
    s = snaps.copy()
    s["snapshot_at"] = pd.to_datetime(s["snapshot_at"], utc=True)
    s = s[s["snapshot_at"] <= now].sort_values("snapshot_at")
    if s.empty:
        return {}
    last = s.iloc[-1]
    prior = s[s["snapshot_at"] <= last["snapshot_at"] - pd.Timedelta(days=7)]
    change = None
    if not prior.empty and prior.iloc[-1]["oi_notional"] and pd.notna(last["oi_notional"]):
        change = float(last["oi_notional"] / prior.iloc[-1]["oi_notional"] - 1)

    def f(x):
        return None if x is None or pd.isna(x) else float(x)

    return {
        "mark_px": f(last["mark_px"]), "premium": f(last["premium"]), "oi_notional": f(last["oi_notional"]),
        "oi_change_7d": change, "max_leverage": f(last["max_leverage"]), "snapshot_at": last["snapshot_at"],
        "oi_history_days": (last["snapshot_at"] - s.iloc[0]["snapshot_at"]).total_seconds() / 86400,
    }  # fmt: skip


def perp_overview(
    store: Store, settings: Settings, now: pd.Timestamp | None = None
) -> list[PerpView]:
    cfg = perp_config(settings)
    fcfg, mcfg = cfg.get("funding") or {}, cfg.get("monitor") or {}
    now = pd.Timestamp(now or utcnow())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    ppy = float(fcfg.get("periods_per_year", 8760))
    window, avg = int(mcfg.get("window_days", 365)), int(mcfg.get("avg_days", 7))
    snaps_all = load_snapshots(store)
    out = []
    for coin in [str(c).upper() for c in cfg.get("coins") or []]:
        rates = load_funding(
            store, coin, since=(now - pd.Timedelta(days=window + avg + 2)).to_pydatetime()
        )
        fs = funding_stats(rates, now, ppy, avg, window, float(mcfg.get("crowded_long_pct", 0.9)),
                           float(mcfg.get("crowded_short_pct", 0.1)))  # fmt: skip
        snaps = snaps_all[snaps_all["coin"] == coin] if not snaps_all.empty else snaps_all
        os_ = oi_stats(snaps, now)
        v = PerpView(coin=coin, last_funding_time=fs.get("last_funding_time"),
                     funding_now_ann=fs.get("funding_now_ann"), funding_1d_ann=fs.get("funding_1d_ann"),
                     funding_avg_ann=fs.get("funding_avg_ann"), funding_30d_ann=fs.get("funding_30d_ann"),
                     percentile=fs.get("percentile"), state=fs["state"], history_days=fs["history_days"],
                     **os_)  # fmt: skip
        v.funding_stale = v.last_funding_time is None or now - v.last_funding_time > FUNDING_STALE
        v.snapshot_stale = v.snapshot_at is None or now - v.snapshot_at > SNAPSHOT_STALE
        out.append(v)
    return out


def funding_history_frame(
    store: Store, settings: Settings, coin: str, days: int = 365, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Daily series for charts: the avg_days mean funding, annualised, plus its percentile bands."""
    cfg = perp_config(settings)
    ppy = float((cfg.get("funding") or {}).get("periods_per_year", 8760))
    avg = int((cfg.get("monitor") or {}).get("avg_days", 7))
    now = pd.Timestamp(now or utcnow())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    rates = load_funding(store, coin, since=(now - pd.Timedelta(days=days + avg)).to_pydatetime())
    if rates.empty:
        return pd.DataFrame()
    daily = (rates.rolling(f"{avg}D").mean() * ppy).resample("1D").last().dropna()
    daily = daily[daily.index >= rates.index[0] + pd.Timedelta(days=avg)]
    return pd.DataFrame({"funding_ann": daily, "zero": np.zeros(len(daily))})
