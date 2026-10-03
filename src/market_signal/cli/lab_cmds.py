"""Read-only Lab inspection. No evaluation, screening or promotion commands."""

from __future__ import annotations

from collections.abc import Callable

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.models.domain import AssetClass
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.compiler import CompileError, compile_registered
from market_signal.research.lab.ledger import Ledger, LedgerError

lab = typer.Typer(no_args_is_help=True, help="Inspect the Strategy Lab ledger (no evaluation).")


def _inspect(read: Callable[[Ledger], object]) -> None:
    try:
        with open_store(read_only=True) as (_, store):
            console.print_json(canonical_json(read(Ledger(store))))
    except (LedgerError, CompileError, duckdb.Error) as exc:
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


@lab.command("compile")
def compile_(
    strategy_id: str,
    dataset_id: str,
    symbol: list[str] = typer.Option(None, "--symbol", help="Symbols (default: all)."),
    asset_class: AssetClass = typer.Option(None, "--asset-class", help="Required for spot."),
) -> None:
    """Compile daily features/signals from a retained snapshot; print summary metadata only.

    Nothing is persisted and no live market table is read. Signals are not trades.
    """
    _inspect(
        lambda ledger: [
            c.metadata.model_dump(mode="python")
            for c in compile_registered(
                ledger,
                strategy_id,
                dataset_id,
                symbols=tuple(symbol or ()),
                asset_class=asset_class,
            )
        ]
    )


def register(app: typer.Typer) -> None:
    app.add_typer(lab, name="lab")
