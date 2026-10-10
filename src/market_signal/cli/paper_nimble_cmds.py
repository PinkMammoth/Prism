"""Released nimble PAPER commands. No manual trade, clock, discretionary size or live mode."""

from __future__ import annotations

import typer

from market_signal.cli.paper_v2_cmds import JSON, call
from market_signal.paper.nimble import engine, report

nimble = typer.Typer(
    no_args_is_help=True, help="Fast deterministic thesis-driven PAPER experiment."
)


def _rid(s, r):
    return r or engine.current(s)


@nimble.command("register")
def register_policy():
    call(engine.register, write=True)


@nimble.command("create")
def create():
    def go(s):
        from market_signal.paper.nimble.worker import marker, publish

        r = engine.create(s)
        publish(marker(s.path), {"run_id": r["run_id"], "activated_at": r["activated_at"]})
        return r

    call(go, write=True)


@nimble.command("evaluate")
def evaluate():
    from market_signal.config import get_settings
    from market_signal.paper.nimble.worker import Worker

    result = Worker(get_settings().paths.db).tick(force=True)
    from market_signal.cli.common import console

    console.print_json(data=result)
    if result["state"] in ("ERROR", "BUSY"):
        raise typer.Exit(1)


@nimble.command("status")
def status(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.status(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("trades")
def trades(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.trades(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("signals")
def signals(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.signals(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("daily")
def daily(day: str = typer.Option(None), run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.daily(s, _rid(s, run_id), day=day), as_json=as_json)


@nimble.command("compare")
def compare(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.compare(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("postmortem")
def postmortem(
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    asset: str = typer.Option("BTC"),
    run_id: str = typer.Option(None),
    as_json: bool = JSON,
):
    call(
        lambda s: report.postmortem(s, _rid(s, run_id), start=start, end=end, asset=asset),
        as_json=as_json,
    )


@nimble.command("recover")
def recover(run_id: str = typer.Option(None)):
    call(lambda s: engine.recover(s, _rid(s, run_id)), write=True)


@nimble.command("kill")
def kill(reason: str = typer.Option(...), run_id: str = typer.Option(None)):
    call(lambda s: engine.kill(s, _rid(s, run_id), reason=reason), write=True)


@nimble.command("brief")
def brief(
    send: bool = typer.Option(False, "--send"),
    trade_details: bool = typer.Option(False, "--trade-details"),
):
    def go(s):
        if not engine.runs(s):
            return {"recorded": False, "reason": "nimble not activated"}
        sender = None
        if send:
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient

            sender = TelegramClient.from_settings(get_settings()).send
        return report.record_daily(s, engine.current(s), sender=sender, trade_details=trade_details)

    result = call(go, write=True)
    if result.get("delivery") == "failed":
        raise typer.Exit(1)


def register(app):
    from market_signal.cli.paper_cmds import paper

    paper.add_typer(nimble, name="nimble")
