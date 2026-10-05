"""``market lab paper``: the paper-only auto-trader (a simulated perp account).

``policy``, ``runs``, ``status``, ``positions``, ``risk``, ``trades``, ``intents``, ``gaps``,
``contributions``, ``equity``, ``events``, ``summary``, ``brief`` (without --record/--send),
``create --dry-run`` and ``run --dry-run`` are read-only. ``create``, ``run``,
``pause``/``resume``/``stop`` and ``evidence`` write only ``paper_*`` tables; ``snapshot`` and
``brief --record/--send`` write only observability artifacts (snapshots, briefs, delivery
attempts); ``run`` and ``brief --send`` may send PAPER-labelled Telegram messages.

There is no live mode: no command, flag or environment variable here (or anywhere in
Prism) places, transmits or signs a real exchange order, and none asks for trading keys.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import duckdb
import typer

from market_signal.cli.common import console, open_store
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger, LedgerError

paper = typer.Typer(
    no_args_is_help=True,
    help="Paper auto-trader: a SIMULATED perp account (never real orders).",
)


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _now(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _call(fn: Callable[[Ledger], object], *, read_only: bool = True, quiet: bool = False):
    try:
        with open_store(read_only=read_only) as (_, store):
            out = fn(Ledger(store))
            if not quiet:
                console.print_json(canonical_json(out))
            return out
    except (LedgerError, ValueError, duckdb.Error) as exc:
        console.print(f"Paper: {exc}", markup=False)
        raise typer.Exit(1) from None


def _one_run(store, run_id: str | None) -> str:
    from market_signal.paper.engine import runs

    if run_id:
        return run_id
    ids = [r["run_id"] for r in runs(store)]
    if not ids:
        raise ValueError("no paper run exists; create one with `market lab paper create`")
    return ids[-1]


@paper.command("policy")
def policy() -> None:
    """Print the released promotion, risk, exit and maturity policies with their IDs."""
    from market_signal.paper import policy as pol

    out = {}
    for name, p in (("promotion", pol.promotion_policy()), ("risk", pol.risk_policy()),
                    ("exit", pol.exit_policy()), ("maturity", pol.maturity_policy())):  # fmt: skip
        out[name] = {"policy_id": p.policy_id, **p.model_dump(mode="json")}
    console.print_json(canonical_json(out))


@paper.command("create")
def create(
    strategies: list[str] = typer.Argument(..., help="Strategy names or IDs (active trackings)."),
    reason: str = typer.Option(..., "--reason", help="Why this paper account is started."),
    label: str = typer.Option(None, "--label"),
    continues: str = typer.Option(None, "--continues", help="A stopped/killed run this follows."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the frozen run; write nothing."),
    promotion_version: int = typer.Option(None, "--promotion-version", help="Default: latest."),
    risk_version: int = typer.Option(None, "--risk-version", help="Default: latest."),
    exit_version: int = typer.Option(None, "--exit-version", help="Default: latest."),
) -> None:
    """WRITE: create a paper account. Its clock starts now; no earlier signal is ever traded."""
    from market_signal.paper import policy as pol
    from market_signal.paper.engine import create_run

    _call(
        lambda ledger: create_run(
            ledger, strategies, reason=reason, origin="cli", software=_software(), label=label,
            continues=continues, dry_run=dry_run,
            promotion=pol.promotion_policy(promotion_version), risk=pol.risk_policy(risk_version),
            exit_=pol.exit_policy(exit_version),
        ),
        read_only=dry_run,
    )  # fmt: skip


@paper.command("run")
def run_(
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the cycle; write/send nothing."),
    full: bool = typer.Option(False, "--full", help="With --dry-run: print every would-be event."),
    now: str = typer.Option(None, "--now", help="ISO time (default: now). Inspection only."),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Position/kill/error Telegram alerts (default on)."
    ),
) -> None:
    """WRITE: process every newly closed bar of each paper run, then send PAPER notifications.

    Idempotent: a bar is processed once; re-running writes nothing new.
    """
    from market_signal.paper.engine import run_all

    def go(ledger):
        def sender_factory():
            from market_signal.config import get_settings
            from market_signal.portfolio.telegram import TelegramClient

            return TelegramClient.from_settings(get_settings()).send

        if now is not None and not dry_run:
            raise ValueError("--now is for --dry-run inspection only")
        out = run_all(ledger, software=_software(), sender_factory=sender_factory,
                      now=_now(now), dry_run=dry_run, alerts=notify)  # fmt: skip
        if dry_run and not full:
            for r in out["runs"]:
                evs = r.pop("would_record", [])
                r["would_record"] = [{"type": e["event_type"], "key": e["event_key"]} for e in evs]
        return out

    out = _call(go, read_only=dry_run)
    failed = any(r.get("status") == "error" for r in out["runs"])
    failed |= any(n["status"] == "failed" for n in out.get("notifications") or [])
    if failed:
        raise typer.Exit(1)


def _view(run_id: str | None, fn, *, as_json: bool, render=None, read_only: bool = True):
    """Run a read-only observability function on one run; print JSON or a rendering."""
    from market_signal.paper import observe

    def go(ledger):
        rid = run_id or observe.current_run(ledger.store)
        out = fn(ledger.store, rid)
        if as_json or render is None:
            console.print_json(canonical_json(out))
        else:
            render(out)
        return out

    return _call(go, read_only=read_only, quiet=True)


def _money(x) -> str:
    return "—" if x is None else f"{x:,.2f}"


def _pct(x, signed: bool = False) -> str:
    return "—" if x is None else (f"{x:+.2%}" if signed else f"{x:.2%}")


def _day(iso) -> str:
    import pandas as pd

    return "—" if iso is None else pd.Timestamp(iso).tz_convert("UTC").strftime("%Y-%m-%d %H:%M")


def _px(x) -> str:
    return "—" if x is None else (f"{x:,.4f}" if x < 10 else f"{x:,.2f}")


def _table(title: str, cols: list[str], rows: list[list]) -> None:
    from rich.table import Table

    t = Table(title=title)
    for c in cols:
        t.add_column(c)
    for r in rows:
        t.add_row(*[str(x) for x in r])
    console.print(t)


def _render_status(o: dict) -> None:
    a, h, r, c = o["account"], o["health"], o["risk"], o["coverage"]
    console.print(f"[bold]PAPER · SIMULATED[/] run {a['run_id']}", highlight=False)
    _table("Account", ["item", "value"], [
        ["status / health", f"{a['status']} / {h['state']}"],
        ["started", f"{_day(a['created_at'])} UTC ({a['age_days']:.1f} days)"],
        ["last processed bar", _day(a["as_of_bar"])],
        ["equity (start)", f"{_money(a['equity'])} ({_money(a['starting_equity'])}) {a['currency']}"],
        ["cash / free cash", f"{_money(a['cash'])} / {_money(a['free_cash'])}"],
        ["realised / unrealised / net PnL",
         f"{_money(a['realised_pnl'])} / {_money(a['unrealised_pnl'])} / {_money(a['net_pnl'])}"],
        ["return on start", _pct(a["return_on_starting_equity"], True)],
        ["peak / drawdown / max drawdown",
         f"{_money(a['peak_equity'])} / {_pct(a['drawdown'])} / {_pct(a['max_drawdown'])}"],
        ["gross exposure / margin in use", f"{_pct(a['gross_exposure'])} / {_money(a['margin_in_use'])}"],
        ["positions open / pending / closed",
         f"{a['open_positions']} / {a['pending_entry_orders']} / {a['closed_trades']}"],
        ["maturity / observed days", f"{a['maturity']} / {a['observed_days']}"],
        ["ledger reconciles", str(a["reconciles_with_ledger"])],
    ])  # fmt: skip
    _render_risk(r)
    _table("Coverage", ["item", "value"], [
        ["bars expected / on time / late / unprocessed / pending",
         f"{c['expected_bars']} / {c['on_time']} / {c['late']} / {c['unprocessed']} / {c['pending']}"],
        ["cycles ok / error / consecutive ok", f"{c['cycles']['ok']} / {c['cycles']['error']} / "
                                              f"{c['cycles']['consecutive_ok']}"],
        ["last ok cycle / last cycle", f"{_day(c['cycles']['last_ok'])} / {_day(c['cycles']['last'])}"],
        ["days without any cycle", ", ".join(c["days_without_a_cycle"]) or "none"],
        ["next bar / entry window", f"{_day(c['next_bar'])} → {_day(c['next_entry_window'][1])} UTC"],
    ])  # fmt: skip
    comp = o["components"]
    _table("Health", ["component", "state", "detail"], [
        ["paper engine", h["state"], "; ".join(h["reasons"]) or "no issues"],
        ["data (perp bars)", comp["data"]["state"], f"newest bar {_day(comp['data']['newest_bar'])}"],
        ["forward tracking", comp["forward_tracking"]["state"],
         f"last check {_day(comp['forward_tracking']['last_check'])}"],
        ["co-pilot", comp["copilot"]["state"], f"last run {_day(comp['copilot']['last_run'])}"],
        ["notifications", "info", f"{h['notifications']['failed_unsent']} failed, "
                                  f"{h['notifications']['unknown_outcome']} unknown (never affects trading)"],
    ])  # fmt: skip
    if o["positions"]:
        _render_positions(o["positions"])
    i = o["intents"]
    console.print(f"Intents: {i or 'none yet'}", highlight=False)


def _render_risk(r: dict) -> None:
    rows = [
        ["new entries permitted", "yes" if r["new_entries_permitted"] else f"NO ({r['why_not']})"],
        ["positions", f"{r['positions']['current']} / {r['positions']['limit']}"],
        ["gross exposure", f"{_pct(r['gross_exposure']['current'])} / {_pct(r['gross_exposure']['limit'])}"],
        ["drawdown", f"{_pct(r['drawdown']['current'])} / {_pct(r['drawdown']['kill_at'])} kill threshold"],
        ["last bar return", f"{_pct(r['daily_loss']['last_bar_return'], True)} (halt at "
                            f"{_pct(r['daily_loss']['halt_at'], True)}; halted: {r['daily_loss']['halted_on_last_bar']})"],
        ["free cash vs reserve", f"{_money(r['free_cash']['current'])} vs {_money(r['free_cash']['required_reserve'])} "
                                 f"(headroom {_money(r['free_cash']['headroom'])})"],
        ["failed-cycle streak", f"{r['failed_cycle_streak']['current']} / {r['failed_cycle_streak']['auto_pause_at']} auto-pause"],
        ["free position slots (account-wide; max 2 per strategy)", f"{r['new_positions_possible_now']} × {_money(r['position_size_now'])} "
                                       f"notional at {r['leverage']:g}x"],
    ]  # fmt: skip
    rows += [[f"asset {k}", f"{_pct(x['current'])} / {_pct(x['limit'])}"]
             for k, x in r["per_asset_exposure"].items()]  # fmt: skip
    rows += [[f"strategy {k}", f"{_pct(x['current'])} / {_pct(x['limit'])}"]
             for k, x in r["per_strategy_exposure"].items()]  # fmt: skip
    if r["near_liquidation"]:
        rows.append(["NEAR MODELLED LIQUIDATION", ", ".join(r["near_liquidation"])])
    _table("Risk headroom (frozen paper_risk_policy)", ["limit", "current / limit"], rows)


def _render_positions(ps: list[dict]) -> None:
    if not ps:
        console.print("No open paper positions.")
        return
    _table("Open paper positions (marked at the last processed close)",
           ["asset", "side", "strategy", "entry (UTC)", "ref → fill", "mark", "units", "notional",
            "lev", "margin", "uPnL", "funding", "fee", "liq≈ (dist)", "exit bar", "bars left"],
           [[p["symbol"], p["side"], p["strategy"], _day(p["entry_time"]),
             f"{_px(p['entry_ref'])} → {_px(p['entry_fill'])}", _px(p["mark"]), f"{p['units']:.4g}",
             _money(p["notional_at_mark"]), f"{p['leverage']:g}x", _money(p["margin_committed"]),
             _money(p["unrealised_pnl"]), _money(p["funding_paid_to_date"]), _money(p["entry_fee"]),
             f"{_px(p['liquidation']['price'])} ({_pct(p['liquidation']['distance'])})"
             + (" NEAR" if p["liquidation"]["near"] else ""),
             _day(p["scheduled_exit_bar"]), p["bars_remaining"]] for p in ps])  # fmt: skip
    console.print("[dim]liq≈: approximate isolated-margin model (isolated_full_margin_loss_v1), "
                  "not Hyperliquid liquidation parity.[/]")  # fmt: skip


@paper.command("runs")
def runs_(run_id: str = typer.Argument(None)) -> None:
    """Every paper run (or one) as JSON: status, equity, policies, last cycle."""
    from market_signal.paper.engine import status

    _call(lambda ledger: status(ledger.store, run_id))


@paper.command("status")
def status_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """The paper account at a glance: account, risk headroom, coverage, health, positions."""
    from market_signal.paper.observe import run_status

    _view(run_id, run_status, as_json=as_json, render=_render_status)


@paper.command("positions")
def positions_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Open positions with marks, margin, funding, approximate liquidation and bars left."""
    from market_signal.paper import observe

    _view(run_id, lambda st, rid: observe.positions_view(observe.RunView(st, rid)), as_json=as_json,
          render=_render_positions)  # fmt: skip


