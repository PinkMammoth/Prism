"""Read-only market radar commands and explicit staged data-only refresh."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import typer

from market_signal.cli.common import console, open_store
from market_signal.universe import ledger

universe = typer.Typer(no_args_is_help=True, help="Prospective dynamic Hyperliquid market radar")
JSON = typer.Option(False, "--json")


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
    from market_signal.data.http import HttpClient
    from market_signal.microstructure.spool import default_root
    from market_signal.ops.runtime import runtime_lock
    from market_signal.universe import source, subscriptions
    from market_signal.universe.policy import POLICY

    now = int(datetime.now(UTC).timestamp())
    if warming or os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        from market_signal.universe.budget import validate

        validate(
            datetime.fromtimestamp(now, UTC),
            rich_assets=6 + POLICY.rich_satellite_cap if warming else 6,
        )
    with open_store(read_only=True) as (_, store):
        prepared = source.prepare(store, now)
        path = store.path
    if prepared["previous"] and now - prepared["previous"]["at"] < POLICY.cadence_seconds:
        _out({"status": "NOT_DUE", "last_evaluation": prepared["previous"]["at"]}, as_json)
        return
    http = HttpClient(
        "hyperliquid_universe",
        "https://api.hyperliquid.xyz",
        timeout_seconds=5,
        max_retries=0,
        requests_per_second=2,
    )
    try:
        fetched = source.fetch(http, prepared, now, warming=warming)
    finally:
        http.close()
    with runtime_lock(path, "dynamic_universe", wait=0), open_store() as (_, store):
        result = source.commit(store, fetched, int(datetime.now(UTC).timestamp()), warming=warming)
        if "evaluation_id" in result:
            subscriptions.publish(
                default_root(path),
                result,
                role=os.environ.get("PRISM_RUNTIME_ROLE", ""),
                runtime_id=os.environ.get("PRISM_RUNTIME_ID", ""),
            )
    _out(result, as_json)


def register(app):
    app.add_typer(universe, name="universe")
