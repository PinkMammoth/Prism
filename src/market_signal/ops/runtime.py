"""Always-on runtime (Phase 14): one authoritative writer, scheduled jobs, backups, health.

Infrastructure only. Nothing here decides, records or alters research, co-pilot or paper
evidence. A job runs the existing CLI commands, in the order the home-PC wrappers ran them,
as child processes. The ``runtime_*`` tables (migration 17) hold deployment/runtime facts only.

- **Authority:** the live database carries an ``authority_claimed`` event naming the one
  runtime allowed to write it (enforced for every writable open in ``Store``). Jobs refuse to
  run anywhere else, and never create a fresh database.
- **Single pipeline:** a job holds an exclusive ``flock`` next to the database for its whole
  duration. The kernel drops it when the process dies, so a crash cannot leave a stale lock.
  DuckDB's own file lock still guards every individual open.
- **Idempotency:** every step is an existing idempotent command, so a retry after a crash,
  restart or duplicate trigger re-records nothing.
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import duckdb

from market_signal.data.store import (
    MIGRATIONS,
    ROLE_ENV,
    RUNTIME_ID_ENV,
    Store,
    authority_claim,
)
from market_signal.models.domain import utcnow

# Each job is the existing commands, in the existing order (forward_run.sh, oi_collect.sh,
# daily.sh). Later steps run even if an earlier one failed, exactly as the wrappers did.
JOBS: dict[str, list[tuple[str, list[str]]]] = {
    "prospective": [
        ("forward", ["lab", "forward", "run"]),  # perp candles/funding (+HL OI snapshot) first
        ("copilot", ["lab", "copilot", "run", "--no-update"]),
        ("paper", ["lab", "paper", "run"]),
        ("brief", ["lab", "paper", "brief", "--send"]),  # snapshot + one brief per paper day
    ],
    "oi": [("oi", ["oi", "collect"])],
    "daily": [("update", ["update"]), ("scan", ["scan"])],
    "backup": [],  # in-process: ``backup_live``
    # Phase 15: intraday perp bars (data only) + observational shadow timing. Never strategy,
    # co-pilot or paper evaluation: those stay on the daily prospective cadence above.
    "intraday": [("bars", ["bars", "update"]), ("shadow", ["bars", "shadow", "--record"])],
}

# UTC. Daily bars close at 00:00 UTC and Hyperliquid serves the closed candle and the settled
# funding hour within seconds, so 00:10 is the main run. The rest are retries inside the
# forward 1-day live window / paper 12 h entry window (all no-ops when nothing is new), and
# keep the gap between prospective runs ≤ 7 h 10 m for the external heartbeat.
SCHEDULE: dict[str, list[str]] = {
    "prospective": ["00:10", "00:45", "03:00", "06:00", "11:00", "17:00"],
    "backup": ["01:30"],
    "oi": ["02:30", "08:30", "14:30", "20:30"],  # Binance backfill + HL snapshot (data only)
    "daily": ["09:00"],  # broad market update + scan (was the 10:00 UK "Prism daily" task)
    # "*:MM" = every hour at MM. One minute after each 15m close; 1h/4h bars are fetched by the
    # same run only once a new one has closed. A missed run is recovered by the next one.
    "intraday": ["*:01", "*:16", "*:31", "*:46"],
}
SCHEDULED_WAIT = 3600  # a scheduled job queues behind a running one for up to this long
JOB_WAIT = {"intraday": 600}  # a late intraday run is pointless: the next one is 15 minutes away
QUIET_JOBS = {"intraday"}  # alert on the first failure after a success only (no 15-minute spam)
STEP_TIMEOUT = float(os.environ.get("PRISM_STEP_TIMEOUT", str(45 * 60)))

DISK_WARN, DISK_CRITICAL = 0.15, 0.05  # free fraction of the database filesystem
BACKUP_KEEP = {"daily": 7, "weekly": 5, "manual": 6}

EXIT_FAILED, EXIT_REFUSED, EXIT_DISK, EXIT_BUSY = 1, 2, 3, 75

log = logging.getLogger("prism.runtime")


class RuntimeRefused(RuntimeError):
    """Not allowed / not provisioned to run here."""


class RuntimeBusy(RuntimeError):
    """Another job holds the runtime lock."""


class DiskCritical(RuntimeError):
    pass


# --------------------------------------------------------------------------- identity


def role() -> str:
    return os.environ.get(ROLE_ENV, "").strip().lower()


def runtime_id() -> str:
    return os.environ.get(RUNTIME_ID_ENV, "").strip()


def revision(root: Path) -> str | None:
    """The deployed git commit: ``git rev-parse HEAD``, else the exported ``REVISION`` file."""
    with suppress(OSError, subprocess.CalledProcessError):
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.strip()  # fmt: skip
        if out:
            return out
    f = root / "REVISION"
    return (f.read_text().strip() or None) if f.is_file() else None


def _root() -> Path:
    from market_signal.config import find_project_root

    return find_project_root()


def _db() -> Path:
    from market_signal.config import get_settings

    return get_settings().paths.db


def backup_dir(db: Path) -> Path:
    env = os.environ.get("PRISM_BACKUP_DIR")
    return Path(env) if env else db.parent / "backups"


def _event(store: Store, event_type: str, payload: dict, rid: str | None = None) -> str:
    eid = f"rtev_{uuid.uuid4().hex}"
    store.con.execute(
        "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?)",
        [eid, event_type, rid or runtime_id() or "unset", utcnow(), revision(_root()),
         json.dumps(payload, sort_keys=True, default=str)],
    )  # fmt: skip
    return eid


def claim_authority(db: Path, new_id: str, reason: str, *, supersede: bool = False) -> dict:
    """Record that runtime ``new_id`` is the only writer of ``db`` (an existing file).

    Opening bypasses the guard on purpose: this is the one command that changes authority.
    Replacing another runtime's claim requires ``supersede`` (planned cutover or rollback).
    """
    if not new_id.strip() or not reason.strip():
        raise ValueError("runtime id and reason are required")
    if not db.exists():
        raise RuntimeRefused(f"{db} does not exist; authority is only claimed on a real database")
    store = Store(db, lock_timeout=float(os.environ.get("PRISM_LOCK_TIMEOUT", "600")),
                  authority_check=False)  # fmt: skip
    try:
        prev = authority_claim(store.con)
        if prev and prev[0] == new_id:
            return {"runtime_id": new_id, "already_claimed_at": str(prev[1]), "recorded": False}
        if prev and not supersede:
            raise RuntimeRefused(f"already claimed by {prev[0]!r} at {prev[1]}; pass --supersede "
                                 "only for a planned cutover or rollback")  # fmt: skip
        payload = {"reason": reason.strip(), "previous": prev[0] if prev else None,
                   "host": socket.gethostname()}  # fmt: skip
        eid = _event(store, "authority_claimed", payload, rid=new_id)
        return {"runtime_id": new_id, "event_id": eid, "previous": payload["previous"],
                "recorded": True}  # fmt: skip
    finally:
        store.close()


def require_authoritative(db: Path) -> None:
    """Jobs run only as the authoritative runtime, on an existing database claimed for it."""
    if role() != "authoritative" or not runtime_id():
        raise RuntimeRefused(
            f"this is not the authoritative runtime ({ROLE_ENV}=authoritative and "
            f"{RUNTIME_ID_ENV} are required); production jobs never run on a development machine"
        )
    if not db.exists():
        raise RuntimeRefused(f"{db} does not exist; the runtime is not provisioned "
                             "(it never creates a fresh live database)")  # fmt: skip


# --------------------------------------------------------------------------- disk


@dataclass
class DiskState:
    path: str
    total: int
    free: int

    @property
    def free_frac(self) -> float:
        return self.free / self.total if self.total else 0.0

    @property
    def level(self) -> str:
        f = self.free_frac
        return "CRITICAL" if f < DISK_CRITICAL else "WARNING" if f < DISK_WARN else "OK"

    def describe(self) -> str:
        return (f"{self.free / 2**30:.2f} GiB free of {self.total / 2**30:.2f} GiB "
                f"({self.free_frac:.1%}; warn < {DISK_WARN:.0%}, critical < {DISK_CRITICAL:.0%})")  # fmt: skip


def disk_state(path: Path) -> DiskState:
    p = path if path.is_dir() else path.parent
    u = shutil.disk_usage(p)
    return DiskState(str(p), u.total, u.free)


# --------------------------------------------------------------------------- lock


@contextmanager
def runtime_lock(db: Path, job: str, wait: float) -> Iterator[None]:
    """Exclusive for one job at a time on this database. Waits up to ``wait`` seconds."""
    path = db.with_name(db.name + ".runtime.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start >= wait:
                    holder = path.read_text().strip() or "unknown"
                    raise RuntimeBusy(f"another runtime job is running ({holder})") from None
                time.sleep(min(2.0, max(wait - (time.monotonic() - start), 0.05)))
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "job": job,
                                 "since": utcnow().isoformat()}).encode())  # fmt: skip
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- logging


def setup_logging() -> None:
    """UTC-stamped lines on stdout (platform logs) and, with PRISM_LOG_DIR, a size-capped
    rotating file (5 × 5 MB) so logs can never fill the disk."""
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    fmt.converter = time.gmtime
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    log.addHandler(out)
    d = os.environ.get("PRISM_LOG_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        f = RotatingFileHandler(Path(d) / "runtime.log", maxBytes=5 * 2**20, backupCount=5)
        f.setFormatter(fmt)
        log.addHandler(f)
    log.propagate = False


# --------------------------------------------------------------------------- notifications


def infra_alert(text: str) -> None:
    """Best-effort Telegram message about the runtime itself (never about trading)."""
    try:
        from market_signal.config import get_settings
        from market_signal.portfolio.telegram import TelegramClient

        TelegramClient.from_settings(get_settings()).send(
            "⚙️ <b>PRISM INFRA</b> (runtime health, not a trading message)\n" + html.escape(text)
        )
    except Exception as exc:  # the alert path must never break a job
        log.warning("infra alert not delivered: %s", type(exc).__name__)


def heartbeat(ok: bool) -> None:
    """Ping PRISM_HEARTBEAT_URL (healthchecks.io style: ``/fail`` suffix on failure)."""
    url = os.environ.get("PRISM_HEARTBEAT_URL", "").strip()
    if not url:
        return
    try:
        import httpx

        httpx.get(url if ok else url.rstrip("/") + "/fail", timeout=10)
    except Exception as exc:
        log.warning("heartbeat not delivered: %s", type(exc).__name__)


# --------------------------------------------------------------------------- jobs


def _run_step(job: str, name: str, args: list[str]) -> dict:
    cmd = [sys.executable, "-m", "market_signal.cli.main", *args]
    log.info("[%s/%s] start: market %s", job, name, " ".join(args))
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env={**os.environ, "COLUMNS": "160"})  # fmt: skip
    timed_out = threading.Event()

    def _kill() -> None:  # a hung step must not hold the runtime lock forever
        timed_out.set()
        p.kill()

    timer = threading.Timer(STEP_TIMEOUT, _kill)
    timer.start()
    try:
        assert p.stdout is not None
        for line in p.stdout:
            log.info("[%s/%s] %s", job, name, line.rstrip())
        rc = p.wait()
    finally:
        timer.cancel()
    dur = round(time.monotonic() - t0, 1)
    log.info("[%s/%s] exit=%s in %.1fs%s", job, name, rc, dur,
             " (TIMED OUT)" if timed_out.is_set() else "")  # fmt: skip
    return {"step": name, "exit": rc, "seconds": dur, "timed_out": timed_out.is_set()}


def run_job(job: str, *, trigger: str, wait: float,
            step_runner: Callable[[str, str, list[str]], dict] = _run_step) -> dict:  # fmt: skip
    """Run one job as the authoritative runtime. Returns a summary with ``exit``."""
    if job not in JOBS:
        raise ValueError(f"unknown job {job!r}; jobs: {sorted(JOBS)}")
    root, db = _root(), _db()
    rev = revision(root)
    require_authoritative(db)
    disk = disk_state(db)
    log.info("job=%s trigger=%s runtime=%s commit=%s python=%s db=%s disk: %s", job, trigger,
             runtime_id(), (rev or "unknown")[:12], sys.version.split()[0], db, disk.describe())  # fmt: skip
    if disk.level == "CRITICAL":
        msg = (f"{job} NOT run on {runtime_id()}: disk critical, {disk.describe()}. Nothing was "
               "deleted. Free space or grow the volume.")  # fmt: skip
        log.error(msg)
        infra_alert(msg)
        if job == "prospective":
            heartbeat(False)
        return {"job": job, "status": "refused_disk", "exit": EXIT_DISK, "disk": disk.describe()}
    if disk.level == "WARNING":
        log.warning("disk low: %s", disk.describe())

    with runtime_lock(db, job, wait):
        cycle_id = f"rtcyc_{uuid.uuid4().hex}"
        started = utcnow()
        with _store(db) as s:  # also enforces the authority claim before anything runs
            s.con.execute(
                "INSERT INTO runtime_cycles VALUES (?, ?, ?, ?, ?, ?, NULL, 'running', ?)",
                [cycle_id, job, runtime_id(), trigger, rev, started,
                 json.dumps({"disk": disk.describe(), "host": socket.gethostname()})],
            )  # fmt: skip
        steps = []
        if job == "backup":
            try:
                out = backup_live(db, kind="daily", locked=True)
                steps.append({"step": "backup", "exit": 0, "path": out["path"]})
            except Exception as exc:
                log.error("[backup] failed: %s", exc)
                steps.append({"step": "backup", "exit": EXIT_FAILED, "error": str(exc)[:500]})
        else:
            for name, args in JOBS[job]:
                steps.append(step_runner(job, name, args))
        ok = all(st["exit"] == 0 for st in steps)
        status = "ok" if ok else "failed"
        finished = utcnow()
        summary = {"steps": steps, "disk": disk.describe(), "host": socket.gethostname(),
                   "seconds": round((finished - started).total_seconds(), 1)}  # fmt: skip
        with _store(db) as s:
            s.con.execute(
                "UPDATE runtime_cycles SET finished_at=?, status=?, payload=? WHERE cycle_id=?",
                [finished, status, json.dumps(summary, default=str), cycle_id],
            )
    log.info("job=%s %s in %.1fs: %s", job, status.upper(), summary["seconds"],
             " ".join(f"{st['step']}={st['exit']}" for st in steps))  # fmt: skip
    if job == "prospective":
        heartbeat(ok)
    if not ok and (job not in QUIET_JOBS or _previous_ok(db, job, cycle_id)):
        infra_alert(f"{job} cycle FAILED on {runtime_id()} ({trigger}): "
                    + ", ".join(f"{st['step']}={st['exit']}" for st in steps)
                    + ". Steps are idempotent; the next scheduled run retries.")  # fmt: skip
    return {"job": job, "cycle_id": cycle_id, "status": status,
            "exit": 0 if ok else EXIT_FAILED, **summary}  # fmt: skip


def _previous_ok(db: Path, job: str, cycle_id: str) -> bool:
    """Was the job's previous cycle (before ``cycle_id``) ok, or is this its first cycle?"""
    with _store(db) as s:
        row = s.con.execute("SELECT status FROM runtime_cycles WHERE job=? AND cycle_id<>? AND "
                            "finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                            [job, cycle_id]).fetchone()  # fmt: skip
    return row is None or row[0] == "ok"


@contextmanager
def _store(db: Path, read_only: bool = False) -> Iterator[Store]:
    s = Store(db, read_only=read_only,
              lock_timeout=float(os.environ.get("PRISM_LOCK_TIMEOUT", "600")))  # fmt: skip
    try:
        yield s
    finally:
        s.close()


def crontab(executable: str = "market") -> str:
    lines = [
        "# Generated by `market ops crontab` (schedule: market_signal.ops.runtime.SCHEDULE). UTC."
    ]
    for job, times in SCHEDULE.items():
        for t in times:
            h, m = t.split(":")
            lines.append(f"{int(m)} {'*' if h == '*' else int(h)} * * * {executable} ops cycle {job} "
                         f"--trigger schedule --wait {JOB_WAIT.get(job, SCHEDULED_WAIT)}")  # fmt: skip
    return "\n".join(lines) + "\n"


def next_run(job: str, now: datetime) -> datetime:
    now = now.astimezone(utcnow().tzinfo)
    cands = []
    for t in SCHEDULE[job]:
        hh, mm = t.split(":")
        if hh == "*":  # hourly at :MM
            c = now.replace(minute=int(mm), second=0, microsecond=0)
            cands.append(c if c > now else c + timedelta(hours=1))
            continue
        c = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        cands.append(c if c > now else c + timedelta(days=1))
    return min(cands)


# --------------------------------------------------------------------------- continuity


EVIDENCE_PREFIXES = ("lab_", "copilot_", "paper_")


def fingerprint(store: Store, *, table_digests: bool = True) -> dict[str, Any]:
    """Everything the prospective experiments depend on, for before/after comparison.

    Read-only. ``runtime_*`` (deployment facts) are deliberately excluded; the authority claim
    is reported separately because a cutover is expected to change it.
    """
    con = store.con

    def rows(sql: str, params: list | None = None) -> list:
        return [list(r) for r in con.execute(sql, params or []).fetchall()]

    def one(sql: str, params: list | None = None):
        r = con.execute(sql, params or []).fetchone()
        return None if r is None else r[0]

    out: dict[str, Any] = {"schema_version": one("SELECT max(version) FROM schema_version")}
    claim = authority_claim(con)
    out["authority"] = None if claim is None else {"runtime_id": claim[0], "since": str(claim[1])}
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                        "WHERE table_schema='main'").fetchall()}  # fmt: skip

    if "paper_runs" in tables:
        from market_signal.paper import engine, observe
        from market_signal.paper import policy as pol

        code = {"promotion": pol.promotion_policy(1).policy_id, "risk": pol.risk_policy(1).policy_id,
                "exit": pol.exit_policy(1).policy_id, "maturity": pol.maturity_policy(1).policy_id}  # fmt: skip
        runs = []
        for r in con.execute(
            "SELECT run_id, created_at, promotion_policy_id, risk_policy_id, execution_model_id, "
            "exit_policy_id, maturity_policy_id, engine_version FROM paper_runs ORDER BY created_at"
        ).fetchall():
            rid = r[0]
            last = con.execute("SELECT seq, event_id, event_type FROM paper_events WHERE run_id=? "
                               "ORDER BY seq DESC LIMIT 1", [rid]).fetchone()  # fmt: skip
            st = engine.account(store, rid)
            try:
                a = observe.account_summary(observe.RunView(store, rid, utcnow()))
                acct = {k: a[k] for k in ("status", "starting_equity", "equity", "cash",
                                          "peak_equity", "open_positions", "pending_entry_orders",
                                          "closed_trades", "maturity", "as_of_bar")}  # fmt: skip
            except Exception as exc:  # pragma: no cover - reported, not hidden
                acct = {"status": st.status, "error": str(exc)}
            runs.append({
                "run_id": rid, "created_at": str(r[1]),
                "policies": {"promotion": r[2], "risk": r[3], "execution_model": r[4],
                             "exit": r[5], "maturity": r[6]},
                "policies_match_code_v1": {"promotion": r[2] == code["promotion"],
                                           "risk": r[3] == code["risk"], "exit": r[5] == code["exit"],
                                           "maturity": r[6] == code["maturity"]},
                "engine_version": r[7],
                "event_count": one("SELECT count(*) FROM paper_events WHERE run_id=?", [rid]),
                "last_event": None if last is None else {"seq": last[0], "event_id": last[1],
                                                         "event_type": last[2]},
                "account": acct,
                "snapshots": one("SELECT count(*) FROM paper_snapshots WHERE run_id=?", [rid])
                if "paper_snapshots" in tables else None,
                "latest_snapshot_id": one("SELECT snapshot_id FROM paper_snapshots WHERE run_id=? "
                                          "ORDER BY as_of_seq DESC, recorded_at DESC LIMIT 1", [rid])
                if "paper_snapshots" in tables else None,
                "briefs": rows("SELECT brief_id, bar_close FROM paper_briefs WHERE run_id=? "
                               "ORDER BY bar_close", [rid]) if "paper_briefs" in tables else None,
            })  # fmt: skip
        out["paper_runs"] = runs

    if "lab_forward_trackings" in tables:
        out["forward_trackings"] = rows(
            "SELECT t.tracking_id, t.strategy_id, t.enrolled_at, (SELECT status FROM "
            "lab_forward_status s WHERE s.tracking_id=t.tracking_id ORDER BY recorded_at DESC "
            "LIMIT 1) FROM lab_forward_trackings t ORDER BY t.tracking_id"
        )
        out["forward_evaluations"] = one("SELECT count(*) FROM lab_forward_evaluations")
    if "copilot_watchlist" in tables:
        from market_signal.copilot.policy import get_policy

        out["copilot_watches"] = rows(
            "SELECT w.watch_id, w.strategy_id, w.policy_id, w.registered_at, (SELECT status FROM "
            "copilot_watch_status s WHERE s.watch_id=w.watch_id ORDER BY recorded_at DESC LIMIT 1) "
            "FROM copilot_watchlist w ORDER BY w.watch_id"
        )
        out["copilot_policy_v1"] = get_policy(1).policy_id
        out["copilot_decisions"] = one("SELECT count(*) FROM copilot_decisions")
    oi = {}
    for t, col in (("perp_oi_history", "observed_at"), ("perp_snapshots", "snapshot_at")):
        if t in tables:
            for src, n, last in con.execute(f"SELECT source, count(*), max({col}) FROM {t} "
                                            "GROUP BY 1 ORDER BY 1").fetchall():  # fmt: skip
                oi[f"{t}/{src}"] = {"rows": n, "last": str(last)}
    out["oi"] = oi
    intraday = {}
    if "perp_intraday_bars" in tables:
        for src, tf, n, last in con.execute("SELECT source, timeframe, count(*), max(open_time) FROM "
                                            "perp_intraday_bars GROUP BY 1, 2 ORDER BY 1, 2").fetchall():  # fmt: skip
            intraday[f"{src}/{tf}"] = {"rows": n, "last": str(last)}
    out["intraday"] = intraday
    if table_digests:
        digests = {}
        for t in sorted(tables):
            if t.startswith(EVIDENCE_PREFIXES):
                n, h = con.execute(f'SELECT count(*), bit_xor(hash(x)) FROM "{t}" x').fetchone()
                digests[t] = [n, None if h is None else str(h)]
        out["evidence_tables"] = digests
    return json.loads(json.dumps(out, default=str))


