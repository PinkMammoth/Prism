"""Canonical intraday timeframes and UTC bar boundaries.

Every intraday bar is the half-open UTC interval ``[open_time, close_time)`` with
``close_time = open_time + interval``. Boundaries are aligned to the Unix epoch (1970-01-01
00:00 UTC), so:

- 15m bars open at :00, :15, :30, :45 of every UTC hour;
- 1h bars open at the top of every UTC hour;
- 4h bars open at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC.

A bar is *closed* at ``close_time`` and never earlier. No local time zone is involved
anywhere: naive datetimes are rejected instead of being guessed.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from market_signal.models.domain import Timeframe

INTRADAY: tuple[Timeframe, ...] = (Timeframe.M15, Timeframe.H1, Timeframe.H4)


def parse_timeframe(value: str | Timeframe) -> Timeframe:
    tf = Timeframe(value)
    if tf not in INTRADAY:
        raise ValueError(
            f"{tf} is not an intraday timeframe; supported: {[t.value for t in INTRADAY]}"
        )
    return tf


def interval(tf: Timeframe) -> pd.Timedelta:
    return pd.Timedelta(seconds=parse_timeframe(tf).seconds)


def utc(t: datetime | pd.Timestamp | str) -> pd.Timestamp:
    """A UTC timestamp. Naive input is an error (never assumed local or UTC)."""
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp {t!r}: intraday times must carry a UTC offset")
    return ts.tz_convert("UTC")


def floor_open(t: datetime | pd.Timestamp, tf: Timeframe) -> pd.Timestamp:
    """Open time of the bar containing instant ``t`` (the bar may still be forming)."""
    return utc(t).floor(interval(tf))


def is_aligned(open_time: datetime | pd.Timestamp, tf: Timeframe) -> bool:
    ts = utc(open_time)
    return ts == ts.floor(interval(tf))


def bar_close(open_time: datetime | pd.Timestamp, tf: Timeframe) -> pd.Timestamp:
    if not is_aligned(open_time, tf):
        raise ValueError(f"{open_time} is not on the {tf} UTC grid")
    return utc(open_time) + interval(tf)


def latest_closed_open(now: datetime | pd.Timestamp, tf: Timeframe) -> pd.Timestamp:
    """Open time of the newest bar whose close is at or before ``now``.

    At 10:37 the newest closed 15m bar is 10:15-10:30 and the newest closed 1h bar is
    09:00-10:00. At exactly 11:00 the 10:00-11:00 bar has closed."""
    return floor_open(now, tf) - interval(tf)


def expected_opens(
    start: datetime | pd.Timestamp, end: datetime | pd.Timestamp, tf: Timeframe
) -> pd.DatetimeIndex:
    """Grid open times in ``[start, end)``; ``start`` is rounded up onto the grid."""
    s, e = utc(start), utc(end)
    first = s.ceil(interval(tf))
    if first >= e:
        return pd.DatetimeIndex([], tz="UTC")
    return pd.date_range(first, e - pd.Timedelta(microseconds=1), freq=interval(tf), tz="UTC")
