"""Scan / asset / hype / regime commands."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from market_signal.cli.common import console, open_store


def _money(x: float | None) -> str:
    if x is None:
        return "–"
    return f"${x:,.2f}" if abs(x) < 1e4 else f"${x:,.0f}"


def _banner(store) -> None:
    from market_signal.demo import is_synthetic

    if is_synthetic(store):
        console.print(
            "[bold black on yellow] SYNTHETIC DEMO DATA — prices, scores and zones are not real [/]"
        )


def scan(limit: int = typer.Option(25, help="Rows to show")) -> None:
    """Score every asset today; rank opportunities; evaluate alerts."""
    from market_signal.scoring.engine import run_scan

    with open_store() as (settings, store):
        _banner(store)
        res = run_scan(store, settings)
        r = res.regimes
        console.print(
            Panel(
                f"Crypto: [bold]{r['crypto']['regime']}[/]   Macro/equities: [bold]{r['macro']['regime']}[/]",
                title="MARKET REGIME",
            )
        )
        t = Table(title="Opportunities")
        for c in (
            "#",
            "asset",
            "class",
            "setup",
            "state",
            "score",
            "cov",
            "fund",
            "value",
            "trend",
            "entry",
            "macro",
            "price",
            "ideal entry",
            "status",
        ):
            t.add_column(c, justify="right" if c in ("score", "price", "ideal entry") else "left")
        for i, a in enumerate(res.assessments[:limit], 1):
            comp = {c.name: c for c in a.components}
            style = {
                "ACTIONABLE": "green",
                "STRONG": "bold green",
                "EXCEPTIONAL": "bold green",
                "WAIT": "yellow",
                "WATCH": "cyan",
            }.get(a.status, "dim")
            t.add_row(str(i), a.symbol, a.asset_class, a.setup.title, a.setup.state, f"{a.score:.0f}" if a.score is not None else "–",
                      f"{a.coverage:.0%}", comp["fundamental"].label(), comp["valuation"].label(), comp["structure"].label(),
                      comp["entry"].label(), comp["macro"].label(), _money(a.price), _money(a.zones.get("ideal_entry")),
                      f"[{style}]{a.status}[/] {a.status_text[:40]}")  # fmt: skip
        console.print(t)
        console.print(f"[bold]{res.headline}[/]")
        try:
            from market_signal.portfolio.alerts import evaluate_alerts

            fired = evaluate_alerts(store, settings, res)
            for f in fired:
                console.print(f"[bold magenta]ALERT[/] {f}")
        except ModuleNotFoundError:
            pass


def asset(symbol: str = typer.Argument(..., help="Asset symbol, e.g. HYPE")) -> None:
    """Structured research summary for one asset: why it scored what it scored."""
    from market_signal.scoring.engine import Scanner

    with open_store() as (settings, store):
        _banner(store)
        a_cfg = settings.asset(symbol)
        sc = Scanner(store, settings)
        a = sc.assess(a_cfg)
        if a is None:
            console.print(f"[red]No data for {symbol}. Run `market update` first.[/]")
            raise typer.Exit(1)
        head = (f"[bold]{a.symbol}[/] — {a.name} ({a.asset_class})   price {_money(a.price)} as of {a.as_of[:16]}\n"
                f"Score [bold]{'–' if a.score is None else f'{a.score:.0f}'}[/]/100 (coverage {a.coverage:.0%})   "
                f"band {a.band}   status [bold]{a.status}[/] — {a.status_text}\nRegime ({a.regime})")  # fmt: skip
        console.print(Panel(head, title="Summary"))
        t = Table(title="Score breakdown")
        t.add_column("component")
        t.add_column("points", justify="right")
        t.add_column("why")
        for c in a.components:
            t.add_row(c.name, c.label(), "\n".join(c.reasons))
        console.print(t)
        s = Table(title="Setups")
        for col in ("setup", "state", "detail", "entry zone", "stop", "research verdict"):
            s.add_column(col)
        for st in a.setups:
            z = f"{st.entry_zone[0]:,.4g}–{st.entry_zone[1]:,.4g}" if st.entry_zone else "–"
            s.add_row(
                st.title,
                st.state,
                st.detail[:70],
                z,
                _money(st.stop),
                st.research_verdict or "not run",
            )
        console.print(s)
        z = a.zones
        zt = Table(title="Price zones")
        zt.add_column("zone")
        zt.add_column("price")

        def rng(v):
            if not v:
                return "–"
            lo, hi = v
            return f"{_money(lo) if lo else '…'} – {_money(hi) if hi else '…'}"

        zt.add_row("current", _money(z["current"]))
        zt.add_row(
            f"fair value ({z.get('zone_basis') or 'no valuation model'})", rng(z.get("fair"))
        )
        zt.add_row("accumulate", rng(z.get("accumulate")))
        zt.add_row("strong buy / dislocation", rng(z.get("strong_buy")))
        zt.add_row("entry zone (technical)", rng(z.get("entry_zone")))
        zt.add_row(
            "distance to ideal entry",
            "–" if z.get("distance_to_ideal") is None else f"{z['distance_to_ideal']:+.1%}",
        )
        zt.add_row(f"invalidation ({z['invalidation_basis']})", _money(z.get("invalidation")))
        for k, v in z["supports"].items():
            zt.add_row(f"support: {k}", _money(v))
        console.print(zt)
        if a.sizing:
            sz = a.sizing
            console.print(Panel(f"{sz['kind']} tier {sz['tier']} × regime {sz['regime_multiplier']}: position {sz['position_fraction']:.1%} of portfolio"
                                + (f", risk {sz['portfolio_risk']:.2%} to stop ({sz['stop_distance']:.1%} away)" if sz.get("portfolio_risk") else "")
                                + f" — {sz['note']}", title="Suggested sizing (no leverage)"))  # fmt: skip
        for w in a.warnings + [f"module note: {n}" for n in a.module_notes]:
            console.print(f"[yellow]⚠ {w}[/]")


def hype() -> None:
    """HYPE valuation monitor (observed vs assumed vs derived, inverse table)."""
    from market_signal.fundamentals.hype import hype_valuation_from_store

    with open_store() as (settings, store):
        _banner(store)
        v = hype_valuation_from_store(store, settings)
        for title, rows in (
            ("Observed", v.observed),
            ("Assumed (config/hype.yaml)", v.assumed),
            ("Derived", list(v.derived.values())),
        ):
            t = Table(title=title)
            for c in ("name", "value", "unit", "source", "as of"):
                t.add_column(c)
            for x in rows:
                val = (
                    "–"
                    if x.value is None
                    else (
                        f"{x.value:.2%}"
                        if x.unit in ("p.a.",) or x.unit == "fraction"
                        else f"{x.value:,.4g}"
                    )
                )
                t.add_row(x.name, val, x.unit, x.source[:60], (x.as_of or "")[:16])
            console.print(t)
        console.print(Panel(f"Current signal: [bold]{v.signal}[/]", title="HYPE"))
        inv = v.inverse_table()
        if not inv.empty:
            t = Table(title="What must be true to justify each price (target yield 4.5%)")
            for c in (
                "HYPE price",
                "required protocol revenue/yr",
                "required USDC",
                "structural yield at price",
                "signal",
            ):
                t.add_column(c, justify="right")
            for _, r in inv.iterrows():
                t.add_row(_money(r["price"]), _money(r["required_core_revenue"]), _money(r["required_usdc"]),
                          "–" if r["yield_at_price"] is None else f"{r['yield_at_price']:.2%}", r["signal"])  # fmt: skip
            console.print(t)
        for w in v.warnings:
            console.print(f"[yellow]⚠ {w}[/]")


def regime() -> None:
    """Current market regime and factor votes."""
    from market_signal.features import FeatureStore
    from market_signal.regimes.engine import build_regimes
    from market_signal.scoring.engine import regime_summary

    with open_store() as (settings, store):
        _banner(store)
        r = regime_summary(build_regimes(store, settings, FeatureStore(store, settings)))
        for k, v in r.items():
            votes = ", ".join(
                f"{n}={'n/a' if x is None else f'{x:+.0f}'}"
                for n, x in (v.get("votes") or {}).items()
            )
            console.print(
                f"[bold]{k}[/]: {v['regime']} (score {v.get('score')}, coverage {v.get('coverage')}) as of {v.get('as_of', '')[:16]}\n  {votes}"
            )


def _plain(html_text: str) -> str:
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", html_text))


def brief(
    send: bool = typer.Option(False, "--send", help="Send via Telegram (default: print only)"),
    update_exit: int = typer.Option(
        0, "--update-exit", help="Exit code of the preceding `market update` (flags failures)"
    ),
) -> None:
    """The daily brief: today's answer, freshness warnings and new alerts (print or Telegram)."""
    from market_signal.brief import build_brief, mark_delivered
    from market_signal.portfolio.telegram import TelegramClient, TelegramError

    with open_store() as (settings, store):
        b = build_brief(store, settings, update_exit=update_exit or None)
        console.print(_plain(b.text), highlight=False, markup=False)
        if not send:
            return
        try:
            TelegramClient.from_settings(settings).send(b.text)
        except TelegramError as exc:
            console.print(f"[red]Telegram: {exc}[/]")
            raise typer.Exit(1) from None
        mark_delivered(store, b.alert_ids)
        console.print(f"[green]Sent to Telegram ({len(b.alert_ids)} alert(s) marked delivered).[/]")