# Keys a host move is allowed to change: the claim itself, and (for oi) data appended by new
# collection. Anything else differing is a continuity failure.
_EXPECTED_TO_CHANGE = {"authority", "oi", "intraday"}


def compare(before: dict, after: dict, *, allow: set[str] = frozenset()) -> list[str]:
    """Differences between two fingerprints, ignoring keys the migration may change."""
    diffs = []
    skip = _EXPECTED_TO_CHANGE | set(allow)
    if "oi" not in allow:  # new collection may append rows; venues never merge or disappear
        b, a = before.get("oi") or {}, after.get("oi") or {}
        if set(b) != set(a):
            diffs.append(f"oi series changed: {sorted(b)} -> {sorted(a)}")
        diffs += [f"oi {k}: rows decreased {b[k]['rows']} -> {a[k]['rows']}"
                  for k in set(b) & set(a) if a[k]["rows"] < b[k]["rows"]]  # fmt: skip
    if "intraday" not in allow:  # market data may be appended (new series too), never lost
        b, a = before.get("intraday") or {}, after.get("intraday") or {}
        diffs += [f"intraday {k}: series disappeared" for k in sorted(set(b) - set(a))]
        diffs += [f"intraday {k}: rows decreased {b[k]['rows']} -> {a[k]['rows']}"
                  for k in sorted(set(b) & set(a)) if a[k]["rows"] < b[k]["rows"]]  # fmt: skip
    for k in sorted(set(before) | set(after)):
        if k in skip:
            continue
        if k == "evidence_tables":
            b, a = before.get(k) or {}, after.get(k) or {}
            for t in sorted(set(b) | set(a)):
                if b.get(t) != a.get(t):
                    diffs.append(f"evidence table {t}: {b.get(t)} -> {a.get(t)}")
        elif before.get(k) != after.get(k):
            diffs.append(f"{k}: differs")
    return diffs