@paper.command("risk")
def risk_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Risk state vs the frozen limits (headroom only; no risk score)."""
    from market_signal.paper import observe

    _view(run_id, lambda st, rid: observe.risk_view(observe.RunView(st, rid)), as_json=as_json,
          render=_render_risk)  # fmt: skip


@paper.command("trades")
def trades_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Closed paper trades: fills, holding, gross/fees/funding/slippage/net, exit, issues."""
    from market_signal.paper import observe

    def render(ts):
        if not ts:
            console.print("No closed paper trades yet.")
            return
        _table("Closed paper trades", ["strategy", "asset", "side", "signal bar", "entry", "exit",
                                       "fill in → out", "bars", "gross", "fees", "funding", "slip",
                                       "net", "on margin", "exit reason", "issues"],
               [[t["strategy"], t["symbol"], t["side"], _day(t["signal_bar"]), _day(t["entry_time"]),
                 _day(t["exit_time"]), f"{_px(t['entry_fill'])} → {_px(t['exit_fill'])}", t["bars_held"],
                 _money(t["gross_pnl"]), _money(t["fees"]), _money(t["funding"]),
                 _money(t["slippage_cost"]), _money(t["net_pnl"]), _pct(t["return_on_margin"], True),
                 t["exit_reason"], "; ".join(t["operational_issues"]) or "—"] for t in ts])  # fmt: skip

    _view(run_id, lambda st, rid: observe.trades_view(observe.RunView(st, rid)), as_json=as_json,
          render=render)  # fmt: skip


