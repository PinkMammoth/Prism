"""``market context``: Phase 23 context intelligence (data, semantics, provenance; no trading).

READ-ONLY (every command supports ``--json`` for agents):
  status                  provider health, event counts, positioning freshness, HL cadence cutover
  providers               provider health only
  events                  events Prism knew at --at (filters: --asset/--category/--active)
  upcoming                scheduled catalysts after --at
  asset SYM               what Prism knows about SYM at --at (active/recent/upcoming events)
  snapshot SYM            the full point-in-time context snapshot (--record to store it: WRITE)
  positioning SYM         OI/funding/basis/long-short context, crowding, vulnerability
  brief                   human-readable brief: macro, active events, positioning, warnings
  event ID                one event's as-of state and its full update history
  taxonomy | feasibility  the versioned taxonomy / data-feasibility audits
WRITE:
  refresh [--group ...]   run providers (macro | news | positioning); idempotent
  import FILE             validated external import (ChatGPT Work / manual), deduplicated
  thesis SNAPSHOT_ID      record a research-only TradeThesis citing a recorded snapshot

Nothing here places orders, sizes positions, alters risk, or feeds forward/co-pilot/paper.
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import typer
from rich.markup import escape
from rich.table import Table

from market_signal.cli.common import console, open_store

context = typer.Typer(no_args_is_help=True,
                      help="Context intelligence (Phase 23): events, macro, positioning. No trading.")  # fmt: skip
JSON = typer.Option(False, "--json", help="Machine-readable output")
AT = typer.Option(None, "--at", help="UTC instant (default now): show only what Prism knew then")


def _at(at: str | None) -> datetime:
    from market_signal.models.domain import utcnow

    return (pd.Timestamp(at, tz="UTC") if at and pd.Timestamp(at).tzinfo is None
            else pd.Timestamp(at).tz_convert("UTC") if at else pd.Timestamp(utcnow())).to_pydatetime()  # fmt: skip


def _out(obj, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    return as_json


def _schema(store) -> None:
    try:
        store.con.execute("SELECT 1 FROM context_events LIMIT 1")
    except Exception:
        console.print("[red]Context tables are missing (schema < 22). Run any write command "
                      "(e.g. `market context refresh`) on a writable database first.[/]")  # fmt: skip
        raise typer.Exit(1) from None


def _events_table(rows: list[dict], title: str) -> None:
    t = Table(title=title)
    for c in ("first seen", "event time", "subcategory", "conf", "mat", "link", "active", "title"):
        t.add_column(c)
    for v in rows:
        t.add_row(str(v["first_seen_at"])[:16], str(v.get("event_time") or "-")[:16], v["subcategory"],
                  v["confidence"], str(v["materiality"]), v["link"], "yes" if v["active"] else "",
                  v["title"][:70])  # fmt: skip
    console.print(t)


@context.command("status")
def status(as_json: bool = JSON) -> None:
    """Provider health, event counts, positioning freshness and the HL fixed-hour cutover."""
    from market_signal.context.positioning import hl_cutover
    from market_signal.context.providers.base import provider_health
    from market_signal.context.service import stale_hours

    with open_store(read_only=True) as (settings, store):
        _schema(store)
        health = provider_health(store, stale_hours(settings))
        counts = store.con.execute(
            "SELECT category, count(*) n, max(first_seen_at) newest FROM context_events GROUP BY 1 "
            "ORDER BY 1").df()  # fmt: skip
        ups = store.con.execute("SELECT count(*) FROM context_event_updates").fetchone()[0]
        hl = store.con.execute(
            "SELECT max(grid_hour), count(DISTINCT grid_hour) FROM context_hl_oi_hourly"
        ).fetchone()
        lsr = store.con.execute(
            "SELECT max(ingested_at), count(*) FROM context_ls_ratios"
        ).fetchone()
        cutover = hl_cutover(store)
    obj = {"providers": health, "events": counts.to_dict("records"), "updates": int(ups),
           "positioning": {"hl_hourly_cutover": cutover,
                           "hl_last_grid_hour": None if hl[0] is None else str(hl[0]),
                           "hl_grid_hours": int(hl[1] or 0),
                           "binance_ratios_last_ingest": None if lsr[0] is None else str(lsr[0]),
                           "binance_ratio_rows": int(lsr[1] or 0)}}  # fmt: skip
    if _out(obj, as_json):
        return
    _providers_table(health)
    console.print(f"events: {', '.join(f'{r['category']}={r['n']}' for r in obj['events']) or 'none'}; "
                  f"updates: {ups}")  # fmt: skip
    console.print(f"positioning: {json.dumps(obj['positioning'], default=str)}")


def _providers_table(health: list[dict]) -> None:
    t = Table(title="Context providers")
    for c in (
        "provider",
        "state",
        "last success",
        "since (h)",
        "fails",
        "latency ms",
        "last error",
    ):
        t.add_column(c)
    style = {"OK": "green", "STALE": "red", "FAILING": "red", "NEVER_RUN": "yellow"}
    for h in health:
        t.add_row(h["provider"], f"[{style.get(h['state'], 'yellow')}]{h['state']}[/]",
                  str(h["last_success"] or "-")[:19], str(h["hours_since_success"] or "-"),
                  str(h["consecutive_failures"]), str(round(h["last_latency_ms"] or 0)),
                  (h["last_error"] or "")[:60])  # fmt: skip
    console.print(t)


@context.command("providers")
def providers(as_json: bool = JSON) -> None:
    """Health of every configured context provider (last success, latency, errors, staleness)."""
    from market_signal.context.providers.base import provider_health
    from market_signal.context.service import stale_hours

    with open_store(read_only=True) as (settings, store):
        _schema(store)
        health = provider_health(store, stale_hours(settings))
    if not _out(health, as_json):
        _providers_table(health)


def _views(store, t, asset=None) -> list[dict]:
    from market_signal.context import ledger
    from market_signal.context.snapshot import event_view

    lo, hi = pd.Timestamp(t) - pd.Timedelta(days=30), pd.Timestamp(t) + pd.Timedelta(days=60)
    states = ledger.states_asof(store, t, since=lo.to_pydatetime(), until=hi.to_pydatetime())
    if asset:
        links = ledger.links_for(store, asset, t)
        return [event_view(s, pd.Timestamp(t), ledger.link_type_for(links, s["event_id"]))
                for s in states if s["event_id"] in set(links["event_id"])]  # fmt: skip
    return [event_view(s, pd.Timestamp(t), "direct") for s in states]


@context.command("events")
def events(at: str = AT, asset: str = typer.Option(None, help="Only events linked to this asset"),
           category: str = typer.Option(None), active: bool = typer.Option(False, "--active"),
           limit: int = typer.Option(50), as_json: bool = JSON) -> None:  # fmt: skip
    """Events Prism knew at --at (default now), newest first."""
    t = _at(at)
    with open_store(read_only=True) as (_, store):
        _schema(store)
        rows = _views(store, t, asset)
    rows = [
        r
        for r in rows
        if (not category or r["category"] == category) and (not active or r["active"])
    ]
    rows = sorted(rows, key=lambda v: (v["first_seen_at"], v["event_id"]), reverse=True)[:limit]
    if not _out(rows, as_json):
        _events_table(rows, f"Context events known at {t:%Y-%m-%d %H:%M} UTC")


@context.command("upcoming")
def upcoming(at: str = AT, days: int = typer.Option(14), as_json: bool = JSON) -> None:
    """Scheduled catalysts after --at (macro calendar, unlocks, listings)."""
    t = _at(at)
    with open_store(read_only=True) as (_, store):
        _schema(store)
        rows = [v for v in _views(store, t) if v["scheduled"] and v["event_time"]
                and pd.Timestamp(t) < pd.Timestamp(v["event_time"]) <= pd.Timestamp(t) + pd.Timedelta(days=days)]  # fmt: skip
    rows.sort(key=lambda v: (v["event_time"], v["event_id"]))
    if not _out(rows, as_json):
        _events_table(rows, f"Scheduled catalysts, next {days} days")


@context.command("asset")
def asset(symbol: str, at: str = AT, as_json: bool = JSON) -> None:
    """What does Prism know about SYMBOL at --at? (events only; see `snapshot` for everything)"""
    t = _at(at)
    with open_store(read_only=True) as (_, store):
        _schema(store)
        rows = _views(store, t, symbol.upper())
    obj = {"asset": symbol.upper(), "as_of": t.isoformat(),
           "active": [r for r in rows if r["active"]],
           "recent": sorted([r for r in rows if not r["scheduled"]], key=lambda v: v["first_seen_at"], reverse=True)[:20],
           "upcoming": sorted([r for r in rows if r["scheduled"] and pd.Timestamp(r["event_time"]) > pd.Timestamp(t)],
                              key=lambda v: v["event_time"])[:10]}  # fmt: skip
    if _out(obj, as_json):
        return
    for k in ("active", "recent", "upcoming"):
        _events_table(obj[k], f"{symbol.upper()}: {k}")


@context.command("snapshot")
def snapshot(symbol: str, at: str = AT, record: bool = typer.Option(False, "--record",
             help="WRITE: append the snapshot to context_snapshots (to cite in a thesis)"),
             as_json: bool = JSON) -> None:  # fmt: skip
    """Point-in-time context snapshot (events, macro, positioning, activity, market, freshness)."""
    from market_signal.context.service import stale_hours
    from market_signal.context.snapshot import context_snapshot

    t = _at(at)
    with open_store(read_only=not record) as (settings, store):
        _schema(store)
        snap = context_snapshot(store, symbol, t, record=record, stale_hours=stale_hours(settings))
    if _out(snap, as_json):
        return
    console.print(f"[bold]{snap['asset']}[/] as of {snap['as_of']}  id {snap['snapshot_id'][:24]}…"
                  + ("  [green](recorded)[/]" if record else ""))  # fmt: skip
    m = snap["macro"]
    nxt = m["next_tier1"]
    console.print(f"next tier-1: {nxt['subcategory'] + ' in ' + str(round(nxt['minutes'] / 60, 1)) + 'h' if nxt else 'none known'}"
                  f"; post-event window: {m['in_post_event_window']}")  # fmt: skip
    _events_table(snap["active_events"], "Active linked events")
    a = snap["activity"]
    console.print(
        f"activity: {a['state']} (expansion={a['range_expansion']}, volume_spike={a['volume_spike']}, rv={a['rv_state']})"
    )
    for v, b in (snap["positioning"] or {}).get("venues", {}).items():
        console.print(f"{v}: OI={b.get('open_interest')} 24h={_f(b.get('oi_change_24h_pct'))}% z={_f(b.get('oi_change_24h_z'))} "
                      f"funding pct={_f(b.get('funding_pct_90d'))} → {b['crowding']['leverage']}/{b['crowding']['skew']}; "
                      f"vulnerability long={b['vulnerability']['long_side']['level']} short={b['vulnerability']['short_side']['level']}")  # fmt: skip
    for w in snap["freshness"]["warnings"]:
        console.print(f"[yellow]warning: {w}[/]")
    console.print("[dim]Context only: no direction, no recommendation, no veto.[/]")


def _f(x) -> str:
    return "-" if x is None else f"{x:.2f}"


@context.command("positioning")
def positioning(symbol: str, at: str = AT, assumed_latency: bool = typer.Option(
                False, "--assumed-latency", help="Research mode for backfilled Binance history"),
                as_json: bool = JSON) -> None:  # fmt: skip
    """OI level/change/percentile/z, funding, basis, long/short ratios; crowding and vulnerability."""
    from market_signal.context.positioning import positioning_context

    t = _at(at)
    with open_store(read_only=True) as (_, store):
        _schema(store)
        pos = positioning_context(store, symbol, t, strict=not assumed_latency)
    if not _out(pos, as_json):
        console.print_json(json.dumps(pos, default=str))


@context.command("brief")
def brief(at: str = AT, as_json: bool = JSON) -> None:
    """Upcoming macro, active crypto events, unusual positioning, elevated activity, warnings."""
    from market_signal.context.service import stale_hours
    from market_signal.context.snapshot import brief as make
    from market_signal.perps.open_interest import oi_config

    t = _at(at)
    with open_store(read_only=True) as (settings, store):
        _schema(store)
        b = make(store, oi_config(settings).coins, t, stale_hours=stale_hours(settings))
    if _out(b, as_json):
        return
    console.print(f"[bold]Context brief[/] {b['as_of'][:16]} UTC  (no recommendations)")
    for e in b["macro_next_24h"]:
        console.print(f"  macro in {e['minutes'] / 60:.1f}h: {e['subcategory']} ({e['title']})")
    if b["next_tier1"]:
        n = b["next_tier1"]
        console.print(
            f"  next tier-1: {n['subcategory']} at {n['event_time'][:16]} ({n['minutes'] / 60:.1f}h)"
        )
    for e in b["active_crypto_events"]:
        console.print(
            f"  active: \\[{e['severity']}] {e['subcategory']} {e['confidence']} — "
            + escape(e["title"][:80])
        )
    for a in b["assets"]:
        console.print(
            f"  {a['asset']:<5} activity={a['activity']:<8} crowding={a['crowding']} vulnerability={a['vulnerability']}"
        )
    for w in b["warnings"]:
        console.print(f"  [yellow]warning: {w}[/]")


@context.command("event")
def event(event_id: str, at: str = AT, as_json: bool = JSON) -> None:
    """One event as Prism knew it at --at, with its full first-seen/update history."""
    from market_signal.context import ledger

    t = _at(at)
    with open_store(read_only=True) as (_, store):
        _schema(store)
        st = ledger.state_asof(store, event_id, t)
    if st is None:
        console.print(f"[yellow]{event_id} was not known to Prism at {t:%Y-%m-%d %H:%M} UTC[/]")
        raise typer.Exit(1)
    if not _out(st, as_json):
        console.print_json(json.dumps(st, default=str))


@context.command("taxonomy")
def taxonomy(as_json: bool = JSON) -> None:
    """The versioned taxonomy, confidence ladder and source tiers."""
    from market_signal.context.taxonomy import taxonomy as tx

    obj = tx()
    if not _out(obj, as_json):
        console.print_json(json.dumps(obj))


@context.command("feasibility")
def feasibility(as_json: bool = JSON) -> None:
    """Liquidation, microstructure and traditional-market data audits (verified 2026-10-06)."""
    from market_signal.context.feasibility import report

    obj = report()
    if not _out(obj, as_json):
        console.print_json(json.dumps(obj))


@context.command("refresh")
def refresh(group: list[str] = typer.Option(None, "--group", help="macro | news | positioning (repeatable)"),
            as_json: bool = JSON) -> None:  # fmt: skip
    """WRITE: run context providers. Idempotent; never rewrites first_seen_at. Exit 1 on failure."""
    from market_signal.context.service import GROUPS
    from market_signal.context.service import refresh as run

    groups = tuple(group or GROUPS)
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        console.print(f"[red]unknown group(s) {bad}; choose from {GROUPS}[/]")
        raise typer.Exit(2)
    with open_store() as (settings, store):
        res = run(settings, store, groups)
    if not _out(res, as_json):
        for r in res:
            console.print(json.dumps(r, default=str))
    if any(r.get("status") == "failed" for r in res):
        raise typer.Exit(1)


@context.command("import")
def import_(path: str, as_json: bool = JSON) -> None:
    """WRITE: validated import of external events (schema context_import_v1, e.g. ChatGPT Work)."""
    from pathlib import Path

    from market_signal.context.providers.importer import import_events

    text = Path(path).read_text()
    with open_store() as (_, store):
        try:
            res = import_events(store, text)
        except ValueError as exc:
            console.print(f"Import rejected: {exc}", style="red", markup=False)
            raise typer.Exit(1) from None
    if not _out(res, as_json):
        console.print(json.dumps(res, indent=2, default=str))
    if res["invalid"]:
        raise typer.Exit(1)


@context.command("thesis")
def thesis(snapshot_id: str,
           direction: str = typer.Option("none", help="long | short | none (a hypothesis only)"),
           rule: str = typer.Option("manual", help="the rule/version that produced the hypothesis"),
           invalidation_kind: str = typer.Option("time"), invalidation: str = typer.Option(...),
           level: float = typer.Option(None), expires: str = typer.Option(..., help="UTC expiry"),
           event: list[str] = typer.Option(None, "--event", help="cited event id (repeatable)"),
           as_json: bool = JSON) -> None:  # fmt: skip
    """WRITE: record a research-only TradeThesis derived from a recorded snapshot."""
    from market_signal.context.thesis import build_thesis, load_snapshot, record_thesis

    with open_store() as (_, store):
        try:
            th = build_thesis(load_snapshot(store, snapshot_id), direction=direction, hypothesis_rule=rule,
                              invalidation={"kind": invalidation_kind, "level": level, "description": invalidation},
                              expires_at=pd.Timestamp(expires, tz="UTC").to_pydatetime(),
                              cited_event_ids=tuple(event or ()))  # fmt: skip
        except ValueError as exc:
            console.print(f"Thesis rejected: {exc}", style="red", markup=False)
            raise typer.Exit(1) from None
        tid = record_thesis(store, th)
    obj = {"thesis_id": tid, **th.model_dump(mode="json")}
    if not _out(obj, as_json):
        console.print(f"recorded {tid} (research_only)")


def register(app: typer.Typer) -> None:
    app.add_typer(context, name="context")
