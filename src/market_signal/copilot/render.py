"""Telegram rendering for co-pilot alerts (HTML, Prism's existing ``TelegramClient``).

Deterministic text from a decision record: header (asset, bias, setup, priority), the
conditions that fired, historical evidence, research/validation/forward maturity, why it
surfaced and a fixed caveat. No probabilities, no "buy"/"sell" wording: the side is the
tested hypothesis (LONG/SHORT BIAS), never an instruction.
"""

from __future__ import annotations

import hashlib
from html import escape

import pandas as pd

RENDER_VERSION = "copilot_render_v1"
MAX_SINGLE = 3  # more alerts than this in one run are sent as one compact digest
SHORT_TITLES = {"ma_trend": "MA Trend", "donchian_breakout": "Donchian Breakout"}
OPS = {"gt": "above", "ge": "at or above", "lt": "below", "le": "at or below",
       "crosses_above": "crossed above", "crosses_below": "crossed below"}  # fmt: skip
PRIORITY_TEXT = {"WATCH": "WATCH", "STRONG_WATCH": "STRONG WATCH"}
WHY = {
    "versions_compatible": None,
    "data_quality": "data current and complete",
    "signal_new": "signal is new",
    "strategy_not_retired": None,
    "evidence_available": None,
    "tier_eligible": "evidence tier eligible",
    "sample_adequate": "adequate sample",
    "effect_positive": "positive historical effect",
    "breadth_ok": "broad asset support",
    "not_isolated_spike": None,
    "full_research_not_adverse": "full research not adverse",
    "validation_not_adverse": None,  # supportive validation shows up as a priority reason
    "forward_not_adverse_mature": None,
}
FOOTER = {
    "EXPLORATORY": "Exploratory evidence — human review only. Not an automated trade signal.",
    "RESEARCH_SUPPORTED": "Research-supported, not validated — human review only. Not an "
    "automated trade signal.",
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def feature_label(name: str) -> str:
    if name == "close":
        return "Close"
    head, _, n = name.rpartition("_")
    if head in ("ema", "sma") and n.isdigit():
        return f"{n}D {head.upper()}"
    return name


def _value(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.4g}" if abs(x) < 1000 else f"{x:,.0f}"


def _pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.1%}" if signed else f"{x:.0%}"


def setup_title(subject: dict) -> str:
    family = subject.get("family") or subject.get("ledger_family") or ""
    title = SHORT_TITLES.get(family) or family.replace("_", " ").title() or subject.get("name")
    params = subject.get("params") or {}
    return f"{title} {'/'.join(str(v) for v in params.values())}".strip()


def condition_lines(signal: dict) -> list[str]:
    """Each condition in words, with the values at the signal bar."""
    feats, truth = signal.get("features") or {}, signal.get("conditions") or {}
    out = []
    for c in signal.get("definition_conditions") or []:
        left, right = c["left"]["name"], c["right"]
        rname = right["name"] if isinstance(right, dict) else None
        text = f"{feature_label(left)} {OPS.get(c['op'], c['op'])} "
        text += feature_label(rname) if rname else _value(right)
        lv, rv = feats.get(left), feats.get(rname) if rname else None
        if lv is not None and (rname is None or rv is not None):
            text += f" ({_value(lv)} vs {_value(rv if rname else right)})"
        # compiler label: "<left> <op> <right name | float repr>"
        label = f"{left} {c['op']} {rname if rname else repr(float(right) + 0.0)}"
        if truth.get(label) is False:
            text += " — not met"
        out.append(text)
    return out


def _stats_line(st: dict) -> str:
    p, q, target = st.get("raw_p"), st.get("q"), st.get("q_target") or 0.10
    parts = [f"Raw p {p:.2f}" if p is not None else "Raw p n/a"]
    if q is None:
        parts.append("BH q n/a (no family correction recorded)")
    elif st.get("fdr_survivor"):
        parts.append(f"BH q {q:.2f} (survived family correction at {target:.2f})")
    else:
        parts.append(f"BH q {q:.2f} (did not survive family correction)")
    return " · ".join(parts)


def research_lines(ev: dict) -> list[str]:
    fr, val, fwd = ev.get("full_research"), ev.get("validation"), ev.get("forward")
    if fr:
        line = f"Full research: {fr['status'].removeprefix('FULL_RESEARCH_')}"
        wf = fr.get("walk_forward") or {}
        if wf.get("adequate_folds"):
            line += f" · walk-forward {wf.get('positive_adequate_folds')}/{wf['adequate_folds']} blocks positive"
        sens = (fr.get("sensitivity") or {}).get("label")
        if sens:
            line += f" · sensitivity {sens}"
    else:
        line = "Full research: not run"
    lines = [line]
    n = ((val or {}).get("sample") or {}).get("independent_events")
    if val and val["status"] not in ("VALIDATION_INSUFFICIENT", "VALIDATION_ERROR"):
        lines.append(f"Validation: {val['status'].removeprefix('VALIDATION_')}"
                     + (f" ({n} independent events)" if n is not None else ""))  # fmt: skip
    elif val is None:
        lines.append("Validation: not run")
    else:
        end = pd.Timestamp(val["window"][1]) if len(val.get("window") or []) == 2 else None
        why = (f"reserved period runs to {end:%d %b %Y}" if n is None and end is not None
               else f"{n} independent events" if n is not None else val["status"].lower())  # fmt: skip
        lines.append(f"Validation: not mature / insufficient ({why})")
    if fwd:
        level = fwd["maturity"].replace("_", " ")
        line = (f"Forward: {level} ({fwd['independent_resolved']} resolved, "
                f"{fwd['signals_recorded']} prospective signals so far)")  # fmt: skip
        if fwd["maturity"] != "TOO_EARLY" and fwd.get("excess_mean") is not None:
            line += f" · excess {_pct(fwd['excess_mean'])}"
            if fwd.get("direction_vs_historical"):
                line += f", {fwd['direction_vs_historical']} direction as history"
        lines.append(line)
    else:
        lines.append("Forward: not prospectively tracked")
    return lines