# --------------------------------------------------------------------------- backups


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with src.open("rb") as fi, tmp.open("wb") as fo:
        shutil.copyfileobj(fi, fo, 2**20)
        fo.flush()
        os.fsync(fo.fileno())
    os.replace(tmp, dest)


def backup_live(db: Path, *, kind: str = "daily", label: str | None = None,
                locked: bool = False, wait: float = 600) -> dict:  # fmt: skip
    """Versioned, verified copy of the live database.

    Checkpoint (so the file is complete without a WAL), then copy while holding a read-only
    DuckDB connection, which keeps every writer out for the duration. Copy to ``.part`` and
    rename, so a crash never leaves a half backup. Verify by opening the copy read-only and
    matching its fingerprint. Then prune to the retention policy (only ``prism-*.duckdb``
    inside the backup directory; never the live file).
    """
    if kind not in BACKUP_KEEP:
        raise ValueError(f"backup kind must be one of {sorted(BACKUP_KEEP)}")
    if not locked:
        with runtime_lock(db, f"backup:{kind}", wait):
            return backup_live(db, kind=kind, label=label, locked=True)
    root = backup_dir(db)
    size = db.stat().st_size
    disk = disk_state(db)
    if disk.free - 1.2 * size < DISK_CRITICAL * disk.total:
        raise DiskCritical(
            f"not enough space for a backup ({size / 2**20:.0f} MiB): {disk.describe()}"
        )
    with _store(db) as s:
        s.con.execute("CHECKPOINT")
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    name = f"prism-{stamp}" + (
        f"-{''.join(c for c in label if c.isalnum() or c in '-_')}" if label else ""
    )
    dest = root / kind / f"{name}.duckdb"
    with _store(db, read_only=True) as ro:
        wal = db.with_name(db.name + ".wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise RuntimeError(
                f"{wal} is not empty after CHECKPOINT; refusing an inconsistent copy"
            )
        expected = fingerprint(ro)
        _copy_atomic(db, dest)
    result = verify_backup(dest, expected)
    weekly = None
    if kind == "daily":
        wk = sorted((root / "weekly").glob("prism-*.duckdb"))
        if not wk or (utcnow().timestamp() - wk[-1].stat().st_mtime) > 6.5 * 86400:
            weekly = root / "weekly" / dest.name
            weekly.parent.mkdir(parents=True, exist_ok=True)
            os.link(dest, weekly)  # same bytes, no extra space while both exist
    pruned = prune_backups(root)
    with _store(db) as s:
        _event(s, "backup_verified", {**result, "kind": kind, "weekly": str(weekly) if weekly else None,
                                      "pruned": pruned})  # fmt: skip
    log.info("backup %s verified (%s MiB, sha256 %s…); pruned %d", dest, round(size / 2**20, 1),
             result["sha256"][:12], len(pruned))  # fmt: skip
    return {**result, "weekly": str(weekly) if weekly else None, "pruned": pruned}


