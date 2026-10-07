"""``market microstructure``: Phase 24A Hyperliquid microstructure collection (data only).

PROCESS (long-running; the runtime container supervises it):
  collect                 persistent public-WebSocket collector -> spool (never the database)
WRITE (authoritative runtime job, or a scratch copy):
  ingest                  spool -> DuckDB, idempotent; records the production cutover once
READ-ONLY (every command supports ``--json``):
  status                  collector process + stored data summary + cutover
  health [--check]        verdict with reasons (--check: exit 1 when unhealthy)
  coverage [--hours N]    per-asset minute status counts (COMPLETE/PARTIAL/.../MISSING)
  latest COIN             the newest stored minute (and the newest spooled one)
  inspect COIN            the last N minutes with derived flow/book fields

No command places orders, reads keys, or touches paper, co-pilot, risk or research state.
"""

from __future__ import annotations

import json
import math
import os

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, open_store

micro = typer.Typer(no_args_is_help=True,
                    help="Hyperliquid microstructure collection (Phase 24A). Data only; no trading.")  # fmt: skip
JSON = typer.Option(False, "--json", help="Machine-readable output")
DIR = typer.Option(
    None, "--dir", help="Collector directory (default PRISM_MICRO_DIR or <db dir>/microstructure)"
)


def _spool(path):
    from market_signal.microstructure.spool import Spool, default_root

    return Spool(path or default_root())


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_clean(v) for v in o]
    if isinstance(o, float) and math.isnan(o):
        return None
    if hasattr(o, "isoformat"):
        return None if pd.isna(o) else o.isoformat()
    if hasattr(o, "tolist"):  # numpy arrays and scalars
        return _clean(o.tolist())
    return o


def _out(obj, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(_clean(obj), indent=2, sort_keys=True, default=str))
    return as_json


def _schema(store) -> bool:
    try:
        store.con.execute("SELECT 1 FROM microstructure_minutes LIMIT 1")
        return True
    except Exception:
        return False


@micro.command("collect")
def collect(
    dir: str = DIR,
    coins: str = typer.Option(",".join(("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")), help="Comma-separated coins"),
    duration: float = typer.Option(0, help="Stop after N seconds (0 = run until SIGTERM); for smoke tests"),
    no_raw: bool = typer.Option(False, "--no-raw", help="Do not keep the short-retention raw journal"),
    raw_days: int = typer.Option(int(os.environ.get("PRISM_MICRO_RAW_DAYS", "3")), help="Raw journal retention (days)"),
    spool_days: int = typer.Option(int(os.environ.get("PRISM_MICRO_SPOOL_DAYS", "10")), help="Spool retention (days, >= 8 for large-print calibration)"),
) -> None:  # fmt: skip
    """Run the persistent collector. Refuses unless this is the authoritative runtime or an
    explicit scratch environment (PRISM_RUNTIME_ROLE=scratch). Public data only."""
    import asyncio

    from market_signal.data.store import ROLE_ENV, RUNTIME_ID_ENV
    from market_signal.microstructure.spool import CollectorBusy
    from market_signal.microstructure.ws import Collector
    from market_signal.ops.runtime import _root, revision, setup_logging

    role = os.environ.get(ROLE_ENV, "").strip().lower()
    rid = os.environ.get(RUNTIME_ID_ENV, "").strip()
    if not ((role == "authoritative" and rid) or role == "scratch"):
        console.print(f"Refused: the collector runs only as the authoritative runtime ({ROLE_ENV}="
                      f"authoritative + {RUNTIME_ID_ENV}) or in an explicit scratch environment "
                      f"({ROLE_ENV}=scratch, which never becomes production data).", style="red", markup=False)  # fmt: skip
        raise typer.Exit(2)
    if spool_days < 8:
        console.print(
            "[red]--spool-days must be >= 8 (the large-print threshold needs 7 prior days)[/]"
        )
        raise typer.Exit(2)
    setup_logging()
    c = Collector(_spool(dir), tuple(x.strip().upper() for x in coins.split(",") if x.strip()),
                  role=role, runtime_id=rid, git_commit=revision(_root()), raw=not no_raw,
                  duration_s=duration or None, spool_days=spool_days, raw_days=raw_days)  # fmt: skip
    try:
        rc = asyncio.run(c.run())
    except CollectorBusy as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(75) from None
    raise typer.Exit(rc)


