"""Reusable, descriptive event-study and reaction-time adapter (no hypotheses run here).

``event_study(store, events, assets)`` measures, per (event, asset): pre-event return, post
returns at fixed horizons, a BTC-adjusted ("abnormal") return, volatility and volume ratios
against the same-length windows of the prior 7 days, Hyperliquid OI and funding change, and
reaction-time analytics (first time |move| crosses each threshold, peak, reversal,
persistence). It produces numbers, never verdicts; a study that tests a hypothesis must be
preregistered separately.

Time basis (``basis``):
  ``first_seen``  (default) Prism's own first sighting: the only basis that answers "could
                  Prism have traded it?"
  ``event_time``  scheduled events (the release instant) — legitimate for scheduled releases
  ``published``   historical corpus only; labelled ``publication_basis_exploratory`` because
                  Prism's latency for those events is unknown. Never confirmatory.

Outcome prices are realised bars after the reference instant (outcomes may use final
values); the reference price is the last bar closed at or before t0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.data.store import Store

EVENT_STUDY_VERSION = "context_event_study_v1"
DEFAULT_HORIZONS_MIN = (15, 60, 240, 1440)
REACTION_THRESHOLDS_PCT = (0.5, 1.0, 2.0)


@dataclass
class Bars:
    """Cached close/volume series per (source, coin, timeframe)."""

    store: Store
    source: str = "hyperliquid"
    timeframe: str = "15m"
    _cache: dict = field(default_factory=dict)

    def get(self, coin: str) -> pd.DataFrame:
        if coin not in self._cache:
            df = self.store.con.execute(
                "SELECT close_time, close, high, low, volume FROM perp_intraday_bars WHERE source=? "
                "AND coin=? AND timeframe=? ORDER BY close_time", [self.source, coin, self.timeframe]).df()  # fmt: skip
            if len(df):
                df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
            self._cache[coin] = df.set_index("close_time") if len(df) else df
        return self._cache[coin]


def _price_at(
    b: pd.DataFrame, t: pd.Timestamp, side: str = "before"
) -> tuple[float | None, pd.Timestamp | None]:
    if b is None or not len(b):
        return None, None
    i = (
        b.index.searchsorted(t, side="right") - 1
        if side == "before"
        else b.index.searchsorted(t, side="left")
    )
    if i < 0 or i >= len(b):
        return None, None
    return float(b["close"].iloc[i]), b.index[i]


def reaction(b: pd.DataFrame, t0: pd.Timestamp, p0: float, max_minutes: int) -> dict:
    """Reaction-time analytics on bars closing in (t0, t0 + max]."""
    w = b[(b.index > t0) & (b.index <= t0 + pd.Timedelta(minutes=max_minutes))]
    out = {f"minutes_to_{th}pct": None for th in REACTION_THRESHOLDS_PCT}
    if not len(w) or not p0:
        return out | {"peak_move_pct": None, "minutes_to_peak": None, "final_move_pct": None,
                      "retrace_share": None, "persistent": None, "first_bar_minutes": None}  # fmt: skip
    up = (w["high"] / p0 - 1) * 100
    dn = (w["low"] / p0 - 1) * 100
    mins = (w.index - t0).total_seconds() / 60
    for th in REACTION_THRESHOLDS_PCT:
        hit = np.flatnonzero((up.to_numpy() >= th) | (dn.to_numpy() <= -th))
        out[f"minutes_to_{th}pct"] = float(mins[hit[0]]) if len(hit) else None
    ext = np.where(up.abs() >= dn.abs(), up, dn)
    k = int(np.argmax(np.abs(ext)))
    peak = float(ext[k])
    final = float((w["close"].iloc[-1] / p0 - 1) * 100)
    return out | {"peak_move_pct": peak, "minutes_to_peak": float(mins[k]), "final_move_pct": final,
                  "retrace_share": None if peak == 0 else float(1 - final / peak),
                  "persistent": bool(np.sign(final) == np.sign(peak) and abs(final) >= 0.5 * abs(peak)),
                  "first_bar_minutes": float(mins[0])}  # fmt: skip


def _t0(ev: dict, basis: str) -> tuple[pd.Timestamp | None, str]:
    if basis == "first_seen":
        return pd.Timestamp(ev["first_seen_at"]), "prism_first_seen"
    if basis == "event_time":
        if not ev.get("event_time"):
            return None, "no_event_time"
        return pd.Timestamp(ev["event_time"]), "scheduled_event_time" if ev.get(
            "scheduled"
        ) else "event_time"
    if basis == "published":
        if not ev.get("published_at"):
            return None, "no_publication_time"
        return pd.Timestamp(ev["published_at"]), "publication_basis_exploratory"
    raise ValueError(f"unknown basis {basis!r}")


def event_study(store: Store, events: list[dict], assets: list[str], *, basis: str = "first_seen",
                horizons_min: tuple[int, ...] = DEFAULT_HORIZONS_MIN, pre_minutes: int = 240,
                source: str = "hyperliquid", timeframe: str = "15m",
                reference: str = "BTC") -> pd.DataFrame:  # fmt: skip
    """One row per (event, asset). ``events`` are as-of folds (``ledger.states_asof``)."""
    bars = Bars(store, source, timeframe)
    rows = []
    for ev in events:
        t0, label = _t0(ev, basis)
        for a in assets:
            row = {"event_id": ev["event_id"], "subcategory": ev["subcategory"], "asset": a,
                   "basis": label, "t0": None if t0 is None else t0.isoformat(),
                   "observation_mode": ev.get("observation_mode"), "version": EVENT_STUDY_VERSION}  # fmt: skip
            b = bars.get(a)
            p0, at0 = _price_at(b, t0) if t0 is not None else (None, None)
            if p0 is None or t0 - at0 > pd.Timedelta(hours=1):
                rows.append(row | {"status": "no_price"})
                continue
            pre, _ = _price_at(b, t0 - pd.Timedelta(minutes=pre_minutes))
            row["pre_return_pct"] = None if not pre else (p0 / pre - 1) * 100
            rb = bars.get(reference) if a != reference else None
            rp0, _ = _price_at(rb, t0) if rb is not None else (None, None)
            for h in horizons_min:
                p1, at1 = _price_at(b, t0 + pd.Timedelta(minutes=h), "after")
                r = None if p1 is None or at1 - (t0 + pd.Timedelta(minutes=h)) > pd.Timedelta(hours=1) \
                    else (p1 / p0 - 1) * 100  # fmt: skip
                row[f"ret_{h}m_pct"] = r
                if rb is not None and rp0:
                    q1, _ = _price_at(rb, t0 + pd.Timedelta(minutes=h), "after")
                    row[f"abn_{h}m_pct"] = None if r is None or not q1 else r - (q1 / rp0 - 1) * 100
                w = b[(b.index > t0) & (b.index <= t0 + pd.Timedelta(minutes=h))]
                base = b[(b.index > t0 - pd.Timedelta(days=7)) & (b.index <= t0)]
                lr = np.log(w["close"]).diff().dropna()
                blr = np.log(base["close"]).diff().dropna()
                row[f"vol_ratio_{h}m"] = (
                    None if len(lr) < 2 or blr.std() == 0 else float(lr.std() / blr.std())
                )
                bv = base["volume"].mean() if len(base) else None
                row[f"volume_ratio_{h}m"] = (
                    None if not bv or not len(w) else float(w["volume"].mean() / bv)
                )
            row |= reaction(b, t0, p0, max(horizons_min))
            row |= _oi_funding_change(store, a, t0, max(horizons_min))
            rows.append(row | {"status": "ok"})
    return pd.DataFrame(rows)


def _oi_funding_change(store: Store, coin: str, t0: pd.Timestamp, minutes: int) -> dict:
    t1 = t0 + pd.Timedelta(minutes=minutes)
    try:
        oi = store.con.execute(
            "SELECT obs_at AS t_at, open_interest FROM (SELECT captured_at AS obs_at, open_interest FROM context_hl_oi_hourly "
            "WHERE coin=? UNION ALL SELECT snapshot_at, open_interest FROM perp_snapshots WHERE coin=? "
            "AND source='hyperliquid') WHERE obs_at BETWEEN ? AND ? ORDER BY obs_at",
            [coin, coin, (t0 - pd.Timedelta(hours=2)).to_pydatetime(), (t1 + pd.Timedelta(hours=2)).to_pydatetime()]).df()  # fmt: skip
        fu = store.con.execute(
            "SELECT avg(funding_rate) FROM perp_funding WHERE coin=? AND source='hyperliquid' AND "
            "time > ? AND time <= ?", [coin, t0.to_pydatetime(), t1.to_pydatetime()]).fetchone()[0]  # fmt: skip
    except Exception:
        return {"oi_change_pct": None, "funding_mean_post": None}
    if len(oi) >= 2:
        oi["at"] = pd.to_datetime(oi["t_at"], utc=True)
        a = oi[oi["at"] <= t0]
        b = oi[oi["at"] >= t1]
        ch = (
            None
            if a.empty or b.empty
            else float(b["open_interest"].iloc[0] / a["open_interest"].iloc[-1] - 1) * 100
        )
    else:
        ch = None
    return {"oi_change_pct": ch, "funding_mean_post": None if fu is None else float(fu)}


def summarise(df: pd.DataFrame, horizon: int = 60) -> pd.DataFrame:
    """Descriptive per-(subcategory, asset) means/medians (no significance claims)."""
    ok = df[df.get("status") == "ok"] if len(df) else df
    if not len(ok):
        return pd.DataFrame()
    c = f"ret_{horizon}m_pct"
    g = ok.groupby(["subcategory", "asset"])
    return pd.DataFrame({"n": g.size(), "mean_ret": g[c].mean(), "median_ret": g[c].median(),
                         "mean_abs_ret": g[c].apply(lambda s: s.abs().mean()),
                         "median_minutes_to_1pct": g["minutes_to_1.0pct"].median()}).reset_index()  # fmt: skip
