"""Causal multi-timeframe alignment: at time t, only bars that were CLOSED and AVAILABLE by t.

A bar is eligible at instant ``t`` when

- ``close_time <= t`` (it has finished; a forming bar never exists in storage anyway), and
- it was available by ``t``:
  - ``Availability.observed()`` (default; live/prospective use): ``first_observed_at <= t``,
    the instant Prism actually held it. A late provider publication delays eligibility;
  - ``Availability.assumed(latency)`` (historical research on backfilled bars, whose true
    publication time is unknown): ``close_time + latency <= t``. Results computed this way
    rest on an assumption and must be labelled so.

Among eligible bars the one with the latest ``open_time`` is returned, with the values it held
at ``t`` when ``as_known=True`` (later revisions undone). It is never forward-filled from an
unfinished bar: if nothing is eligible the row is empty (NaN). ``bars_behind`` says how many
grid bars older than the newest theoretically closed bar it is (0 = up to date).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from market_signal.data.store import Store
from market_signal.intraday.bars import VALUES, load_bars
from market_signal.intraday.grid import interval, latest_closed_open, parse_timeframe, utc
from market_signal.models.domain import Timeframe


@dataclass(frozen=True)
class Availability:
    mode: str  # "observed" | "assumed"
    latency: timedelta = timedelta(0)

    @classmethod
    def observed(cls) -> Availability:
        return cls("observed")

    @classmethod
    def assumed(cls, latency: timedelta) -> Availability:
        if latency < timedelta(0):
            raise ValueError("publication latency cannot be negative")
        return cls("assumed", latency)

    def available_at(self, bars: pd.DataFrame) -> pd.Series:
        if self.mode == "observed":
            return bars[["close_time", "first_observed_at"]].max(axis=1)
        if self.mode == "assumed":
            return bars["close_time"] + pd.Timedelta(self.latency)
        raise ValueError(f"unknown availability mode {self.mode!r}")


OUT_COLUMNS = ["open_time", "close_time", *VALUES, "available_at", "bars_behind", "revision"]


def align(
    times,
    bars: pd.DataFrame,
    tf: Timeframe,
    availability: Availability | None = None,
    *,
    revisions: pd.DataFrame | None = None,
    max_bars_behind: int | None = None,
) -> pd.DataFrame:
    """For each instant in ``times`` (UTC), the newest ``tf`` bar eligible at that instant.

    ``bars``: one series as returned by ``load_bars`` (canonical values). ``revisions``: that
    series' rows from ``perp_intraday_revisions``; when given, each aligned bar carries the
    values it held at the instant (a revision recorded after it is undone). Pure function.
    """
    tf = parse_timeframe(tf)
    availability = availability or Availability.observed()
    idx = (
        pd.DatetimeIndex([utc(t) for t in times]) if len(times) else pd.DatetimeIndex([], tz="UTC")
    )
    out = pd.DataFrame(index=idx, columns=OUT_COLUMNS)
    if bars.empty or idx.empty:
        return out
    b = bars.sort_values("open_time").reset_index(drop=True).copy()
    b["available_at"] = availability.available_at(b)
    # newest open_time among bars available by t: cumulative max over availability order
    # (a late-published older bar must never displace a newer one that was already available)
    order = b.sort_values(["available_at", "open_time"]).reset_index(drop=True)
    order["best_open"] = order["open_time"].cummax()
    q = pd.DataFrame({"t": idx}).reset_index(names="i").sort_values("t")
    m = pd.merge_asof(q, order[["available_at", "best_open"]], left_on="t", right_on="available_at",
                      direction="backward")  # fmt: skip
    m = m.sort_values("i")
    best = pd.DatetimeIndex(pd.to_datetime(m["best_open"], utc=True))
    picked = b.set_index("open_time").reindex(best)
    res = pd.DataFrame(
        {
            "open_time": best,
            "close_time": picked["close_time"].to_numpy(),
            **{c: picked[c].to_numpy() for c in VALUES},
            "available_at": picked["available_at"].to_numpy(),
            "revision": picked["revision"].to_numpy() if "revision" in picked else np.nan,
        },
        index=idx,
    )
    expected = pd.DatetimeIndex([latest_closed_open(t, tf) for t in idx])
    behind = (expected - pd.DatetimeIndex(res["open_time"])) / interval(tf)
    res["bars_behind"] = np.where(res["open_time"].isna(), np.nan, behind)
    if revisions is not None and not revisions.empty:
        res = _undo_revisions(res, revisions)
    if max_bars_behind is not None:
        too_old = res["bars_behind"] > max_bars_behind
        res.loc[too_old, :] = np.nan
    return res[OUT_COLUMNS]


def _undo_revisions(res: pd.DataFrame, revisions: pd.DataFrame) -> pd.DataFrame:
    r = revisions.copy()
    r["open_time"] = pd.to_datetime(r["open_time"], utc=True)
    r["revised_at"] = pd.to_datetime(r["revised_at"], utc=True)
    by_bar = {k: g.sort_values("revision") for k, g in r.groupby("open_time")}
    res = res.copy()
    for t, row in res.iterrows():
        g = by_bar.get(row["open_time"])
        if g is None:
            continue
        later = g[g["revised_at"] > t]
        if later.empty:
            continue
        first = later.iloc[0]
        for c in VALUES:
            res.at[t, c] = first[f"old_{c}"]
        res.at[t, "revision"] = int(first["revision"]) - 1
    return res


def load_revisions(store: Store, source: str, coin: str, tf: Timeframe) -> pd.DataFrame:
    return store.query(
        "SELECT * FROM perp_intraday_revisions WHERE source=? AND coin=? AND timeframe=? "
        "ORDER BY open_time, revision",
        [source, coin, parse_timeframe(tf).value],
    )


def snapshot(
    store: Store,
    source: str,
    coin: str,
    at,
    tfs: tuple[Timeframe, ...],
    availability: Availability | None = None,
) -> pd.DataFrame:
    """The newest eligible bar per timeframe at instant ``at`` (values as known then)."""
    at = utc(at)
    rows = []
    for tf in tfs:
        tf = parse_timeframe(tf)
        bars = load_bars(store, source, coin, tf, at - 50 * interval(tf), at + interval(tf))
        a = align([at], bars, tf, availability, revisions=load_revisions(store, source, coin, tf))
        r = a.iloc[0].to_dict()
        rows.append(
            {"timeframe": tf.value, "theoretical_latest_open": latest_closed_open(at, tf), **r}
        )
    return pd.DataFrame(rows)
