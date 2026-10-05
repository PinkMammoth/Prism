"""``market lab edge``: Phase 20 time-varying edge and lifecycle evidence (research only).

READ-ONLY queries (stored, immutable edge profiles; ``--json`` for agents):
  status <strategy>      does this strategy appear to have a credible edge now?
  history <strategy>     the chronological evidence curve (the life of the edge)
  compare <strategy>     lifetime vs rolling vs weighted vs regime vs stress vs forward
  lifecycle <strategy>   simulated lifecycle transitions (append-only history)
  list                   latest profile per strategy and venue (filter by edge state)
  calibrate              synthetic planted-edge calibration (no data read)

WRITES (governed; scratch copies for research):
  study register|run     freeze / evaluate a lifecycle study (Phase 17 study adapter)
  profile build <run>    append the run's cutoff profiles to ``lab_edge_profiles``

No query accepts a window, weight, threshold or date range: windows are fixed by the frozen
policy, and ``--as-of`` only selects an evaluation that is already stored. Nothing here
grants alert, paper or live permissions, and no consumer reads these records.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from market_signal.cli.common import console, open_store

edge = typer.Typer(
    no_args_is_help=True,
    help="Time-varying edge and strategy-lifecycle evidence (Phase 20, research only).",
)
study = typer.Typer(no_args_is_help=True, help="Phase 20 lifecycle methodology studies.")
profile = typer.Typer(no_args_is_help=True, help="Immutable edge-profile snapshots.")
edge.add_typer(study, name="study")
edge.add_typer(profile, name="profile")


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _fail(exc: Exception) -> None:
    console.print(f"Edge: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


def _emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, default=str, indent=1))
    else:
        console.print_json(json.dumps(obj, default=str))


def _profiles(strategy: str | None, venue: str | None, as_of: str | None) -> list[dict]:
    from market_signal.research.lab.ledger import Ledger, LedgerError
    from market_signal.research.lifecycle.profile import load_profiles

    with open_store(read_only=True) as (_, store):
        try:
            rows = load_profiles(Ledger(store), strategy=strategy, venue=venue, as_of=as_of)
        except LedgerError as exc:
            _fail(exc)
    if strategy and not rows:
        _fail(ValueError(f"no stored edge profile for {strategy!r}"))
    return rows


def _latest(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:  # newest first
        key = (r["strategy_id"], r["venue"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


VENUE = typer.Option(None, help="binance or hyperliquid (default: every stored venue)")
AS_OF = typer.Option(None, "--as-of", help="Select a STORED evaluation time (never a new window)")
JSON = typer.Option(False, "--json", help="Machine-readable output")


@edge.command("status")
def status(strategy: str = typer.Argument(..., help="Strategy name or ID"),
           venue: str = VENUE, as_of: str = AS_OF, as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: edge state, divergence, recent/lifetime/regime/forward summary."""
    from market_signal.research.lifecycle.report import profile_status

    _emit([profile_status(p) for p in _latest(_profiles(strategy, venue, as_of))], as_json)