def telegram_setup(
    test: bool = typer.Option(
        True, "--test/--no-test", help="Send a test message to TELEGRAM_CHAT_ID"
    ),
) -> None:
    """Find your Telegram chat id (message your bot first), then send a test message."""
    from market_signal.config import get_settings
    from market_signal.portfolio.telegram import TelegramClient, TelegramError

    settings = get_settings()
    try:
        tg = TelegramClient.from_settings(settings, require_chat=False)
        me = tg.get_me()
        console.print(
            f"[green]Token OK[/]: bot @{me.get('username', '?')} ({me.get('first_name', '')})."
        )
        chats = tg.recent_chats()
    except TelegramError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    if not tg.chat_id:
        if not chats:
            console.print("No messages found. Open your bot in Telegram, press Start / send it any "
                          "message, then run this again.")  # fmt: skip
            raise typer.Exit(1)
        for c in chats:
            console.print(f"chat id [bold]{c['id']}[/]  ({c['type']}, {c['name']})")
        console.print(
            "Add yours to .env as TELEGRAM_CHAT_ID=<id>, then run `market telegram-setup` again."
        )
        return
    console.print(f"Using TELEGRAM_CHAT_ID={tg.chat_id}.")
    if test:
        try:
            tg.send("<b>Prism</b>: Telegram is set up. The daily brief will arrive here.")
        except TelegramError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from None
        console.print("[green]Test message sent.[/]")


