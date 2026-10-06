"""Order lifecycle and the paper execution adapter.

The lifecycle (``ORDER_STATES``) is the one a future live adapter would also expose:
an intent becomes a ``SUBMITTED`` order, which ends ``FILLED``, ``REJECTED``, ``EXPIRED``
or ``CANCELLED``. ``PARTIALLY_FILLED`` exists in the vocabulary but the v1 fill model
never produces it.

``PaperExecutionAdapter`` is the only adapter. It is pure arithmetic over a stored bar and
the frozen ``ExecutionModel``: it has no network client, no credentials and no transport.
``transmits_orders`` is the literal ``False``, and the engine refuses any other adapter
type, so swapping in an order-placing object would fail before a single order existed.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from market_signal.paper.policy import ExecutionModel
from market_signal.research.lab.common import LabModel, Number, Symbol

ORDER_STATES = ("SUBMITTED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "EXPIRED", "CANCELLED")
TERMINAL_ORDER_STATES = ("FILLED", "REJECTED", "EXPIRED", "CANCELLED")


class PaperOrder(LabModel):
    """A simulated market order. ``side`` is the order direction (+1 buy, -1 sell)."""

    order_id: str
    purpose: Literal["entry", "exit"]
    symbol: Symbol
    side: Literal[1, -1]
    position_side: Literal[1, -1]
    reduce_only: bool
    reference: Literal["next_bar_open", "exit_bar_close"]
    fill_bar_close: str  # ISO close time of the bar whose open/close is the reference
    target_notional: Annotated[Number, Field(gt=0)] | None = None  # entry
    units: Annotated[Number, Field(gt=0)] | None = None  # exit


class PaperExecutionAdapter:
    """Simulated fills only. Structurally incapable of transmitting an order."""

    mode: Final = "paper"
    transmits_orders: Final = False

    def __init__(self, model: ExecutionModel):
        if model.mode != "paper":
            raise ValueError("the paper adapter only accepts a paper execution model")
        self.model = model

    def validate(self, order: PaperOrder) -> str | None:
        """Pre-submission check; a returned reason means the order is REJECTED."""
        try:
            self.model.cost(order.symbol)
        except ValueError as exc:
            return str(exc)
        if order.purpose == "entry" and (order.target_notional is None or order.reduce_only):
            return "an entry order needs a target notional and cannot be reduce-only"
        if order.purpose == "exit" and (order.units is None or not order.reduce_only):
            return "an exit order closes known units and is reduce-only"
        if order.side != (
            order.position_side if order.purpose == "entry" else -order.position_side
        ):
            return "order direction does not match its purpose"
        return None

    def fill(self, order: PaperOrder, reference_price: float) -> dict:
        """Full immediate fill at the reference price, moved against the order by slippage."""
        if not (reference_price > 0):
            raise ValueError("a fill needs a positive reference price")
        c = self.model.cost(order.symbol)
        price = reference_price * (1 + order.side * c.slippage_bps / 1e4)
        units = order.units if order.units is not None else order.target_notional / price
        notional = units * price
        return {
            "order_id": order.order_id,
            "state": "FILLED",
            "reference": order.reference,
            "reference_price": reference_price,
            "slippage_bps": c.slippage_bps,
            "slippage_per_unit": abs(price - reference_price),
            "fill_price": price,
            "units": units,
            "notional": notional,
            "fee_bps": c.fee_bps,
            "fee": notional * c.fee_bps / 1e4,
            "execution_model_id": self.model.policy_id,
        }


def require_paper_adapter(adapter: object) -> PaperExecutionAdapter:
    """The engine's guard: exactly the paper adapter, which never transmits."""
    if type(adapter) is not PaperExecutionAdapter or adapter.transmits_orders is not False:
        raise TypeError("the paper engine only runs with PaperExecutionAdapter")
    return adapter
