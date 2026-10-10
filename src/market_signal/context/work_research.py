"""Causal post-availability descriptive reactions; never PnL or execution hypotheses.

Prism's fast event layer studies how markets react to information, not whether the
market's interpretation was ultimately correct. Denials do not erase observed reactions.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import numpy as np
import pandas as pd

from market_signal.research.lab.common import canonical_json

VERSION = "work_event_reaction_v1"
HORIZONS = (1, 5, 15, 30, 60, 120, 240)


def enroll(store, completion, receipt):
    """Freeze the first Work availability per logical event/asset, including prior-known events."""
    eid, at = completion["logical_event_id"], completion["context_available_at"]
    links = store.con.execute(
        "SELECT asset,link_type FROM context_asset_links WHERE event_id=? AND linked_at<=?",
        [eid, at],
    ).fetchall()
    p = receipt["payload"]
    with store.transaction():
        for asset in sorted({a for a, _ in links}):
            payload = {
                "version": VERSION,
                "provider": completion["provider"],
                "direct_assets": p["item"].get("assets", []),
                "market_wide": p["item"].get("market_wide", False),
                "link_types": sorted({k for a, k in links if a == asset}),
                "subcategory": p["item"]["subcategory"],
                "auth_identity": receipt["auth_identity"],
                "gateway_received_at": receipt["gateway_received_at"],
                "attribution": "first_work_availability_not_proof_of_market_causation",
            }
            store.con.execute(
                "INSERT INTO context_work_research_pending VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                [eid, asset, at, receipt["receipt_id"], canonical_json(payload)],
            )


def measure(store, asset, available, horizon, now):
    t0, end, t = (
        pd.Timestamp(available),
        pd.Timestamp(available) + pd.Timedelta(minutes=horizon),
        pd.Timestamp(now),
    )
    # Prospective minute data only. No 15-minute interpolation for 1/5-minute results.
    bars = store.con.execute(
        "SELECT minute_open,revision,status,trade_cov,last_px,high_px,low_px,buy_vol,sell_vol,"
        "spread_bps_mean,bid5_mean,ask5_mean,oi_end,funding_end,ingested_at,finalized_at "
        "FROM microstructure_minutes WHERE coin=? AND minute_open>=? AND minute_open<=? "
        "AND ingested_at<=? AND finalized_at<=? "
        "QUALIFY row_number() OVER(PARTITION BY minute_open ORDER BY revision DESC)=1 "
        "ORDER BY minute_open",
        [
            asset,
            (t0 - pd.Timedelta(minutes=max(horizon, 2) + 2)).to_pydatetime(),
            end.to_pydatetime(),
            t.to_pydatetime(),
            t.to_pydatetime(),
        ],
    ).df()
    if bars.empty:
        return {"status": "no_minute_data"}
    bars["open"] = pd.to_datetime(bars.minute_open, utc=True)
    bars["end"] = bars["open"] + pd.Timedelta(minutes=1)
    valid = bars[
        (bars.status == "COMPLETE")
        & (bars.trade_cov >= 0.9)
        & (bars.last_px > 0)
        & (bars.low_px > 0)
        & (bars.high_px >= bars.last_px)
        & (bars.low_px <= bars.last_px)
        & np.isfinite(bars.last_px)
        & np.isfinite(bars.high_px)
        & np.isfinite(bars.low_px)
    ]
    base = valid[
        (valid.end <= t0)
        & (pd.to_datetime(valid.ingested_at, utc=True) <= t0)
        & (pd.to_datetime(valid.finalized_at, utc=True) <= t0)
    ]
    if base.empty or t0 - base.end.iloc[-1] > pd.Timedelta(seconds=90):
        return {"status": "no_causal_reference"}
    b0 = base.iloc[-1]
    terminal = valid[(valid.end >= end) & (valid.end <= end + pd.Timedelta(seconds=60))]
    if terminal.empty:
        return {"status": "endpoint_unavailable"}
    b1 = terminal.iloc[0]
    # Require every intervening minute, including the bar crossing availability, for return
    # coverage. Extrema/volume exclude that crossing bar (pre-availability contamination).
    cover = valid[(valid.open > b0["open"]) & (valid.open <= b1["open"])]
    expected = int((b1["open"] - b0["open"]).total_seconds() / 60)
    if len(cover) != expected:
        return {"status": "minute_coverage_gap"}
    post = cover[(cover.open >= t0) & (cover.end <= end)]
    pre = valid[(valid.end <= t0) & (valid.open >= t0 - pd.Timedelta(minutes=horizon))]
    p0 = float(b0.last_px)
    lr = np.log(post.last_px).diff().dropna()
    pre_volume = float((pre.buy_vol + pre.sell_vol).sum()) if len(pre) else None
    post_volume = float((post.buy_vol + post.sell_vol).sum()) if len(post) else None

    def number(x):
        return None if pd.isna(x) or not np.isfinite(x) else float(x)

    oi0, oi1 = number(b0.oi_end), number(b1.oi_end)
    return {
        "status": "ok",
        "price_basis": "hyperliquid_complete_minute_last_trade",
        "reference_price": p0,
        "reference_close_at": b0.end.isoformat(),
        "outcome_price": float(b1.last_px),
        "outcome_close_at": b1.end.isoformat(),
        "endpoint_delay_s": (b1.end - end).total_seconds(),
        "reference_age_s": (t0 - b0.end).total_seconds(),
        "return_pct": (float(b1.last_px) / p0 - 1) * 100,
        "up_excursion_pct": number((post.high_px.max() / p0 - 1) * 100) if len(post) else None,
        "down_excursion_pct": number((post.low_px.min() / p0 - 1) * 100) if len(post) else None,
        "mfe_long_pct": max(0, float((post.high_px.max() / p0 - 1) * 100)) if len(post) else None,
        "mae_long_pct": min(0, float((post.low_px.min() / p0 - 1) * 100)) if len(post) else None,
        "mfe_short_pct": max(0, float((1 - post.low_px.min() / p0) * 100)) if len(post) else None,
        "mae_short_pct": min(0, float((1 - post.high_px.max() / p0) * 100)) if len(post) else None,
        "realized_vol_pct": number(np.sqrt(np.square(lr).sum()) * 100) if len(lr) >= 2 else None,
        "full_post_minutes": len(post),
        "partial_first_minute_excluded": True,
        "volume_ratio": post_volume / len(post) / (pre_volume / len(pre))
        if post_volume is not None and pre_volume and len(pre) >= max(1, horizon - 1)
        else None,
        "oi_change_pct": (oi1 / oi0 - 1) * 100 if oi0 and oi1 is not None else None,
        "funding_reference": number(b0.funding_end),
        "funding_endpoint": number(b1.funding_end),
        "spread_reference_bps": number(b0.spread_bps_mean),
        "spread_endpoint_bps": number(b1.spread_bps_mean),
        "bid5_endpoint": number(b1.bid5_mean),
        "ask5_endpoint": number(b1.ask5_mean),
        "market_data_available_at": pd.Timestamp(cover.ingested_at.max()).isoformat(),
        "minute_revisions": [int(b0.revision), *[int(r) for r in cover.revision]],
    }


def collect(store, *, now, limit=50, budget_seconds=2.0):
    """Bounded, restart-safe catch-up; immutable outcomes, missingness after 24h grace."""
    rows = store.con.execute(
        "SELECT * FROM (SELECT p.event_id,p.asset,p.available_at,p.receipt_id,p.payload "
        "FROM context_work_research_pending p LEFT JOIN context_work_reactions r "
        "ON p.event_id=r.event_id AND p.asset=r.asset GROUP BY ALL "
        "HAVING count(r.horizon_minutes)<7) q ORDER BY hash(event_id,asset,floor(epoch(?) / 60)) LIMIT ?",
        [now, limit],
    ).fetchall()
    count = 0
    started = time.monotonic()
    for eid, asset, available, rid, raw in rows:
        for h in HORIZONS:
            if time.monotonic() - started > budget_seconds:
                return {
                    "recorded": count,
                    "examined_event_assets": len(rows),
                    "descriptive_only": True,
                    "budget_exhausted": True,
                }
            if pd.Timestamp(now) < pd.Timestamp(available) + pd.Timedelta(minutes=h):
                continue
            if store.con.execute(
                "SELECT 1 FROM context_work_reactions WHERE event_id=? AND asset=? AND horizon_minutes=?",
                [eid, asset, h],
            ).fetchone():
                continue
            outcome = measure(store, asset, available, h, now)
            if outcome["status"] != "ok" and now < available + timedelta(minutes=h, hours=24):
                continue
            payload = (
                json.loads(raw)
                | outcome
                | {
                    "available_at": available.isoformat(),
                    "receipt_id": rid,
                    "horizon_minutes": h,
                    "research_only": True,
                    "truth_evolution_does_not_erase_reaction": True,
                }
            )
            with store.transaction():
                store.con.execute(
                    "INSERT INTO context_work_reactions VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [eid, asset, h, now, canonical_json(payload)],
                )
            count += 1
    return {"recorded": count, "examined_event_assets": len(rows), "descriptive_only": True}
