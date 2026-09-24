"""Data commands: update, doctor, import-csv, assets."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, fmt_age, open_store
from market_signal.data.providers.base import BarsResult
from market_signal.models.domain import Timeframe, utcnow


def update(
    symbols: list[str] = typer.Option(None, "--symbol", "-s", help="Limit to these assets"),
    timeframes: list[str] = typer.Option(["1d", "4h"], "--tf", help="Timeframes (1d, 4h, 1h)"),
    only: str = typer.Option("all", help="all | prices | macro | fundamentals"),
) -> None:
    """Fetch latest data (incremental, idempotent)."""
    from market_signal.data.registry import ProviderRegistry
    from market_signal.data.update import update_prices

    failed = 0
    with open_store() as (settings, store):
        if only in ("all", "prices"):
            registry = ProviderRegistry(settings)
            report = update_prices(
                store, settings, registry, [s.upper() for s in symbols] if symbols else None,
                [Timeframe(t) for t in timeframes],
            )  # fmt: skip
            t = Table(title="Price update")
            for col in (
                "asset",
                "tf",
                "source",
                "status",
                "new",
                "revised",
                "rejected",
                "warn",
                "note",
            ):
                t.add_column(col)
            for r in report.results:
                style = {"ok": "green", "failed": "red"}.get(r.status, "yellow")
                t.add_row(r.symbol, r.timeframe, r.source, f"[{style}]{r.status}[/]", str(r.inserted),
                          str(r.revised), str(r.rejected), str(r.warnings), r.message[:80])  # fmt: skip
            console.print(t)
            failed += len(report.failed)
        for extra in _extra_updaters():
            if only in ("all", extra.kind):
                failed += extra.run(settings, store)
    if failed:
        console.print(
            f"[red]{failed} update step(s) failed — see messages above and `market doctor`.[/]"
        )
        raise typer.Exit(1)


def _extra_updaters() -> list:
    """Macro/fundamental updaters registered by later phases."""
    try:
        from market_signal.data.updaters import UPDATERS
    except ModuleNotFoundError:
        return []
    return list(UPDATERS)


def doctor(live: bool = typer.Option(False, help="Also probe provider connectivity")) -> None:
    """Check configuration, keys, database health and data freshness."""
    with open_store() as (settings, store):
        _doctor_config(settings)
        _doctor_freshness(settings, store)
        _doctor_issues(store)
        if live:
            _doctor_live(settings)


def _doctor_config(settings) -> None:
    t = Table(title="Configuration & keys")
    t.add_column("item")
    t.add_column("status")
    t.add_row("project root", str(settings.paths.root))
    t.add_row("database", str(settings.paths.db))
    t.add_row(
        "assets configured", f"{len(settings.universe)} ({len(settings.active_assets())} active)"
    )
    for env, purpose in [
        ("TIINGO_API_KEY", "equities/ETFs/commodity ETPs"),
        ("FRED_API_KEY", "macro + regime (macro half)"),
        ("EIA_API_KEY", "oil inventories (optional)"),
        ("SEC_USER_AGENT", "equity fundamentals (EDGAR)"),
    ]:
        ok = settings.secret(env) is not None
        t.add_row(
            env,
            ("[green]set[/]" if ok else "[yellow]missing[/] → ")
            + ("" if ok else f"{purpose} disabled"),
        )
    console.print(t)


def _doctor_freshness(settings, store) -> None:
    inv = store.series_inventory()
    quality = settings.providers.get("quality") or {}
    stale = quality.get("stale_hours") or {}
    now = pd.Timestamp(utcnow())
    t = Table(title="Data freshness")
    for col in ("asset", "tf", "source", "bars", "first", "last close", "updated", "status"):
        t.add_column(col)
    seen = set()
    for _, r in inv.iterrows():
        seen.add((r["symbol"], r["timeframe"]))
        asset = settings.universe.get(r["symbol"])
        cal = asset.calendar.value if asset else "crypto"
        age_close = (now - pd.Timestamp(r["last_close"])).total_seconds() / 3600
        age_ingest = (now - pd.Timestamp(r["last_ingested"])).total_seconds() / 3600
        limit = float(stale.get(cal, 48)) * (1 if r["timeframe"] == "1d" else 0.5)
        status = (
            "[green]fresh[/]" if age_close <= limit else f"[red]STALE ({fmt_age(age_close)})[/]"
        )
        t.add_row(r["symbol"], r["timeframe"], r["source"], str(r["n_bars"]), f"{pd.Timestamp(r['first_ts']):%Y-%m-%d}",
                  f"{pd.Timestamp(r['last_close']):%Y-%m-%d %H:%M}", fmt_age(age_ingest), status)  # fmt: skip
    for a in settings.active_assets():
        for tf in a.series:
            if (a.symbol, tf.value) not in seen:
                t.add_row(
                    a.symbol,
                    tf.value,
                    a.series[tf].provider,
                    "0",
                    "-",
                    "-",
                    "never",
                    "[red]NO DATA[/]",
                )
    console.print(t)
    try:
        macro = store.query(
            "SELECT series_id, max(obs_date) AS last_obs, max(available_at) AS last_avail FROM macro_observations GROUP BY 1 ORDER BY 1"
        )
    except Exception:
        macro = pd.DataFrame()
    if not macro.empty:
        m = Table(title="Macro freshness")
        for col in ("series", "last observation", "available since"):
            m.add_column(col)
        for _, r in macro.iterrows():
            m.add_row(
                r["series_id"],
                str(r["last_obs"])[:10],
                fmt_age((now - pd.Timestamp(r["last_avail"])).total_seconds() / 3600),
            )
        console.print(m)


def _doctor_issues(store) -> None:
    issues = store.query(
        """SELECT symbol, timeframe, source, check_name, severity, count(*) AS n, max(detail) AS example
           FROM data_quality_issues GROUP BY ALL ORDER BY severity, symbol"""
    )
    runs = store.query(
        """SELECT provider, dataset, entity, finished_at, error FROM ingestion_runs
           WHERE status='failed' ORDER BY started_at DESC LIMIT 15"""
    )
    changes = store.query("SELECT * FROM series_changes ORDER BY detected_at DESC")
    if not issues.empty:
        t = Table(title="Data-quality issues")
        for col in issues.columns:
            t.add_column(col)
        for _, r in issues.iterrows():
            t.add_row(*[str(v)[:70] for v in r.to_list()])
        console.print(t)
    if not runs.empty:
        t = Table(title="Recent failed ingestion runs")
        for col in runs.columns:
            t.add_column(col)
        for _, r in runs.iterrows():
            t.add_row(*[str(v)[:90] for v in r.to_list()])
        console.print(t)
    if not changes.empty:
        console.print(
            "[yellow]Provider changes detected (series kept separate, never stitched):[/]"
        )
        console.print(changes.to_string(index=False))
    if issues.empty and runs.empty:
        console.print("[green]No data-quality issues or failed runs recorded.[/]")


def _doctor_live(settings) -> None:
    from market_signal.data.http import ProviderError
    from market_signal.data.registry import ProviderRegistry

    reg = ProviderRegistry(settings)
    t = Table(title="Live provider probes")
    t.add_column("provider")
    t.add_column("probe")
    t.add_column("result")
    probes = [
        ("coinbase", "BTC-USD"),
        ("hyperliquid", "HYPE"),
        ("tiingo", "SPY"),
        ("bitstamp", "btcusd"),
    ]
    for name, sym in probes:
        try:
            q = reg.market(name).get_latest_price(sym)
            t.add_row(name, sym, f"[green]ok[/] {q.price:,.4g} @ {q.as_of:%Y-%m-%d %H:%M}")
        except ProviderError as exc:
            t.add_row(name, sym, f"[red]{str(exc)[:100]}[/]")
    try:
        from market_signal.data.updaters import live_probes

        for name, probe, result in live_probes(settings):
            t.add_row(name, probe, result)
    except ModuleNotFoundError:
        pass
    console.print(t)


def import_csv(
    path: Path = typer.Argument(..., exists=True, readable=True),
    asset: str = typer.Option(..., help="Asset symbol from universe.yaml"),
    source: str = typer.Option(..., help="Source label for this series, e.g. csv_stooq_2026"),
    timeframe: str = typer.Option("1d"),
) -> None:
    """Import a user-supplied CSV as its own labelled series (never merged)."""
    from market_signal.data.providers.equities import read_csv_bars
    from market_signal.data.update import ingest_bars

    if not source.startswith("csv"):
        source = f"csv:{source}"
    with open_store() as (settings, store):
        a = settings.asset(asset)
        tf = Timeframe(timeframe)
        bars, actions, raw = read_csv_bars(path, a.calendar, tf)
        run_id = store.start_run(
            "csv", f"bars_{tf.value}", a.symbol, {"file": str(path), "source": source}
        )
        archived = store.archive_raw([raw], run_id)
        res = ingest_bars(
            store,
            settings,
            a,
            BarsResult(source, tf, bars, actions, [raw]),
            run_id=run_id,
            fetched_at=utcnow(),
        )
        store.finish_run(run_id, status="ok", rows_received=len(bars), rows_written=res.inserted + res.revised,
                         rows_rejected=res.rejected, archived=archived, schema_fingerprint="csv")  # fmt: skip
        console.print(
            f"Imported {a.symbol} {tf}: {res.inserted} new, {res.revised} revised, {res.rejected} rejected → source '{source}'"
        )


def assets() -> None:
    """List the configured universe."""
    with open_store() as (settings, _):
        t = Table(title="Universe")
        for col in ("symbol", "name", "class", "calendar", "series", "notes"):
            t.add_column(col)
        for a in settings.universe.values():
            series = ", ".join(f"{tf.value}:{s.provider}:{s.symbol}" for tf, s in a.series.items())
            t.add_row(a.symbol, a.name, a.asset_class.value, a.calendar.value, series, a.notes[:60])
        console.print(t)


def demo_data(
    years: int = typer.Option(8, help="Years of synthetic history"),
    seed: int = typer.Option(7),
) -> None:
    """Build the SYNTHETIC demo database (data/demo.duckdb) for exercising the app offline."""
    import os

    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db

    settings = get_settings()
    path = settings.paths.data / "demo.duckdb"
    if path.exists():
        path.unlink()
    os.environ["PRISM_SOURCE_OVERRIDE"] = "synthetic"
    store = Store(path, settings.paths.data / "demo_raw")
    try:
        counts = build_demo_db(settings, store, years=years, seed=seed)
    finally:
        store.close()
    console.print(
        f"[yellow]Built SYNTHETIC demo DB {path} ({sum(counts.values())} bars). "
        "Use `market --demo <command>` and `streamlit run dashboard/app.py -- --demo`.[/]"
    )
