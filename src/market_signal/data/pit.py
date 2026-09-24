"""Point-in-time ("as-of") access to non-price data.

The central primitive is ``asof_values(rows, times)``: for each query time t it returns
the value of the *latest observation* whose *latest vintage* was available at t.
Rows whose ``pit_method`` is not admissible are rejected when ``research=True``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.data.store import Store
from market_signal.models.domain import BACKTEST_ADMISSIBLE_PIT


class PitViolation(ValueError):
    pass


def load_macro(store: Store, series_id: str, research: bool = True) -> pd.DataFrame:
    df = store.query(
        """SELECT series_id, obs_date, value, realtime_start, available_at, pit_method
           FROM macro_observations WHERE series_id=? ORDER BY available_at, obs_date""",
        [series_id],
    )
    if df.empty:
        return df
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    if research:
        bad = ~df["pit_method"].isin([m.value for m in BACKTEST_ADMISSIBLE_PIT])
        if bad.any():
            methods = sorted(df.loc[bad, "pit_method"].unique())
            raise PitViolation(f"{series_id}: pit_method {methods} is not admissible for research")
    return df


def asof_values(rows: pd.DataFrame, times: pd.Series | pd.DatetimeIndex) -> pd.DataFrame:
    """Values known at each time in ``times`` (UTC timestamps, any order).

    rows: obs_date, value, available_at (one row per observation *vintage*).
    Returns a frame aligned to ``times`` with columns value, obs_date, known_since.
    Missing observations stay NaN; a NaN vintage (e.g. FRED '.') is a known-missing value.
    """
    times_idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    n = len(times_idx)
    out_val = np.full(n, np.nan)
    out_obs = np.full(n, None, dtype=object)
    out_known = np.full(n, pd.NaT, dtype=object)
    if rows is None or rows.empty or n == 0:
        return pd.DataFrame(
            {"value": out_val, "obs_date": out_obs, "known_since": out_known}, index=times_idx
        )

    r = rows.sort_values(["available_at", "obs_date"]).reset_index(drop=True)
    avail = pd.DatetimeIndex(r["available_at"]).asi8
    obs = r["obs_date"].to_numpy()
    vals = r["value"].to_numpy(dtype=float)

    order = np.argsort(times_idx.asi8, kind="stable")
    t_sorted = times_idx.asi8[order]
    latest: dict = {}  # obs_date -> (value, available_at)
    max_obs = None
    j = 0
    for k, t in zip(order, t_sorted, strict=True):
        while j < len(r) and avail[j] <= t:
            latest[obs[j]] = (vals[j], avail[j])
            if max_obs is None or obs[j] > max_obs:
                max_obs = obs[j]
            j += 1
        if max_obs is not None:
            v, a = latest[max_obs]
            out_val[k] = v
            out_obs[k] = max_obs
            out_known[k] = pd.Timestamp(a, tz="UTC")
    return pd.DataFrame(
        {"value": out_val, "obs_date": out_obs, "known_since": out_known}, index=times_idx
    )


def macro_panel(
    store: Store, series_ids: list[str], times: pd.Series | pd.DatetimeIndex, research: bool = True
) -> pd.DataFrame:
    """Wide frame of PIT macro values at ``times``; missing series -> NaN column."""
    idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    out = pd.DataFrame(index=idx)
    for sid in series_ids:
        rows = load_macro(store, sid, research=research)
        out[sid] = asof_values(rows, idx)["value"].to_numpy() if not rows.empty else np.nan
    return out


def daily_history_asof(rows: pd.DataFrame, t: pd.Timestamp) -> pd.Series:
    """The full observation history *as known at t* (for rolling stats on macro series).

    For each obs_date, the latest vintage with available_at <= t.
    """
    if rows.empty:
        return pd.Series(dtype=float)
    known = rows[rows["available_at"] <= t]
    known = known.sort_values(["obs_date", "available_at"]).drop_duplicates("obs_date", keep="last")
    return pd.Series(known["value"].to_numpy(), index=pd.to_datetime(known["obs_date"]))
