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


def register(app: typer.Typer) -> None:
    app.command("scan")(scan)
    app.command("asset")(asset)
    app.command("hype")(hype)
    app.command("regime")(regime)
