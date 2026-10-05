"""Markdown rendering of a stored Phase 18 result (presentation only; computes nothing new)."""

from __future__ import annotations

from collections import Counter

from market_signal.research.relative.study.verdicts import VERDICTS

BPS_KINDS = ("event", "contrast", "spread")


def _fmt(v, kind: str = "event", plus: bool = True) -> str:
    if v is None:
        return "–"
    if kind in BPS_KINDS:
        return f"{v * 1e4:+.1f}" if plus else f"{v * 1e4:.1f}"
    return f"{v:+.3f}" if plus else f"{v:.3f}"


def _p(v) -> str:
    return "–" if v is None else (f"{v:.4f}" if v < 0.001 else f"{v:.3f}")


def _ci(ci, kind: str) -> str:
    if not ci or ci[0] is None:
        return "–"
    return f"[{_fmt(ci[0], kind)}, {_fmt(ci[1], kind)}]"


def _verdicts(a: dict) -> dict:
    out: dict = {}
    for r in a["verdicts"]:
        out[(r["venue"], r["family"], r["member"], r["direction"])] = r["verdict"]
    return out


def render(payload: dict) -> str:
    lines = [f"# Phase 18 result `{payload['study_id']}`", "", f"> {payload['statement']}", "",
             f"> {payload['availability']['statement']} (assumed latency "
             f"{payload['availability']['assumed_latency_s']:g} s)", ""]  # fmt: skip
    for an, a in payload["architectures"].items():
        lines += [
            f"## {an} ({a['timeframe']}; primary horizon {a['primary_horizon_bars']} bars)",
            "",
        ]
        cnt = Counter((r["venue"], r["verdict"]) for r in a["verdicts"])
        lines += ["| venue | " + " | ".join(VERDICTS) + " |", "|---|" + "---:|" * len(VERDICTS)]
        for v in a["venues"]:
            lines.append(f"| {v} | " + " | ".join(str(cnt.get((v, x), 0)) for x in VERDICTS) + " |")
        lines.append("")
        vmap = _verdicts(a)
        for v, vv in a["venues"].items():
            lines += [
                f"### {an} — {v} ({vv['event_window'][0][:10]} → {vv['event_window'][1][:10]})",
                "",
            ]
            if vv.get("empty"):
                lines += ["(no data)", ""]
                continue
            for fam, fd in vv["families"].items():
                lines += [f"**{fam}** (m = {fd['m']} of {fd['preregistered']})", "",
                          "| member | target | n | stat (cont.) | 95% CI | net cont. | p | p naive | q | cont. / rev. |",
                          "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]  # fmt: skip
                for r in fd["members"]:
                    k = r["kind"]
                    lines.append(
                        f"| {r['member']} | {r['target']} | {r.get('independent_events', 0)} | "
                        f"{_fmt(r.get('stat'), k)} | {_ci(r.get('ci95'), k)} | "
                        f"{_fmt(r.get('net_mean'), 'event')} | {_p(r.get('p_value'))} | "
                        f"{_p(r.get('p_naive_independent_draws'))} | {_p(r.get('q_value'))} | "
                        f"{vmap.get((v, fam, r['member'], 'continuation'), '–')} / "
                        f"{vmap.get((v, fam, r['member'], 'reversal'), '–')} |"
                    )
                lines.append("")
            dec = vv.get("decomposition") or {}
            if dec.get("h6_events"):
                c6 = dec["conditional_from_h6_entry"]
                c5 = dec["executable_from_breakdown"]
                lines += [f"H6 → breakdown: {dec['confirmed']} of {dec['h6_events']} confirmed; "
                          f"conditional (H6 entry, not executable) {_fmt(c6['excess_mean'])} bps "
                          f"(n = {c6['independent']}); executable (breakdown entry) "
                          f"{_fmt(c5['excess_mean'])} bps (n = {c5['independent']}).", ""]  # fmt: skip
        if a.get("cross_venue"):
            labels = Counter(r["label"] for r in a["cross_venue"])
            lines += [f"Cross-venue labels: {dict(labels)}", ""]
    return "\n".join(lines)
