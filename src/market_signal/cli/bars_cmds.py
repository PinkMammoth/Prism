"""``market bars``: intraday perp bars (Phase 15). Market data only; never a signal.

update    incremental (live venues; what the runtime schedules every 15 minutes)
backfill  explicit range, disk-guarded, idempotent
status    coverage per venue/coin/timeframe (first/last, expected/actual, gaps, staleness)
gaps      missing intervals with their category
show      stored bars (optionally as Prism knew them at an instant)
align     causal multi-timeframe view at an instant
shadow    shadow execution timing around paper entries (observational)
"""

from __future__ import annotations

import json

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, open_store

bars = typer.Typer(
    no_args_is_help=True, help="Intraday perp bars (15m/1h/4h): data only, never a signal."
)

STYLE = {"OK": "green", "HISTORICAL": "cyan", "GAPS": "yellow", "STALE": "red", "INVALID": "red",
         "NO DATA": "yellow", "ok": "green", "failed": "red"}  # fmt: skip


def _tfs(timeframe: list[str] | None):
    from market_signal.intraday.grid import parse_timeframe

    return [parse_timeframe(t) for t in timeframe] if timeframe else None


def _coins(coin: list[str] | None):
    return [c.upper() for c in coin] if coin else None


def _ts(value: str | None):
    if value is None:
        return None
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")  # CLI input is UTC


def _fmt(t) -> str:
    return "-" if t is None or pd.isna(t) else f"{pd.Timestamp(t):%Y-%m-%d %H:%M}"


def render_results(df: pd.DataFrame, title: str) -> None:
    t = Table(title=title)
    for c in ("venue", "coin", "tf", "from", "to", "recv", "new", "revised", "same", "forming", "invalid", "status", "note"):  # fmt: skip
        t.add_column(c)
    for _, r in df.iterrows():
        t.add_row(r["venue"], r["coin"], r["timeframe"], _fmt(r["start"]), _fmt(r["end"]),
                  str(r["received"]), str(r["inserted"]), str(r["revised"]), str(r["unchanged"]),
                  str(r["forming"]), str(r["invalid"]), f"[{STYLE.get(r['status'], 'yellow')}]{r['status']}[/]",
                  str(r["note"])[:90])  # fmt: skip
    console.print(t)


@bars.command("update")
def update(
    venue: str = typer.Option(
        None, help="Only this venue (a history venue must be named to update it)"
    ),
    coin: list[str] = typer.Option(None, help="Only these coins (repeatable)"),
    timeframe: list[str] = typer.Option(None, "--tf", help="Only these timeframes: 15m, 1h, 4h"),
) -> None:
    """WRITE: incremental update from a small overlap before the newest stored bar.
    No date arguments (for the scheduler). Exit 1 if any series failed."""
    from market_signal.intraday.ingest import DiskUnsafe
    from market_signal.intraday.ingest import update as run_update

    with open_store() as (settings, store):
        try:
            df = run_update(settings, store, venue=venue, coins=_coins(coin), tfs=_tfs(timeframe))
        except DiskUnsafe as exc:
            console.print(str(exc), style="red", markup=False)
            raise typer.Exit(3) from None
    if df.empty:
        console.print("Nothing to fetch (no live venue configured, or every series is current).")
        return
    render_results(df, "Intraday bars: incremental update")
    if (df["status"] != "ok").any():
        raise typer.Exit(1)


@bars.command("backfill")
def backfill(
    venue: str = typer.Option(..., help="hyperliquid | binance"),
    start: str = typer.Option(
        None, help="UTC start (inclusive); default: configured history start"
    ),
    end: str = typer.Option(None, help="UTC end (exclusive); default: the newest closed bar"),
    coin: list[str] = typer.Option(None, help="Only these coins (repeatable)"),
    timeframe: list[str] = typer.Option(None, "--tf", help="Only these timeframes: 15m, 1h, 4h"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the plan and storage estimate only"
    ),
) -> None:
    """WRITE: deterministic backfill of an explicit range. Disk-guarded before any write;
    provider-retention limits are reported per series, never silently truncated."""
    from market_signal.intraday.ingest import DiskUnsafe
    from market_signal.intraday.ingest import backfill as run_backfill

    with open_store(read_only=dry_run) as (settings, store):
        try:
            plans, est, df = run_backfill(settings, store, venue=venue, start=_ts(start), end=_ts(end),
                                          coins=_coins(coin), tfs=_tfs(timeframe), dry_run=dry_run)  # fmt: skip
        except DiskUnsafe as exc:
            console.print(str(exc), style="red", markup=False)
            raise typer.Exit(3) from None
    t = Table(title=f"Backfill plan ({venue})")
    for c in ("coin", "tf", "from", "to", "bars", "note"):
        t.add_column(c)
    for p in plans:
        t.add_row(p.coin, p.tf.value, _fmt(p.start), _fmt(p.end), f"{p.bars:,}", "; ".join(p.notes))
    console.print(t)
    console.print(f"Estimate: {est['rows']:,.0f} bars ≈ {est['db_bytes'] / 2**20:.1f} MiB database, "
                  f"{est['raw_bytes'] / 2**20:.1f} MiB raw archive.")  # fmt: skip
    if dry_run:
        return
    render_results(df, f"Backfill result ({venue})")
    if (df["status"] != "ok").any():
        raise typer.Exit(1)


