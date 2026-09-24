"""Trading calendars and bar timing.

Bar timestamps are stored as the bar's *open* time in UTC. ``close_time`` is when the
bar's final values become knowable; every point-in-time join keys off it.

The NYSE holiday calendar is rules-based (no external dependency). It is used for gap
detection only: price data defines the actual sessions. Early closes (13:00 ET) are not
modelled, so a 16:00 ET close_time is used. That is later than the true close and
therefore conservative for point-in-time joins.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import pandas as pd

from market_signal.models.domain import Calendar, Timeframe

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

NYSE_OPEN = time(9, 30)
NYSE_CLOSE = time(16, 0)

# One-off closures (weather, national mourning, 9/11).
_NYSE_SPECIAL_CLOSURES = {
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11), date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30),
    date(2018, 12, 5), date(2025, 1, 9),
}  # fmt: skip


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@cache
def nyse_holidays(year: int) -> frozenset[date]:
    hols = {
        _nth_weekday(year, 1, 0, 3),  # MLK (since 1998)
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    ny = date(year, 1, 1)
    if ny.weekday() != 5:  # NYSE does not observe New Year on the prior Friday
        hols.add(_observed(ny))
    if year >= 2022:
        hols.add(_observed(date(year, 6, 19)))
    hols |= {d for d in _NYSE_SPECIAL_CLOSURES if d.year == year}
    return frozenset(hols)


def is_nyse_session(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def nyse_sessions(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if is_nyse_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def nyse_open_utc(d: date) -> datetime:
    return datetime.combine(d, NYSE_OPEN, tzinfo=NY).astimezone(UTC)


def nyse_close_utc(d: date) -> datetime:
    return datetime.combine(d, NYSE_CLOSE, tzinfo=NY).astimezone(UTC)


def close_times(ts: pd.Series, calendar: Calendar, timeframe: Timeframe) -> pd.Series:
    """Vectorised bar close time (UTC) from bar open timestamps (UTC)."""
    if calendar == Calendar.CRYPTO_24_7 or timeframe != Timeframe.D1:
        return ts + pd.Timedelta(seconds=timeframe.seconds)
    # NYSE daily: 16:00 New York on the session date
    local_dates = ts.dt.tz_convert(NY).dt.date
    return pd.Series(
        [pd.Timestamp(nyse_close_utc(d)) for d in local_dates], index=ts.index, dtype=ts.dtype
    )


def expected_bar_opens(
    calendar: Calendar, timeframe: Timeframe, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    """Expected bar open timestamps between start and end inclusive (for gap checks)."""
    if calendar == Calendar.CRYPTO_24_7:
        freq = {Timeframe.D1: "1D", Timeframe.H4: "4h", Timeframe.H1: "1h"}[timeframe]
        return pd.date_range(start, end, freq=freq, tz="UTC")
    if timeframe != Timeframe.D1:
        raise ValueError("Intraday NYSE bars are not supported in V1")
    start_d = start.tz_convert(NY).date()
    end_d = end.tz_convert(NY).date()
    return pd.DatetimeIndex([pd.Timestamp(nyse_open_utc(d)) for d in nyse_sessions(start_d, end_d)])
