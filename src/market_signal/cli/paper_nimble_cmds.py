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


def _execution(action):
    """Run creation/mutation needs a full-authority execution profile (legacy: unchanged)."""
    from market_signal.cli.common import console
    from market_signal.ops import profiles

    try:
        profiles.require_execution(f"paper nimble {action}")
    except profiles.ProfileError as exc:
        console.print(f"Refused: {exc}", style="red", markup=False)
        raise typer.Exit(profiles.EXIT_PROFILE) from None


@nimble.command("register")
def register_policy(
    events: bool = typer.Option(False, "--events"),
    dynamic: bool = typer.Option(False, "--dynamic-universe"),
    core: bool = typer.Option(False, "--core-six", help="Static core-six successor policy."),
):
    _execution("register")
    call(lambda s: engine.register(s, events=events, dynamic=dynamic, core=core), write=True)


@nimble.command("create")
def create(
    events: bool = typer.Option(False, "--events"),
    variant: str = typer.Option(None),
    dynamic: bool = typer.Option(False, "--dynamic-universe"),
    core: bool = typer.Option(False, "--core-six", help="Exactly one static core-six account."),
):
    _execution("create")
    if events and dynamic:
        raise typer.BadParameter("Dynamic universe does not expand the frozen event cohorts")

    def go(s):
        from market_signal.paper.nimble.worker import marker, publish

        r = (
            engine.create_cohorts(s)
            if events and variant == "both"
            else engine.create(s, events=events, variant=variant, dynamic=dynamic, core=core)
        )
        publish(
            marker(s.path),
            {
                "run_id": r["run_id"],
                "run_ids": [
                    x["run_id"]
                    for x in engine.runs(s)
                    if engine.account(s, x["run_id"])["status"] == "ACTIVE"
                    or engine.positions(s, x["run_id"])
                ],
                "activated_at": r["activated_at"],
            },
        )
        return r

    call(go, write=True)


@nimble.command("evaluate")
def evaluate():
    _execution("evaluate")
    from market_signal.config import get_settings
    from market_signal.paper.nimble.worker import Worker

    result = Worker(get_settings().paths.db).tick_all(force=True)
    from market_signal.cli.common import console

    console.print_json(data=result)
    if result["state"] in ("ERROR", "BUSY"):
        raise typer.Exit(1)


@nimble.command("status")
def status(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.status(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("funnel")
def funnel(
    run_id: str = typer.Option(None),
    start: str = typer.Option(None, help="UTC window start (default: activation)."),
    end: str = typer.Option(None, help="UTC window end (default: now)."),
    as_json: bool = JSON,
):
    """READ-ONLY opportunity funnel, accounting, latency and input frontiers."""
    call(lambda s: report.funnel(s, _rid(s, run_id), start=start, end=end), as_json=as_json)


@nimble.command("trades")
def trades(run_id: str = typer.Option(None), as_json: bool = JSON):
    call(lambda s: report.trades(s, _rid(s, run_id)), as_json=as_json)


@nimble.command("events")
def events_report(run_id: str = typer.Option(None), as_json: bool = JSON):
    from market_signal.paper.nimble import event_report

    call(
        lambda s: event_report.summary(s, run_id) if run_id else event_report.comparison(s),
        as_json=as_json,
    )


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
        unconfigured = False
        if send:
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient, TelegramError

            try:
                sender = TelegramClient.from_settings(get_settings()).send
            except TelegramError:
                # No delivery channel: the daily report is still recorded, never skipped.
                unconfigured = True
        from market_signal.paper.nimble import event_spec, spec

        runs = engine.runs(s)
        others = [
            report.record_daily(s, r["run_id"], trade_details=trade_details)
            for r in runs
            if r["policy_id"] == event_spec.policy_id()
        ]
        baseline = next(
            (r["run_id"] for r in reversed(runs) if r["policy_id"] == spec.policy_id()),
            engine.current(s),
        )
        deliver = sender
        if sender and others:

            def deliver(text):
                import json

                variants = {
                    r["run_id"]: json.loads(r["definition"]).get("event_variant") for r in runs
                }
                text += "\n" + "\n".join(
                    f"Event {variants[r['report']['run_id']]}: {r['report']['trades']} closed, net ${r['report']['net_pnl']:.4f}"
                    for r in others
                )
                return sender(text)

        result = report.record_daily(s, baseline, sender=deliver, trade_details=trade_details)
        if unconfigured:
            result["delivery"] = "unconfigured"
        return {**result, "event_cohort_reports": others}

    result = call(go, write=True)
    if result.get("delivery") == "failed":
        raise typer.Exit(1)


def register(app):
    from market_signal.cli.paper_cmds import paper

    paper.add_typer(nimble, name="nimble")
