"""Forward outcomes of 15-minute signal windows (long direction; side applied later).

* **Entry**: the first minute whose close is at or after the window's availability (latest
  finalization of its 15 minutes); reference = that minute's end-of-minute mid. The
  signal-to-entry delay is recorded. No same-window hindsight.
* **Exit** at horizon h: the end-of-minute mid of the minute closing h minutes after entry.
  No stops, no targets. A missing mid at entry or exit is NaN, never interpolated.
* **Pending vs missing**: an exit after the data's end (the checkpoint's ``known_at``) is
  ``pending`` (not yet matured, excluded and counted); a gap inside the data is ``missing``.
* **Funding** (Hyperliquid, hourly): the settlements at UTC hour boundaries in
  (entry, exit], each at the asset context's funding rate observed in the minute before the
  boundary (the last valid value within the prior hour). Positive rate: longs pay.
* **Path** (primary horizon): side-signed MFE/MAE on end-of-minute mids, minutes to each, and
  which of +-0.5 sigma(1 h) was touched first (``favourable``/``adverse``/``neither``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.microdir import spec as sp


def compute(f: pd.DataFrame, g: pd.DataFrame) -> pd.DataFrame:
    """Add long-direction outcome columns to the window frame ``f`` (built from grid ``g``)."""
    f = f.copy()
    n = len(g)
    if not len(f):
        return f
    t0 = g["minute_open"].iloc[0]
    mid = ((pd.to_numeric(g["bid_end"], errors="coerce") + pd.to_numeric(g["ask_end"], errors="coerce"))
           / 2).to_numpy(float)  # fmt: skip
    status = g["status"].to_numpy()
    mid = np.where(status == "MISSING", np.nan, mid)
    funding = pd.Series(pd.to_numeric(g["funding_end"], errors="coerce").to_numpy(float))
    fund_ff = funding.ffill(limit=60).to_numpy()
    avail = pd.to_datetime(f["available_at"], utc=True)
    # entry minute: first minute with close >= availability <=> open >= availability - 1 min
    rel = (avail - pd.Timedelta(minutes=1) - t0) / pd.Timedelta(minutes=1)
    e = np.ceil(rel.to_numpy(float))
    e = np.where(np.isfinite(e), e, -1).astype(np.int64)
    ok_e = (e >= 0) & (e < n)
    ec = np.clip(e, 0, n - 1)
    p0 = np.where(ok_e, mid[ec], np.nan)
    f["entry_minute"] = np.where(ok_e, e, -1)
    f["entry_time"] = t0 + pd.to_timedelta(np.where(ok_e, e + 1, 0), unit="min")
    f.loc[~ok_e, "entry_time"] = pd.NaT
    f["entry_delay_s"] = (f["entry_time"] - f["window_close"]).dt.total_seconds()
    f["entry_mid"] = p0
    for h in sp.HORIZONS_MIN:
        x = e + h
        inside = ok_e & (x < n)
        p1 = np.where(inside, mid[np.clip(x, 0, n - 1)], np.nan)
        f[f"gross_long_{h}"] = p1 / p0 - 1
        f[f"pending_{h}"] = ok_e & (x >= n)
        f[f"missing_{h}"] = inside & ~np.isfinite(f[f"gross_long_{h}"].to_numpy())
    # funding over the primary horizon (long pays positive)
    H = sp.PRIMARY_HORIZON
    # a settlement at the close of minute j when j is the hour's last minute; the position
    # (entry at close of minute a, exit at close of minute b) pays settlements j in (a, b]
    boundary = (t0.minute + np.arange(n)) % 60 == 59
    cs_rate = np.cumsum(np.where(boundary & np.isfinite(fund_ff), fund_ff, 0.0))
    cs_bad = np.cumsum(boundary & ~np.isfinite(fund_ff))
    a = np.clip(e, 0, n - 1)
    b = np.clip(e + H, 0, n - 1)
    live = ok_e & (e + H < n)
    fl = np.where(live & (cs_bad[b] - cs_bad[a] == 0), cs_rate[b] - cs_rate[a], np.nan)
    f["funding_long"] = fl
    # path over the primary horizon
    k = np.arange(1, H + 1)
    rows = np.flatnonzero(ok_e & (e + H < n) & np.isfinite(p0))
    for c in ("path_max", "path_min", "t_max", "t_min", "first_touch", "rv_path"):
        f[c] = np.nan if c != "first_touch" else 0.0
    if len(rows):
        P = mid[e[rows][:, None] + k[None, :]] / p0[rows][:, None] - 1
        good = np.isfinite(P).mean(1) >= 0.9
        Pf = np.where(np.isfinite(P), P, np.nan)
        with np.errstate(invalid="ignore"):
            mx, mn = np.nanmax(Pf, 1), np.nanmin(Pf, 1)
        tmax = np.nanargmax(np.where(np.isfinite(Pf), Pf, -np.inf), 1) + 1
        tmin = np.nanargmin(np.where(np.isfinite(Pf), Pf, np.inf), 1) + 1
        theta = 0.5 * 2.0 * f["sigma15"].to_numpy(float)[rows]  # 0.5 sigma(1h) ~ sigma15
        up = np.where(Pf >= theta[:, None], k[None, :], H + 1).min(1)
        dn = np.where(Pf <= -theta[:, None], k[None, :], H + 1).min(1)
        touch = np.where(up < dn, 1, np.where(dn < up, -1, 0))  # long-direction: +1 up first
        lr = np.diff(np.log(np.concatenate([np.ones((len(rows), 1)), 1 + Pf], 1)), axis=1)
        rv = np.nanstd(lr, 1) * np.sqrt(H)
        for c, v in (("path_max", mx), ("path_min", mn), ("t_max", tmax), ("t_min", tmin),
                     ("first_touch", touch), ("rv_path", rv)):  # fmt: skip
            col = f[c].to_numpy(float).copy()
            col[rows] = np.where(good, v, np.nan)
            f[c] = col
    return f


def side_view(ev: pd.DataFrame, side: str, cost_bps: float, fee_bps: float,
              slip_bps: float) -> pd.DataFrame:  # fmt: skip
    """Side-signed returns and costs for events (vectorised over rows of one coin or many:
    ``cost_bps``/``fee_bps``/``slip_bps`` may be arrays aligned with ``ev``)."""
    d = 1.0 if side == "long" else -1.0
    out = pd.DataFrame(index=ev.index)
    for h in sp.HORIZONS_MIN:
        out[f"gross_{h}"] = d * ev[f"gross_long_{h}"]
    H = sp.PRIMARY_HORIZON
    out["gross"] = out[f"gross_{H}"]
    out["fees"] = 2 * np.asarray(fee_bps, float) / 1e4
    out["slippage"] = 2 * np.asarray(slip_bps, float) / 1e4
    out["funding"] = d * ev["funding_long"]
    out["net"] = out["gross"] - out["fees"] - out["slippage"] - out["funding"]
    # passive-entry sensitivity: maker fee on entry, no entry slippage, taker exit
    out["net_passive"] = (out["gross"] - (sp.MAKER_FEE_BPS + np.asarray(fee_bps, float)
                                          + np.asarray(slip_bps, float)) / 1e4 - out["funding"])  # fmt: skip
    for h in sp.HORIZONS_MIN:
        out[f"net_{h}"] = out[f"gross_{h}"] - 2 * np.asarray(cost_bps, float) / 1e4
    out["mfe"] = ev["path_max"] if side == "long" else -ev["path_min"]
    out["mae"] = ev["path_min"] if side == "long" else -ev["path_max"]
    out["t_mfe"] = ev["t_max"] if side == "long" else ev["t_min"]
    out["t_mae"] = ev["t_min"] if side == "long" else ev["t_max"]
    out["favourable_first"] = d * ev["first_touch"]
    out["rv_path"] = ev["rv_path"]
    return out
