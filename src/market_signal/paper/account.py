"""Paper account state, derived only by replaying the immutable event ledger.

``replay(events)`` folds the append-only ``paper_events`` of one run into an
``AccountState``. The engine never stores "current position" rows: positions, cash,
orders and marks are always this fold. Audit-only events (signals, intents, risk
decisions, data issues) change nothing here but stay in the ledger.

Margin semantics (risk policy ``isolated``): opening a position moves its isolated margin
and the entry fee out of cash. Funding is paid from (or credited to) that position's
margin balance, which moves its liquidation price. Closing returns
``margin balance + side x units x (exit fill - entry fill) - exit fee`` to cash; a
liquidation returns nothing (the whole remaining isolated margin is lost).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from market_signal.perps.backtest import liquidation_price

EVENT_TYPES = (
    "run_created",
    "status_changed",
    "signals_evaluated",
    "signal_consumed",
    "intent_created",
    "risk_decision",
    "order_submitted",
    "order_filled",
    "order_rejected",
    "order_expired",
    "order_cancelled",
    "position_opened",
    "funding_accrued",
    "liquidation",
    "exit_intent",
    "position_closed",
    "account_mark",
    "kill_switch",
    "data_issue",
)
RUN_STATUSES = ("ACTIVE", "PAUSED", "STOPPED", "KILLED")
TERMINAL_STATUSES = ("STOPPED", "KILLED")
EPS = 1e-6


class ImpossibleState(RuntimeError):
    """The ledger describes something a consistent account cannot be in."""


@dataclass
class AccountState:
    run_id: str | None = None
    created_at: str | None = None
    starting_equity: float = 0.0
    cash: float = 0.0
    status: str | None = None
    positions: dict[str, dict] = field(default_factory=dict)  # symbol -> open position
    orders: dict[str, dict] = field(default_factory=dict)  # order_id -> unfilled order
    closed: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)
    last_bar: str | None = None
    peak_equity: float = 0.0
    halted_bars: set[str] = field(default_factory=set)
    seq: int = 0
    counts: Counter = field(default_factory=Counter)

    # ------------------------------------------------------------------ valuation
    @staticmethod
    def position_value(p: dict, price: float) -> float:
        return p["margin_bal"] + p["side"] * p["units"] * (price - p["entry_fill"])

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            self.position_value(p, prices.get(s, p["last_price"]))
            for s, p in self.positions.items()
        )

    def liquidation_price(self, p: dict) -> float:
        return liquidation_price(p["entry_fill"], p["side"], p["units"], p["margin_bal"],
                                 p["maintenance_rate"])  # fmt: skip

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def flat(self) -> bool:
        return not self.positions and not self.orders

    def entry_orders(self) -> list[dict]:
        return [o for o in self.orders.values() if o["order"]["purpose"] == "entry"]


def apply(state: AccountState, event: dict) -> None:
    """Fold one event into the state (mutates). Raises ``ImpossibleState`` on contradiction."""
    t, p = event["event_type"], event["payload"]
    if event["seq"] != state.seq + 1:
        raise ImpossibleState(f"event sequence gap at {event['seq']} (expected {state.seq + 1})")
    state.seq = event["seq"]
    state.counts[t] += 1
    if t == "run_created":
        if state.run_id is not None:
            raise ImpossibleState("run created twice")
        state.run_id, state.created_at = event["run_id"], p["created_at"]
        state.starting_equity = state.cash = state.peak_equity = float(p["starting_equity"])
        state.status = "ACTIVE"
        return
    if state.run_id is None:
        raise ImpossibleState("event before run_created")
    if t == "status_changed":
        if state.terminal:
            raise ImpossibleState(f"status change after terminal {state.status}")
        state.status = p["status"]
    elif t == "order_submitted":
        oid = p["order"]["order_id"]
        if oid in state.orders:
            raise ImpossibleState(f"order {oid} submitted twice")
        state.orders[oid] = p
    elif t in ("order_rejected", "order_expired", "order_cancelled", "order_filled"):
        if p["order_id"] not in state.orders:
            raise ImpossibleState(f"{t} for unknown order {p['order_id']}")
        del state.orders[p["order_id"]]
    elif t == "position_opened":
        s = p["symbol"]
        if s in state.positions:
            raise ImpossibleState(f"second position on {s}")
        state.cash -= p["margin"] + p["entry_fee"]
        state.positions[s] = {**p, "margin_bal": p["margin"], "funding_paid": 0.0,
                              "funding_missing": 0, "last_price": p["entry_fill"]}  # fmt: skip
    elif t == "funding_accrued":
        pos = _position(state, p)
        pos["margin_bal"] -= p["amount"]
        pos["funding_paid"] += p["amount"]
        pos["funding_missing"] += p["missing"]
    elif t == "position_closed":
        pos = _position(state, p)
        state.cash += p["proceeds"]
        del state.positions[pos["symbol"]]
        state.closed.append({**pos, **p})
    elif t == "account_mark":
        for s, price in p["prices"].items():
            if s in state.positions:
                state.positions[s]["last_price"] = price
        state.last_bar = p["bar_close"]
        state.peak_equity = max(state.peak_equity, p["equity"])
        state.marks.append(p)
    elif t == "kill_switch" and p["kind"] == "daily_loss_halt":
        state.halted_bars.add(p["bar_close"])
    if state.cash < -EPS:
        raise ImpossibleState(f"negative cash {state.cash:.6f} after {t}")


def _position(state: AccountState, p: dict) -> dict:
    pos = state.positions.get(p["symbol"])
    if pos is None or pos["position_id"] != p["position_id"]:
        raise ImpossibleState(f"event for a position that is not open: {p['position_id']}")
    return pos


def replay(events: list[dict]) -> AccountState:
    state = AccountState()
    for e in events:
        apply(state, e)
    return state
