"""Atomic desired set for the DB-free collector. Stale/corrupt plans cannot remove risk."""

from __future__ import annotations

import json
import os
from pathlib import Path

from market_signal.universe.policy import CORE, POLICY


def publish(root: Path, decision, *, role, runtime_id):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "policy_id": POLICY.identity,
        "at": decision["at"],
        "role": role,
        "runtime_id": runtime_id,
        "coins": decision["desired_subscriptions"],
        "pins": sorted(decision["pins"]),
        "evaluation_id": decision["evaluation_id"],
    }
    temp = root / ".universe_subscriptions.tmp"
    temp.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temp, root / "universe_subscriptions.json")


def read(root: Path, current, now, *, role, runtime_id, restart=False):
    try:
        data = json.loads((root / "universe_subscriptions.json").read_text())
        coins = data["coins"]
        pins = data["pins"]
        if (
            data["policy_id"] != POLICY.identity
            or data["role"] != role
            or data["runtime_id"] != runtime_id
            or not isinstance(data["at"], int)
            or now < data["at"]
            or (not restart and now - data["at"] > 2 * POLICY.cadence_seconds)
            or not isinstance(coins, list)
            or any(not isinstance(c, str) or not c for c in coins)
            or len(coins) != len(set(coins))
            or not set(CORE).issubset(coins)
            or not set(pins).issubset(coins)
            or len(set(coins) - set(CORE) - set(pins)) > POLICY.rich_satellite_cap
        ):
            return tuple(current), "INVALID_OR_STALE_PLAN"
        return tuple(sorted(coins)), None
    except (OSError, ValueError, KeyError, TypeError):
        return tuple(current), "PLAN_UNAVAILABLE"
