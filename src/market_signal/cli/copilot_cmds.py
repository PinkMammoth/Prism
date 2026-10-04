"""``market lab copilot``: the perps co-pilot (human alerts from Lab evidence).

``policy``, ``watchlist``, ``candidates``, ``preview``, ``decisions``, ``show`` and
``run --dry-run`` are read-only. ``watch``, ``pause``/``resume``/``stop`` and ``run``
write only co-pilot tables; ``run`` may send Telegram messages. Nothing here places,
sizes or approves an order, and no Lab record is written.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger, LedgerError

copilot = typer.Typer(
    no_args_is_help=True,
    help="Perps co-pilot: human alerts from Strategy Lab evidence (never orders).",
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
        console.print(f"Co-pilot: {exc}", markup=False)
        raise typer.Exit(1) from None


def _plain(html_text: str) -> str:
    from market_signal.cli.scan_cmds import _plain as plain

    return plain(html_text)


@copilot.command("policy")
def policy(version: int = typer.Option(None, "--version", help="Default: latest.")) -> None:
    """Print a released co-pilot policy and its content ID."""
    from market_signal.copilot.policy import get_policy

    p = get_policy(version)
    console.print_json(canonical_json({"policy_id": p.policy_id, **p.model_dump(mode="python")}))


@copilot.command("watch")
def watch(
    profile_id: str,
    reason: str = typer.Option(..., "--reason", help="Why this strategy is watched."),
    policy_version: int = typer.Option(None, "--policy-version", help="Default: latest."),
    label: str = typer.Option(None, "--label"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the frozen watch only."),
) -> None:
    """WRITE: add a strategy (by its historical evidence profile) to the co-pilot watchlist.

    Only bars that close after registration can alert. A strategy has at most one open
    watch; a policy change is a new watch (stop the old one first).
    """
    from market_signal.copilot.engine import register_watch
    from market_signal.copilot.policy import get_policy

    _call(
        lambda ledger: register_watch(
            ledger, profile_id, reason=reason, origin="cli", software=_software(),
            policy=get_policy(policy_version), label=label, dry_run=dry_run,
        ),
        read_only=dry_run,
    )  # fmt: skip


@copilot.command("watchlist")
def watchlist() -> None:
    """List watched strategies, their status and decision counts."""
    from market_signal.copilot.engine import list_watches

    _call(lambda ledger: list_watches(ledger.store))


def _status(watch_id: str, status: str, reason: str) -> None:
    from market_signal.copilot.engine import set_watch_status

    _call(lambda ledger: set_watch_status(ledger.store, watch_id, status, reason=reason),
          read_only=False)  # fmt: skip


@copilot.command("pause")
def pause(watch_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: pause alerts for a watch (signals meanwhile are never replayed)."""
    _status(watch_id, "paused", reason)


