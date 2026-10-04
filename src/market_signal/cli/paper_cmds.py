"""``market lab paper``: the paper-only auto-trader (a simulated perp account).

``policy``, ``status``, ``positions``, ``trades``, ``events``, ``summary``, ``create --dry-run``
and ``run --dry-run`` are read-only. ``create``, ``run``, ``pause``/``resume``/``stop`` and
``evidence`` write only ``paper_*`` tables; ``run`` may send PAPER-labelled Telegram
notifications.

There is no live mode: no command, flag or environment variable here (or anywhere in
Prism) places, transmits or signs a real exchange order, and none asks for trading keys.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger, LedgerError

paper = typer.Typer(
    no_args_is_help=True,
    help="Paper auto-trader: a SIMULATED perp account (never real orders).",
)


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _now(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _call(fn: Callable[[Ledger], object], *, read_only: bool = True, quiet: bool = False):
    try:
        with open_store(read_only=read_only) as (_, store):
            out = fn(Ledger(store))
            if not quiet:
                console.print_json(canonical_json(out))
            return out
    except (LedgerError, ValueError, duckdb.Error) as exc:
        console.print(f"Paper: {exc}", markup=False)
        raise typer.Exit(1) from None


def _one_run(store, run_id: str | None) -> str:
    from market_signal.paper.engine import runs

    if run_id:
        return run_id
    ids = [r["run_id"] for r in runs(store)]
    if not ids:
        raise ValueError("no paper run exists; create one with `market lab paper create`")
    return ids[-1]


@paper.command("policy")
def policy() -> None:
    """Print the released promotion, risk, exit and maturity policies with their IDs."""
    from market_signal.paper import policy as pol

    out = {}
    for name, p in (("promotion", pol.promotion_policy()), ("risk", pol.risk_policy()),
                    ("exit", pol.exit_policy()), ("maturity", pol.maturity_policy())):  # fmt: skip
        out[name] = {"policy_id": p.policy_id, **p.model_dump(mode="json")}
    console.print_json(canonical_json(out))


@paper.command("create")
def create(
    strategies: list[str] = typer.Argument(..., help="Strategy names or IDs (active trackings)."),
    reason: str = typer.Option(..., "--reason", help="Why this paper account is started."),
    label: str = typer.Option(None, "--label"),
    continues: str = typer.Option(None, "--continues", help="A stopped/killed run this follows."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the frozen run; write nothing."),
    promotion_version: int = typer.Option(None, "--promotion-version", help="Default: latest."),
    risk_version: int = typer.Option(None, "--risk-version", help="Default: latest."),
    exit_version: int = typer.Option(None, "--exit-version", help="Default: latest."),
) -> None:
    """WRITE: create a paper account. Its clock starts now; no earlier signal is ever traded."""
    from market_signal.paper import policy as pol
    from market_signal.paper.engine import create_run

    _call(
        lambda ledger: create_run(
            ledger, strategies, reason=reason, origin="cli", software=_software(), label=label,
            continues=continues, dry_run=dry_run,
            promotion=pol.promotion_policy(promotion_version), risk=pol.risk_policy(risk_version),
            exit_=pol.exit_policy(exit_version),
        ),
        read_only=dry_run,
    )  # fmt: skip


@paper.command("run")
def run_(
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the cycle; write/send nothing."),
    full: bool = typer.Option(False, "--full", help="With --dry-run: print every would-be event."),
    now: str = typer.Option(None, "--now", help="ISO time (default: now). Inspection only."),
) -> None:
    """WRITE: process every newly closed bar of each paper run, then send PAPER notifications.

    Idempotent: a bar is processed once; re-running writes nothing new.
    """
    from market_signal.paper.engine import run_all

    def go(ledger):
        def sender_factory():
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient

            return TelegramClient.from_settings(get_settings()).send

        if now is not None and not dry_run:
            raise ValueError("--now is for --dry-run inspection only")
        out = run_all(ledger, software=_software(), sender_factory=sender_factory,
                      now=_now(now), dry_run=dry_run)  # fmt: skip
        if dry_run and not full:
            for r in out["runs"]:
                evs = r.pop("would_record", [])
                r["would_record"] = [{"type": e["event_type"], "key": e["event_key"]} for e in evs]
        return out

    out = _call(go, read_only=dry_run)
    failed = any(r.get("status") == "error" for r in out["runs"])
    failed |= any(n["status"] == "failed" for n in out.get("notifications") or [])
    if failed:
        raise typer.Exit(1)


@paper.command("status")
def status_(run_id: str = typer.Argument(None)) -> None:
    """Every paper run (or one): status, equity, positions, policies, last cycle."""
    from market_signal.paper.engine import status

    _call(lambda ledger: status(ledger.store, run_id))


@paper.command("positions")
def positions_(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """Open paper positions (marked at the last processed close) and unfilled entry orders."""
    from market_signal.paper.engine import positions

    _call(lambda ledger: positions(ledger.store, _one_run(ledger.store, run_id)))


@paper.command("trades")
def trades_(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """Closed paper trades with gross/net PnL, fees, slippage and funding."""
    from market_signal.paper.engine import trades

    _call(lambda ledger: trades(ledger.store, _one_run(ledger.store, run_id)))


@paper.command("events")
def events_(
    run_id: str = typer.Argument(None, help="Default: the newest run."),
    limit: int = typer.Option(100, "--limit"),
    event_type: str = typer.Option(None, "--type", help="e.g. risk_decision, position_closed."),
) -> None:
    """The immutable event ledger (newest last)."""
    from market_signal.paper.engine import list_events

    _call(lambda ledger: list_events(ledger.store, _one_run(ledger.store, run_id), limit=limit,
                                     event_type=event_type))  # fmt: skip


def _set(run_id: str, status: str, reason: str) -> None:
    from market_signal.paper.engine import set_status

    _call(lambda ledger: set_status(ledger.store, run_id, status, reason=reason), read_only=False)


@paper.command("pause")
def pause(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: no new paper entries; open positions keep running to their scheduled exits."""
    _set(run_id, "PAUSED", reason)


@paper.command("resume")
def resume(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: resume a paused paper run (signals meanwhile are never replayed)."""
    _set(run_id, "ACTIVE", reason)


@paper.command("stop")
def stop(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: stop a paper run permanently (open positions still run to their exits)."""
    _set(run_id, "STOPPED", reason)


@paper.command("summary")
def summary(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """Read-only ``paper_execution`` evidence summary computed from the ledger."""
    from market_signal.paper.engine import paper_summary

    _call(lambda ledger: paper_summary(ledger.store, _one_run(ledger.store, run_id)))


@paper.command("evidence")
def evidence(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """WRITE: append the current ``paper_execution`` summary (never touches Lab evidence)."""
    from market_signal.paper.engine import record_evidence

    _call(lambda ledger: record_evidence(ledger.store, _one_run(ledger.store, run_id)),
          read_only=False)  # fmt: skip
