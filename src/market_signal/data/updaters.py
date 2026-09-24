"""Non-price updaters (macro, energy, fundamentals) used by ``market update``.

Each updater records ingestion runs and archives raw payloads exactly like price updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from market_signal.config import Settings
from market_signal.data.http import HttpClient, ProviderError
from market_signal.data.providers.macro import EiaProvider, FredProvider
from market_signal.data.store import Store

console = Console()


def _http(settings: Settings, name: str, **headers: str) -> HttpClient:
    from market_signal.data.registry import ProviderRegistry

    return ProviderRegistry(settings).http(name, **headers)


def upsert_macro(store: Store, df: pd.DataFrame, source: str, run_id: str) -> int:
    if df.empty:
        return 0
    stage = df.copy()
    stage["source"] = source
    stage["run_id"] = run_id
    stage["realtime_end"] = stage["realtime_end"].astype("object")
    store.con.register("_macro", stage)
    try:
        before = store.con.execute("SELECT count(*) FROM macro_observations").fetchone()[0]
        store.con.execute(
            """INSERT OR REPLACE INTO macro_observations
               SELECT series_id, source, obs_date, realtime_start, CAST(realtime_end AS DATE), value,
                      available_at, pit_method, run_id FROM _macro"""
        )
        after = store.con.execute("SELECT count(*) FROM macro_observations").fetchone()[0]
    finally:
        store.con.unregister("_macro")
    return int(after - before)


def _last_obs(store: Store, series_id: str) -> datetime | None:
    row = store.con.execute(
        "SELECT max(obs_date) FROM macro_observations WHERE series_id=?", [series_id]
    ).fetchone()
    if row and row[0] is not None:
        return datetime.combine(row[0], datetime.min.time(), tzinfo=UTC)
    return None


def update_macro(settings: Settings, store: Store, series: list[str] | None = None) -> pd.DataFrame:
    cfg = settings.yaml("macro.yaml")
    results = []
    fred = FredProvider(_http(settings, "fred"), settings.secret("FRED_API_KEY"))
    for sid, spec in (cfg.get("series") or {}).items():
        if series and sid not in series:
            continue
        policy = spec.get("pit_policy", "market_close")
        # vintage series are re-fetched fully (revisions can touch any date);
        # market series resume a few weeks back to catch late prints
        last = _last_obs(store, sid)
        start = None if policy == "vintage" or last is None else last - timedelta(days=30)
        run_id = store.start_run("fred", f"macro_{policy}", sid, {"start": start})
        try:
            df = fred.get_series(
                sid, start, pit_policy=policy, lag_days=int(spec.get("lag_days", 2))
            )
            archived = store.archive_raw(fred.drain_raw(), run_id)
            n = upsert_macro(store, df, "fred", run_id)
            store.finish_run(
                run_id, status="ok", rows_received=len(df), rows_written=n, archived=archived
            )
            results.append((sid, "ok", len(df), n, policy))
        except ProviderError as exc:
            store.finish_run(
                run_id,
                status="failed",
                archived=store.archive_raw(fred.drain_raw(), run_id),
                error=str(exc)[:500],
            )
            results.append((sid, "failed", 0, 0, str(exc)[:80]))
    eia = EiaProvider(_http(settings, "eia"), settings.secret("EIA_API_KEY"))
    for sid, spec in (cfg.get("eia") or {}).items():
        if series and sid not in series:
            continue
        last = _last_obs(store, sid)
        start = None if last is None else last - timedelta(days=60)
        run_id = store.start_run("eia", "energy_weekly", sid, {"start": start})
        try:
            df = eia.get_series(
                sid, start, route=spec["route"], lag_days=int(spec.get("lag_days", 6))
            )
            archived = store.archive_raw(eia.drain_raw(), run_id)
            n = upsert_macro(store, df, "eia", run_id)
            store.finish_run(
                run_id, status="ok", rows_received=len(df), rows_written=n, archived=archived
            )
            results.append((sid, "ok", len(df), n, "release_rule"))
        except ProviderError as exc:
            store.finish_run(run_id, status="failed", error=str(exc)[:500])
            results.append((sid, "failed", 0, 0, str(exc)[:80]))
    return pd.DataFrame(results, columns=["series", "status", "received", "new_rows", "note"])


@dataclass
class Updater:
    kind: str
    title: str
    fn: Any

    def run(self, settings: Settings, store: Store) -> int:
        df = self.fn(settings, store)
        t = Table(title=self.title)
        for c in df.columns:
            t.add_column(str(c))
        for _, r in df.iterrows():
            style = "green" if r.get("status") == "ok" else "red"
            t.add_row(*[f"[{style}]{v}[/]" if k == "status" else str(v) for k, v in r.items()])
        console.print(t)
        return int((df["status"] == "failed").sum()) if "status" in df else 0


def _update_edgar(settings: Settings, store: Store) -> pd.DataFrame:
    from market_signal.fundamentals.equity import update_edgar

    return update_edgar(settings, store)


def _update_crypto(settings: Settings, store: Store) -> pd.DataFrame:
    from market_signal.fundamentals.crypto_data import update_crypto_fundamentals

    return update_crypto_fundamentals(settings, store)


UPDATERS: list[Updater] = [
    Updater("macro", "Macro update (FRED/ALFRED, EIA)", update_macro),
    Updater("fundamentals", "Equity fundamentals (SEC EDGAR, filing-date PIT)", _update_edgar),
    Updater(
        "fundamentals",
        "Crypto fundamentals (DefiLlama, Hyperliquid) — current/prospective only",
        _update_crypto,
    ),
]


def register_updater(u: Updater) -> None:
    if all(x.title != u.title for x in UPDATERS):
        UPDATERS.append(u)


def live_probes(settings: Settings) -> list[tuple[str, str, str]]:
    out = []
    fred = FredProvider(_http(settings, "fred"), settings.secret("FRED_API_KEY"))
    try:
        df = fred.get_series("DGS10", datetime.now(UTC) - timedelta(days=10))
        out.append(("fred", "DGS10", f"[green]ok[/] {len(df)} obs"))
    except ProviderError as exc:
        out.append(("fred", "DGS10", f"[red]{str(exc)[:100]}[/]"))
    return out