def verify_backup(path: Path, expected: dict | None = None) -> dict:
    """Open a backup read-only (never modifies it) and check it is a complete Prism database:
    readable, current schema, the paper run(s) and the latest ledger/snapshot IDs present, and
    (if ``expected`` is given) the same fingerprint as the live database at copy time."""
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"backup {path} missing or empty")
    sha = _sha256(path)
    st = Store(path, read_only=True)
    try:
        fp = fingerprint(st)
    finally:
        st.close()
    problems = []
    if fp["schema_version"] != len(MIGRATIONS):
        problems.append(f"schema version {fp['schema_version']} != {len(MIGRATIONS)}")
    if not fp.get("paper_runs"):
        problems.append("no paper run")
    if expected is not None:
        problems += compare(expected, fp, allow={"oi"})
        if expected.get("authority") != fp.get("authority"):
            problems.append("authority claim differs")
    if problems:
        raise RuntimeError(f"backup {path} failed verification: {'; '.join(problems)}")
    return {"path": str(path), "sha256": sha, "bytes": path.stat().st_size,
            "schema_version": fp["schema_version"],
            "paper_runs": [{"run_id": r["run_id"], "event_count": r["event_count"],
                            "last_event": r["last_event"], "latest_snapshot_id": r["latest_snapshot_id"]}
                           for r in fp["paper_runs"]]}  # fmt: skip


