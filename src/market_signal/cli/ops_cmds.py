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
    job: str = typer.Argument(..., help="prospective | oi | daily | backup"),
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
    from market_signal.ops import runtime as rt

    try:
        out = rt.claim_authority(_db(db), runtime_id, reason, supersede=supersede)
    except (rt.RuntimeRefused, ValueError) as exc:
        _fail(f"Refused: {exc}", 2)
    console.print_json(json.dumps(out))


@ops.command("record-deploy")
def record_deploy(note: str = typer.Option(..., "--note")) -> None:
    """WRITE (authoritative runtime only): record a deployment of the current revision."""
    from market_signal.data.store import NotAuthoritative
    from market_signal.ops import runtime as rt

    db = _db(None)
    try:
        rt.require_authoritative(db)
        with rt.runtime_lock(db, "record-deploy", 600), rt._store(db) as s:
            eid = rt._event(s, "deployed", {"note": note, "python": sys.version.split()[0]})
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
    """Print the production schedule as a crontab (UTC; consumed by supercronic)."""
    from market_signal.ops import runtime as rt

    sys.stdout.write(rt.crontab(executable))


def register(app: typer.Typer) -> None:
    app.add_typer(ops, name="ops")
