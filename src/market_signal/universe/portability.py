"""Hypothesis audit. Scientific verdicts and frozen feature definitions are never changed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from market_signal.paper.v2 import spec
from market_signal.universe.policy import CORE, POLICY


def mapping():
    out = []
    for h in spec.bootstrap():
        if h.source_phase == 22:
            classification, requirement = "PORTABLE_WITH_ASSET_DATA_GATE", "TECHNICAL_WARMUP"
        elif h.source_phase == 23 and h.source_key == "crowding_vulnerability":
            # crowding uses cross-venue Binance ratios and asset-context registration.
            classification, requirement = "CORE_ONLY", "BINANCE_CORROBORATION_UNVALIDATED"
        elif h.source_phase == 23:
            classification, requirement = (
                "NOT_YET_VALIDATED",
                "POSITIONING_CONTEXT_ASSET_REGISTRATION",
            )
        else:
            classification, requirement = "PORTABLE_WITH_ASSET_DATA_GATE", "MICRO_WARMUP"
        out.append(
            {
                "hypothesis_id": h.hypothesis_id,
                "key": h.source_key,
                "name": h.name,
                "source_phase": h.source_phase,
                "classification": classification,
                "requirement": requirement,
                "scientific_verdict": h.scientific_verdict,
                "feature_version": h.feature_version,
            }
        )
    from market_signal.paper.nimble.event_spec import bootstrap

    for h in bootstrap():
        out.append(
            {
                "hypothesis_id": h.hypothesis_id,
                "key": h.source_key,
                "name": h.name,
                "source_phase": 30,
                "classification": "CORE_ONLY",
                "requirement": "FROZEN_EVENT_ASSET_MAPPING",
                "feature_version": h.feature_version,
            }
        )
    return out


def availability(store, asset, now, rich_since):
    """Conservative radar gates; entry adapter must still check exact current feature readiness.

    Micro history is counted as complete quarter-hours after current rich-collection start.
    No old discontinuous collection segment is reused on satellite re-entry.
    """
    from market_signal.intraday.align import Availability
    from market_signal.intraday.bars import load_bars
    from market_signal.models.domain import Timeframe
    from market_signal.paper.v2.data import strategy, technical_ready
    from market_signal.research.discovery.primitives import Frame
    from market_signal.research.structure.series import BarSeries

    at = datetime.fromtimestamp(now, UTC)
    is_core = asset in CORE
    bars = load_bars(
        store,
        "hyperliquid",
        asset,
        Timeframe.H1,
        start=at - timedelta(days=POLICY.technical_history_days),
        end=at,
        known_at=at,
    )
    frame = None
    technical_fresh = False
    if len(bars):
        series = BarSeries.from_frame(
            bars,
            venue="hyperliquid",
            coin=asset,
            timeframe="1h",
            availability=Availability.observed(),
        )
        frame = Frame.from_series(series, readonly=True)
        technical_fresh = at.replace(minute=0, second=0, microsecond=0) == series.close_time[-1]
    after = (
        datetime.fromtimestamp(rich_since or now, UTC)
        if not is_core
        else at - timedelta(days=POLICY.micro_history_days)
    )
    grid_at = at.replace(minute=(at.minute // 15) * 15, second=0, microsecond=0)
    row = store.con.execute(
        "SELECT count(*) FILTER (WHERE w + INTERVAL 15 MINUTE < ?), "
        "coalesce(bool_or(w + INTERVAL 15 MINUTE = ?), false) FROM "
        "(SELECT date_trunc('hour',minute_open) + "
        "floor(date_part('minute',minute_open)/15)*INTERVAL 15 MINUTE AS w "
        "FROM microstructure_minutes WHERE coin=? AND status='COMPLETE' "
        "AND feature_version='microstructure_1m_v1' AND minute_open>=? "
        "AND minute_open<? AND ingested_at<=? GROUP BY w HAVING count(*)=15)",
        [
            grid_at,
            grid_at,
            asset,
            max(after, at - timedelta(days=POLICY.micro_history_days)),
            at,
            at,
        ],
    ).fetchone()
    prior_complete = row[0] if row else 0
    current_micro_complete = bool(row and row[1])
    micro = prior_complete >= POLICY.micro_prior_windows
    lp = store.con.execute(
        "SELECT status FROM microstructure_lp_thresholds WHERE coin=? AND day=? AND computed_at<=? ORDER BY day DESC LIMIT 1",
        [asset, at.date(), at],
    ).fetchone()
    large_print = bool(lp and lp[0] == "ok")
    positioning = (
        store.con.execute(
            "SELECT count(*) FROM context_hl_oi_hourly WHERE coin=? AND captured_at BETWEEN ? AND ?",
            [asset, at - timedelta(minutes=POLICY.positioning_max_history_minutes), at],
        ).fetchone()[0]
        >= POLICY.positioning_observations
    )
    settled = store.con.execute(
        "SELECT max(time) FROM perp_funding WHERE source='hyperliquid' AND coin=? AND available_at<=?",
        [asset, at],
    ).fetchone()[0]
    funding_ready = settled is not None and at - settled <= timedelta(
        minutes=POLICY.funding_max_age_minutes
    )
    hypotheses = []
    for entry in mapping():
        reason = []
        if not is_core and entry["classification"] in ("CORE_ONLY", "NOT_YET_VALIDATED"):
            reason.append(entry["requirement"])
        if entry["source_phase"] == 22:
            h = next(h for h in spec.bootstrap() if h.hypothesis_id == entry["hypothesis_id"])
            if frame is None or not technical_ready(frame, strategy(h.parameters["strategy"])):
                reason.append("TECHNICAL_WARMUP")
            if not technical_fresh:
                reason.append("DATA_STALE")
        elif entry["source_phase"] == 23 and not positioning:
            reason.append("POSITIONING_WARMUP")
        elif entry["source_phase"] == 24:
            if not micro:
                reason.append("MICRO_WARMUP")
            if not current_micro_complete:
                reason.append("DATA_DEGRADED")
        elif entry["source_phase"] == 30:
            reason.append("FROZEN_EVENT_ASSET_MAPPING")
        if not is_core and not funding_ready:
            reason.append("FUNDING_UNAVAILABLE")
        hypotheses.append(
            {
                **entry,
                "available": not reason,
                "reasons": reason,
                "gate": "history_gate_plus_exact_predicate_at_execution",
            }
        )
    return {
        "warmup": {
            "technical": all(h["available"] for h in hypotheses if h["source_phase"] == 22),
            "positioning": positioning,
            "microstructure": micro,
            "large_print": large_print,
        },
        "complete_micro_windows": prior_complete,
        "hypotheses": hypotheses,
    }
