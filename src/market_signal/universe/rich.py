"""Bounded satellite bar/funding preparation using free public REST data.

Microstructure still arrives through prospective subscriptions. Candle backfills are
available only at ingestion, and prospective OI is never historically manufactured.
"""

from datetime import UTC, datetime

import pandas as pd

from market_signal.intraday.bars import upsert_bars
from market_signal.models.domain import Timeframe
from market_signal.universe.policy import CORE, POLICY


def fetch(http, assets, now, *, initialized=(), hour_due=True):
    out = {}
    for asset in sorted(set(assets) - set(CORE)):
        if asset in initialized and not hour_due:
            continue
        start = now - (POLICY.technical_history_days * 86400 if asset not in initialized else 7200)
        record = {"bars": [], "funding": [], "errors": []}
        try:
            payload = http.post_json(
                "/info",
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": asset,
                        "interval": "1h",
                        "startTime": start * 1000,
                        "endTime": now * 1000,
                    },
                },
            )
            for c in payload:
                if (
                    c.get("s") != asset
                    or c.get("i") != "1h"
                    or int(c["T"]) != int(c["t"]) + 3600000 - 1
                ):
                    raise ValueError("unexpected satellite hourly candle schema")
                if int(c["T"]) < now * 1000:
                    record["bars"].append(
                        {
                            "open_time": pd.Timestamp(int(c["t"]), unit="ms", tz="UTC"),
                            **{
                                field: float(c[k])
                                for field, k in (
                                    ("open", "o"),
                                    ("high", "h"),
                                    ("low", "l"),
                                    ("close", "c"),
                                    ("volume", "v"),
                                )
                            },
                            "trades": int(c["n"]),
                        }
                    )
            record["funding"] = http.post_json(
                "/info",
                {
                    "type": "fundingHistory",
                    "coin": asset,
                    "startTime": (now - 2 * 86400) * 1000,
                    "endTime": now * 1000,
                },
            )
        except Exception as exc:
            record["errors"].append(f"{type(exc).__name__}: {str(exc)[:160]}")
        out[asset] = record
    return out


def commit(store, fetched, now):
    at = datetime.fromtimestamp(now, UTC)
    for asset, rec in fetched.items():
        if rec["bars"]:
            upsert_bars(
                store,
                "hyperliquid",
                asset,
                Timeframe.H1,
                pd.DataFrame(rec["bars"]),
                observed_at=at,
                run_id="dynamic_universe_warming_v1",
            )
        for f in rec["funding"]:
            settled = datetime.fromtimestamp(int(f["time"]) / 1000, UTC)
            if f.get("coin") != asset or settled > at:
                continue
            # Never overwrite existing provenance, never assign market-close knowledge.
            store.con.execute(
                "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    asset,
                    "hyperliquid",
                    settled,
                    float(f["fundingRate"]),
                    float(f["premium"]) if f.get("premium") is not None else None,
                    at,
                    "observed",
                    "dynamic_universe_warming_v1",
                ],
            )
