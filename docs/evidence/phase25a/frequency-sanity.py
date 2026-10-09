"""Reproduce the frozen technical occupancy sanity; never writes paper/Phase 22 rows.

Run with --database pointing to a scratch copy of the retained Phase 22 study DB.
This preserves the original long-only sanity mechanics and adds the existing frozen
cancel-both conflict rule for the seven exact short controls. No outcome selects a rule.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from itertools import groupby
from pathlib import Path

import numpy as np
import pandas as pd

from market_signal.data.store import Store
from market_signal.paper.v2 import spec
from market_signal.research.discovery.catalogue import strategies
from market_signal.research.discovery.collect import strategy_events
from market_signal.research.discovery.run import venue_data
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.structure_study import get_study
from market_signal.research.structure.study.data import load_coin

STUDY = "sstudy_f60a05a62799548cd444753c8de7c5d7b3d9bdd17a6b5fe8b5c9a08b0d790c45"
DIGEST = "d6c9e4a060b69b02da40c3a260a40a337aaa8a3007e6ac382ce87ca089b7c4ad"
MINUTE_NS = 60 * 10**9


def occupancy(signals, start, end):
    positions, cooldown, admitted = {}, {}, []
    reasons, supports = Counter(), 0
    side_reasons, side_supports = {s: Counter() for s in ("long", "short")}, Counter()

    def reject(r, reason):
        reasons[reason] += 1
        side_reasons[r["side"]][reason] += 1

    for t, batch in groupby(signals, key=lambda r: r["signal_ns"]):
        batch = list(batch)
        sides = {}
        for r in batch:
            sides.setdefault(r["coin"], set()).add(r["side"])
        positions = {a: p for a, p in positions.items() if p["exit_ns"] > t}
        for r in batch:
            k = (r["key"], r["coin"])
            if len(sides[r["coin"]]) > 1:
                reject(r, "CONFLICT")
                continue
            if t < cooldown.get(k, 0):
                reject(r, "COOLDOWN")
                continue
            if r["coin"] in positions:
                existing = positions[r["coin"]]
                if existing["side"] != r["side"]:
                    reject(r, "CONFLICT")
                elif t - existing["signal_ns"] <= 10 * MINUTE_NS:
                    supports += 1
                    side_supports[r["side"]] += 1
                    cooldown[k] = t + max(15, r["horizon_minutes"] // 2) * MINUTE_NS
                else:
                    reject(r, "DUPLICATE_EPISODE")
                continue
            if len(positions) >= 3:
                reject(r, "MAX_POSITIONS")
                continue
            if not np.isfinite(r["net"]):
                reject(r, "FUNDING_OR_PATH_UNAVAILABLE")
                continue
            positions[r["coin"]] = r
            admitted.append(r)
            cooldown[k] = t + max(15, r["horizon_minutes"] // 2) * MINUTE_NS
    days = pd.date_range(start.floor("D"), end.floor("D"), freq="D", inclusive="left")
    n = len(days)
    assert len(signals) == len(admitted) + supports + sum(reasons.values())
    counts = Counter(pd.Timestamp(r["signal_ns"], tz="UTC").floor("D") for r in admitted)
    return {
        "raw_signals": len(signals),
        "signals_per_day": len(signals) / n,
        "signal_sides": {s: sum(r["side"] == s for r in signals) for s in ("long", "short")},
        "occupancy_admissions": len(admitted),
        "occupancy_admissions_per_day": len(admitted) / n,
        "long_per_day": sum(r["side"] == "long" for r in admitted) / n,
        "short_per_day": sum(r["side"] == "short" for r in admitted) / n,
        "support_events": supports,
        "rejection_reasons": dict(reasons),
        "side_audit": {
            s: {
                "signals": sum(r["side"] == s for r in signals),
                "admissions": sum(r["side"] == s for r in admitted),
                "support_events": side_supports[s],
                "rejections": dict(side_reasons[s]),
            }
            for s in ("long", "short")
        },
        "zero_day_share": sum(counts[d] == 0 for d in days) / n,
        "days_ge_share": {str(k): sum(counts[d] >= k for d in days) / n for k in (1, 3, 5, 10)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = Store(args.database, read_only=True)
    try:
        ledger = Ledger(store)
        definition, _ = get_study(ledger, STUDY)
        manifest = definition.manifest

        def load(venue, coin):
            return load_coin(
                ledger,
                definition.dataset(venue, coin),
                venue=venue,
                coin=coin,
                latency_s=manifest.assumed_latency_s,
            )

        costs = {c.coin: c.per_side for c in definition.costs if c.venue == "binance"}
        vd = venue_data(manifest, "binance", load, costs)
        start, end = (
            pd.Timestamp(manifest.windows.contemporary_start),
            pd.Timestamp(manifest.windows.end),
        )
        signals = []
        for h in strategies():
            if h.key not in spec.ALL_TECHNICAL_KEYS:
                continue
            events = strategy_events(vd, h, start.value, end.value)
            for r in events.to_dict("records"):
                signals.append(
                    {
                        "signal_ns": r["signal_ns"],
                        "coin": r["coin"],
                        "key": h.key,
                        "side": h.side,
                        "horizon_minutes": h.primary_minutes,
                        "exit_ns": r["exit_ns"],
                        "net": r[f"net_{h.primary_horizon}"],
                    }
                )
        signals.sort(key=lambda r: (r["signal_ns"], r["coin"], r["key"]))
        previous = occupancy([r for r in signals if r["side"] == "long"], start, end)
        # The original seven-long methodology must reproduce its published counts exactly.
        assert previous["raw_signals"] == 5342 and previous["occupancy_admissions"] == 2635
        assert previous["support_events"] == 242
        result = {
            "kind": "HISTORICAL_TECHNICAL_ONLY_MECHANICAL_FREQUENCY_SANITY_NOT_V2_TRADES",
            "baseline_digest": DIGEST,
            "study_id": STUDY,
            "universe_id": spec.universe_id(),
            "version": spec.VERSION,
            "venue": "binance",
            "period": [start.isoformat(), end.isoformat()],
            "days": (end - start).days,
            "frozen_keys": spec.ALL_TECHNICAL_KEYS,
            **occupancy(signals, start, end),
            "previous_long_only_sanity": previous,
            "limitations": [
                "Binance historical assumed latency, not current Hyperliquid activity",
                "Occupancy only: no equity/lot/catastrophe simulation",
                "No positioning/microstructure frequency estimate: young data",
                "Historical path and funding availability exclude incomplete outcomes; no profitability selection",
                "Original key ordering retained; approximates rather than reproduces engine hashed priority",
                "Existing cancel-both and occupied opposite-side conflict rules applied symmetrically",
                "No parameters were tuned; no paper or Phase 22 tables written",
            ],
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
