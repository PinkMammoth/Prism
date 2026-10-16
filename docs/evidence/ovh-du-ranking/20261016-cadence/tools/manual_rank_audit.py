"""Independent arithmetic spot checks of public stored inputs; never a writer."""

import json
import math
import sys

sys.path.insert(0, "/opt/prism/current/src")
from market_signal.data.store import Store
from market_signal.universe import ledger
from market_signal.universe.policy import CORE


def audit():
    store = Store("/data/prism.duckdb", read_only=True, lock_timeout=30)
    try:
        ev = ledger.latest(store)
    finally:
        store.close()
    assert ev
    eligible = [r for r in ev["rows"] if not (set(r["reasons"]) - {"NEW_LISTING_WARMUP"})]
    getters = {
        "rv": lambda m: m["rv_bps"],
        "range": lambda m: m["range_bps"],
        "return": lambda m: None if m["return_bps"] is None else abs(m["return_bps"]),
        "volume": lambda m: math.log1p(max(m["volume_usd"], 0)),
        "volume_acceleration": lambda m: m["volume_acceleration"],
        "frequency": lambda m: m["trades_per_minute"],
        "oi": lambda m: math.log1p(max(m["oi_usd"], 0)),
        "oi_change": lambda m: m["oi_change"],
        "funding": lambda m: abs(m["funding"]),
    }
    choices = ["BTC", "ETH"]
    for predicate in (
        lambda r: r["asset"] not in CORE and r["market"]["supported"] and r["score"] > 0,
        lambda r: not r["market"]["active"],
        lambda r: ":" in r["asset"],
    ):
        found = next((r["asset"] for r in ev["rows"] if predicate(r)), None)
        if found:
            choices.append(found)
    reports = []
    for asset in choices:
        row = next(r for r in ev["rows"] if r["asset"] == asset)
        m = row["market"]
        normal = {}
        for key, getter in getters.items():
            value = getter(m)
            values = [getter(r["market"]) for r in eligible]
            values = [v for v in values if v is not None and math.isfinite(v)]
            normal[key] = (
                0
                if row not in eligible or value is None or not values
                else 0.5
                * (sum(v < value for v in values) + sum(v <= value for v in values))
                / len(values)
            )
        movement = 0.5 * normal["rv"] + 0.3 * normal["range"] + 0.2 * normal["return"]
        participation = (
            0.4 * normal["volume"] + 0.3 * normal["volume_acceleration"] + 0.3 * normal["frequency"]
        )
        positioning = 0.5 * normal["oi"] + 0.3 * normal["oi_change"] + 0.2 * normal["funding"]
        execution = (
            0
            if m["spread_bps"] is None or m["depth_usd"] is None
            else max(0, 1 - max(m["spread_bps"], 0) / 30) * min(1, max(m["depth_usd"], 0) / 100000)
        )
        completeness = (
            sum(
                m[k] is not None
                for k in (
                    "rv_bps",
                    "range_bps",
                    "return_bps",
                    "volume_acceleration",
                    "oi_change",
                    "trades_per_minute",
                )
            )
            / 6
        )
        score = (
            0
            if set(row["reasons"]) - {"NEW_LISTING_WARMUP"}
            else round(
                movement**0.45 * participation**0.35 * positioning**0.2 * execution * completeness,
                12,
            )
        )
        components = {
            "movement": movement,
            "participation": participation,
            "positioning": positioning,
            "execution": execution,
            "data": completeness,
        }
        assert score == row["score"]
        assert all(
            math.isclose(v, row["components"][k], abs_tol=1e-12) for k, v in components.items()
        )
        assert all(
            math.isclose(v, row["components"]["normalized"][k], abs_tol=1e-12)
            for k, v in normal.items()
        )
        reports.append(
            {
                "asset": asset,
                "market_inputs": m,
                "reasons": row["reasons"],
                "normalized_manual": normal,
                "manual_components": components,
                "manual_score": score,
                "stored_score": row["score"],
                "matches": True,
                "entry_eligible": row["entry_eligible"],
            }
        )
    return {
        "evaluation_id": ev["evaluation_id"],
        "evidence_clock": ev["at"],
        "policy_id": ev["policy_id"],
        "normalization_cohort_size": len(eligible),
        "manual_assets": reports,
        "all_match": True,
        "method": "Independent midrank/weighted-component/geometric-score arithmetic, no engine.rank call.",
    }


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2))
