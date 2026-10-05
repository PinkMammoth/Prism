"""``market lab incubation``: Phase 21 fast candidate incubation (exploratory paper only).

READ-ONLY queries (recorded, immutable prospective records; ``--json`` for agents):
  candidates               what looks interesting right now? (latest decision per candidate)
  candidate <strategy>     evidence by era, each policy's decision/explanation, episodes, shadow
  status                   freezes, levels by policy and side, last run, Phase 12 paper baseline
  opportunities            prospective opportunity rate per policy
  compare                  prospective policy comparison (no winner is selected)
  episodes                 recorded episodes (admission, deactivation, shadow outcomes)
  policies                 the frozen policy definitions and IDs
  pool                     the frozen candidate pool and its long/short balance
  event-schema             the future external-event interface (schema only)
  calibrate                synthetic temporary-edge calibration (no data read)
  replay                   retrospective policy diagnostics on a store (read-only)

WRITES (append-only ``incubation_*`` tables):
  freeze                   register the frozen definition; prospective collection starts now
  run                      evaluate the newest bar in its live window, record shadow intents,
                           resolve shadow outcomes (the runtime's prospective job runs this)

No query accepts a window, threshold or date: the frozen policies decide. Nothing here
places an order, touches the Phase 12 paper account, or grants live trading.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from market_signal.cli.common import console, open_store

incubation = typer.Typer(
    no_args_is_help=True,
    help="Fast candidate incubation: exploratory paper admission (Phase 21; never live).",
)
JSON = typer.Option(False, "--json", help="Machine-readable output")
POLICY = typer.Option(None, "--policy", help="conservative, balanced or aggressive")


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _fail(exc: Exception) -> None:
    console.print(f"Incubation: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


def _emit(obj) -> None:
    print(json.dumps(obj, default=str, indent=1))


def _read(fn):
    from market_signal.research.lab.ledger import LedgerError

    with open_store(read_only=True) as (_, store):
        try:
            return fn(store)
        except (LedgerError, ValueError) as exc:
            _fail(exc)


@incubation.command("candidates")
def candidates_cmd(policy: str = POLICY,
                   level: str = typer.Option(None, help="e.g. EXPLORATORY_PAPER, WATCH"),
                   side: str = typer.Option(None, help="long or short"),
                   show_all: bool = typer.Option(False, "--all", help="Include NEUTRAL/INSUFFICIENT"),
                   as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: the latest recorded decision of every candidate (admitted and WATCH first)."""
    from market_signal.research.incubation import prospective as pr

    if policy:
        _check_policy(policy)
    rows = _read(lambda st: pr.candidates(st, profile=policy, level=level, side=side))
    if not show_all and not level:
        rows = [r for r in rows if r["level"] not in ("NEUTRAL", "INSUFFICIENT")]
    if as_json:
        _emit(rows)
        return
    if not rows:
        console.print("No candidates at these levels (try --all).")
    for r in rows:
        e = r["explanation"]
        console.print(f"{r['policy']:12s} {r['level']:17s} {r['side']:5s} {r['strategy']:40s} "
                      f"recent {e['recent_expectancy']} (t {e['recent_t']}, n {e['sample_size']}) "
                      f"signals {','.join(r['signals_at_bar']) or '-'}", markup=False)  # fmt: skip


def _check_policy(policy: str) -> None:
    from market_signal.research.incubation.policy import get

    try:
        get(policy)
    except ValueError as exc:
        _fail(exc)


@incubation.command("candidate")
def candidate_cmd(strategy: str = typer.Argument(..., help="Strategy name or ID")) -> None:
    """READ-ONLY: everything recorded for one candidate (always JSON)."""
    from market_signal.research.incubation import prospective as pr

    _emit(_read(lambda st: pr.candidate(st, strategy)))


def _paper_baseline(store) -> dict:
    """The Phase 12 paper account, read-only, as the conservative operational baseline."""
    try:
        from market_signal.paper.engine import status as paper_status

        runs = paper_status(store)
    except Exception as exc:  # absent tables / no run: report, never fail the query
        return {"available": False, "reason": str(exc)}
    return {"available": True, "runs": [
        {k: r.get(k) for k in ("run_id", "status", "created_at", "cohort", "equity",
                               "last_processed_bar", "open_positions", "pending_entry_orders",
                               "closed_trades", "drawdown")} for r in runs],
            "note": "read-only; Phase 21 never writes the paper account"}  # fmt: skip


@incubation.command("status")
def status_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: freezes, levels by policy and side, last run, and the paper baseline."""
    from market_signal.research.incubation import prospective as pr

    out = _read(lambda st: {**pr.status(st), "paper_baseline": _paper_baseline(st)})
    _emit(out) if as_json else console.print_json(json.dumps(out, default=str))


@incubation.command("opportunities")
def opportunities_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: prospective opportunity rate per policy (a diagnostic, not a target)."""
    from market_signal.research.incubation import prospective as pr

    out = _read(pr.opportunities)
    _emit(out) if as_json else console.print_json(json.dumps(out, default=str))