def perps() -> None:
    """Perp funding & open-interest monitor (context only: no perp strategy is tested yet)."""
    from market_signal.perps.monitor import perp_overview

    def pct(v, signed=True):
        return "–" if v is None else (f"{v:+.0%}" if signed else f"{v:.0%}")

    def compact(v):
        if v is None:
            return "–"
        for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(v) >= div:
                return f"${v / div:.1f}{unit}"
        return f"${v:,.0f}"

    with open_store(read_only=True) as (settings, store):
        _banner(store)
        rows = perp_overview(store, settings)
        if not rows:
            console.print("No perp coins configured (config/perps.yaml).")
            return
        t = Table(title="Perps: funding (annualised) and open interest. Context, not signals")
        cols = ("coin", "7d fund", "30d", "pctile", "state", "OI", "OI 7d", "max lev")
        for c in cols:
            t.add_column(c, justify="left" if c in ("coin", "state") else "right", no_wrap=True)
        style = {"CROWDED LONG": "red", "CROWDED SHORT": "cyan", "NEUTRAL": "green"}
        for v in rows:
            stale = " [yellow](stale)[/]" if v.funding_stale else ""
            t.add_row(v.coin, pct(v.funding_avg_ann) + stale, pct(v.funding_30d_ann),
                      pct(v.percentile, False), f"[{style.get(v.state, 'yellow')}]{v.state}[/]",
                      compact(v.oi_notional), pct(v.oi_change_7d),
                      "–" if v.max_leverage is None else f"{v.max_leverage:.0f}x")  # fmt: skip
        console.print(t)
        console.print("Funding is annualised; > 0 means longs pay shorts. pctile = this coin's 7-day funding vs its own past year. "
                      "Whether extremes predict anything is untested (Phase 3).")  # fmt: skip


def register(app: typer.Typer) -> None:
    app.command("perps")(perps)
    app.command("brief")(brief)
    app.command("telegram-setup")(telegram_setup)
    app.command("scan")(scan)
    app.command("asset")(asset)
    app.command("hype")(hype)
    app.command("regime")(regime)