@copilot.command("resume")
def resume(watch_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: resume a paused watch."""
    _status(watch_id, "active", reason)


@copilot.command("stop")
def stop(watch_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: stop a watch permanently."""
    _status(watch_id, "stopped", reason)


def _brief(c: dict) -> dict:
    """Candidate summary for the terminal (full detail stays in the JSON payload)."""
    out = {k: c.get(k) for k in ("strategy", "symbol", "bar_close", "state", "note", "error")}
    out = {k: v for k, v in out.items() if v is not None}
    sig = c.get("signal")
    if sig:
        out["conditions"] = sig["conditions"]
        out["new_signal_on_bar"] = sig["fired"]
        out["forward_tracker_recorded"] = sig.get("forward_tracker_recorded")
    res = c.get("result") or c.get("if_fired")
    if res:
        key = "decision" if c.get("result") else "if_it_fired"
        out[key] = {"decision": res["decision"], "priority": res["priority"],
                    "blocked_by": res["blocked_by"], "caveats": res["caveats"]}  # fmt: skip
    return out


@copilot.command("candidates")
def candidates_(
    full: bool = typer.Option(False, "--full", help="Include evidence views and every rule."),
    now: str = typer.Option(None, "--now", help="ISO time (default: now)."),
) -> None:
    """Current signal state of every watched strategy/asset and what the policy says.

    Read-only. For assets that did not fire, ``if_it_fired`` shows what the policy would
    decide; it is inspection only and is never recorded or sent.
    """
    from market_signal.copilot.engine import candidates

    def read(ledger):
        cands = candidates(ledger, now=_now(now))
        return cands if full else [_brief(c) for c in cands]

    _call(read)


@copilot.command("preview")
def preview(
    strategy: str = typer.Argument(..., help="Strategy name or ID of a watched strategy."),
    symbol: str = typer.Option(..., "--symbol"),
    now: str = typer.Option(None, "--now", help="ISO time (default: now)."),
) -> None:
    """Render the Telegram message for a watched strategy/asset on its newest bar.

    Read-only rendering check. Unless the bar genuinely fired, the text is labelled
    PREVIEW — no current signal. Nothing is recorded or sent.
    """
    from market_signal.copilot import render
    from market_signal.copilot.engine import _record, candidates
    from market_signal.models.domain import utcnow

    def read(ledger):
        hit = [c for c in candidates(ledger, now=_now(now))
               if strategy in (c["strategy"], c["strategy_id"]) and c.get("symbol") == symbol.upper()]  # fmt: skip
        if not hit or not hit[0].get("evidence") or not hit[0].get("signal"):
            raise ValueError(f"no evaluable candidate for {strategy} {symbol}: "
                             f"{hit[0]['state'] if hit else 'not watched'}")  # fmt: skip
        c = hit[0]
        fired = c["state"] == "SIGNAL"
        rec = _record({**c, "result": c.get("result") or c["if_fired"]}, _now(now) or utcnow())
        console.print(_plain(render.render_alert(rec, preview=not fired)), highlight=False,
                      markup=False)  # fmt: skip
        return None

    _call(read, quiet=True)


@copilot.command("run")
def run_(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Evaluate and render; write/send nothing."
    ),
    update: bool = typer.Option(
        False, "--update/--no-update", help="Ingest Hyperliquid perp candles/funding first."
    ),
) -> None:
    """WRITE: evaluate live signals, record decisions, send new ALERTs via Telegram.

    Idempotent: a strategy/asset/bar is decided once per policy and a sent alert is never
    resent. Telegram failures are recorded and retried while the signal bar is live.
    """
    from market_signal.copilot.engine import run

    if update and not dry_run:
        from market_signal.perps.data import update_perps

        try:
            with open_store() as (settings, store):
                result = update_perps(settings, store)
                failed = result[result["status"] != "ok"]
                console.print(f"perp update: {len(result) - len(failed)} ok, {len(failed)} failed")
        except Exception as exc:  # a failed update must not stop the evaluation
            console.print(f"perp update failed: {exc}", markup=False)

    def go(ledger):
        def sender_factory():
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient

            return TelegramClient.from_settings(get_settings()).send

        out = run(ledger, software=_software(), sender_factory=sender_factory, dry_run=dry_run)
        if dry_run:
            for text in out.pop("would_send"):
                console.print("----- would send -----", markup=False)
                console.print(_plain(text), highlight=False, markup=False)
            out["new_decisions"] = [
                {k: r[k] for k in ("strategy", "symbol", "bar_close")} | {
                    "decision": r["result"]["decision"], "priority": r["result"]["priority"],
                    "blocked_by": r["result"]["blocked_by"]}
                for r in out["new_decisions"]
            ]  # fmt: skip
        return out

    out = _call(go, read_only=dry_run)
    if any(d["status"] == "failed" for d in out.get("deliveries") or []):
        raise typer.Exit(1)


@copilot.command("decisions")
def decisions(limit: int = typer.Option(50, "--limit")) -> None:
    """Recorded ALERT/SUPPRESS decisions (newest first) with delivery state."""
    from market_signal.copilot.engine import list_decisions

    _call(lambda ledger: list_decisions(ledger.store, limit))


@copilot.command("show")
def show(
    decision_id: str,
    message: bool = typer.Option(False, "--message", help="Print the rendered message only."),
) -> None:
    """One decision: evidence used, every rule result, policy, message, delivery attempts."""
    from market_signal.copilot.engine import show_decision

    def read(ledger):
        out = show_decision(ledger.store, decision_id)
        if message:
            text = (out["record"].get("message") or {}).get("text") or "(no message rendered)"
            console.print(_plain(text), highlight=False, markup=False)
            return None
        return out

    _call(read, quiet=message)
