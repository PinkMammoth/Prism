"""Point-in-time macro event table for the ``macro_shock`` perp strategy.

Two inputs, both already ingested by ``market update`` (config/macro.yaml):
  DGS2      2-year Treasury yield, ``market_close``: available_at = obs_date + 2 days, 00:00 UTC.
  CPIAUCSL  CPI, ALFRED ``vintage``: available_at = realtime_start + 1 day, 00:00 UTC.

When each value counts as known here (``known_at``):
  DGS2 for US day D: the later of its stored ``available_at`` (D+2 00:00 UTC) and 00:00 UTC
    the day after the next business day. The second rule only bites for a Friday D, which FRED
    posts on Monday: Friday's move counts from Tuesday 00:00, not Sunday. This is stricter than
    the stored policy, so paper-tracking (which reads FRED) can see every event the backtest
    uses.
  CPI released on R: its stored ``available_at`` (R+1 00:00 UTC; the print is at 12:30 UTC on R).
  A hot-CPI event needs the release-day DGS2 move too, so it's known at max(both) = R+2 00:00.
The signal bar is the first daily bar whose close is at or after ``known_at``. Entry is the
next open, i.e. ``known_at`` itself for 00:00 UTC daily bars. The market reacts to both prints
within minutes, so this experiment tests the *drift after a ~1.5–3 day delay*, not the reaction.

Everything for an event is computed from data available at its ``known_at``:
  - DGS2 change = value(D) − previous valid value (≤ 5 calendar days earlier; otherwise
    missing). σ = std of the changes on the 365 calendar days *before* D (≥ 200 of them),
    so the shock doesn't inflate its own yardstick. z = change / σ.
  - CPI m/m for month M and the mean m/m of the 12 months before it, all taken from the
    vintage known at the release (later revisions are never used). Any missing month makes
    the comparison missing.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from market_signal.data.pit import PitViolation, daily_history_asof, load_macro

DGS2, CPI = "DGS2", "CPIAUCSL"
MAX_GAP_DAYS = 5  # a DGS2 change across a longer gap than this is missing, not a 1-day move
SIGMA_WINDOW = "365D"
SIGMA_MIN_OBS = 200
RELEASE_LAG_DAYS = (25, 80)  # CPI for month M (dated the 1st) is out ~6 weeks later
STALE_BAR_DAYS = 2  # an event whose first eligible bar is later than this is dropped
COLUMNS = ["ms_event_date", "ms_dgs2_bp", "ms_dgs2_z", "ms_cpi_surprise", "ms_known_at"]

_cache: dict[tuple, pd.DataFrame] = {}


def _utc(x) -> pd.Series:
    return pd.to_datetime(x, utc=True)


def dgs2_moves(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per DGS2 observation date: change_bp, sigma_bp, z, known_at."""
    cols = ["obs_date", "value", "change_bp", "sigma_bp", "z", "known_at"]
    if rows is None or rows.empty:
        return pd.DataFrame(columns=cols)
    r = rows.copy()
    r["available_at"] = _utc(r["available_at"])
    # the first published value of each day (DGS2 isn't revised; this guards against re-stores)
    r = r.sort_values(["obs_date", "available_at"]).drop_duplicates("obs_date", keep="first")
    r = r[np.isfinite(r["value"].astype(float))]  # FRED '.' (holidays) stay missing
    if r.empty:
        return pd.DataFrame(columns=cols)
    d = pd.DatetimeIndex(pd.to_datetime(r["obs_date"]))
    v = r["value"].to_numpy(float)
    chg = np.full(len(v), np.nan)
    gap = np.diff(d.to_numpy()).astype("timedelta64[D]").astype(int)
    ok = gap <= MAX_GAP_DAYS
    chg[1:][ok] = (v[1:] - v[:-1])[ok] * 100.0
    s = pd.Series(chg, index=d)
    sigma = s.rolling(SIGMA_WINDOW, min_periods=SIGMA_MIN_OBS, closed="left").std()
    sigma = sigma.where(sigma > 1e-6)  # a flat year has no yardstick: z is missing
    nxt_bday = (d + pd.offsets.BDay(1)).tz_localize("UTC") + pd.Timedelta(days=1)
    known = np.maximum(
        pd.DatetimeIndex(r["available_at"]).as_unit("ns").asi8, nxt_bday.as_unit("ns").asi8
    )
    return pd.DataFrame({
        "obs_date": d, "value": v, "change_bp": chg, "sigma_bp": sigma.to_numpy(),
        "z": chg / sigma.to_numpy(), "known_at": pd.to_datetime(known, utc=True),
    }).reset_index(drop=True)  # fmt: skip