@incubation.command("compare")
def compare_cmd(as_json: bool = JSON) -> None:
    """READ-ONLY: CONSERVATIVE vs BALANCED vs AGGRESSIVE on the prospective record."""
    from market_signal.research.incubation import prospective as pr

    out = _read(lambda st: {**pr.compare(st), "paper_baseline": _paper_baseline(st)})
    _emit(out) if as_json else console.print_json(json.dumps(out, default=str))


@incubation.command("episodes")
def episodes_cmd(policy: str = POLICY, as_json: bool = JSON) -> None:
    """READ-ONLY: prospectively recorded episodes."""
    from market_signal.research.incubation import prospective as pr

    if policy:
        _check_policy(policy)
    out = _read(lambda st: pr.episodes(st, profile=policy))
    _emit(out) if as_json else console.print_json(json.dumps(out, default=str))


@incubation.command("policies")
def policies_cmd() -> None:
    """READ-ONLY: the frozen policy definitions, IDs and design rules."""
    from market_signal.research.incubation.policy import POLICIES, ShadowExecution
    from market_signal.research.incubation.synthetic import DESIGN_RULES

    _emit({"policies": {n: {"policy_id": p.policy_id, **p.model_dump(mode="json")}
                        for n, p in POLICIES.items()},
           "shadow_execution": ShadowExecution().model_dump(mode="json"),
           "design_rules": DESIGN_RULES})  # fmt: skip


@incubation.command("pool")
def pool_cmd() -> None:
    """READ-ONLY: what a freeze locks: pool, balance, policies, execution, costs, cadence."""
    from market_signal.config import get_settings
    from market_signal.perps.data import perp_config
    from market_signal.research.incubation import prospective as pr

    s = get_settings()
    defn = pr.build_freeze(perp_config(s), s.paths.config / "lab" / "families")
    _emit({**pr.frozen_summary(defn),
           "members": [{"name": m.name, "family": m.family, "side": m.side,
                        "strategy_id": m.strategy_id} for m in defn.members]})  # fmt: skip


@incubation.command("event-schema")
def event_schema_cmd() -> None:
    """READ-ONLY: the future external-event interface (Phase 21 ingests nothing)."""
    from market_signal.research.incubation.events import ExternalEvent

    _emit({"schema": ExternalEvent.model_json_schema(),
           "availability_rule": "available_at = first_seen_at (Prism's own first observation); "
           "publication time is provenance only", "implemented": "schema only"})  # fmt: skip


@incubation.command("calibrate")
def calibrate_cmd(quick: bool = typer.Option(False, help="Few seeds, no surface/grid (smoke)"),
                  workers: int = typer.Option(1, help="Processes (identical results for any count)"),
                  out: str = typer.Option(None, help="Write the JSON report here")) -> None:  # fmt: skip
    """READ-ONLY: synthetic temporary-edge calibration of the frozen policies (no data)."""
    from market_signal.research.incubation.synthetic import calibrate

    kw = dict(seeds=10, null_seeds=10, surface=False, grid=False) if quick else {}
    text = json.dumps(calibrate(workers=workers, **kw), indent=1, default=str)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@incubation.command("replay")
def replay_cmd(out: str = typer.Option(..., help="Write the JSON report here"),
               limit: int = typer.Option(None, help="First N pool members only (smoke)")) -> None:  # fmt: skip
    """READ-ONLY: retrospective diagnostics of the frozen policies to 2026-10-01 (never tuning)."""
    from market_signal.perps.data import perp_config
    from market_signal.research.incubation.replay import evaluate

    with open_store(read_only=True) as (settings, store):
        payload, meta = evaluate(store, perp_config(settings),
                                 settings.paths.config / "lab" / "families", limit=limit)  # fmt: skip
    Path(out).write_text(json.dumps({"payload": payload, "meta": meta}, indent=1, default=str))
    console.print(f"wrote {out}; digest {meta['digest']}; {meta['wall_seconds']} s")


@incubation.command("freeze")
def freeze_cmd(reason: str = typer.Option(..., help="Why prospective collection starts now"),
               dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be frozen")) -> None:  # fmt: skip
    """WRITE: register the frozen definition; only bars closing after now are ever evaluated."""
    from market_signal.perps.data import perp_config
    from market_signal.research.incubation import prospective as pr
    from market_signal.research.lab.ledger import Ledger, LedgerError

    with open_store(read_only=dry_run) as (settings, store):
        defn = pr.build_freeze(perp_config(settings), settings.paths.config / "lab" / "families")
        if dry_run:
            _emit(pr.frozen_summary(defn))
            return
        try:
            res = pr.freeze(Ledger(store), defn, reason=reason, origin="cli", software=_software())
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    _emit(res)


@incubation.command("run")
def run_cmd(now: str = typer.Option(None, help="ISO time (tests / replays of the runner)"),
            dry_run: bool = typer.Option(False, "--dry-run")) -> None:  # fmt: skip
    """WRITE: one idempotent prospective incubation cycle (shadow records only)."""
    from market_signal.research.incubation import prospective as pr
    from market_signal.research.lab.ledger import Ledger, LedgerError

    with open_store(read_only=dry_run) as (_, store):
        try:
            res = pr.run(Ledger(store), software=_software(),
                         now=None if now is None else datetime.fromisoformat(now), dry_run=dry_run)  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    _emit(res)
