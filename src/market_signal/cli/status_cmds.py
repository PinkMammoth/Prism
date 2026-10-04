"""``market status``: one read-only operational view of every Prism component.

It summarises what ``market doctor`` and the component commands already check (data
freshness, OI coverage) and adds the Strategy Lab forward tracker, co-pilot and paper
trader. ``market doctor`` is unchanged and remains the detailed diagnostic.
"""

from __future__ import annotations

import json

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, fmt_age, open_store
from market_signal.models.domain import utcnow

STALE = pd.Timedelta(hours=36)  # a component meant to run daily, silent this long, is stale


def _age_h(now: pd.Timestamp, t) -> float | None:
    return (
        None
        if t is None or pd.isna(t)
        else (now - pd.Timestamp(t).tz_convert("UTC")).total_seconds() / 3600
    )


def _one(store, sql: str):
    try:
        row = store.con.execute(sql).fetchone()
    except Exception:
        return None
    return None if not row else row[0]


def system_status(store, settings, now=None) -> list[dict]:
    """Rows of (component, state, detail). States: OK, ATTENTION, STALE, NOT SET UP, PAUSED,
    KILLED... from explicit recorded facts. Read-only."""
    now = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    rows = []
    from market_signal.data.freshness import check_freshness

    fr = check_freshness(store, settings, now)
    probs = fr.problems(include_scan=False)
    rows.append({"component": "Data", "state": "STALE" if fr.data_stale else "ATTENTION" if probs else "OK",
                 "detail": " ".join(probs) or "daily bars current"})  # fmt: skip
    try:
        from market_signal.perps.open_interest import oi_coverage

        cov = oi_coverage(store, settings, now.to_pydatetime())
        bad = cov[cov["status"].isin(["STALE", "AT RISK", "LOST", "NO DATA"])]
        rows.append({"component": "OI", "state": "ATTENTION" if len(bad) else "OK",
                     "detail": (f"{len(bad)} series need attention: "
                                + ", ".join(f"{r.venue}/{r.coin} {r.status}" for r in bad.itertuples())[:200])
                     if len(bad) else f"{len(cov)} series collecting"})  # fmt: skip
    except Exception as exc:
        rows.append({"component": "OI", "state": "ATTENTION", "detail": f"unavailable: {exc}"})

    last = _one(store, "SELECT max(finished_at) FROM lab_forward_runs WHERE kind='check'")
    active = _one(store, "SELECT count(*) FROM lab_forward_trackings t WHERE (SELECT status FROM "
                         "lab_forward_status s WHERE s.tracking_id=t.tracking_id ORDER BY recorded_at "
                         "DESC LIMIT 1)='active'")  # fmt: skip
    newest = _one(store, "SELECT max(bar_close) FROM lab_forward_evaluations")
    age = _age_h(now, last)
    rows.append({"component": "Forward tracker",
                 "state": "NOT SET UP" if last is None else "STALE" if age > STALE.total_seconds() / 3600 else "OK",
                 "detail": f"{active or 0} active trackings; last check {fmt_age(age)}; newest evaluated "
                           f"bar {str(newest)[:16] if newest else 'none yet'}"})  # fmt: skip

    last = _one(store, "SELECT max(finished_at) FROM copilot_runs")
    watches = _one(store, "SELECT count(*) FROM copilot_watchlist w WHERE (SELECT status FROM "
                          "copilot_watch_status s WHERE s.watch_id=w.watch_id ORDER BY recorded_at DESC "
                          "LIMIT 1)='active'")  # fmt: skip
    alerts = _one(store, "SELECT count(*) FROM copilot_decisions WHERE decision='ALERT'")
    age = _age_h(now, last)
    rows.append({"component": "Co-pilot",
                 "state": "NOT SET UP" if last is None else "STALE" if age > STALE.total_seconds() / 3600 else "OK",
                 "detail": f"{watches or 0} active watches; {alerts or 0} alerts so far; last run {fmt_age(age)}"})  # fmt: skip

    try:
        from market_signal.paper import observe

        rid = observe.current_run(store)
        v = observe.RunView(store, rid, now)
        h = observe.health(v)
        a = observe.account_summary(v)
        rows.append({"component": "Paper trader",
                     "state": "OK" if h["state"] == "HEALTHY" else h["state"],
                     "detail": f"{a['status']}; equity {a['equity']:,.2f} {a['currency']} "
                               f"({a['return_on_starting_equity']:+.2%}); {a['open_positions']} open; "
                               f"{a['maturity']}" + (f"; {'; '.join(h['reasons'])}" if h["reasons"] else "")})  # fmt: skip
    except Exception as exc:
        rows.append({"component": "Paper trader", "state": "NOT SET UP", "detail": str(exc)})
    # Infrastructure health (Phase 14), kept separate from strategy evidence.
    try:
        from market_signal.ops.runtime import health_rows

        rows.extend(health_rows(store, now.to_pydatetime()))
    except Exception as exc:
        rows.append(
            {"component": "Runtime (infra)", "state": "ATTENTION", "detail": f"unavailable: {exc}"}
        )
    return rows


def status(as_json: bool = typer.Option(False, "--json")) -> None:
    """One-screen operational status: data, OI, forward tracker, co-pilot, paper trader, and
    the always-on runtime (authority, last cycles, backups, disk, Telegram)."""
    with open_store(read_only=True) as (settings, store):
        rows = system_status(store, settings)
    if as_json:
        console.print_json(json.dumps(rows))
        return
    t = Table(title="Prism status (read-only; details: market doctor / market lab … status)")
    for c in ("component", "state", "detail"):
        t.add_column(c)
    colour = {"OK": "green", "HEALTHY": "green"}
    for r in rows:
        c = colour.get(
            r["state"], "yellow" if r["state"] in ("ATTENTION", "DEGRADED", "PAUSED") else "red"
        )
        t.add_row(r["component"], f"[{c}]{r['state']}[/]", r["detail"])
    console.print(t)


def register(app: typer.Typer) -> None:
    app.command("status")(status)
