"""Macro providers: FRED/ALFRED (point-in-time vintages) and EIA (weekly petroleum).

Output contract for ``get_series`` (``MACRO_COLUMNS``):
    series_id, obs_date (date), value (float, NaN = FRED '.'), realtime_start (date),
    realtime_end (date|None), available_at (UTC timestamp), pit_method (str)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.data.providers.base import HttpProvider
from market_signal.data.providers.equities import MissingApiKey
from market_signal.models.domain import PitMethod

MACRO_COLUMNS = [
    "series_id", "obs_date", "value", "realtime_start", "realtime_end", "available_at", "pit_method",
]  # fmt: skip


def _utc_midnight(d: pd.Series) -> pd.Series:
    return pd.to_datetime(d).dt.tz_localize("UTC")


class FredProvider(HttpProvider):
    """FRED API. Revised series are fetched over the full real-time period (ALFRED), so
    each row carries the date that value was first published."""

    name = "fred"
    PAGE = 100000

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key

    def _observations(self, series_id: str, extra: dict[str, Any]) -> list[dict]:
        if not self.api_key:
            raise MissingApiKey(
                "FRED_API_KEY not set (free: https://fred.stlouisfed.org/docs/api/api_key.html)"
            )
        out: list[dict] = []
        offset = 0
        while True:
            payload = self.http.get_json(
                "/fred/series/observations",
                params={"series_id": series_id, "api_key": self.api_key, "file_type": "json",
                        "limit": self.PAGE, "offset": offset, **extra},
                redact_params=("api_key",),
            )  # fmt: skip
            if "error_message" in payload:
                raise ProviderError(f"fred {series_id}: {payload['error_message']}")
            try:
                obs = payload["observations"]
                count = int(payload.get("count", len(obs)))
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaError(f"fred observations schema changed for {series_id}") from exc
            out.extend(obs)
            offset += len(obs)
            if not obs or offset >= count:
                return out

    @staticmethod
    def parse(series_id: str, obs: list[dict], pit_policy: str, lag_days: int = 2) -> pd.DataFrame:
        if not obs:
            return pd.DataFrame(columns=MACRO_COLUMNS)
        df = pd.DataFrame(obs)
        need = {"date", "value", "realtime_start", "realtime_end"}
        if not need.issubset(df.columns):
            raise SchemaError(f"fred {series_id}: missing {need - set(df.columns)}")
        df["series_id"] = series_id
        df["obs_date"] = pd.to_datetime(df["date"]).dt.date
        # FRED encodes missing as "." — it stays missing (NaN), never zero
        df["value"] = pd.to_numeric(df["value"].replace(".", np.nan), errors="coerce")
        df["realtime_start"] = pd.to_datetime(df["realtime_start"]).dt.date
        rt_end = pd.to_datetime(df["realtime_end"].replace("9999-12-31", None), errors="coerce")
        df["realtime_end"] = rt_end.dt.date.where(rt_end.notna(), None)
        if pit_policy == "vintage":
            df["available_at"] = _utc_midnight(df["realtime_start"]) + pd.Timedelta(days=1)
            df["pit_method"] = PitMethod.VINTAGE.value
        else:
            df["available_at"] = _utc_midnight(df["obs_date"]) + pd.Timedelta(days=lag_days)
            df["realtime_start"] = df["obs_date"]
            df["pit_method"] = (
                PitMethod.MARKET_CLOSE.value
                if pit_policy == "market_close"
                else PitMethod.RECONSTRUCTED.value
            )
        return df[MACRO_COLUMNS].reset_index(drop=True)

    def get_series(
        self,
        series_id: str,
        start: datetime | None = None,
        *,
        pit_policy: str = "market_close",
        lag_days: int = 2,
    ) -> pd.DataFrame:
        extra: dict[str, Any] = {}
        if start is not None:
            extra["observation_start"] = start.date().isoformat()
        if pit_policy == "vintage":
            extra |= {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31"}
        obs = self._observations(series_id, extra)
        return self.parse(series_id, obs, pit_policy, lag_days)


class EiaProvider(HttpProvider):
    """EIA API v2 via legacy series ids. Availability = period end + lag_days (release rule)."""

    name = "eia"

    def __init__(self, http: HttpClient, api_key: str | None):
        super().__init__(http)
        self.api_key = api_key

    @staticmethod
    def parse(series_id: str, payload: Any, lag_days: int) -> pd.DataFrame:
        try:
            data = payload["response"]["data"]
        except (KeyError, TypeError) as exc:
            raise SchemaError(f"eia {series_id}: unexpected payload {str(payload)[:200]}") from exc
        if not data:
            return pd.DataFrame(columns=MACRO_COLUMNS)
        df = pd.DataFrame(data)
        if not {"period", "value"}.issubset(df.columns):
            raise SchemaError(f"eia {series_id}: missing period/value")
        df["series_id"] = series_id
        df["obs_date"] = pd.to_datetime(df["period"]).dt.date
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["realtime_start"] = df["obs_date"]
        df["realtime_end"] = None
        df["available_at"] = _utc_midnight(df["obs_date"]) + pd.Timedelta(days=lag_days)
        df["pit_method"] = PitMethod.RELEASE_RULE.value
        return (
            df[MACRO_COLUMNS]
            .drop_duplicates(["obs_date"])
            .sort_values("obs_date")
            .reset_index(drop=True)
        )

    def get_series(
        self, series_id: str, start: datetime | None = None, *, route: str = "", lag_days: int = 6
    ) -> pd.DataFrame:
        if not self.api_key:
            raise MissingApiKey(
                "EIA_API_KEY not set (free: https://www.eia.gov/opendata/register.php)"
            )
        params = {"api_key": self.api_key}
        if start is not None:
            params["start"] = start.date().isoformat()
        payload = self.http.get_json(
            f"/v2/seriesid/{route or series_id}", params=params, redact_params=("api_key",)
        )
        return self.parse(series_id, payload, lag_days)
