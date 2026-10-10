"""Free source poll/spool commands; ingestion remains the authoritative worker's job."""

from __future__ import annotations

import json

import typer

app = typer.Typer(no_args_is_help=True)


@app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json"), check: bool = typer.Option(False, "--check")
):
    from market_signal.context.free_sources.health import status as describe
    from market_signal.context.free_sources.spool import Spool, default_root

    result = describe(Spool(default_root()))
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        for s in result["sources"]:
            print(
                f"{s['id']}: {s['health_state']} HTTP={s['http_status']} cadence={s['cadence_seconds']}s next={s['next_poll']}"
            )
        print(f"pending={result['backlog']} DATA_COST=£0/month")

    if check:
        unhealthy = any(not h["healthy"] for h in result["processes"].values())
        unhealthy |= any(
            s["polling_active"] and (s["consecutive_failures"] or 0) >= 3 for s in result["sources"]
        )
        unhealthy |= (result["oldest_pending_age_seconds"] or 0) > 300
        if unhealthy:
            raise typer.Exit(1)


@app.command("metrics")
def metrics(as_json: bool = typer.Option(True, "--json")):
    from market_signal.cli.common import open_store
    from market_signal.context.free_sources.health import metrics as describe
    from market_signal.context.free_sources.spool import Spool, default_root

    with open_store(read_only=True) as (_, store):
        print(json.dumps(describe(store, Spool(default_root())), indent=2))


@app.command("poll")
def poll(stage: int = typer.Option(1, min=1, max=5)):
    """Poll due sources once, durably spool accepted candidates; never open DuckDB."""
    from market_signal.context.free_sources.collector import Collector
    from market_signal.context.free_sources.registry import load, verify_cost_report
    from market_signal.context.free_sources.spool import Spool, default_root

    verify_cost_report(load())
    c = Collector(Spool(default_root()), stage=stage)
    try:
        print(json.dumps(c.tick(), indent=2))
    finally:
        c.close()


@app.command("rollout")
def rollout(stage: int = typer.Argument(..., min=1, max=5)):
    """Durably select a reviewed zero-cost rollout stage; never change paper policy."""
    from market_signal.context.free_sources.registry import load, verify_cost_report
    from market_signal.context.free_sources.spool import Spool, default_root, replace_durable
    from market_signal.context.gateway.spool import now

    verify_cost_report(load())
    spool = Spool(default_root())
    replace_durable(spool.root / "rollout.json", {"stage": stage, "selected_at": now().isoformat()})
    print(json.dumps({"stage": stage, "DATA_COST": "£0", "takes_effect": "next collector tick"}))
