"""Strategy Lab commands.

Inspection, ``compile``, ``families``/``family show`` and ``batch generate`` (without
``--register``) are read-only. ``preregister``, ``screen``, ``batch generate --register``,
``batch create``, ``batch run`` and ``evidence build`` are the only writes; they follow the ledger lifecycle (freeze /
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
from market_signal.research.lab.evidence import (
    POLICIES,
    REPORT_POLICIES,
    batch_report,
    build_profiles,
    gather,
    load_profiles,
    record_profiles,
)
from market_signal.research.lab.families import (
    catalogue_summary,
    generate,
    load_catalogue,
    load_request,
    plan_family_batch,
    register_family_batch,
)
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


family = typer.Typer(no_args_is_help=True, help="Structured strategy families (templates).")
lab.add_typer(family, name="family")


def _catalogue() -> dict:
    from market_signal.config import get_settings

    return load_catalogue(get_settings().paths.config / "lab" / "families")


def _print(obj) -> None:
    console.print_json(canonical_json(obj))


@lab.command("families")
def families() -> None:
    """List the strategy-family catalogue (no database access)."""
    try:
        _print(catalogue_summary(_catalogue()))
    except ValueError as exc:
        console.print(f"Lab: {exc}", markup=False)
        raise typer.Exit(1) from None


@family.command("show")
def family_show(
    name: str,
    version: int = typer.Option(None, "--version", help="Default: latest version."),
    market: str = typer.Option("perp", "--market", help="perp | spot"),
) -> None:
    """Dry run: every variant a family generates for a market. Nothing is registered."""
    try:
        cat = _catalogue()
        versions = sorted(v for (n, v) in cat if n == name)
        if not versions:
            raise ValueError(f"unknown family {name}")
        f = cat[(name, version or versions[-1])]
        variants = generate(f, market)
        _print({"family": f.family, "version": f.version, "family_id": f.family_id,
                "title": f.title, "rationale": f.rationale, "market": market,
                "sides": list(f.sides(market)), "variants": len(variants),
                "members": [v.summary() for v in variants]})  # fmt: skip
    except (ValueError, KeyError) as exc:
        console.print(f"Lab: {exc}", markup=False)
        raise typer.Exit(1) from None


@batch.command("generate")
def batch_generate(
    request: Path,
    register: bool = typer.Option(False, "--register", help="WRITE: register new variants."),
    out: Path = typer.Option(None, "--out", help="Where to write the batch manifest YAML."),
) -> None:
    """Families -> a Phase 5 batch manifest. Default is a dry run (read-only, no results).

    The dry run shows variants per family, parameters, strategy IDs, registration state and
    whether the Monte Carlo resolution supports the family size. With --register, missing
    variants are submitted to the ledger and the manifest is written to --out; the batch
    is NOT frozen or run (use `market lab batch create` then `batch run`).
    """
    import yaml

    req = load_request(request)
    if not register:

        def dry(ledger):
            report = plan_family_batch(ledger, req, _catalogue())
            report.pop("_variants")
            return report

        _inspect(dry)
        return
    if out is None:
        console.print("Lab: --register needs --out for the manifest", markup=False)
        raise typer.Exit(1)
    if out.exists():
        console.print(f"Lab: {out} exists; manifests are never overwritten", markup=False)
        raise typer.Exit(1)

    def write(ledger):
        manifest, submitted = register_family_batch(ledger, req, _catalogue(), origin="cli")
        data = manifest.model_dump(mode="json")
        out.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        return {
            "manifest": str(out),
            "members": len(manifest.members),
            "new_submissions": submitted,
        }

    _inspect(write, read_only=False)


evidence = typer.Typer(
    no_args_is_help=True,
    help="Consumer-neutral evidence profiles (descriptive; never trade decisions).",
)
lab.add_typer(evidence, name="evidence")


def _policy(version: int | None):
    version = version or max(POLICIES)
    if version not in POLICIES:
        raise LedgerError(f"unknown evidence policy version {version}; known: {sorted(POLICIES)}")
    return POLICIES[version]


@evidence.command("build")
def evidence_build(
    batch_id: str,
    run: str = typer.Option(None, "--run", help="Default: latest completed run."),
    policy_version: int = typer.Option(None, "--policy-version", help="Default: latest."),
) -> None:
    """WRITE: profile every member of a completed batch under an evidence policy version.

    Idempotent and append-only: identical inputs, policy and builder give the same profile
    IDs. Profiles under other policies or builders are kept, never rewritten.
    """

    def build(ledger):
        records, analysis = gather(ledger, batch_id, run)
        policy = _policy(policy_version)
        profiles = build_profiles(
            records, analysis, policy, builder_software_id=_software().software_id
        )
        out = record_profiles(ledger, profiles, policy)
        return {**out, "analysis_id": analysis["analysis_id"], "policy_version": policy.version}

    _inspect(build, read_only=False)


@evidence.command("report")
def evidence_report(
    batch_id: str,
    run: str = typer.Option(None, "--run", help="Default: latest completed run."),
    policy_version: int = typer.Option(None, "--policy-version", help="Default: latest."),
    report_version: int = typer.Option(None, "--report-version", help="Default: latest."),
) -> None:
    """Descriptive batch evidence report (families, plateaus, spikes, breadth, horizons)."""

    def report(ledger):
        _, analysis = gather(ledger, batch_id, run)
        policy_id = _policy(policy_version).policy_id
        rv = report_version or max(REPORT_POLICIES)
        if rv not in REPORT_POLICIES:
            raise LedgerError(f"unknown report policy version {rv}")
        profiles = [
            p
            for p in load_profiles(ledger, analysis_id=analysis["analysis_id"])
            if p["policy_id"] == policy_id
        ]
        if not profiles:
            raise LedgerError("no profiles for this analysis and policy; run `evidence build`")
        return {"analysis_id": analysis["analysis_id"], "policy_id": policy_id,
                **batch_report(profiles, REPORT_POLICIES[rv])}  # fmt: skip

    _inspect(report)


@evidence.command("show")
def evidence_show(strategy_id: str) -> None:
    """Every recorded evidence profile for one strategy (all policies and batches)."""
    _inspect(lambda ledger: load_profiles(ledger, strategy_id=strategy_id))


def register(app: typer.Typer) -> None:
    app.add_typer(lab, name="lab")
