"""Gateway operator commands. No DB inspection required for receipts/health."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from market_signal.context.gateway.spool import Spool, default_root

app = typer.Typer(
    no_args_is_help=True, help="Information-only real-time context gateway (Phase 26A)."
)
JSON = typer.Option(False, "--json")


def out(obj, as_json):
    print(json.dumps(obj, indent=2 if not as_json else None, default=str, sort_keys=True))


@app.command("status")
def status(as_json: bool = JSON):
    from market_signal.context.gateway.health import status as get

    out(get(Spool(default_root())), as_json)


@app.command("receipts")
def receipts(limit: int = typer.Option(20, min=1, max=100), as_json: bool = JSON):
    sp = Spool(default_root())
    rows = sorted(sp.records(), key=lambda r: r["gateway_received_at"], reverse=True)[:limit]
    out(
        [
            {
                k: r[k]
                for k in ("receipt_id", "gateway_received_at", "auth_identity", "payload_sha256")
            }
            | {"completion": sp.read("done", r["receipt_id"])}
            for r in rows
        ],
        as_json,
    )


@app.command("backlog")
def backlog(as_json: bool = JSON):
    out(
        [
            {k: r[k] for k in ("receipt_id", "gateway_received_at")}
            for r in Spool(default_root()).pending()
        ],
        as_json,
    )


@app.command("inspect")
def inspect(receipt_id: str, as_json: bool = JSON):
    from market_signal.context.gateway.health import inspect as get

    try:
        out(get(Spool(default_root()), receipt_id), as_json)
    except ValueError as exc:
        out({"error": str(exc)}, as_json)
        raise typer.Exit(2) from None


@app.command("serve")
def serve(dev: bool = typer.Option(False, "--dev", help="Loopback HTTP for development ONLY")):
    from market_signal.context.gateway.launch import run

    run(dev=dev)


@app.command("ingest")
def ingest(as_json: bool = JSON):
    from market_signal.config import get_settings
    from market_signal.context.gateway.worker import tick

    out(tick(get_settings().paths.db, Spool(default_root())), as_json)


@app.command("worker")
def worker():
    from market_signal.config import get_settings
    from market_signal.context.gateway.worker import run

    run(get_settings().paths.db)


@app.command("test")
def test(
    url: str = typer.Option(None, help="HTTPS gateway URL. Omit for isolated local demo."),
    as_json: bool = JSON,
):
    from market_signal.context.gateway.demo import demo, sample

    if not url:
        out(demo(), as_json)
        return
    import httpx

    from market_signal.context.gateway.contract import public_url

    public_url(url)
    token = os.environ.get("PRISM_CONTEXT_TEST_TOKEN")
    if not token:
        raise typer.BadParameter("set PRISM_CONTEXT_TEST_TOKEN (never put it in shell arguments)")
    response = httpx.post(
        url.rstrip("/") + "/context/v1/events",
        json=sample(test=True),
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    out(response.json(), as_json)
    if response.status_code >= 400:
        raise typer.Exit(1)


@app.command("contract")
def contract(
    as_json: bool = JSON, path: Path = typer.Option(None, help="Write action JSON schema")
):
    from market_signal.context.gateway.contract import action_schema

    schema = {"name": "submit_market_event", "inputSchema": action_schema()}
    if path:
        path.write_text(json.dumps(schema, indent=2) + "\n")
    out(schema, as_json)


@app.command("health")
def health(check: bool = typer.Option(False, "--check"), as_json: bool = JSON):
    from market_signal.context.gateway.health import status as get
    from market_signal.context.gateway.server import publish_live
    from market_signal.context.gateway.spool import now

    spool = Spool(default_root())
    result = get(spool)
    if check:
        from datetime import datetime

        from market_signal.ops.runtime import infra_alert

        prior_path = spool.root / "watchdog.json"
        prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
        current = {}
        for code in result["alerts"]:
            item = prior.get(code, {"since": now().isoformat(), "alerted": False})
            if (
                not item["alerted"]
                and (now() - datetime.fromisoformat(item["since"])).total_seconds() >= 60
            ):
                infra_alert(f"Context gateway: {code}. Check market context gateway status --json.")
                item["alerted"] = True
            current[code] = item
        publish_live(spool, "watchdog.json", current)
    out(result, as_json)
    if check and result["alerts"]:
        raise typer.Exit(1)
