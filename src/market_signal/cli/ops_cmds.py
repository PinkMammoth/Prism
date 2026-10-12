"""``market ops``: the always-on authoritative runtime (Phase 14; infrastructure only).

Jobs run the existing commands in their existing order; nothing here changes research,
co-pilot or paper semantics. See docs/OPERATIONS.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from market_signal.cli.common import console

ops = typer.Typer(no_args_is_help=True, help="Always-on runtime: authority, jobs, backups, health.")


def _db(db: Path | None) -> Path:
    from market_signal.config import get_settings

    return db or get_settings().paths.db


def _fail(msg: str, code: int) -> None:
    console.print(msg, style="red", markup=False)
    raise typer.Exit(code)


@ops.command("cycle")
def cycle(
    job: str = typer.Argument(..., help="prospective | oi | daily | backup | intraday"),
    trigger: str = typer.Option("manual", "--trigger", help="schedule | boot | manual (recorded)."),
    wait: float = typer.Option(0, "--wait", help="Seconds to queue behind a running job."),
) -> None:
    """WRITE (authoritative runtime only): run one job under the runtime lock.

    Exit codes: 0 ok, 1 a step failed, 2 refused (not authoritative / not provisioned),
    3 disk critical (nothing run), 75 another job is running.
    """
    from market_signal.data.store import NotAuthoritative
    from market_signal.ops import runtime as rt

    rt.setup_logging()
    try:
        out = rt.run_job(job, trigger=trigger, wait=wait)
    except (rt.RuntimeRefused, NotAuthoritative) as exc:
        rt.log.error("refused: %s", exc)
        raise typer.Exit(rt.EXIT_REFUSED) from None
    except rt.RuntimeBusy as exc:
        rt.log.warning("skipped: %s", exc)
        raise typer.Exit(rt.EXIT_BUSY) from None
    raise typer.Exit(out["exit"])


@ops.command("claim-authority")
def claim_authority(
    runtime_id: str = typer.Option(..., "--runtime-id"),
    reason: str = typer.Option(..., "--reason"),
    supersede: bool = typer.Option(False, "--supersede", help="Replace another runtime's claim."),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """WRITE: name the ONE runtime allowed to write this live database (cutover/rollback)."""
    from market_signal.ops import profiles
    from market_signal.ops import runtime as rt

    try:
        prof = profiles.resolve()
        if prof.explicit and runtime_id not in prof.profile.runtime_ids.values():
            raise ValueError(
                f"profile {prof.profile.identity} may only claim "
                f"{sorted(prof.profile.runtime_ids.values())}, not {runtime_id!r}"
            )
        out = rt.claim_authority(_db(db), runtime_id, reason, supersede=supersede)
    except profiles.ProfileError as exc:
        _fail(f"Refused: {exc}", profiles.EXIT_PROFILE)
    except (rt.RuntimeRefused, ValueError) as exc:
        _fail(f"Refused: {exc}", 2)
    console.print_json(json.dumps(out))


@ops.command("record-deploy")
def record_deploy(note: str = typer.Option(..., "--note")) -> None:
    """WRITE (authoritative runtime only): record a deployment of the current revision."""
    from market_signal.data.store import NotAuthoritative
    from market_signal.ops import profiles
    from market_signal.ops import runtime as rt

    db = _db(None)
    try:
        prof = profiles.resolve()
        rt.require_authoritative(db)
        with rt.runtime_lock(db, "record-deploy", 600), rt._store(db) as s:
            eid = rt._event(s, "deployed", {"note": note, "python": sys.version.split()[0],
                                            "profile": prof.record()})  # fmt: skip
    except profiles.ProfileError as exc:
        _fail(f"Refused: {exc}", profiles.EXIT_PROFILE)
    except (rt.RuntimeRefused, NotAuthoritative, rt.RuntimeBusy) as exc:
        _fail(f"Refused: {exc}", 2)
    console.print_json(json.dumps({"event_id": eid, "git_commit": rt.revision(rt._root())}))


@ops.command("backup")
def backup(
    kind: str = typer.Option("manual", "--kind", help="manual | daily | weekly"),
    label: str = typer.Option(None, "--label", help="e.g. pre-deploy-<commit>"),
    wait: float = typer.Option(600, "--wait"),
) -> None:
    """WRITE (authoritative runtime only): checkpoint, copy, verify and prune backups."""
    from market_signal.data.store import NotAuthoritative
    from market_signal.ops import runtime as rt

    rt.setup_logging()
    db = _db(None)
    try:
        rt.require_authoritative(db)
        out = rt.backup_live(db, kind=kind, label=label, wait=wait)
    except (
        rt.RuntimeRefused,
        NotAuthoritative,
        rt.RuntimeBusy,
        rt.DiskCritical,
        RuntimeError,
    ) as exc:
        _fail(f"Backup failed: {exc}", 1)
    console.print_json(json.dumps(out, default=str))


@ops.command("verify-backup")
def verify_backup(path: Path) -> None:
    """Read-only: open a backup (never modifying it) and check schema, run and ledger IDs."""
    from market_signal.ops import runtime as rt

    try:
        out = rt.verify_backup(path)
    except Exception as exc:
        _fail(f"Verification failed: {exc}", 1)
    console.print_json(json.dumps(out, default=str))


@ops.command("restore-test")
def restore_test(path: Path, scratch: Path = typer.Option(None, "--scratch")) -> None:
    """Restore rehearsal on a temporary scratch copy (production untouched)."""
    from market_signal.ops import runtime as rt

    out = rt.restore_test(path, scratch)
    console.print_json(json.dumps(out, default=str))
    if not out["restored_ok"]:
        raise typer.Exit(1)


@ops.command("continuity")
def continuity(
    db: Path = typer.Option(None, "--db"),
    out: Path = typer.Option(None, "--out", help="Write the fingerprint JSON here."),
    compare: Path = typer.Option(None, "--compare", help="Earlier fingerprint to compare with."),
) -> None:
    """Read-only fingerprint of every prospective experiment (paper run, ledger, trackings,
    watches, policies, evidence tables). With --compare, exit 1 on any unexpected change."""
    from market_signal.data.store import Store
    from market_signal.ops import runtime as rt

    st = Store(_db(db), read_only=True)
    try:
        fp = rt.fingerprint(st)
    finally:
        st.close()
    if out:
        out.write_text(json.dumps(fp, indent=1, sort_keys=True))
    if compare:
        diffs = rt.compare(json.loads(compare.read_text()), fp)
        console.print_json(json.dumps({"identical_evidence": not diffs, "differences": diffs}))
        if diffs:
            raise typer.Exit(1)
        return
    if not out:
        console.print_json(json.dumps(fp, default=str))


@ops.command("preflight")
def preflight() -> None:
    """Read-only deployment diagnostics: revision, role, database, claim, disk. Exit 1 if this
    process could not run jobs."""
    import platform

    from market_signal.data.store import Store, authority_claim
    from market_signal.ops import runtime as rt

    db = _db(None)
    info = {"revision": rt.revision(rt._root()), "python": platform.python_version(),
            "platform": platform.platform(), "role": rt.role() or "unset",
            "runtime_id": rt.runtime_id() or "unset", "db": str(db), "db_exists": db.exists()}  # fmt: skip
    ready = info["role"] == "authoritative" and db.exists()
    if db.exists():
        d = rt.disk_state(db)
        info["disk"] = {"level": d.level, "detail": d.describe()}
        st = Store(db, read_only=True, lock_timeout=60)
        try:
            claim = authority_claim(st.con)
            info["schema_version"] = st.con.execute(
                "SELECT max(version) FROM schema_version"
            ).fetchone()[0]
        finally:
            st.close()
        info["claim"] = claim and {"runtime_id": claim[0], "since": str(claim[1])}
        ready = (
            ready and claim is not None and claim[0] == rt.runtime_id() and d.level != "CRITICAL"
        )
    info["ready_for_jobs"] = ready
    console.print_json(json.dumps(info, default=str))
    if not ready:
        raise typer.Exit(1)


@ops.command("crontab")
def crontab(executable: str = typer.Option("market", "--executable")) -> None:
    """Print this profile's schedule as a crontab (UTC; consumed by supercronic).

    Only the jobs the resolved profile/authority allows, plus the watchdogs of the long-running
    processes it supervises. An invalid profile prints nothing and exits 78.
    """
    from market_signal.ops import profiles
    from market_signal.ops import runtime as rt

    try:
        prof = profiles.resolve()
    except profiles.ProfileError as exc:
        _fail(f"Refused: {exc}", profiles.EXIT_PROFILE)
    excluded = [job for job in rt.JOBS if not prof.job_allowed(job)]
    db = rt._db()
    if db.exists():
        with rt._store(db, read_only=True) as store:
            from market_signal.paper.v2.engine import work_needed

            if store.con.execute(
                "SELECT 1 FROM paper_v2_runs LIMIT 1"
            ).fetchone() and not work_needed(store):
                excluded += [
                    job
                    for job in ("paper_v2", "paper_v2_brief", "paper_v2_funding")
                    if not rt.active_steps(store, job)
                ]
    # Independent watchdogs catch a dead worker as well as a dead HTTP listener. No DB open.
    watchdogs = []
    if prof.process_allowed("context_gateway"):
        watchdogs.append(f"* * * * * {executable} context gateway health --check --json")
    if prof.process_allowed("free_source_collector"):
        watchdogs.append(f"* * * * * {executable} context sources status --check")
    sys.stdout.write(rt.crontab(executable, excluded_jobs=excluded) + "".join(
        line + "\n" for line in watchdogs))  # fmt: skip


@ops.command("profile")
def profile(
    process: str = typer.Option(None, "--process", help="Exit 0 only if this process runs."),
    condition: bool = typer.Option(
        False, "--condition", help="systemd ExecCondition: disabled -> 1, invalid -> 255."
    ),
    shell: bool = typer.Option(False, "--shell", help="PRISM_PROC_<name>=0|1 lines."),
) -> None:
    """Read-only: the validated runtime profile of this environment (no DB open).

    Exit 0 valid, 78 invalid/contradictory configuration (fail closed). With --process:
    0 enabled, 1 not enabled here.
    """
    from market_signal.ops import profiles

    try:
        prof = profiles.resolve()
    except profiles.ProfileError as exc:
        print(f"profile refused: {exc}", file=sys.stderr)
        raise typer.Exit(255 if condition else profiles.EXIT_PROFILE) from None
    if process is not None:
        if process not in profiles.PROCESSES:
            _fail(f"unknown process {process!r}; known: {sorted(profiles.PROCESSES)}", 2)
        enabled = prof.process_allowed(process)
        print(f"{process}: {'enabled' if enabled else 'not enabled'} "
              f"({prof.profile.identity}, authority={prof.authority})", file=sys.stderr)  # fmt: skip
        raise typer.Exit(0 if enabled else 1)
    if shell:
        for name in profiles.PROCESSES:
            sys.stdout.write(f"PRISM_PROC_{name}={int(prof.process_allowed(name))}\n")
        sys.stdout.write(f"PRISM_COLLECTOR_DYNAMIC={int(prof.collector_dynamic)}\n")
        sys.stdout.write(f"PRISM_BOOT_CATCHUP_JOB={prof.boot_catchup_job() or ''}\n")
        return
    console.print_json(json.dumps(prof.describe(), default=str))


@ops.command("boot-catchup")
def boot_catchup(wait: float = typer.Option(3600, "--wait")) -> None:
    """WRITE: restart/deploy catch-up: one idempotent cycle of the profile's catch-up job
    (prospective for research/legacy; none for the lab). A no-op if nothing is new."""
    from market_signal.ops import profiles

    try:
        job = profiles.resolve().boot_catchup_job()
    except profiles.ProfileError as exc:
        _fail(f"Refused: {exc}", profiles.EXIT_PROFILE)
    if not job:
        console.print("no boot catch-up job in this profile")
        return
    cycle(job, trigger="boot", wait=wait)


def register(app: typer.Typer) -> None:
    app.add_typer(ops, name="ops")


@ops.command("backup-inventory")
def backup_inventory() -> None:
    """Read-only inventory of recovery backups and export receipts."""
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    db = _db(None)
    console.print_json(json.dumps(b.inventory(rt.backup_dir(db), db)))


@ops.command("backup-retain")
def backup_retain(
    apply: bool = typer.Option(
        False, "--apply", help="Delete eligible exported copies; default dry-run."
    ),
    keep: int = typer.Option(1, "--keep"),
    previous: Path = typer.Option(None, "--previous"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """One global backup; a second requires a named rollback copy and reason."""
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    db = _db(None)
    if apply:
        rt.require_authoritative(db)
    with rt.runtime_lock(db, "backup-retention", 600):
        if apply:
            b.save_policy(rt.backup_dir(db), db, keep=keep, previous=previous, reason=reason)
        result = b.retain(
            rt.backup_dir(db), db, keep=keep, previous=previous, reason=reason, apply=apply
        )
    console.print_json(json.dumps(result))


@ops.command("backup-pin-latest")
def backup_pin_latest(reason: str = typer.Option(..., "--reason")) -> None:
    """Preserve the pre-deploy safety copy until health validation releases its rollback pin."""
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    db = _db(None)
    rt.require_authoritative(db)
    with rt.runtime_lock(db, "backup-pin", 600):
        root = rt.backup_dir(db)
        rows = [r for r in b.inventory(root, db) if r["manifest"].get("verification_result")]
        if not rows:
            _fail("No verified backup to pin", 1)
        result = b.save_policy(root, db, keep=2, previous=Path(rows[-1]["path"]), reason=reason)
    console.print_json(json.dumps(result))


@ops.command("lab-identity")
def lab_identity() -> None:
    """Read-only: the frozen autotrader-lab experiment identity and seed policy (no DB)."""
    from market_signal.ops import lab

    console.print_json(json.dumps({"experiment": lab.experiment(),
                                   "seed_policy": lab.SEED_POLICY_ID,
                                   "seed_policy_digest": lab.policy_digest()}))  # fmt: skip


@ops.command("lab-seed")
def lab_seed(
    source: Path = typer.Argument(..., help="A verified snapshot/backup (never the live DB)."),
    target: Path = typer.Argument(..., help="New lab database path (must not exist)."),
    frontier: str = typer.Option(
        ..., "--frontier", help="UTC ISO instant; nothing later is seeded."
    ),
    manifest: Path = typer.Option(None, "--manifest", help="Default: <target>.seed.json"),
) -> None:
    """WRITE (new file only): build an unclaimed lab database holding the minimum causal seed.

    Run with PRISM_RUNTIME_ROLE=scratch. The source is attached read-only and checksummed;
    the result carries no authority claim and no paper run.
    """
    from datetime import datetime

    from market_signal.config import get_settings
    from market_signal.ops import lab

    at = datetime.fromisoformat(frontier.replace("Z", "+00:00"))
    try:
        out = lab.build_seed(source, target, at, live_db=get_settings().paths.db)
    except lab.SeedRefused as exc:
        _fail(f"Refused: {exc}", 2)
    path = manifest or target.with_name(target.name + ".seed.json")
    path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n")
    console.print_json(json.dumps({k: out[k] for k in ("seed_id", "frontier", "target_sha256")}
                                  | {"manifest": str(path)}))  # fmt: skip
