"""Strategy Lab commands.

Inspection and ``compile`` are read-only. ``preregister``, ``screen``, ``batch create``
and ``batch run`` are the only writes; they follow the ledger lifecycle (freeze /
preregister -> start -> one terminal result -> one batch analysis). There are no
promotion or AI-generation commands.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.models.domain import AssetClass
from market_signal.research.lab.batch import (
    freeze_batch,
    inspect_batch,
    list_batches,
    load_manifest,
    run_batch,
)
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.compiler import CompileError, compile_registered
from market_signal.research.lab.ledger import Ledger, LedgerError
from market_signal.research.lab.provenance import capture_software
from market_signal.research.lab.screen import run_screen

lab = typer.Typer(no_args_is_help=True, help="Strategy Lab ledger, compiler and fast screen.")


def _inspect(read: Callable[[Ledger], object], *, read_only: bool = True) -> object:
    try:
        with open_store(read_only=read_only) as (_, store):
            out = read(Ledger(store))
            console.print_json(canonical_json(out))
            return out
    except (LedgerError, CompileError, ValueError, duckdb.Error) as exc:
        console.print(f"Lab: {exc}", markup=False)
        raise typer.Exit(1) from None


def _software():
    # The installed package's source tree, identical for preregister and screen.
    import market_signal

    return capture_software(Path(market_signal.__file__).resolve().parents[2])


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


@lab.command("preregister")
def preregister(
    submission_id: str,
    plan_id: str,
    dataset_id: str,
    role: str = typer.Option(
        ..., "--role", help="discovery | development | validation | final_holdout"
    ),
    asset: list[str] = typer.Option(..., "--asset", help="Assets (repeat)."),
    batch: str = typer.Option(None, "--batch"),
    rerun_of: str = typer.Option(None, "--rerun-of", help="Prior attempt of the same experiment."),
    rerun_reason: str = typer.Option(None, "--rerun-reason"),
) -> None:
    """WRITE: commit a preregistered attempt (before any evaluation)."""
    _inspect(
        lambda ledger: ledger.preregister(
            submission_id, plan_id, dataset_id, role=role, assets=tuple(asset),
            software=_software(), origin="cli", batch_id=batch, rerun_of=rerun_of,
            rerun_reason=rerun_reason,
        ).model_dump(mode="python"),
        read_only=False,
    )  # fmt: skip


@lab.command("screen")
def screen_(experiment_id: str) -> None:
    """WRITE: run a preregistered fast-screen attempt and attach its terminal result.

    Triage only (NO_EVENTS, INSUFFICIENT_EVENTS, WEAK, INTERESTING, ERROR). INTERESTING
    means "candidate for full research", never validated or tradeable. Inspect the stored
    result with `market lab experiment <id>`.
    """
    out = _inspect(
        lambda ledger: run_screen(ledger, experiment_id, software=_software()), read_only=False
    )
    if out["triage"] == "ERROR":  # recorded as an errored result; still a failed run
        raise typer.Exit(1)


batch = typer.Typer(no_args_is_help=True, help="Preregistered search batches (testing families).")
lab.add_typer(batch, name="batch")


@lab.command("batches")
def batches() -> None:
    """List frozen batches and their status (FROZEN, RUNNING, COMPLETED, FAILED)."""
    _inspect(list_batches)


@batch.command("create")
def batch_create(manifest: Path) -> None:
    """WRITE: validate a YAML batch manifest and freeze its testing family permanently.

    Membership, plan, dataset, role, primary horizon, correction and survivor rule can
    never change afterwards; a different family is a new batch. Nothing is screened.
    """
    _inspect(
        lambda ledger: {"batch_id": freeze_batch(ledger, load_manifest(manifest), origin="cli")},
        read_only=False,
    )


@batch.command("show")
def batch_show(batch_id: str) -> None:
    """Inspect a batch definition, its runs and their immutable analyses."""
    _inspect(lambda ledger: inspect_batch(ledger, batch_id))


@batch.command("run")
def batch_run(
    batch_id: str,
    rerun_of: str = typer.Option(None, "--rerun-of", help="Earlier run of this batch."),
    rerun_reason: str = typer.Option(None, "--rerun-reason"),
) -> None:
    """WRITE: preregister and screen every member, then record the FDR analysis.

    Prints counts and survivors; the full analysis is in `market lab batch show`.
    FDR_SURVIVOR is a candidate for independent research, never a validation.
    """

    def run(ledger):
        out = run_batch(
            ledger, batch_id, software=_software(), rerun_of=rerun_of, rerun_reason=rerun_reason
        )
        return {
            "analysis_id": out["analysis_id"],
            "run_id": out["run_id"],
            "status": out["status"],
            "counts": out.get("counts"),
            "error": out.get("error"),
            "fdr_survivors": [
                m["strategy_id"]
                for m in out.get("members", [])
                if m["batch_status"] == "FDR_SURVIVOR"
            ],
        }

    out = _inspect(run, read_only=False)
    if out["status"] != "completed":
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.add_typer(lab, name="lab")