def why_line(result: dict) -> str:
    passed = [WHY[c["rule"]] for c in result["checks"] if c["passed"] and WHY.get(c["rule"])]
    return "Why surfaced: " + ", ".join(passed)


def render_alert(record: dict, *, preview: bool = False) -> str:
    """One alert (or, with ``preview``, an explicitly labelled rendering check)."""
    e = escape
    ev, sig, res = record["evidence"], record["signal"], record["result"]
    side = "LONG BIAS" if record["side"] == "long" else "SHORT BIAS"
    prio = PRIORITY_TEXT.get(res.get("priority") or "", "not surfaced")
    bar = pd.Timestamp(record["bar_close"])
    lines = []
    if preview:
        lines += ["<b>PREVIEW — no current signal.</b> Rendering check only; not an alert.", ""]
    lines += [
        f"<b>{e(record['symbol'])} · {side}</b>",
        f"{e(setup_title(ev['subject']))} · <b>{prio}</b>",
        "",
        f"<b>Setup</b> (daily close {bar:%d %b %H:%M} UTC)",
        *(f"• {e(t)}" for t in condition_lines(sig)),
        "• " + ("New signal on this close" if sig.get("fired") else "Not a new signal on this close")
        + f" ({sig.get('cooldown_bars', 0)}-bar cooldown)",
        "",
        f"<b>Evidence</b> · {e(ev['tier'])}"
        + (f" (historical {e(ev['historical_tier'])})" if ev["historical_tier"] != ev["tier"] else ""),
        f"{e(ev['primary_horizon'])} excess in the tested direction vs same-asset baseline: "
        f"{_pct(ev['effect']['excess_mean'])}"
        f" (net {_pct(ev['effect'].get('net_mean'))}, hit rate {_pct(ev['effect'].get('hit_rate'), False)})",
        f"{ev['sample']['independent_events']} independent events · "
        f"{ev['assets']['assets_with_events']} assets ({ev['assets'].get('positive')} positive)",
        f"Parameter neighbourhood: {e(str(ev['neighbourhood'].get('label') or 'n/a'))}",
        e(_stats_line(ev["statistics"])),
        "",
        "<b>Research</b>",
        *(e(t) for t in research_lines(ev)),
        "",
    ]  # fmt: skip
    if res["decision"] == "ALERT":
        lines.append(e(why_line(res)))
        lines.append(e("Priority: " + "; ".join(res["priority_reasons"])))
    else:
        lines.append(e("Would not surface: " + ", ".join(res["blocked_by"])))
    lines.append(
        f"<i>{e(FOOTER.get(ev['tier'], 'Human review only. Not an automated trade signal.'))}</i>"
    )
    return "\n".join(lines)


def render_digest(records: list[dict]) -> str:
    """Several alerts from one run in one compact message (no alert is dropped)."""
    e = escape
    bars = sorted({pd.Timestamp(r["bar_close"]) for r in records})
    when = ", ".join(f"{b:%d %b}" for b in bars)
    lines = [f"<b>Prism co-pilot · {len(records)} setups on the {when} close</b>", ""]
    for i, r in enumerate(records, 1):
        ev, res = r["evidence"], r["result"]
        side = "LONG BIAS" if r["side"] == "long" else "SHORT BIAS"
        st = ev["statistics"]
        q = st.get("q")
        fr = (ev.get("full_research") or {}).get("status", "NOT RUN").removeprefix("FULL_RESEARCH_")
        val = (ev.get("validation") or {}).get("status", "NOT RUN").removeprefix("VALIDATION_")
        fwd = (ev.get("forward") or {}).get("maturity", "NOT TRACKED").replace("_", " ")
        lines += [
            f"{i}. <b>{e(r['symbol'])} · {side}</b> · {e(setup_title(ev['subject']))} · "
            f"<b>{PRIORITY_TEXT[res['priority']]}</b>",
            f"    {e(ev['tier'])} · {e(ev['primary_horizon'])} excess {_pct(ev['effect']['excess_mean'])} · "
            f"{ev['sample']['independent_events']} events / {ev['assets']['assets_with_events']} assets · "
            + ("q n/a" if q is None else f"q {q:.2f}" + ("" if st.get("fdr_survivor") else " (not FDR-significant)")),
            f"    Full research {e(fr)} · Validation {e(val)} · Forward {e(fwd)}",
        ]  # fmt: skip
    lines += ["", "<i>Details: <code>market lab copilot decisions</code>. Human review only — "
              "not automated trade signals.</i>"]  # fmt: skip
    return "\n".join(lines)