def restore_test(path: Path, scratch: Path | None = None) -> dict:
    """Restore rehearsal on a scratch copy: copy, open writable as ``scratch`` (as a restore
    would, applying any pending migrations), fingerprint, delete. Production is untouched."""
    with tempfile.TemporaryDirectory(dir=scratch) as d:
        copy = Path(d) / "restore-test.duckdb"
        _copy_atomic(path, copy)
        before = os.environ.get(ROLE_ENV)
        os.environ[ROLE_ENV] = "scratch"
        try:
            st = Store(copy)
            try:
                fp = fingerprint(st)
            finally:
                st.close()
        finally:
            if before is None:
                os.environ.pop(ROLE_ENV, None)
            else:
                os.environ[ROLE_ENV] = before
        st = Store(path, read_only=True)
        try:
            orig = fingerprint(st)
        finally:
            st.close()
        diffs = compare(orig, fp, allow={"schema_version"})
    return {"path": str(path), "restored_ok": not diffs, "differences": diffs,
            "paper_runs": [r["run_id"] for r in fp.get("paper_runs") or []]}  # fmt: skip


def prune_backups(root: Path) -> list[str]:
    removed = []
    for kind, keep in BACKUP_KEEP.items():
        files = sorted((root / kind).glob("prism-*.duckdb"))
        for f in files[: max(len(files) - keep, 0)]:
            f.unlink()
            removed.append(str(f))
        for part in (root / kind).glob("*.part"):  # an interrupted copy, never a backup
            part.unlink()
            removed.append(str(part))
    return removed


