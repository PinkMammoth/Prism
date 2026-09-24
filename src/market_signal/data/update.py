"""Update orchestration: fetch → drop incomplete bars → validate → archive → upsert.

Every (asset, timeframe) update is an ``ingestion_runs`` row. Failures in one series never
abort the others, but are reported and make the CLI exit non-zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd

from market_signal.config import Settings
from market_signal.data.calendars import close_times
from market_signal.data.http import ProviderError, RawPayload
from market_signal.data.providers.base import BarsResult
from market_signal.data.registry import ProviderRegistry, source_label
from market_signal.data.store import Store
from market_signal.data.validation import BatchRejected, validate_batch, validate_series
from market_signal.models.domain import Asset, Timeframe, utcnow

OVERLAP_BARS = 5  # re-fetch a few recent bars each update to catch provider revisions


@dataclass
class SeriesUpdateResult:
    symbol: str
    timeframe: str
    source: str
    status: str  # ok | skipped | failed
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0
    rejected: int = 0
    warnings: int = 0
    message: str = ""


@dataclass
class UpdateReport:
    results: list[SeriesUpdateResult] = field(default_factory=list)

    @property
    def failed(self) -> list[SeriesUpdateResult]:
        return [r for r in self.results if r.status == "failed"]


def _start_for(
    store: Store, asset: Asset, tf: Timeframe, source: str, settings: Settings
) -> datetime:
    last = store.last_bar_ts(asset.symbol, tf, source)
    if last is not None:
        return (last - timedelta(seconds=tf.seconds * OVERLAP_BARS)).to_pydatetime()
    backfill = (settings.providers.get("backfill") or {}).get(tf.value, "2010-01-01")
    return datetime.fromisoformat(str(backfill)).replace(tzinfo=UTC)


def ingest_bars(
    store: Store,
    settings: Settings,
    asset: Asset,
    result: BarsResult,
    *,
    run_id: str,
    fetched_at: datetime,
) -> SeriesUpdateResult:
    """Validate and persist a provider result. Shared by live updates and CSV import."""
    tf = result.timeframe
    bars = result.bars.copy()
    if not bars.empty:
        bars["close_time"] = close_times(bars["ts"], asset.calendar, tf)
        # never store a bar that has not closed yet
        incomplete = bars["close_time"] > pd.Timestamp(fetched_at)
        bars = bars[~incomplete].reset_index(drop=True)
    quality = settings.providers.get("quality") or {}
    vr = validate_batch(
        bars,
        symbol=asset.symbol,
        timeframe=tf,
        source=result.source,
        calendar=asset.calendar,
        run_id=run_id,
        max_invalid_fraction=float(quality.get("max_invalid_fraction", 0.01)),
    )
    counts = store.upsert_bars(asset.symbol, tf, result.source, vr.clean, run_id)
    store.upsert_actions(asset.symbol, result.source, result.actions, run_id)
    stored = store.get_bars(asset.symbol, tf, result.source)
    cal = asset.calendar.value
    series_issues = validate_series(
        stored,
        symbol=asset.symbol,
        timeframe=tf,
        source=result.source,
        calendar=asset.calendar,
        run_id=run_id,
        jump_abs_log_return=float((quality.get("jump_abs_log_return") or {}).get(cal, 0.35)),
        jump_mad_multiple=float(quality.get("jump_mad_multiple", 12)),
        stale_hours=None,  # staleness is a doctor concern, not an ingest concern
    )
    issues = (
        pd.concat([vr.issues, series_issues], ignore_index=True)
        if not series_issues.empty
        else vr.issues
    )
    # series-level issues are re-derived each run; keep only the latest run's copy
    store.con.execute(
        "DELETE FROM data_quality_issues WHERE symbol=? AND timeframe=? AND source=? "
        "AND check_name IN ('missing_bars','abnormal_jump','unexpected_session','empty')",
        [asset.symbol, tf.value, result.source],
    )
    store.log_issues(issues)
    n_warn = int((issues["severity"] == "warning").sum()) if not issues.empty else 0
    return SeriesUpdateResult(
        asset.symbol, tf.value, result.source, "ok",
        inserted=counts["inserted"], revised=counts["revised"], unchanged=counts["unchanged"],
        rejected=vr.n_errors, warnings=n_warn,
    )  # fmt: skip


def update_series(
    store: Store, settings: Settings, registry: ProviderRegistry, asset: Asset, tf: Timeframe
) -> SeriesUpdateResult:
    spec = asset.series_for(tf)
    if spec is None:
        return SeriesUpdateResult(asset.symbol, tf.value, "-", "skipped", message="not configured")
    source = source_label(spec.provider, tf)

    # explicit, logged provider changes — never stitched
    for existing in store.sources_for(asset.symbol, tf):
        if existing != source:
            store.record_series_change(
                asset.symbol, tf, existing, source,
                "configured provider differs from stored series; old series kept separately",
            )  # fmt: skip

    start = _start_for(store, asset, tf, source, settings)
    fetched_at = utcnow()
    run_id = store.start_run(
        spec.provider, f"bars_{tf.value}", asset.symbol, {"symbol": spec.symbol, "start": start}
    )
    raw: list[RawPayload] = []
    try:
        provider = registry.market(spec.provider)
        if tf == Timeframe.D1:
            result = provider.get_daily_bars(spec.symbol, start, fetched_at)
        else:
            result = provider.get_intraday_bars(spec.symbol, tf, start, fetched_at)
        raw = result.raw
        prev_fp = store.last_schema_fingerprint(spec.provider, f"bars_{tf.value}")
        archived = store.archive_raw(raw, run_id)
        res = ingest_bars(store, settings, asset, result, run_id=run_id, fetched_at=fetched_at)
        if prev_fp and result.schema_fingerprint and prev_fp != result.schema_fingerprint:
            res.message = (
                f"schema fingerprint changed {prev_fp}→{result.schema_fingerprint} (parsed OK)"
            )
        store.finish_run(
            run_id, status="ok", rows_received=len(result.bars),
            rows_written=res.inserted + res.revised, rows_rejected=res.rejected,
            archived=archived, schema_fingerprint=result.schema_fingerprint,
        )  # fmt: skip
        return res
    except (ProviderError, BatchRejected, KeyError) as exc:
        archived = store.archive_raw(raw, run_id) if raw else []
        store.finish_run(run_id, status="failed", archived=archived, error=str(exc)[:500])
        return SeriesUpdateResult(asset.symbol, tf.value, source, "failed", message=str(exc)[:300])


def update_prices(
    store: Store,
    settings: Settings,
    registry: ProviderRegistry,
    symbols: list[str] | None = None,
    timeframes: list[Timeframe] | None = None,
) -> UpdateReport:
    report = UpdateReport()
    store.upsert_assets(settings.universe.values())
    assets = [settings.asset(s) for s in symbols] if symbols else settings.active_assets()
    tfs = timeframes or [Timeframe.D1, Timeframe.H4]
    for asset in assets:
        for tf in tfs:
            if asset.series_for(tf) is None:
                continue
            report.results.append(update_series(store, settings, registry, asset, tf))
    return report