def _coverage(store, settings, venue=None, coins=None, tfs=None, now=None):
    import duckdb

    from market_signal.intraday.bars import coverage
    from market_signal.intraday.ingest import intraday_config
    from market_signal.models.domain import utcnow

    cfg = intraday_config(settings)
    try:
        return coverage(store, cfg.series(venue, coins, tfs), now or utcnow())
    except duckdb.CatalogException:
        console.print("[yellow]This database has no intraday tables yet (migration 18 runs on the "
                      "next writable open, e.g. `market bars update`).[/]")  # fmt: skip
        raise typer.Exit(1) from None


@bars.command("status")
def status(
    venue: str = typer.Option(None),
    coin: list[str] = typer.Option(None),
    timeframe: list[str] = typer.Option(None, "--tf"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Coverage per venue/coin/timeframe: extent, expected vs actual bars, gaps by category,
    duplicates/malformed rows, revisions, newest-bar age and status."""
    with open_store(read_only=True) as (settings, store):
        cov = _coverage(store, settings, venue, _coins(coin), _tfs(timeframe))
    if as_json:
        console.print_json(cov.to_json(orient="records", date_format="iso"))
        return
    t = Table(title="Intraday bars coverage (UTC, half-open [open, close))")
    for c in ("venue", "coin", "tf", "first", "last", "expected", "rows", "missing", "prov.miss", "recov.", "perm.", "dup/bad", "zero vol", "revised", "live obs", "age", "status"):  # fmt: skip
        t.add_column(c)
    for _, r in cov.iterrows():
        t.add_row(r["venue"], r["coin"], r["timeframe"], _fmt(r["first"]), _fmt(r["last"]),
                  f"{r['expected']:,}", f"{r['rows']:,}", str(r["missing"]), str(r["provider_missing"]),
                  str(r["recoverable"]), str(r["permanent"]), f"{r['duplicates']}/{r['malformed']}",
                  str(r["zero_volume"]), str(r["revised"]), f"{r['observed_live']:,}",
                  "-" if r["age_min"] is None or pd.isna(r["age_min"]) else f"{r['age_min']:.0f}m",
                  f"[{STYLE.get(r['status'], 'yellow')}]{r['status']}[/]")  # fmt: skip
    console.print(t)
    console.print("[dim]missing = prov.miss (asked after close, provider served nothing: not a local failure) "
                  "+ recov. (local miss, still within provider retention: `market bars backfill`) "
                  "+ perm. (local miss beyond retention). live obs = bars first observed within one "
                  "interval of their close; the rest were backfilled (publication time unknown). zero vol = "
                  "bars the provider served with no trades (e.g. a venue filling maintenance with flat bars).[/]")  # fmt: skip


@bars.command("gaps")
def gaps_cmd(
    venue: str = typer.Option(None),
    coin: list[str] = typer.Option(None),
    timeframe: list[str] = typer.Option(None, "--tf"),
    since: str = typer.Option(None, help="Only gaps after this UTC time"),
) -> None:
    """Every missing interval with its category."""
    from market_signal.intraday.bars import gaps
    from market_signal.intraday.ingest import intraday_config
    from market_signal.models.domain import utcnow

    with open_store(read_only=True) as (settings, store):
        cfg = intraday_config(settings)
        t = Table(title="Intraday gaps")
        for c in ("venue", "coin", "tf", "from", "to", "bars", "category"):
            t.add_column(c)
        n = 0
        for v, c, tf, pol in cfg.series(venue, _coins(coin), _tfs(timeframe)):
            for _, g in gaps(store, v, c, tf, pol, utcnow(), _ts(since)).iterrows():
                n += 1
                t.add_row(
                    v, c, tf.value, _fmt(g["start"]), _fmt(g["end"]), str(g["bars"]), g["category"]
                )
    console.print(t if n else "No gaps between each series' first and newest stored bar.")


@bars.command("show")
def show(
    coin: str = typer.Argument(...),
    timeframe: str = typer.Option("15m", "--tf"),
    venue: str = typer.Option("hyperliquid"),
    since: str = typer.Option(None, help="UTC open-time start"),
    until: str = typer.Option(None, help="UTC open-time end (exclusive)"),
    known_at: str = typer.Option(
        None, "--known-at", help="Show only what Prism knew at this UTC instant"
    ),
    limit: int = typer.Option(20, help="Newest N rows"),
) -> None:
    """Stored bars (canonical values, or as known at an instant)."""
    from market_signal.intraday.bars import load_bars

    with open_store(read_only=True) as (_settings, store):
        df = load_bars(store, venue, coin.upper(), _tfs([timeframe])[0], _ts(since), _ts(until),
                       known_at=_ts(known_at)).tail(limit)  # fmt: skip
    t = Table(
        title=f"{venue} {coin.upper()} {timeframe}"
        + (f" as known at {known_at}" if known_at else "")
    )
    for c in (
        "open",
        "close (excl.)",
        "O",
        "H",
        "L",
        "C",
        "volume",
        "trades",
        "first observed",
        "live",
        "rev",
    ):
        t.add_column(c)
    for _, r in df.iterrows():
        t.add_row(_fmt(r["open_time"]), _fmt(r["close_time"]), f"{r['open']:g}", f"{r['high']:g}",
                  f"{r['low']:g}", f"{r['close']:g}", f"{r['volume']:g}", str(r["trades"]),
                  f"{pd.Timestamp(r['first_observed_at']):%Y-%m-%d %H:%M:%S}",
                  "y" if r["observed_live"] else "n", str(r["revision"]))  # fmt: skip
    console.print(t)


@bars.command("align")
def align_cmd(
    coin: str = typer.Argument(...),
    at: str = typer.Option(..., "--at", help="UTC instant"),
    venue: str = typer.Option("hyperliquid"),
    assume_latency: float = typer.Option(
        None,
        "--assume-latency-s",
        help="Use close+latency instead of observed availability (historical)",
    ),
) -> None:
    """The newest bar per timeframe that was closed AND available at ``--at``."""
    from datetime import timedelta

    from market_signal.intraday.align import Availability, snapshot
    from market_signal.intraday.grid import INTRADAY

    av = (
        Availability.observed()
        if assume_latency is None
        else Availability.assumed(timedelta(seconds=assume_latency))
    )
    with open_store(read_only=True) as (_settings, store):
        df = snapshot(store, venue, coin.upper(), _ts(at), INTRADAY, av)
    t = Table(
        title=f"Causal view of {venue} {coin.upper()} at {_ts(at):%Y-%m-%d %H:%M:%S} UTC ({av.mode})"
    )
    for c in (
        "tf",
        "newest closed (theory)",
        "eligible bar",
        "closes",
        "available at",
        "behind",
        "close",
    ):
        t.add_column(c)
    for _, r in df.iterrows():
        t.add_row(r["timeframe"], _fmt(r["theoretical_latest_open"]), _fmt(r["open_time"]), _fmt(r["close_time"]),
                  "-" if pd.isna(r["available_at"]) else f"{pd.Timestamp(r['available_at']):%Y-%m-%d %H:%M:%S}",
                  "-" if pd.isna(r["bars_behind"]) else f"{r['bars_behind']:.0f}",
                  "-" if pd.isna(r["close"]) else f"{r['close']:g}")  # fmt: skip
    console.print(t)


@bars.command("shadow")
def shadow_cmd(
    record: bool = typer.Option(
        False, "--record", help="WRITE: record newly available shadow observations"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Shadow execution timing for paper entries (observational; the paper run is unchanged)."""
    from market_signal.intraday import shadow

    with open_store(read_only=not record) as (_settings, store):
        if record:
            new = shadow.record(store)
            console.print(f"shadow: {len(new)} new observation(s) recorded ({shadow.RULE_VERSION})")
        rep = shadow.report(store)
    if as_json:
        console.print_json(json.dumps(rep, default=str))
        return
    if not rep["entry_orders"]:
        console.print("No paper entry orders yet: shadow execution has no samples.")
        return
    df = shadow.summary_frame(rep)
    console.print(
        df.to_string(index=False) if not df.empty else "No shadow observations recorded yet."
    )
    console.print(
        f"entry orders {rep['entry_orders']}; pending {len(rep['pending'])}; missed {len(rep['missed'])}"
    )


def register(app: typer.Typer) -> None:
    app.add_typer(bars, name="bars")


@bars.command("funding")
def funding() -> None:
    """WRITE: bounded hourly public settled funding, for intraday paper accounting."""
    from market_signal.perps.data import update_settled_funding
    from market_signal.research.lab.common import canonical_json

    with open_store() as (settings, store):
        out = update_settled_funding(settings, store)
    console.print_json(canonical_json(out))
    if any(r["status"] == "failed" for r in out):
        raise typer.Exit(1)
