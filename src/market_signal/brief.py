"""The daily brief: Today's answer as one short message (sent via Telegram).

Built from the most recently *stored* scan with the same presentation rules as the
dashboard's Today page (``market_signal.presenter``), plus data-freshness warnings and any
alert events not yet delivered. Deterministic; no new analytics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

import pandas as pd

from market_signal.config import Settings
from market_signal.data.freshness import Freshness, check_freshness
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.presenter import (
    build_view,
    closest_candidate,
    load_evidence,
    market_state,
    money,
    triage,
)
from market_signal.scoring.engine import load_latest_scan

MAX_CARDS = 5
MAX_ALERTS = 10
ALERT_LOOKBACK_DAYS = 7  # older undelivered alerts are no longer news


@dataclass
class Brief:
    text: str  # Telegram HTML
    alert_ids: list[str] = field(default_factory=list)
    freshness: Freshness | None = None


def pending_alerts(store: Store, now: pd.Timestamp) -> pd.DataFrame:
    try:
        return store.query(
            "SELECT event_id, fired_at, symbol, message FROM alert_events "
            "WHERE delivered_at IS NULL AND fired_at >= ? ORDER BY fired_at",
            [now - pd.Timedelta(days=ALERT_LOOKBACK_DAYS)],
        )
    except Exception:
        return pd.DataFrame(columns=["event_id", "fired_at", "symbol", "message"])


def mark_delivered(store: Store, event_ids: list[str]) -> None:
    if event_ids:
        marks = ",".join("?" * len(event_ids))
        store.con.execute(
            f"UPDATE alert_events SET delivered_at = ? WHERE event_id IN ({marks})",
            [utcnow(), *event_ids],
        )


def build_brief(
    store: Store,
    settings: Settings,
    update_exit: int | None = None,
    now: pd.Timestamp | None = None,
) -> Brief:
    now = pd.Timestamp(now or utcnow())
    e = escape
    fresh = check_freshness(store, settings, now)
    latest = load_latest_scan(store)
    lines = [f"<b>Prism · {now:%a %d %b}</b>"]
    from market_signal.demo import is_synthetic

    if is_synthetic(store):
        lines.append("<b>SYNTHETIC DEMO DATA</b>: not real prices; no conclusions.")

    warn = fresh.problems()
    if update_exit:
        warn.insert(
            0, f"The last data update had failures (exit {update_exit}); see data/daily.log."
        )
    if fresh.data_stale or fresh.scan_stale:
        warn.append("The answer below may be out of date. Check the scheduled task.")
    if warn:
        lines.append("")
        lines += [f"⚠️ {e(w)}" for w in warn]

    if latest is None:
        lines += ["", "No scan stored yet. Run <code>market scan</code>."]
        return Brief("\n".join(lines), freshness=fresh)
    res, created = latest
    evidence = load_evidence(
        store, int(settings.yaml("backtest.yaml")["statistics"]["min_events_for_conclusion"])
    )
    views = [build_view(a, evidence) for a in res.assessments]
    act, wait, _ = triage(views)

    lines.append("")
    if act:
        lines.append(f"<b>{len(act)} ACTIONABLE: {e(', '.join(v.symbol for v in act))}</b>")
        if wait:
            lines.append(f"{len(wait)} more waiting for a better price.")
    else:
        lines.append("<b>NO ACTIONABLE OPPORTUNITIES TODAY</b>")
        lines.append("Holding cash is a valid result.")
        c = closest_candidate(views)
        if c is not None:
            lines.append(f"Closest: <b>{e(c.symbol)}</b>, {e(c.setup)}: {e(c.short_reason.lower())} "
                         f"(evidence {e(c.evidence.label)})")  # fmt: skip

    featured = (act + wait)[:MAX_CARDS]
    if featured:
        lines += ["", "<b>Best opportunities</b>"]
        for i, v in enumerate(featured, 1):
            score = "–" if v.score is None else f"{v.score:.0f}"
            ev = v.evidence.label + (" · limited sample" if v.evidence.sample_limited else "")
            lines.append(
                f"{i}. <b>{e(v.symbol)}</b> {e(v.decision)} · {e(v.setup)} · setup {score} {e(v.strength)}"
            )
            lines.append(
                f"    {e(v.entry.distance_text)}: now {e(money(v.entry.current))}, preferred {e(v.entry.preferred)}"
            )
            lines.append(f"    Evidence {e(ev)}" + (f" · Risk: {e(v.risks[0])}" if v.risks else ""))

    ms = " · ".join(
        f"{r['label']} {r['regime'].replace('_', ' ')}" for r in market_state(res.regimes)
    )
    lines += ["", f"<b>Market</b>: {e(ms)}"]

    alerts = pending_alerts(store, now)
    if not alerts.empty:
        lines += ["", f"<b>Alerts</b> ({len(alerts)} new)"]
        lines += [f"• {e(m)}" for m in alerts["message"].head(MAX_ALERTS)]
        if len(alerts) > MAX_ALERTS:
            lines.append(f"… and {len(alerts) - MAX_ALERTS} more (Data health &amp; alerts page)")

    lines += [
        "",
        f"<i>Scan {created:%d %b %H:%M} UTC. Decision support only; Prism never places trades.</i>",
    ]
    return Brief("\n".join(lines), list(alerts["event_id"]), fresh)
