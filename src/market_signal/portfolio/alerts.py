"""Local alerts evaluated against scan results.

Architecture: rules → evaluation (pure) → events stored in ``alert_events`` → notifiers.
Only a console notifier ships in V1 (no paid infrastructure). New channels implement the
``Notifier`` protocol and are named in ``config/alerts.yaml``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from market_signal.config import Settings
from market_signal.data.store import Store, new_id
from market_signal.models.domain import utcnow

KINDS = ("price_below", "price_above", "score_gte", "enters_zone", "status_change")


@dataclass
class AlertEvent:
    rule_id: str
    symbol: str
    message: str
    payload: dict[str, Any]


class Notifier(Protocol):
    def send(self, event: AlertEvent) -> None: ...


class ConsoleNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, event: AlertEvent) -> None:
        self.sent.append(event.message)


NOTIFIERS = {"console": ConsoleNotifier}


def rule_id(rule: dict[str, Any]) -> str:
    return "rule_" + hashlib.sha1(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:12]


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    kind = rule.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown alert kind {kind!r}; expected one of {KINDS}")
    if kind in ("price_below", "price_above") and ("symbol" not in rule or "price" not in rule):
        raise ValueError(f"{kind} needs symbol and price")
    if kind == "score_gte" and "score" not in rule:
        raise ValueError("score_gte needs score")
    if kind == "enters_zone" and (
        "symbol" not in rule or rule.get("zone") not in ("entry_zone", "accumulate", "strong_buy")
    ):
        raise ValueError("enters_zone needs symbol and zone in entry_zone|accumulate|strong_buy")
    return rule


def add_rule(store: Store, rule: dict[str, Any]) -> str:
    validate_rule(rule)
    rid = rule_id(rule)
    store.con.execute(
        "INSERT OR REPLACE INTO alert_rules VALUES (?,?,?,?,?,?)",
        [rid, rule["kind"], rule.get("symbol"), json.dumps(rule), True, utcnow()],
    )
    return rid


def sync_yaml_rules(store: Store, settings: Settings) -> None:
    path = settings.paths.config / "alerts.yaml"
    if path.exists():
        for rule in settings.yaml("alerts.yaml").get("rules") or []:
            add_rule(store, rule)


def _in_zone(price: float, zone: Any) -> bool:
    if not zone:
        return False
    lo, hi = zone
    return (lo is None or price >= lo) and (hi is None or price <= hi)


def evaluate_rule(
    rule: dict[str, Any], assessments: list[Any], previous_status: dict[str, str]
) -> list[tuple[str, str]]:
    """Pure evaluation → list of (symbol, message)."""
    kind, sym = rule["kind"], (rule.get("symbol") or "").upper() or None
    out = []
    for a in assessments:
        if sym and a.symbol != sym:
            continue
        if kind == "price_below" and a.price <= float(rule["price"]):
            out.append((a.symbol, f"{a.symbol} {a.price:,.4g} <= {rule['price']}"))
        elif kind == "price_above" and a.price >= float(rule["price"]):
            out.append((a.symbol, f"{a.symbol} {a.price:,.4g} >= {rule['price']}"))
        elif kind == "score_gte" and a.score is not None and a.score >= float(rule["score"]):
            out.append(
                (a.symbol, f"{a.symbol} score {a.score:.0f} >= {rule['score']} ({a.status})")
            )
        elif kind == "enters_zone" and _in_zone(a.price, a.zones.get(rule["zone"])):
            out.append(
                (
                    a.symbol,
                    f"{a.symbol} {a.price:,.4g} is inside its {rule['zone'].replace('_', ' ')}",
                )
            )
        elif kind == "status_change":
            targets = set(rule.get("to") or ["ACTIONABLE", "STRONG", "EXCEPTIONAL"])
            prev = previous_status.get(a.symbol)
            if (
                a.status in targets
                and prev is not None
                and prev != a.status
                and prev not in targets
            ):
                out.append((a.symbol, f"{a.symbol} status {prev} → {a.status}: {a.status_text}"))
    return out


def _previous_status(store: Store, current_scan: str) -> dict[str, str]:
    df = store.query(
        """SELECT symbol, status FROM scan_results WHERE scan_id =
           (SELECT scan_id FROM scan_runs WHERE scan_id <> ? ORDER BY created_at DESC LIMIT 1)""",
        [current_scan],
    )
    return dict(zip(df["symbol"], df["status"], strict=True)) if not df.empty else {}


def evaluate_alerts(
    store: Store, settings: Settings, scan: Any, notifiers: list[Notifier] | None = None
) -> list[str]:
    sync_yaml_rules(store, settings)
    cfg = settings.yaml("alerts.yaml") if (settings.paths.config / "alerts.yaml").exists() else {}
    cooldown = pd.Timedelta(hours=float(cfg.get("cooldown_hours", 24)))
    notifiers = (
        notifiers
        if notifiers is not None
        else [NOTIFIERS[n]() for n in cfg.get("notifiers", ["console"]) if n in NOTIFIERS]
    )
    rules = store.query("SELECT rule_id, params FROM alert_rules WHERE active")
    prev = _previous_status(store, scan.scan_id)
    fired = []
    now = pd.Timestamp(utcnow())
    for _, r in rules.iterrows():
        rule = json.loads(r["params"])
        for symbol, msg in evaluate_rule(rule, scan.assessments, prev):
            last = store.query(
                "SELECT max(fired_at) t FROM alert_events WHERE rule_id=? AND symbol=?",
                [r["rule_id"], symbol],
            ).iloc[0]["t"]
            if pd.notna(last) and now - pd.Timestamp(last) < cooldown:
                continue
            ev = AlertEvent(r["rule_id"], symbol, msg, rule)
            store.con.execute(
                "INSERT INTO alert_events VALUES (?,?,?,?,?,?,?)",
                [new_id("al_"), r["rule_id"], now, symbol, msg, json.dumps(rule), False],
            )
            for n in notifiers:
                n.send(ev)
            fired.append(msg)
    return fired