# --------------------------------------------------------------------------- health


def health_rows(store: Store, now: datetime | None = None) -> list[dict]:
    """Infrastructure rows for ``market status`` (separate from strategy evidence). Read-only."""
    now = now or utcnow()
    con = store.con
    rows = []
    claim = authority_claim(con)
    if claim is None:
        return [{"component": "Runtime (infra)", "state": "NOT SET UP",
                 "detail": "no authority claim: development/unclaimed database"}]  # fmt: skip
    me = role() == "authoritative" and runtime_id() == claim[0]
    last = {}
    with suppress(duckdb.Error):
        for job, status, fin, start in con.execute(
            "SELECT job, status, finished_at, started_at FROM runtime_cycles c WHERE started_at = "
            "(SELECT max(started_at) FROM runtime_cycles d WHERE d.job=c.job)"
        ).fetchall():
            last[job] = (status, fin, start)
        ok_pro = con.execute("SELECT max(finished_at) FROM runtime_cycles WHERE job='prospective' "
                             "AND status='ok'").fetchone()[0]  # fmt: skip
    age_h = (
        None
        if not last.get("prospective") or ok_pro is None
        else (now - ok_pro).total_seconds() / 3600
    )
    max_gap = 7.2 + 1  # the schedule's longest gap plus an hour
    state = (
        "OK"
        if age_h is not None and age_h <= max_gap
        else "STALE"
        if age_h is not None
        else "NOT RUN"
    )
    if last.get("prospective", ("ok",))[0] == "failed":
        state = "ATTENTION" if state == "OK" else state
    jobs = "; ".join(f"{j} {s} {str(st)[5:16]}" for j, (s, _f, st) in sorted(last.items()))
    rows.append({"component": "Runtime (infra)", "state": state,
                 "detail": f"authoritative: {claim[0]} since {str(claim[1])[:16]}"
                           f"{' (this process)' if me else ' — this process is NOT it (read-only copy)'}; "
                           f"last ok prospective {'never' if age_h is None else f'{age_h:.1f}h ago'}; "
                           f"next {next_run('prospective', now).strftime('%m-%d %H:%M')} UTC; {jobs}"})  # fmt: skip
    b = None
    with suppress(duckdb.Error):
        b = con.execute("SELECT recorded_at, payload FROM runtime_events WHERE event_type="
                        "'backup_verified' ORDER BY recorded_at DESC LIMIT 1").fetchone()  # fmt: skip
    if b is None:
        rows.append(
            {"component": "Backups (infra)", "state": "NOT SET UP", "detail": "no verified backup"}
        )
    else:
        bh = (now - b[0]).total_seconds() / 3600
        p = json.loads(b[1])
        rows.append({"component": "Backups (infra)", "state": "OK" if bh <= 26 else "STALE",
                     "detail": f"latest verified {bh:.1f}h ago: {Path(p['path']).name} "
                               f"({p['bytes'] / 2**20:.0f} MiB); keep {BACKUP_KEEP}"})  # fmt: skip
    if me or role() == "":
        d = disk_state(store.path)
        rows.append({"component": "Disk (infra)",
                     "state": {"OK": "OK", "WARNING": "ATTENTION", "CRITICAL": "CRITICAL"}[d.level],
                     "detail": d.describe()})  # fmt: skip
    tg = []
    with suppress(duckdb.Error):
        tg = con.execute(
            "SELECT status, recorded_at FROM (SELECT status, recorded_at FROM copilot_deliveries "
            "UNION ALL SELECT status, recorded_at FROM paper_notifications) "
            "WHERE recorded_at >= ? ORDER BY recorded_at",
            [now - timedelta(days=1)],
        ).fetchall()
    failed = [r for r in tg if r[0] == "failed"]
    rows.append({"component": "Telegram (infra)",
                 "state": "ATTENTION" if tg and tg[-1][0] == "failed" else "OK",
                 "detail": f"last 24h: {len(tg)} delivery records, {len(failed)} failed"
                           + (f"; latest {tg[-1][0]} {str(tg[-1][1])[5:16]}" if tg else "")})  # fmt: skip
    return rows
