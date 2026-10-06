"""Scheduled macro calendar providers (official sources only).

* ``FredCalendarProvider``: release dates from the FRED release calendar (BLS/BEA schedules
  as published to FRED), at each agency's standing release time.
* ``CentralBankCalendarProvider``: FOMC/BOJ/ECB/BOE decision dates from
  ``config/context/macro_calendar.yaml`` (copied from each bank's published schedule).
* ``FredActualsProvider``: after a release, the first print from the ALFRED vintage of the
  release date (``actual``), the prior period as known the day before (``previous``) and as
  revised in the new vintage (``revision_of_previous``). Values are never back-filled into the
  schedule observation: they arrive as ``value_release`` updates stamped when Prism saw them.

Schedule knowledge is itself information: a scheduled event exists for Prism from the first
time a calendar run stored it. Re-running a calendar is idempotent (stable dedup keys).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from market_signal.context.model import MacroAttrs, Observation
from market_signal.context.providers.base import FetchResult
from market_signal.context.taxonomy import SourceType
from market_signal.data.http import HttpClient, ProviderError, SchemaError

CALENDAR_VERSION = "macro_calendar_v1"
LIVE_ACTUAL_HOURS = 24  # an actual first fetched later than this after release is 'historical'


def scheduled_utc(d: date, hhmm: str, tz: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


def dedup_key(subcategory: str, d: date) -> str:
    return f"macro:{subcategory}:{d.isoformat()}"


def _macro_obs(*, source_id: str, source_type: SourceType, sub: str, title: str, when: datetime,
               attrs: MacroAttrs, ref: str | None, country: str | None = None,
               region: str | None = None) -> Observation:  # fmt: skip
    return Observation(source_id=source_id, source_type=source_type, source_ref=ref,
                       subcategory=sub, title=title, scheduled=True, event_time=when,
                       confidence="OFFICIAL", market_wide=True, scope="systemic",
                       country=country, region=region, attributes=attrs,
                       dedup_key=dedup_key(sub, when.date()))  # fmt: skip


class FredCalendarProvider:
    name = "fred_calendar"
    stale_hours = 30.0

    def __init__(self, http: HttpClient, api_key: str | None, calendar: dict,
                 horizon_days: int = 120, lookback_days: int = 7):  # fmt: skip
        self.http, self.api_key, self.cal = http, api_key, calendar
        self.horizon, self.lookback = horizon_days, lookback_days

    def fetch(self, now: datetime, last_state: dict) -> FetchResult:
        if not self.api_key:
            raise ProviderError("FRED_API_KEY not set")
        fr = FetchResult()
        start = (now - timedelta(days=self.lookback)).date()
        end = (now + timedelta(days=self.horizon)).date()
        for rel in self.cal.get("fred_releases") or []:
            payload = self.http.get_json("/fred/release/dates", params={
                "release_id": rel["release_id"], "api_key": self.api_key, "file_type": "json",
                "include_release_dates_with_no_data": "true", "realtime_start": start.isoformat(),
                "realtime_end": "9999-12-31", "sort_order": "asc", "limit": 1000,
            }, redact_params=("api_key",))  # fmt: skip
            try:
                dates = [date.fromisoformat(x["date"]) for x in payload["release_dates"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaError(
                    f"fred release/dates schema changed ({rel['release_id']})"
                ) from exc
            for d in dates:
                fr.received += 1
                if not start <= d <= end:
                    fr.filtered += 1
                    continue
                when = scheduled_utc(d, rel["time"], rel["tz"])
                for ev in rel["events"]:
                    attrs = MacroAttrs(series_key=ev["series_key"], unit=ev.get("unit"),
                                       importance=ev.get("importance", 1), markets=("USD", "rates"),
                                       timezone=rel["tz"], time_precision="exact",
                                       schedule_source=f"fred:release:{rel['release_id']}")  # fmt: skip
                    fr.observations.append(_macro_obs(
                        source_id="fred_release_calendar", source_type=SourceType.OFFICIAL_STATISTICS,
                        sub=ev["subcategory"], title=f"{rel['name']} ({ev['series_key']})",
                        when=when, attrs=attrs, country="US",
                        ref=f"https://fred.stlouisfed.org/releases/calendar?rid={rel['release_id']}"))  # fmt: skip
        fr.raw = self.http.drain()
        return fr


class CentralBankCalendarProvider:
    name = "central_bank_calendar"
    stale_hours = 30.0

    def __init__(self, calendar: dict, horizon_days: int = 400, lookback_days: int = 7):
        self.cal, self.horizon, self.lookback = calendar, horizon_days, lookback_days

    def fetch(self, now: datetime, last_state: dict) -> FetchResult:
        fr = FetchResult()
        lo, hi = (
            (now - timedelta(days=self.lookback)).date(),
            (now + timedelta(days=self.horizon)).date(),
        )
        for bank, c in (self.cal.get("central_banks") or {}).items():
            for d in c["decision_dates"]:
                d = d if isinstance(d, date) else date.fromisoformat(str(d))
                fr.received += 1
                if not lo <= d <= hi:
                    fr.filtered += 1
                    continue
                attrs = MacroAttrs(series_key=c["series_key"], unit="pct", importance=1,
                                   markets=tuple(c.get("markets") or ()), timezone=c["tz"],
                                   time_precision=c.get("time_precision", "exact"),
                                   schedule_source=f"{c['source']} (verified {c.get('verified')})")  # fmt: skip
                fr.observations.append(_macro_obs(
                    source_id=f"{bank}_schedule", source_type=SourceType.CENTRAL_BANK,
                    sub=c["subcategory"], title=f"{bank.upper()} policy decision",
                    when=scheduled_utc(d, c["time"], c["tz"]), attrs=attrs, ref=c["source"],
                    country=c.get("country"), region=c.get("region")))  # fmt: skip
        return fr


# --------------------------------------------------------------------------- actuals


def transform(values: pd.Series, how: str) -> pd.Series:
    """Release-convention value from a level series (index: observation dates)."""
    v = values.astype(float)
    if how == "pct_change":
        return (v / v.shift(1) - 1) * 100
    if how == "diff":
        return v - v.shift(1)
    if how == "level":
        return v
    raise ValueError(f"unknown transform {how!r}")


class FredActualsProvider:
    """First prints for released calendar events that have no actual yet (reads the ledger)."""

    name = "fred_actuals"
    stale_hours = 30.0

    def __init__(self, http: HttpClient, api_key: str | None, calendar: dict, store,
                 max_age_days: int = 45):  # fmt: skip
        self.http, self.api_key, self.cal, self.store = http, api_key, calendar, store
        self.max_age_days = max_age_days
        self.rules = {ev["subcategory"]: (rel, ev) for rel in calendar.get("fred_releases") or []
                      for ev in rel["events"]}  # fmt: skip

    def _vintage(self, series: str, rt: date) -> pd.Series:
        p = self.http.get_json("/fred/series/observations", params={
            "series_id": series, "api_key": self.api_key, "file_type": "json",
            "realtime_start": rt.isoformat(), "realtime_end": rt.isoformat(),
            "observation_start": (rt - timedelta(days=500)).isoformat()},
            redact_params=("api_key",))  # fmt: skip
        try:
            obs = p["observations"]
        except (KeyError, TypeError) as exc:
            raise SchemaError(f"fred observations schema changed for {series}") from exc
        s = pd.Series({pd.Timestamp(o["date"]): pd.to_numeric(o["value"], errors="coerce")
                       for o in obs}).sort_index()  # fmt: skip
        return s.dropna()

    def pending(self, now: datetime) -> list[dict]:
        from market_signal.context.ledger import states_asof

        subs = sorted(self.rules)
        ids = self.store.con.execute(
            f"SELECT event_id FROM context_events WHERE scheduled AND subcategory IN "
            f"({','.join('?' * len(subs))}) AND event_time <= ? AND event_time >= ?",
            [*subs, now, now - timedelta(days=self.max_age_days)]).df()["event_id"].tolist()  # fmt: skip
        return [s for s in states_asof(self.store, now, event_ids=ids)
                if (s["attributes"] or {}).get("actual") is None]  # fmt: skip

    def fetch(self, now: datetime, last_state: dict) -> FetchResult:
        if not self.api_key:
            raise ProviderError("FRED_API_KEY not set")
        fr = FetchResult()
        for st in self.pending(now):
            fr.received += 1
            rel, ev = self.rules[st["subcategory"]]
            et = pd.Timestamp(st["event_time"])
            rd = et.tz_convert(rel["tz"]).date()
            after = transform(self._vintage(ev["fred_series"], rd), ev["transform"]).dropna()
            before = transform(self._vintage(ev["fred_series"], rd - timedelta(days=1)),
                               ev["transform"]).dropna()  # fmt: skip
            if after.empty or before.empty or after.index[-1] <= before.index[-1]:
                fr.filtered += 1  # FRED not updated yet: retried next run
                fr.notes.append(f"{st['subcategory']} {rd}: vintage not yet published")
                continue
            period = after.index[-1]
            prev_period = before.index[-1]
            attrs = MacroAttrs(
                series_key=ev["series_key"], unit=ev.get("unit"), importance=ev.get("importance", 1),
                period=f"{period:%Y-%m}", actual=round(float(after.iloc[-1]), 6),
                previous=round(float(before.iloc[-1]), 6),
                revision_of_previous=round(float(after.get(prev_period)), 6)
                if prev_period in after.index else None,
                markets=("USD", "rates"), timezone=rel["tz"])  # fmt: skip
            live = (pd.Timestamp(now) - et) <= pd.Timedelta(hours=LIVE_ACTUAL_HOURS)
            fr.observations.append(Observation(
                source_id="fred_alfred", source_type=SourceType.OFFICIAL_STATISTICS,
                source_ref=f"https://alfred.stlouisfed.org/series?seid={ev['fred_series']}",
                subcategory=st["subcategory"], title=st["title"], scheduled=True,
                event_time=et.to_pydatetime(), published_at=et.to_pydatetime(),
                confidence="OFFICIAL", market_wide=True, scope="systemic", country="US",
                attributes=attrs, dedup_key=st["dedup_key"], update_kind="value_release",
                observation_mode="live" if live else "historical"))  # fmt: skip
        fr.raw = self.http.drain()
        return fr


def load_calendar(settings: Any) -> dict:
    return settings.yaml("context/macro_calendar.yaml")
