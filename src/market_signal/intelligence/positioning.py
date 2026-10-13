"""Venue-separated, causal positioning and descriptive daily/weekly persistence."""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.context.positioning import (
    CROWDING_VERSION,
    TERMINOLOGY,
    _funding_block,
    _oi_block,
    crowding,
)
from market_signal.intelligence.clocks import utc
from market_signal.intelligence.definitions import DEFINITION, PERSISTENCE_VERSION


def _frame(rows, columns):
    return pd.DataFrame(rows, columns=columns)


def persistence(oi, funding, cutoff, *, settlement_hours):
    """Known-at-cutoff historical summaries; not contemporaneous historical eligibility.

    Complete hourly endpoint/interior OI samples and the full declared settlement grid
    are required for a trend/rate statement. Partial coverage remains visible, not zero.
    """
    t = utc(cutoff)
    result = {"version": PERSISTENCE_VERSION, "daily": {}, "weekly": {}}
    for name, hours in DEFINITION["persistence"]["windows_hours"].items():
        grid = pd.date_range(t - pd.Timedelta(hours=hours), t, freq="1h")
        vals = []
        for at in grid:
            prior = oi[oi["at"] <= at] if len(oi) else oi
            last = prior.iloc[-1] if len(prior) else None
            value = None
            if last is not None and at - last["at"] < pd.Timedelta(hours=1):
                v = last["open_interest"]
                if pd.notna(v) and np.isfinite(v) and v > 0:
                    value = float(v)
            vals.append(value)
        oi_complete = all(v is not None for v in vals)
        f = (
            funding[(funding["time"] > grid[0]) & (funding["time"] <= t)]
            if len(funding)
            else funding
        )
        expected = None
        matched = 0
        if settlement_hours:
            expected_grid = pd.date_range(
                grid[0].floor("D"), t.ceil("D"), freq=f"{settlement_hours}h"
            )
            expected_grid = expected_grid[(expected_grid > grid[0]) & (expected_grid <= t)]
            expected = len(expected_grid)
            matched = int(f["time"].isin(expected_grid).sum()) if len(f) else 0
        complete = bool(expected and matched == expected and len(f) == expected)
        rates = f["funding_rate"].to_numpy(float) if complete else np.array([])
        complete = complete and bool(np.isfinite(rates).all())
        result[name] = {
            "window_start": grid[0].isoformat(),
            "window_end": t.isoformat(),
            "oi_expected_samples": len(grid),
            "oi_known_samples": sum(v is not None for v in vals),
            "oi_complete": oi_complete,
            "oi_change_pct": (vals[-1] / vals[0] - 1) * 100 if oi_complete else None,
            "funding_settlement_hours": settlement_hours,
            "funding_expected_settlements": expected,
            "funding_known_settlements": len(f),
            "funding_complete": complete,
            "funding_positive_share": float((rates > 0).mean()) if complete else None,
            "funding_negative_share": float((rates < 0).mean()) if complete else None,
            "funding_zero_share": float((rates == 0).mean()) if complete else None,
            "funding_sum": float(rates.sum()) if complete else None,
        }
    return result


