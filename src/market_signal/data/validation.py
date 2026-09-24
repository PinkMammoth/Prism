"""Data-quality checks. Fail loudly rather than store nonsense.

``validate_batch`` runs on every incoming batch *before* it is written:
  - errors (rejected rows): non-finite/non-positive prices, high < max(open, close, low),
    low > min(open, close), negative volume, misaligned timestamps, conflicting duplicates.
  - if more than ``max_invalid_fraction`` of rows are invalid the whole batch is refused
    (``BatchRejected``): that usually means a schema/units change, not bad ticks.
``validate_series`` runs on stored series: gaps, abnormal jumps, staleness.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.data.calendars import expected_bar_opens, nyse_open_utc
from market_signal.models.domain import Calendar, Severity, Timeframe, utcnow

ISSUE_COLUMNS = [
    "run_id",
    "symbol",
    "timeframe",
    "source",
    "check_name",
    "severity",
    "ts",
    "detail",
    "detected_at",
]


class BatchRejected(ValueError):
    pass


@dataclass
class ValidationResult:
    clean: pd.DataFrame
    issues: pd.DataFrame

    @property
    def n_errors(self) -> int:
        return (
            int((self.issues["severity"] == Severity.ERROR.value).sum())
            if not self.issues.empty
            else 0
        )


def _issues(rows: list[dict], ctx: dict) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=ISSUE_COLUMNS)
    df = pd.DataFrame(rows)
    for k, v in ctx.items():
        df[k] = v
    df["detected_at"] = utcnow()
    return df[ISSUE_COLUMNS]


def _misaligned(ts: pd.Series, calendar: Calendar, timeframe: Timeframe) -> pd.Series:
    if calendar == Calendar.CRYPTO_24_7 or timeframe != Timeframe.D1:
        secs = (ts - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
        return (secs % timeframe.seconds) != 0
    # NYSE daily: ts must equal the 09:30 NY open of that local date
    local = ts.dt.tz_convert("America/New_York")
    expected = pd.Series([pd.Timestamp(nyse_open_utc(d)) for d in local.dt.date], index=ts.index)
    return ts != expected


def validate_batch(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe,
    source: str,
    calendar: Calendar,
    run_id: str,
    max_invalid_fraction: float = 0.01,
) -> ValidationResult:
    ctx = {"run_id": run_id, "symbol": symbol, "timeframe": timeframe.value, "source": source}
    rows: list[dict] = []
    if bars.empty:
        return ValidationResult(bars, _issues(rows, ctx))
    if bars["ts"].dt.tz is None or str(bars["ts"].dt.tz) != "UTC":
        raise BatchRejected(f"{symbol}: timestamps must be tz-aware UTC (got {bars['ts'].dt.tz})")

    df = bars.copy()
    bad = pd.Series(False, index=df.index)

    def flag(mask: pd.Series, check: str, detail: str, severity: Severity = Severity.ERROR) -> None:
        nonlocal bad
        for ts in df.loc[mask, "ts"]:
            rows.append(
                {"check_name": check, "severity": severity.value, "ts": ts, "detail": detail}
            )
        if severity == Severity.ERROR:
            bad = bad | mask

    prices = df[["open", "high", "low", "close"]]
    flag(
        ~np.isfinite(prices).all(axis=1) | (prices <= 0).any(axis=1),
        "price_invalid",
        "non-finite or non-positive price",
    )
    flag(
        df["high"] < df[["open", "close", "low"]].max(axis=1) - 1e-12,
        "ohlc_high",
        "high below open/close/low",
    )
    flag(df["low"] > df[["open", "close"]].min(axis=1) + 1e-12, "ohlc_low", "low above open/close")
    flag(df["volume"] < 0, "volume_negative", "negative volume")
    flag(
        _misaligned(df["ts"], calendar, timeframe),
        "ts_misaligned",
        f"timestamp not aligned to {calendar}/{timeframe} bar opens",
    )
    if (df["volume"] == 0).any():
        flag(df["volume"] == 0, "volume_zero", "zero volume bar", Severity.WARNING)

    dup = df.duplicated("ts", keep=False)
    if dup.any():
        conflicting = (
            df[dup]
            .groupby("ts")[["open", "high", "low", "close", "volume"]]
            .nunique()
            .gt(1)
            .any(axis=1)
        )
        conflict_ts = set(conflicting[conflicting].index)
        flag(df["ts"].isin(conflict_ts), "duplicate_conflict", "conflicting duplicate bars")
        flag(
            dup & ~df["ts"].isin(conflict_ts),
            "duplicate_identical",
            "identical duplicate dropped",
            Severity.WARNING,
        )
        df = df[~(df.duplicated("ts", keep="first") & ~df["ts"].isin(conflict_ts))]
        bad = bad.reindex(df.index, fill_value=False)

    n_bad = int(bad.sum())
    if len(df) and n_bad / len(df) > max_invalid_fraction and n_bad > 1:
        raise BatchRejected(
            f"{symbol} {timeframe} {source}: {n_bad}/{len(df)} invalid rows exceeds "
            f"{max_invalid_fraction:.1%}; likely a schema/units change — refusing to store"
        )
    return ValidationResult(df[~bad].reset_index(drop=True), _issues(rows, ctx))


def validate_series(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe,
    source: str,
    calendar: Calendar,
    run_id: str = "",
    jump_abs_log_return: float = 0.35,
    jump_mad_multiple: float = 12.0,
    stale_hours: float | None = None,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Checks on a stored series: missing bars, abnormal jumps, staleness (warnings)."""
    ctx = {"run_id": run_id, "symbol": symbol, "timeframe": timeframe.value, "source": source}
    rows: list[dict] = []
    if bars.empty:
        rows.append(
            {
                "check_name": "empty",
                "severity": Severity.ERROR.value,
                "ts": None,
                "detail": "no bars stored",
            }
        )
        return _issues(rows, ctx)

    ts = pd.DatetimeIndex(bars["ts"])
    if timeframe != Timeframe.W1 and not (calendar == Calendar.NYSE and timeframe != Timeframe.D1):
        expected = expected_bar_opens(calendar, timeframe, ts.min(), ts.max())
        missing = expected.difference(ts)
        if len(missing):
            # summarise contiguous runs rather than one issue per bar
            runs = _contiguous_runs(missing, expected)
            for start, end, n in runs[:50]:
                rows.append({
                    "check_name": "missing_bars",
                    "severity": Severity.WARNING.value,
                    "ts": start,
                    "detail": f"{n} missing bar(s) {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}",
                })  # fmt: skip
        extra = ts.difference(expected)
        for t in extra[:20]:
            rows.append({"check_name": "unexpected_session", "severity": Severity.WARNING.value, "ts": t,
                         "detail": "bar on a non-session date (calendar mismatch?)"})  # fmt: skip

    logret = np.log(bars["close"]).diff()
    mad = (
        (logret - logret.rolling(60, min_periods=20).median())
        .abs()
        .rolling(60, min_periods=20)
        .median()
    )
    jump = (logret.abs() > jump_abs_log_return) & (
        logret.abs() > jump_mad_multiple * 1.4826 * mad.shift(1)
    )
    for i in np.flatnonzero(jump.fillna(False).to_numpy()):
        rows.append({"check_name": "abnormal_jump", "severity": Severity.WARNING.value, "ts": bars["ts"].iloc[i],
                     "detail": f"log return {logret.iloc[i]:+.3f} (verify split/bad tick)"})  # fmt: skip

    if stale_hours is not None:
        now = now or pd.Timestamp(utcnow())
        last_close = pd.Timestamp(bars["close_time"].max()) if "close_time" in bars else ts.max()
        age_h = (now - last_close).total_seconds() / 3600
        if age_h > stale_hours:
            rows.append({"check_name": "stale", "severity": Severity.WARNING.value, "ts": last_close,
                         "detail": f"last bar closed {age_h:.0f}h ago (threshold {stale_hours}h)"})  # fmt: skip
    return _issues(rows, ctx)


def _contiguous_runs(missing: pd.DatetimeIndex, expected: pd.DatetimeIndex) -> list[tuple]:
    pos = expected.get_indexer(missing)
    runs, start = [], 0
    for i in range(1, len(pos) + 1):
        if i == len(pos) or pos[i] != pos[i - 1] + 1:
            runs.append((missing[start], missing[i - 1], i - start))
            start = i
    return runs