@micro.command("ingest")
def ingest_cmd(dir: str = DIR, as_json: bool = JSON) -> None:
    """Ingest the collector spool into the database (idempotent; single writer)."""
    from market_signal.microstructure.ingest import ingest

    sp = _spool(dir)
    with open_store() as (_settings, store):
        out = ingest(store, sp)
    if not _out(out, as_json):
        console.print(f"microstructure ingest {out['status'].upper()}: " +
                      ", ".join(f"{k}={v}" for k, v in sorted(out.items()) if k not in ("status",)))  # fmt: skip
    if out["status"] != "ok":
        raise typer.Exit(1)


@micro.command("health")
def health(dir: str = DIR, check: bool = typer.Option(False, "--check", help="Exit 1 when unhealthy"),
           as_json: bool = JSON) -> None:  # fmt: skip
    """Collector + data health with reasons."""
    from market_signal.microstructure.health import evaluate

    with open_store(read_only=True) as (_s, store):
        ev = evaluate(store if _schema(store) else None, _spool(dir))
    if not _out(ev, as_json):
        console.print(f"Microstructure: {'[green]HEALTHY[/]' if ev['healthy'] else '[red]UNHEALTHY[/]'} "
                      f"(collector {ev['collector']})")  # fmt: skip
        for i in ev["issues"]:
            console.print(f"  [red]•[/] {i}", markup=True)
        for w in ev["warnings"]:
            console.print(f"  [yellow]•[/] {w}")
    if check and not ev["healthy"]:
        raise typer.Exit(1)


@micro.command("status")
def status(dir: str = DIR, as_json: bool = JSON) -> None:
    """Collector process, stored data, retention usage and the production cutover."""
    from market_signal.microstructure import definitions as d
    from market_signal.microstructure.health import collector_state
    from market_signal.microstructure.query import cutover

    sp = _spool(dir)
    col = collector_state(sp)
    with open_store(read_only=True) as (_s, store):
        ok = _schema(store)
        co = cutover(store) if ok else None
        counts = store.con.execute(
            "SELECT count(*), min(minute_open), max(minute_open), count(DISTINCT coin) FROM "
            "microstructure_minutes").fetchone() if ok else (0, None, None, 0)  # fmt: skip
        revs = (
            store.con.execute("SELECT count(*) FROM microstructure_revisions").fetchone()[0]
            if ok
            else 0
        )
        late = dict(store.con.execute("SELECT disposition, count(*) FROM microstructure_late_events "
                                      "GROUP BY 1").fetchall()) if ok else {}  # fmt: skip
    st = col["status"] or {}
    obj = {"feature_version": d.FEATURE_VERSION, "definition": d.definition_digest(),
           "collector": {"state": col["state"], "issues": col["issues"], "warnings": col["warnings"],
                         "connected": st.get("connected"), "reconnects": st.get("reconnects"),
                         "run_id": st.get("run_id"), "role": st.get("role"),
                         "msgs_per_s": st.get("msgs_per_s_60s"), "latency_ms": st.get("latency_ms"),
                         "cpu_s": st.get("cpu_s"), "max_rss_mb": st.get("max_rss_mb"),
                         "counters": st.get("counters")},
           "stored": {"rows": counts[0], "first_minute": counts[1], "last_minute": counts[2],
                      "coins": counts[3], "revisions": revs, "late_events": late},
           "cutover": co, "disk": sp.usage()}  # fmt: skip
    if _out(obj, as_json):
        return
    console.print(
        f"[bold]Microstructure[/] {d.FEATURE_VERSION} (definition {d.definition_digest()})"
    )
    console.print(f"collector: {col['state']}; connected={st.get('connected')} reconnects={st.get('reconnects')} "
                  f"msgs/s={st.get('msgs_per_s_60s')} latency={st.get('latency_ms')}")  # fmt: skip
    for i in col["issues"]:
        console.print(f"  [red]• {i}[/]")
    console.print(f"stored: {counts[0]} rows, {counts[3]} coins, {counts[1]} → {counts[2]}; "
                  f"revisions {revs}; late {late}")  # fmt: skip
    console.print(
        f"cutover: {co['first_minute'] + ' (' + co['runtime_id'] + ')' if co else 'not yet (no production minute ingested)'}"
    )
    u = sp.usage()
    console.print(
        f"files: spool {u['spool_bytes'] / 2**20:.1f} MiB, raw {u['raw_bytes'] / 2**20:.1f} MiB"
    )


