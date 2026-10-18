"""Read-only market radar commands and explicit staged data-only refresh."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import nullcontext, redirect_stdout
from datetime import UTC, datetime

import typer

from market_signal.cli.common import console, open_store
from market_signal.universe import ledger

universe = typer.Typer(no_args_is_help=True, help="Prospective dynamic Hyperliquid market radar")
JSON = typer.Option(False, "--json")
# Apply before opening the DB: SET after connection misses catalog/startup allocation.
# This applies only to radar refreshes; other CLI jobs retain their current defaults.
REFRESH_DB_CONFIG = {"threads": "1", "memory_limit": "64MB"}


def _out(value, as_json):
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False))
    else:
        console.print(value)


@universe.command("status")
def status(as_json: bool = JSON):
    with open_store(read_only=True) as (_, store):
        value = ledger.status(store)
    _out(value, as_json)


@universe.command("rank")
def rank(as_json: bool = JSON):
    with open_store(read_only=True) as (_, store):
        value = ledger.status(store)
    _out(
        {"policy_id": value["policy_id"], "at": value["last_evaluation"], "rows": value["rows"]},
        as_json,
    )


@universe.command("asset")
def asset(asset: str, as_json: bool = JSON):
    with open_store(read_only=True) as (_, store):
        value = ledger.status(store)
    found = next((r for r in value["rows"] if r["asset"] == asset), None)
    if found is None:
        state = value.get("states", {}).get(asset)
        if state is None:
            raise typer.BadParameter("Asset has no prospective discovery observation")
        found = {
            "asset": asset,
            "tier": state["tier"],
            "state": state,
            "market": None,
            "reasons": ["MARKET_INACTIVE"],
        }
    _out({"policy_id": value["policy_id"], "at": value["last_evaluation"], **found}, as_json)


@universe.command("refresh")
def refresh(warming: bool = typer.Option(False, "--warming"), as_json: bool = JSON):
    """Data-only writer. Network collection takes place outside the authoritative runtime lock."""
    from market_signal.ops import profiles
    from market_signal.universe.policy import POLICY

    profile = profiles.current()
    if profile.explicit and (
        not profile.process_allowed("universe_radar") or (warming and not profile.collector_dynamic)
    ):
        console.print("Refused: the explicit profile has not enabled this Dynamic Universe stage.")
        raise typer.Exit(profiles.EXIT_PROFILE)
    now = int(datetime.now(UTC).timestamp())
    if warming or os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        from market_signal.universe.budget import validate

        validate(
            datetime.fromtimestamp(now, UTC),
            rich_assets=6 + POLICY.rich_satellite_cap if warming else 6,
        )
    from market_signal.config import get_settings
    from market_signal.universe import cadence

    # Keep wait/progress output off the machine-readable result channel.
    with (
        cadence.refresh_lock(get_settings().paths.db),
        redirect_stdout(sys.stderr) if as_json else nullcontext(),
    ):
        result = _refresh_once(warming)
    _out(result, as_json)


def _refresh_once(warming):
    from market_signal.data.http import HttpClient
    from market_signal.microstructure.spool import default_root
    from market_signal.ops.runtime import runtime_lock
    from market_signal.universe import cadence, source, subscriptions
    from market_signal.universe.telemetry import Requests

    timing = {"refresh_started_at": time.time()}
    now = int(timing["refresh_started_at"])
    with open_store(read_only=True, connect_config=REFRESH_DB_CONFIG) as (_, store):
        prepared = source.prepare(store, now)
        path = store.path
    if not cadence.due(prepared["previous"], now):
        return {"status": "NOT_DUE", "last_evaluation": prepared["previous"]["at"]}
    http = HttpClient(
        "hyperliquid_universe",
        "https://api.hyperliquid.xyz",
        timeout_seconds=5,
        max_retries=0,
        requests_per_second=2,
    )
    metrics = Requests()
    http._http().event_hooks["request"].append(metrics.request)
    http._http().event_hooks["response"].append(metrics.response)
    timing["source_fetch_started_at"] = time.time()
    try:
        fetched = source.fetch(http, prepared, now, warming=warming)
    finally:
        http.close()
    timing["source_refresh_completed_at"] = time.time()
    timing["api"] = metrics.summary()
    timing["api"]["captured_response_body_bytes"] = sum(len(p.body) for p in http.captured)
    # Ordinary short writer ownership is contention, not a failed observation.
    # The whole child still has the worker's 300s timeout; HTTP never holds this lock.
    timing["runtime_lock_requested_at"] = time.time()
    with runtime_lock(path, "dynamic_universe", wait=60):
        timing["runtime_lock_acquired_at"] = time.time()
        timing["runtime_lock_wait_seconds"] = (
            timing["runtime_lock_acquired_at"] - timing["runtime_lock_requested_at"]
        )
        with open_store(connect_config=REFRESH_DB_CONFIG) as (_, store):
            # This is the existing evidence clock, sampled at commit entry. Do not replace
            # it with the nominal slot or call it the completed transaction timestamp.
            evidence_clock = int(datetime.now(UTC).timestamp())
            timing["evidence_clock"] = evidence_clock
            result = source.commit(
                store, fetched, evidence_clock, warming=warming, telemetry=timing
            )
            if "evaluation_id" in result:
                subscriptions.publish(
                    default_root(path),
                    result,
                    role=os.environ.get("PRISM_RUNTIME_ROLE", ""),
                    runtime_id=os.environ.get("PRISM_RUNTIME_ID", ""),
                )
    timing["refresh_completed_at"] = time.time()
    timing["previous_evidence_clock"] = prepared["previous"]["at"] if prepared["previous"] else None
    timing["continuity_gap_seconds"] = (
        evidence_clock - prepared["previous"]["at"] if prepared["previous"] else None
    )
    result["telemetry"] = timing
    return result


def register(app):
    app.add_typer(universe, name="universe")
