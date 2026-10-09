"""Exploratory paper CLI. No manual trade, size, risk, clock or live-mode command."""

from __future__ import annotations

import typer

from market_signal.cli.common import console, open_store
from market_signal.paper.v2 import engine, report
from market_signal.research.lab.common import canonical_json

v2 = typer.Typer(no_args_is_help=True, help="Deterministic exploratory PAPER research account.")
JSON = typer.Option(False, "--json", help="Machine-readable output.")


def call(fn, *, write=False, as_json=True):
    try:
        with open_store(read_only=not write) as (_, store):
            result = fn(store)
        console.print_json(canonical_json(result))
        return result
    except ValueError as exc:
        console.print(f"Paper v2: {exc}", markup=False)
        raise typer.Exit(1) from None


def _rid(store, run_id):
    return run_id or engine.current(store)


@v2.command("register")
def register_universe() -> None:
    """WRITE: register ONLY the released immutable bootstrap universe and policies."""
    call(engine.register, write=True)


@v2.command("create")
def create() -> None:
    """WRITE: activate a new $100 simulated USDC run NOW, after v1 retirement/deployment."""
    call(engine.create, write=True)


@v2.command("evaluate")
def evaluate() -> None:
    """WRITE: evaluate registered hypotheses at the current causal time; no historical fills."""

    def go(store):
        return [engine.evaluate(store, r["run_id"]) for r in engine.runs(store)]

    result = call(go, write=True)
    if any(r.get("status") == "error" for r in result):
        raise typer.Exit(1)


@v2.command("status")
def status(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.status(s, _rid(s, run_id)), as_json=as_json)


@v2.command("trades")
def trades(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.trades(s, _rid(s, run_id)), as_json=as_json)


@v2.command("signals")
def signals(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.opportunities(s, _rid(s, run_id)), as_json=as_json)


@v2.command("rejected")
def rejected(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.opportunities(s, _rid(s, run_id), rejected=True), as_json=as_json)


@v2.command("hypotheses")
def hypotheses(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.hypotheses(s, _rid(s, run_id)), as_json=as_json)


@v2.command("explain")
def explain(trade_id: str, run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.explain(s, _rid(s, run_id), trade_id), as_json=as_json)


@v2.command("postmortem")
def postmortem(
    start: str = typer.Option(...),
    end: str = typer.Option(...),
    asset: str = typer.Option("BTC"),
    run_id: str = typer.Option(None),
    as_json: bool = JSON,
) -> None:
    call(
        lambda s: report.postmortem(s, _rid(s, run_id), start=start, end=end, asset=asset.upper()),
        as_json=as_json,
    )


@v2.command("compare-v1")
def compare(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.compare_v1(s, _rid(s, run_id)), as_json=as_json)


@v2.command("frequency")
def frequency(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    call(lambda s: report.frequency(s, _rid(s, run_id)), as_json=as_json)


@v2.command("daily")
def daily(
    day: str = typer.Option(None), run_id: str = typer.Option(None), as_json: bool = JSON
) -> None:
    call(lambda s: report.daily(s, _rid(s, run_id), day=day), as_json=as_json)


@v2.command("brief")
def brief(send: bool = typer.Option(False, "--send")) -> None:
    """WRITE: freeze yesterday's report; optional explicit once-per-day Telegram send."""

    def go(store):
        if not engine.runs(store):
            return {"recorded": False, "reason": "v2 not activated"}
        sender = None
        if send:
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient

            sender = TelegramClient.from_settings(get_settings()).send
        return report.record_daily(store, engine.current(store), sender=sender)

    result = call(go, write=True)
    if result.get("delivery") == "failed":
        raise typer.Exit(1)


@v2.command("kill")
def kill(reason: str = typer.Option(...), run_id: str = typer.Option(None)) -> None:
    """WRITE: human global PAPER kill; deterministic closing on the next quote."""
    call(lambda s: engine.kill(s, _rid(s, run_id), reason=reason), write=True)


def retire_v1(run_id: str = typer.Option(None), as_json: bool = JSON) -> None:
    """WRITE: retire v1 without rewriting history; open positions drain at fixed exits."""
    from market_signal.paper import observe
    from market_signal.paper.retirement import retire

    call(lambda s: retire(s, run_id or observe.current_run(s)), write=True, as_json=as_json)


def register_commands(app: typer.Typer) -> None:
    from market_signal.cli.paper_cmds import paper

    paper.add_typer(v2, name="v2")
    paper.command("retire-v1")(retire_v1)
    paper.command("postmortem")(postmortem)
    app.add_typer(paper, name="paper")


def register(app: typer.Typer) -> None:
    register_commands(app)