@micro.command("coverage")
def coverage_cmd(
    hours: int = typer.Option(24, help="Window (hours)"), as_json: bool = JSON
) -> None:
    """Per-asset minute status counts over the window."""
    from market_signal.microstructure.health import coverage

    with open_store(read_only=True) as (_s, store):
        cov = coverage(store, minutes=hours * 60) if _schema(store) else pd.DataFrame()
    rows = cov.to_dict("records") if len(cov) else []
    if _out(rows, as_json):
        return
    t = Table(title=f"Microstructure coverage, last {hours}h")
    cols = ["coin", "COMPLETE", "PARTIAL", "TRADE_ONLY", "BOOK_ONLY", "GAP", "MISSING", "complete_frac",
            "newest_minute"]  # fmt: skip
    for c in cols:
        t.add_column(c)
    for r in rows:
        t.add_row(*[str(r[c])[:19] for c in cols])
    console.print(t)


def _frame(store, coin: str, minutes: int):
    from market_signal.microstructure.query import load_microstructure
    from market_signal.models.domain import utcnow

    end = pd.Timestamp(utcnow()).floor("min")
    return load_microstructure(store, coin.upper(), end - pd.Timedelta(minutes=minutes), end,
                               production_only=False)  # fmt: skip


SHOW = ["minute_open", "status", "n_trades", "delta_ntl", "buy_frac", "lp_n", "spread_bps_mean",
        "imb5", "imb20", "mid_end", "oi_end", "lat_p50_ms", "available_at"]  # fmt: skip


@micro.command("latest")
def latest(coin: str, dir: str = DIR, as_json: bool = JSON) -> None:
    """The newest stored minute for COIN (all derived fields) and the newest spooled minute."""
    from market_signal.microstructure import definitions as d

    with open_store(read_only=True) as (_s, store):
        r = store.con.execute("SELECT max(minute_open) FROM microstructure_minutes WHERE coin=?",
                              [coin.upper()]).fetchone()[0] if _schema(store) else None  # fmt: skip
        row = None
        if r is not None:
            from market_signal.microstructure.query import load_microstructure

            t = pd.Timestamp(r)
            df = load_microstructure(store, coin.upper(), t, t + pd.Timedelta(minutes=1),
                                     production_only=False)  # fmt: skip
            row = df.iloc[-1].to_dict()
    spooled = None
    for f in reversed(_spool(dir).files()[-3:]):
        for rec in reversed(_spool(dir).read_lines(f)[0]):
            if rec.get("kind") == "minute" and rec.get("coin") == coin.upper() and \
                    rec.get("feature_version") == d.FEATURE_VERSION:  # fmt: skip
                spooled = {k: v for k, v in rec.items() if k != "fh"}
                break
        if spooled:
            break
    obj = {"coin": coin.upper(), "stored": row, "spooled": spooled}
    if _out(obj, as_json):
        return
    if row is None and spooled is None:
        console.print(f"no microstructure minute for {coin.upper()} yet")
        return
    for k, v in (row or spooled).items():
        console.print(f"{k:>18}: {v}")


@micro.command("inspect")
def inspect(coin: str, minutes: int = typer.Option(10, help="Number of recent minutes"),
            as_json: bool = JSON) -> None:  # fmt: skip
    """The last N minutes for COIN (gap-filled: MISSING rows are shown, never zero-filled)."""
    with open_store(read_only=True) as (_s, store):
        df = _frame(store, coin, minutes) if _schema(store) else pd.DataFrame(columns=SHOW)
    if _out(df[SHOW].to_dict("records") if len(df) else [], as_json):
        return
    t = Table(title=f"{coin.upper()} microstructure, last {minutes} min (UTC)")
    for c in SHOW:
        t.add_column(c)
    for r in df[SHOW].itertuples(index=False):
        t.add_row(*[(f"{v:.4g}" if isinstance(v, float) else str(v)[:19]) for v in r])
    console.print(t)


def register(app: typer.Typer) -> None:
    app.add_typer(micro, name="microstructure")