def cpi_releases(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per monthly CPI release: the new month's m/m change as first reported, the mean
    m/m of the 12 months before it (same vintage), and their difference (``surprise``)."""
    cols = ["release_date", "obs_month", "mom", "trailing_avg", "surprise", "known_at"]
    if rows is None or rows.empty:
        return pd.DataFrame(columns=cols)
    r = rows.copy()
    r["available_at"] = _utc(r["available_at"])
    r["obs_date"] = pd.to_datetime(r["obs_date"])
    r["realtime_start"] = pd.to_datetime(r["realtime_start"])
    first = r.sort_values("realtime_start").drop_duplicates("obs_date", keep="first")
    out = []
    for rel, g in first.groupby("realtime_start"):
        m = g["obs_date"].max()  # the month this release adds
        if not RELEASE_LAG_DAYS[0] <= (rel - m).days <= RELEASE_LAG_DAYS[1]:
            continue  # ALFRED's initial bulk vintage, not a monthly release
        known = g.loc[g["obs_date"] == m, "available_at"].iloc[0]
        hist = daily_history_asof(r, known)
        idx = pd.date_range(m - pd.DateOffset(months=13), m, freq="MS")
        lvl = hist.reindex(idx).to_numpy(float)
        mom = lvl[1:] / lvl[:-1] - 1  # 13 m/m changes, the last one is month m
        prior = mom[:-1]
        avg = float(prior.mean()) if np.isfinite(prior).all() else np.nan
        out.append({"release_date": rel, "obs_month": m, "mom": mom[-1], "trailing_avg": avg,
                    "surprise": mom[-1] - avg, "known_at": known})  # fmt: skip
    return pd.DataFrame(out, columns=cols)


def event_table(dgs2_rows: pd.DataFrame, cpi_rows: pd.DataFrame) -> pd.DataFrame:
    """One row per US trading day D with a DGS2 move: the move, its z-score, the CPI surprise if
    CPI was released on D, and when all of that was known."""
    mv = dgs2_moves(dgs2_rows)
    if mv.empty:
        return pd.DataFrame(columns=["event_date", "dgs2_bp", "dgs2_z", "cpi_surprise", "known_at"])
    cpi = cpi_releases(cpi_rows)
    ev = pd.DataFrame({"event_date": mv["obs_date"], "dgs2_bp": mv["change_bp"],
                       "dgs2_z": mv["z"], "known_at": mv["known_at"]})  # fmt: skip
    ev["cpi_surprise"] = np.nan
    if not cpi.empty:
        c = cpi.set_index(pd.DatetimeIndex(cpi["release_date"]))
        pos = ev["event_date"].isin(c.index)
        ev.loc[pos, "cpi_surprise"] = c.loc[ev.loc[pos, "event_date"], "surprise"].to_numpy()
        ck = _utc(c.loc[ev.loc[pos, "event_date"], "known_at"]).to_numpy()
        ev.loc[pos, "known_at"] = np.maximum(ev.loc[pos, "known_at"].to_numpy(), ck)
    ev = ev[np.isfinite(ev["dgs2_bp"].astype(float))]
    return ev[["event_date", "dgs2_bp", "dgs2_z", "cpi_surprise", "known_at"]].reset_index(
        drop=True
    )


def load_event_table(store) -> pd.DataFrame:
    """The event table from stored DGS2 + CPIAUCSL (research-admissible rows only). Empty if
    either series is missing. Cached per process on the stored rows."""
    try:
        d = load_macro(store, DGS2, research=True)
        c = load_macro(store, CPI, research=True)
    except PitViolation as exc:  # never silently use inadmissible data
        warnings.warn(f"macro_shock: {exc}; no macro events", stacklevel=2)
        return event_table(pd.DataFrame(), pd.DataFrame())
    except Exception:  # no macro table yet
        return event_table(pd.DataFrame(), pd.DataFrame())

    def sig(df: pd.DataFrame) -> tuple:
        return (
            (len(df), str(df["available_at"].max()), float(df["value"].sum(skipna=True)))
            if len(df)
            else (0,)
        )

    key = (sig(d), sig(c))
    if key not in _cache:
        _cache.clear()
        _cache[key] = event_table(d, c)
    return _cache[key]


def attach_macro_events(frame: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``frame`` (daily bars with ``close_time``) with the ``ms_*`` columns set on each
    event's signal bar (first bar closing at/after ``known_at``) and NaN elsewhere."""
    f = frame.copy()
    n = len(f)
    date = np.full(n, np.datetime64("NaT", "ns"))
    known = np.full(n, np.datetime64("NaT", "ns"))
    vals = {c: np.full(n, np.nan) for c in ("ms_dgs2_bp", "ms_dgs2_z", "ms_cpi_surprise")}
    if n and events is not None and not events.empty:
        ct = pd.DatetimeIndex(_utc(f["close_time"])).as_unit("ns").asi8
        k = pd.DatetimeIndex(_utc(events["known_at"])).as_unit("ns").asi8
        pos = np.searchsorted(ct, k, side="left")  # first close >= known_at
        ok = pos < n
        ok[ok] &= (ct[pos[ok]] - k[ok]) <= pd.Timedelta(days=STALE_BAR_DAYS).value
        ev = events[ok].assign(_bar=pos[ok]).drop_duplicates("_bar", keep="last")
        b = ev["_bar"].to_numpy()
        date[b] = pd.DatetimeIndex(ev["event_date"]).as_unit("ns").to_numpy()
        known[b] = pd.DatetimeIndex(_utc(ev["known_at"])).tz_localize(None).as_unit("ns").to_numpy()
        for c, src in (
            ("ms_dgs2_bp", "dgs2_bp"),
            ("ms_dgs2_z", "dgs2_z"),
            ("ms_cpi_surprise", "cpi_surprise"),
        ):
            vals[c][b] = ev[src].to_numpy(float)
    f["ms_event_date"] = date  # US trading day of the move / release (naive date)
    f["ms_known_at"] = pd.DatetimeIndex(known).tz_localize("UTC")
    for c, v in vals.items():
        f[c] = v
    return f