@edge.command("history")
def history(strategy: str = typer.Argument(...), venue: str = VENUE, as_of: str = AS_OF,
            as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: rolling net/excess/events/uncertainty/hit rate/MAE/MFE/drawdown/cost curve."""
    out = [{"strategy": p["strategy_name"], "venue": p["venue"], "as_of": p["as_of"],
            "window": "see policy curve_window", "curve": p["curve"], "eras": p["eras"],
            "decay": p["decay"]} for p in _latest(_profiles(strategy, venue, as_of))]  # fmt: skip
    _emit(out, as_json)


@edge.command("compare")
def compare(strategy: str = typer.Argument(...), venue: str = VENUE, as_of: str = AS_OF,
            as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: every evidence dimension side by side (recent never replaces lifetime)."""
    from market_signal.research.lifecycle.report import profile_compare

    _emit([profile_compare(p) for p in _latest(_profiles(strategy, venue, as_of))], as_json)


@edge.command("lifecycle")
def lifecycle(strategy: str = typer.Argument(...), venue: str = VENUE, as_of: str = AS_OF,
              mode: str = typer.Option("recent", help="static, recent or regime"),
              as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: simulated lifecycle summary and its append-only transition history."""
    out = []
    for p in _latest(_profiles(strategy, venue, as_of)):
        if mode not in p["lifecycle"]:
            _fail(ValueError(f"unknown mode {mode}"))
        out.append({"strategy": p["strategy_name"], "venue": p["venue"], "as_of": p["as_of"],
                    "mode": mode, "simulated": True, **p["lifecycle"][mode]})  # fmt: skip
    _emit(out, as_json)


@edge.command("list")
def list_cmd(state: str = typer.Option(None, help="Filter by edge state"), venue: str = VENUE,
             as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: latest stored profile per strategy/venue ("what currently appears credible?")."""
    from market_signal.research.lifecycle.report import profile_status

    rows = [profile_status(p) for p in _latest(_profiles(None, venue, None))]
    if state:
        rows = [r for r in rows if r["edge_state"] == state.upper()]
    if as_json:
        _emit(rows, True)
        return
    for r in rows:
        console.print(f"{r['venue']:11s} {r['strategy']:42s} {r['edge_state']:12s} "
                      f"{r['divergence']:20s} recent {r['recent']['mean']} (t {r['recent']['t']}) "
                      f"sim {r['simulated_lifecycle_state']}", markup=False)  # fmt: skip


@edge.command("calibrate")
def calibrate(quick: bool = typer.Option(False, help="20 seeds per scenario (smoke)"),
    workers: int = typer.Option(1, help="Processes (results are identical for any count)"),
              out: str = typer.Option(None, help="Write the JSON report here")) -> None:  # fmt: skip
    """READ-ONLY: synthetic planted-edge calibration under the frozen policy (no data)."""
    from market_signal.research.lifecycle.synthetic import calibrate as run

    seeds = ({k: 20 for k in ("dead", "stable", "emerging", "decaying", "reversing", "regime")}
             if quick else None)  # fmt: skip
    res = run(seeds=seeds, grid_seeds=20 if quick else 60, workers=workers)
    text = json.dumps(res, indent=1, default=str)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@study.command("register")
def study_register(manifest: str = typer.Argument(..., help="Phase 20 lifecycle manifest YAML"),
                   reason: str = typer.Option(..., help="Why this study is being frozen")) -> None:  # fmt: skip
    """WRITE: capture + retain the datasets and freeze the study. Evaluates nothing."""
    from market_signal.perps.data import perp_config
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError
    from market_signal.research.lifecycle.study import load_manifest

    man = load_manifest(manifest)
    with open_store() as (settings, store):
        try:
            defn = ss.register(Ledger(store), man, perps_cfg=perp_config(settings),
                               software=_software(), origin=f"cli:{manifest}", reason=reason,
                               catalogue_dir=settings.paths.config / "lab" / "families")  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(f"Frozen study [bold]{defn.study_id}[/] ({man.name}), EXPLORATORY; "
                  f"policy {defn.policy_id}; {len(defn.strategies)} strategies")  # fmt: skip
    for d in defn.datasets:
        console.print(f"  dataset {d.venue}/{d.coin}: {d.dataset_id}")


@study.command("run")
def study_run(study_id: str = typer.Argument(...),
              rerun_of: str = typer.Option(None, help="Earlier run of this study (explicit rerun)"),
              reason: str = typer.Option(None, help="Rerun reason (required with --rerun-of)")) -> None:  # fmt: skip
    """WRITE: commit a run row, evaluate from the retained snapshots, record the result."""
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError

    with open_store() as (_, store):
        try:
            out = ss.run(Ledger(store), study_id, software=_software(), rerun_of=rerun_of,
                         rerun_reason=reason)  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(f"run {out['run_id']}: {out['status']} digest={out['result_digest']}")
    console.print(out["meta"])
    if out["status"] != "COMPLETED":
        console.print(out["payload"].get("error", {}).get("message", ""), markup=False)
        raise typer.Exit(1)


@study.command("report")
def study_report(run_id: str = typer.Argument(...),
                 out: str = typer.Option(None, help="Write here instead of printing"),
                 as_json: bool = JSON) -> None:  # fmt: skip
    """READ-ONLY: render a stored result (presentation only; computes nothing new)."""
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger
    from market_signal.research.lifecycle.report import study_report as render

    with open_store(read_only=True) as (_, store):
        payload = ss.result_payload(Ledger(store), run_id)
    text = json.dumps(payload, indent=1) if as_json else render(payload)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@profile.command("build")
def profile_build(
    run_id: str = typer.Argument(..., help="A COMPLETED lifecycle study run"),
) -> None:
    """WRITE: append the run's cutoff profiles (with read-only Phase 8 forward evidence) to
    ``lab_edge_profiles``. Identical profiles are not rewritten."""
    from market_signal.research.lab.ledger import Ledger, LedgerError
    from market_signal.research.lifecycle.profile import profiles_from_run, record_profiles

    with open_store() as (_, store):
        try:
            ledger = Ledger(store)
            profs = profiles_from_run(ledger, run_id)
            res = record_profiles(ledger, profs, software=_software())
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(
        f"{len(profs)} profiles: {res['inserted']} inserted, {res['existing']} already stored"
    )
