"""PAPER notification texts. Every message is labelled SIMULATED and says no order was placed.

Deliberately few: a position opened, a position closed, a kill switch / automatic pause,
and the start of an engine error streak. Wording avoids BUY/SELL so a message can never be
mistaken for a real execution or an instruction.
"""

from __future__ import annotations

import hashlib

import pandas as pd

MAX_SINGLE = 4  # more pending messages than this are sent as one digest
HEADER = "🧪 <b>PAPER · SIMULATED</b> — no real order was placed"
FOOTER = "Research account only. Not an instruction to trade."


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _when(iso: str) -> str:
    return pd.Timestamp(iso).tz_convert("UTC").strftime("%d %b %H:%M UTC")


def _side(side: int) -> str:
    return "LONG" if side > 0 else "SHORT"


def _usd(x: float) -> str:
    return f"{x:,.2f} USDC"


def _px(x: float) -> str:
    return f"{x:,.4f}" if x < 10 else f"{x:,.2f}"


def render_event(e: dict) -> str:
    t, p = e["event_type"], e["payload"]
    if t == "position_opened":
        body = [
            f"<b>Paper position opened: {p['symbol']} {_side(p['side'])}</b>",
            f"Strategy: {p['strategy']} (signal on the {_when(p['signal_bar'])} close)",
            f"Simulated fill {_px(p['entry_fill'])} at the {_when(p['entry_time'])} open "
            f"(reference {_px(p['entry_ref'])} + slippage) · fee {_usd(p['entry_fee'])}",
            f"Notional {_usd(p['notional'])} · {p['leverage']:g}x isolated · margin {_usd(p['margin'])}",
            f"Approx. liquidation {_px(p['liquidation_price_at_entry'])} · scheduled exit at the "
            f"{_when(p['exit_bar'])} close",
        ]
    elif t == "position_closed":
        why = {"time_exit": "scheduled time exit", "liquidation": "SIMULATED LIQUIDATION"}[
            p["reason"]
        ]
        body = [
            f"<b>Paper position closed: {p['symbol']} {_side(p['side'])}</b> ({why})",
            f"Strategy: {p['strategy']} · {p['bars_held']} bars",
            f"Exit {_px(p['exit_fill'])} at the {_when(p['exit_time'])} close",
            f"Net {_usd(p['net_pnl'])} ({p['return_on_notional']:+.2%} on notional) · fees "
            f"{_usd(p['fees'])} · funding cash flow {_usd(-p['funding'])}",
        ]
    elif t == "kill_switch":
        body = [f"<b>Paper kill switch: {p['kind']}</b>", p["detail"], f"Effect: {p['effect']}"]
    elif t == "status_changed":
        body = [f"<b>Paper run {p['status']}</b> (automatic)", p["reason"],
                "Open paper positions keep running to their scheduled exits."]  # fmt: skip
    else:
        body = [f"Paper event {t}"]
    return "\n".join([HEADER, "", *body, "", f"Run {e['run_id'][:21]}…", FOOTER])


def render_error(summary: dict) -> str:
    return "\n".join([HEADER, "", "<b>Paper engine error</b>",
                      str(summary.get("error", "unknown error"))[:500],
                      "No paper events were recorded by the failed cycle; it will be retried.",
                      "", FOOTER])  # fmt: skip


def render_digest(texts: list[str]) -> str:
    lines = [HEADER, "", f"<b>{len(texts)} paper updates</b>"]
    for t in texts:
        first = next((x for x in t.split("\n")[2:] if x.strip()), "")
        lines.append("• " + first)
    lines += ["", "Details: market lab paper events", FOOTER]
    return "\n".join(lines)