def positioning_summary(operation, asset, *, funding_cadence_hours=None):
    t = operation.cutoff
    cadence = {"hyperliquid": 1, "binance": None}
    cadence.update(funding_cadence_hours or {})
    if set(cadence) != {"hyperliquid", "binance"} or any(
        value is not None and (type(value) is not int or value not in (1, 4, 8))
        for value in cadence.values()
    ):
        raise ValueError("funding cadence must explicitly specify 1, 4, 8 hours or unknown")
    if cadence["hyperliquid"] != 1:
        raise ValueError("this definition requires hourly Hyperliquid funding")
    since = t - pd.Timedelta(days=DEFINITION["positioning_lookback_days"])
    oi = {"hyperliquid": [], "binance": []}
    refs = {"hyperliquid": [], "binance": []}
    for table, at_column, market_column in (
        ("context_hl_oi_hourly", "captured_at", "captured_at"),
        ("perp_snapshots", "snapshot_at", "snapshot_at"),
        ("perp_oi_history", "ingested_at", "observed_at"),
    ):
        where = f"coin=? AND {market_column}>=? AND {market_column}<=?"
        if table == "perp_snapshots":
            where += " AND source='hyperliquid'"
        if table == "perp_oi_history":
            where += " AND source='binance' AND period='1h'"
        for row in operation.rows(
            table, where=where, params=[asset, since, t], order=market_column
        ):
            venue = "binance" if table == "perp_oi_history" else "hyperliquid"
            market = utc(row[market_column]) + (
                pd.Timedelta(hours=1) if venue == "binance" else pd.Timedelta(0)
            )
            observed = max(utc(row[at_column]), market)
            if table == "perp_oi_history":
                # The legacy table retains current values, not prior OI revisions.
                # Never expose a later replacement under its original ingestion clock.
                observed = max(observed, utc(row["updated_at"]))
            key = [asset, venue, str(market), table]
            eid = operation.include(table, key, row, observed, market_time=market)
            if eid:
                oi[venue].append({"at": market, **row})
                refs[venue].append(eid)
    ratios = []
    for row in operation.rows(
        "context_ls_ratios",
        where="coin=? AND source='binance' AND period='1h' AND observed_at>=? AND observed_at<=?",
        params=[asset, since, t],
        order="observed_at,metric",
    ):
        end = utc(row["observed_at"]) + pd.Timedelta(hours=1)
        eid = operation.include(
            "context_ls_ratios",
            [asset, row["metric"], str(end)],
            row,
            max(utc(row["ingested_at"]), end),
            market_time=end,
        )
        if eid:
            ratios.append({"at": end, **row})
            refs["binance"].append(eid)
    funding = {"hyperliquid": [], "binance": []}
    for row in operation.rows(
        "perp_funding",
        where="coin=? AND time>=? AND time<=?",
        params=[asset, t - pd.Timedelta(days=120), t],
        order="source,time",
    ):
        venue = row["source"]
        if venue not in funding:
            continue
        observed = operation.observed_ingestion(row)
        eid = operation.include(
            "perp_funding",
            [asset, venue, str(utc(row["time"]))],
            row,
            observed,
            market_time=row["time"],
        )
        if eid:
            funding[venue].append({**row, "time": utc(row["time"])})
            refs[venue].append(eid)
    venues = {}
    for venue in ("hyperliquid", "binance"):
        o = pd.DataFrame(oi[venue])
        if len(o):
            o = o.sort_values("at", kind="stable").drop_duplicates("at", keep="first")
        else:
            o = _frame([], ["at", "open_interest", "oi_notional"])
        f = _frame(funding[venue], ["time", "funding_rate"])
        block = (
            _oi_block(o, t, pd.Timedelta(hours=3), pd.Timedelta(hours=1))
            if len(o)
            else {"open_interest": None}
        )
        fs = _funding_block(f, t, 1)
        fs.pop(
            "funding_annualised", None
        )  # no annualization with an unknown/variable contract cadence
        block.update(fs)
        block["stale"] = not len(o) or t - o["at"].iloc[-1] > pd.Timedelta(hours=3)
        if venue == "hyperliquid" and len(o) and not block["stale"]:
            last = o.iloc[-1]
            mark, oracle = last.get("mark_px"), last.get("oracle_px")
            block["basis_mark_vs_oracle_bps"] = (
                (float(mark) / float(oracle) - 1) * 10000
                if pd.notna(mark) and pd.notna(oracle) and oracle > 0
                else None
            )
            block["predicted_funding_hourly"] = last.get("funding_rate")
        if venue == "binance":
            for metric in ("global_account", "top_account", "top_position", "taker_volume"):
                eligible = [r for r in ratios if r["metric"] == metric]
                block[metric] = (
                    {
                        k: eligible[-1][k]
                        for k in ("ratio", "long_share", "short_share", "buy_volume", "sell_volume")
                    }
                    if eligible and t - eligible[-1]["at"] <= pd.Timedelta(hours=3)
                    else None
                )
            account = [r["ratio"] for r in ratios if r["metric"] == "global_account"]
            prior = pd.Series(account[:-1], dtype=float).dropna()
            current = account[-1] if account and block["global_account"] else None
            block["ls_account_pct_30d"] = (
                float((prior < current).mean() + 0.5 * (prior == current).mean())
                if current is not None and len(prior) >= 20
                else None
            )
        block["crowding"] = crowding(block)
        block["persistence"] = persistence(o, f, t, settlement_hours=cadence[venue])
        block["evidence_ids"] = sorted(refs[venue])
        venues[venue] = block
    return {
        "version": "intelligence_positioning_v1",
        "crowding_version": CROWDING_VERSION,
        "as_of": t.isoformat(),
        "asset": asset,
        "venues": venues,
        "terminology": TERMINOLOGY,
        "funding_cadence_hours": cadence,
        "semantics": "venue-separated descriptive conditions, not direction or sizing",
    }
