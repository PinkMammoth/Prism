"""Markdown rendering of a stored Phase 19 result (presentation only; computes nothing new)."""

from __future__ import annotations

from collections import Counter

from market_signal.research.oiprice.study.verdicts import VERDICTS


def _fmt(v, kind: str = "event") -> str:
    if v is None:
        return "–"
    return f"{v:+.3f}" if kind == "ic" else f"{v * 1e4:+.1f}"


def _p(v) -> str:
    return "–" if v is None else (f"{v:.4f}" if v < 0.001 else f"{v:.3f}")


def _ci(ci, kind: str) -> str:
    if not ci or ci[0] is None:
        return "–"
    return f"[{_fmt(ci[0], kind)}, {_fmt(ci[1], kind)}]"


def render(payload: dict) -> str:
    a = payload["availability"]
    pr = payload["primary"]
    lines = [f"# Phase 19 result `{payload['study_id']}`", "", f"> {payload['statement']}", "",
             f"> {a['statement']} (bars {a['assumed_bar_latency_s']:g} s, OI "
             f"{a['assumed_oi_latency_s']:g} s)", "",
             f"Venue **{pr['venue']}**, events {pr['event_window'][0][:16]} → "
             f"{pr['event_window'][1][:16]}, primary horizon {payload['primary_horizon_bars']} h. "
             f"Cross-venue gate: {pr['cross_venue_gate']}.", ""]  # fmt: skip
    cnt = Counter(r["verdict"] for r in pr["verdicts"])
    lines += ["| " + " | ".join(VERDICTS) + " |", "|" + "---:|" * len(VERDICTS),
              "| " + " | ".join(str(cnt.get(x, 0)) for x in VERDICTS) + " |", ""]  # fmt: skip
    vmap: dict = {}
    for r in pr["verdicts"]:
        vmap.setdefault((r["family"], r["member"]), []).append(f"{r['direction']}: {r['verdict']}")
    for fam, fd in pr["families"].items():
        lines += [f"**{fam}** (m = {fd['m']} of {fd['preregistered']})", "",
                  "| member | baseline | n | stat (+) | 95% CI | net (+) | p | p iid | q | verdicts |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]  # fmt: skip
        for r in fd["members"]:
            k = r["kind"]
            lines.append(
                f"| {r['member']} | {r['baseline']} | {r.get('independent_events', 0)} | "
                f"{_fmt(r.get('stat'), k)} | {_ci(r.get('ci95'), k)} | {_fmt(r.get('net_mean'))} | "
                f"{_p(r.get('p_value'))} | {_p(r.get('p_naive_iid'))} | {_p(r.get('q_value'))} | "
                f"{'; '.join(vmap.get((fam, r['member']), []))} |"
            )
        lines.append("")
    return "\n".join(lines)
