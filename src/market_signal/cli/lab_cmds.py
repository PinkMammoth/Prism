"""Read-only Lab inspection. No evaluation or promotion commands."""

from __future__ import annotations

from collections.abc import Callable

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger, LedgerError

lab = typer.Typer(no_args_is_help=True, help="Inspect the Strategy Lab ledger (no evaluation).")


def _inspect(read: Callable[[Ledger], object]) -> None:
    try:
        with open_store(read_only=True) as (_, store):
            console.print_json(canonical_json(read(Ledger(store))))
    except (LedgerError, duckdb.Error) as exc:
        console.print(f"Lab inspection: {exc}", markup=False)
        raise typer.Exit(1) from None


@lab.command("strategies")
def strategies() -> None:
    """List canonical strategies and their fixed families."""
    _inspect(lambda ledger: ledger.list_strategies())


@lab.command("experiments")
def experiments() -> None:
    """List preregistered attempts, including failures and explicit reruns."""
    _inspect(lambda ledger: ledger.list_experiments())


@lab.command("experiment")
def experiment(experiment_id: str) -> None:
    """Inspect a preregistration, lifecycle, terminal result and recorded inspections."""
    _inspect(lambda ledger: ledger.inspect_experiment(experiment_id))


@lab.command("plan")
def plan(plan_id: str) -> None:
    """Inspect a frozen evaluation plan."""
    _inspect(lambda ledger: ledger.get_plan(plan_id).model_dump(mode="python"))


@lab.command("dataset")
def dataset(dataset_id: str) -> None:
    """Inspect dataset provenance and fingerprint strength (does not expose stored rows)."""
    _inspect(lambda ledger: ledger.get_dataset(dataset_id).model_dump(mode="python"))


def register(app: typer.Typer) -> None:
    app.add_typer(lab, name="lab")