@paper.command("intents")
def intents_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    skipped: bool = typer.Option(False, "--skipped", help="Only intents that did not trade."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Every signal Prism acted on and its disposition: ENTERED, PENDING_FILL, EXPECTED_SKIP
    (deliberate) or MISSED_EXECUTION (operational)."""
    from market_signal.paper import observe

    def fn(st, rid):
        v = observe.RunView(st, rid)
        return observe.skipped_view(v) if skipped else observe.intents_view(v)

    def render(rows):
        if not rows:
            console.print("No intents yet (no strategy signal since the paper clock started).")
            return
        _table("Paper intents", ["bar", "strategy", "asset", "side", "promotion", "risk",
                                 "disposition", "category", "reason"],
               [[_day(i["bar"]), i["strategy"], i["symbol"], i["side"], i["promotion"], i["risk"],
                 i["disposition"], i["category"], i["reason"]] for i in rows])  # fmt: skip

    _view(run_id, fn, as_json=as_json, render=render)


@paper.command("gaps")
def gaps_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Operational gap audit: bars not processed inside their entry window, and what that cost."""
    from market_signal.paper import observe

    def fn(st, rid):
        v = observe.RunView(st, rid)
        cov = observe.coverage(v)
        return {"coverage": {k: x for k, x in cov.items() if k != "bars"}, "gaps": observe.gaps_view(v),
                "notification_lag": observe.notification_lag(v)}  # fmt: skip

    def render(o):
        c = o["coverage"]
        console.print(f"Bars expected {c['expected_bars']}: on time {c['on_time']}, late {c['late']}, "
                      f"unprocessed {c['unprocessed']}, pending {c['pending']}. Days without any cycle: "
                      f"{', '.join(c['days_without_a_cycle']) or 'none'}.", highlight=False)  # fmt: skip
        if o["gaps"]:
            _table("Operational gaps", ["bar", "status", "cause", "cycles in window", "signals",
                                        "missed trades", "positions held", "marks recovered", "unknowable"],
                   [[_day(g["bar"]), g["status"], g["cause"], g["cycles_in_window"], g["signals"],
                     g["trades_made_impossible"], ", ".join(g["positions_held"]) or "—",
                     g["marks_recovered"], "; ".join(g["unknowable"])] for g in o["gaps"]])  # fmt: skip
        else:
            console.print("No operational gaps.")
        if o["notification_lag"]:
            _table("Notification lag (simulated execution time → recorded → Telegram)",
                   ["event", "asset", "execution (UTC)", "recorded lag h", "notified lag h"],
                   [[n["event"], n["symbol"], _day(n["execution_time"]), n["record_lag_hours"],
                     n["notification_lag_hours"] if n["notification_lag_hours"] is not None else "not sent"]
                    for n in o["notification_lag"]])  # fmt: skip

    _view(run_id, fn, as_json=as_json, render=render)


@paper.command("contributions")
def contributions_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Descriptive totals per strategy and per asset (no ranking)."""
    from market_signal.paper import observe

    def render(o):
        _table("Per strategy (descriptive)", ["strategy", "signals", "intents", "entered", "closed",
                                              "wins/losses", "net", "fees", "funding", "skips", "missed"],
               [[k, s["signals"], s["intents"], s["entered"], s["closed_trades"], f"{s['wins']}/{s['losses']}",
                 _money(s["net_pnl"]), _money(s["fees"]), _money(s["funding"]), s["expected_skips"],
                 s["missed_executions"]] for k, s in o["strategies"].items()])  # fmt: skip
        _table("Per asset (descriptive)", ["asset", "closed", "net", "avg exposure", "funding", "fees",
                                           "missed", "open"],
               [[k, a["closed_trades"], _money(a["net_pnl"]), _pct(a["avg_exposure"]), _money(a["funding"]),
                 _money(a["fees"]), a["missed_executions"], a["open"]] for k, a in o["assets"].items()])  # fmt: skip

    _view(run_id, lambda st, rid: observe.contributions(observe.RunView(st, rid)), as_json=as_json,
          render=render)  # fmt: skip


@paper.command("equity")
def equity_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Daily equity curve from the recorded closes (no intraday values are inferred)."""
    from market_signal.paper import observe

    def render(rows):
        if not rows:
            console.print("No completed paper day yet.")
            return
        _table("Paper equity (daily marks)", ["bar (UTC)", "equity", "peak", "drawdown", "exposure", "open"],
               [[_day(r["bar"]), _money(r["equity"]), _money(r["peak"]), _pct(r["drawdown"]),
                 _pct(r["exposure"]), r["open_positions"]] for r in rows])  # fmt: skip

    _view(run_id, lambda st, rid: observe.equity_curve(observe.RunView(st, rid)), as_json=as_json,
          render=render)  # fmt: skip


@paper.command("snapshot")
def snapshot_(run_id: str = typer.Argument(None, help="Default: the open run.")) -> None:
    """WRITE: append an immutable paper_execution snapshot as of the ledger's last event
    (idempotent: an existing snapshot for that point is returned unchanged)."""
    from market_signal.paper import observe

    _view(run_id, lambda st, rid: observe.record_snapshot(st, rid), as_json=True, read_only=False)


@paper.command("brief")
def brief_(
    run_id: str = typer.Argument(None, help="Default: the open run."),
    record: bool = typer.Option(False, "--record", help="WRITE: snapshot + store today's brief."),
    send: bool = typer.Option(False, "--send", help="WRITE: also deliver it via Telegram (once)."),
) -> None:
    """The compact PAPER daily brief. Read-only unless --record/--send.

    --send stores the brief for the last completed paper day once and delivers it at most once
    (failed attempts retried for 24 h; an attempt of unknown outcome is never resent).
    """
    from market_signal.paper import observe

    def sender_factory():
        from market_signal.config import get_settings
        from market_signal.portfolio.telegram import TelegramClient

        return TelegramClient.from_settings(get_settings()).send

    def fn(st, rid):
        if record or send:
            return observe.record_brief(st, rid, send=send, sender_factory=sender_factory)
        return observe.brief(observe.RunView(st, rid))

    def render(o):
        console.print(
            _plain(o["text"]) if o.get("text") else o.get("note"), highlight=False, markup=False
        )
        if record or send:
            extra = {k: o[k] for k in ("snapshot_id", "snapshot_recorded", "brief_id", "brief_recorded",
                                       "delivery") if k in o}  # fmt: skip
            console.print_json(canonical_json(extra))

    out = _view(run_id, fn, as_json=False, render=render, read_only=not (record or send))
    if (out.get("delivery") or {}).get("status") == "failed":
        raise typer.Exit(1)


def _plain(html_text: str) -> str:
    from market_signal.cli.scan_cmds import _plain as plain

    return plain(html_text)


@paper.command("events")
def events_(
    run_id: str = typer.Argument(None, help="Default: the newest run."),
    limit: int = typer.Option(100, "--limit"),
    event_type: str = typer.Option(None, "--type", help="e.g. risk_decision, position_closed."),
) -> None:
    """The immutable event ledger (newest last)."""
    from market_signal.paper.engine import list_events

    _call(lambda ledger: list_events(ledger.store, _one_run(ledger.store, run_id), limit=limit,
                                     event_type=event_type))  # fmt: skip


def _set(run_id: str, status: str, reason: str) -> None:
    from market_signal.paper.engine import set_status

    _call(lambda ledger: set_status(ledger.store, run_id, status, reason=reason), read_only=False)


@paper.command("pause")
def pause(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: no new paper entries; open positions keep running to their scheduled exits."""
    _set(run_id, "PAUSED", reason)


@paper.command("resume")
def resume(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: resume a paused paper run (signals meanwhile are never replayed)."""
    _set(run_id, "ACTIVE", reason)


@paper.command("stop")
def stop(run_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """WRITE: stop a paper run permanently (open positions still run to their exits)."""
    _set(run_id, "STOPPED", reason)


@paper.command("summary")
def summary(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """Read-only ``paper_execution`` evidence summary computed from the ledger."""
    from market_signal.paper.engine import paper_summary

    _call(lambda ledger: paper_summary(ledger.store, _one_run(ledger.store, run_id)))


@paper.command("evidence")
def evidence(run_id: str = typer.Argument(None, help="Default: the newest run.")) -> None:
    """WRITE: append the current ``paper_execution`` summary (never touches Lab evidence)."""
    from market_signal.paper.engine import record_evidence

    _call(lambda ledger: record_evidence(ledger.store, _one_run(ledger.store, run_id)),
          read_only=False)  # fmt: skip
