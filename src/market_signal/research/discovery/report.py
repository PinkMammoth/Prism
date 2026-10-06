"""Render a stored Phase 22 discovery result as markdown (presentation only)."""

from __future__ import annotations


def _p(x, digits: int = 2) -> str:
    return "–" if x is None else f"{100 * x:.{digits}f}%"


def _n(x, digits: int = 2) -> str:
    return "–" if x is None else f"{x:.{digits}f}"


def render(payload: dict) -> str:
    out = [f"# Phase 22 intraday discovery — {payload.get('study_id')}", "",
           f"> {payload['statement']}", "",
           f"Windows: {payload['windows']}", "",
           "## Verdicts", "", "| verdict | variants |", "|---|---|"]  # fmt: skip
    out += [f"| {k} | {v} |" for k, v in payload["verdict_counts"].items()]
    out += ["", "## Families (contemporary, primary horizon)", "",
            "| family | m | testable | BH discoveries | gross | cost drag | net | best (t) | verdicts |",
            "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for f, r in payload["families"].items():
        out.append(f"| {f} | {r.get('m')} | {r.get('testable')} | {len(r.get('discoveries', []))} | "
                   f"{_p(r.get('mean_gross'))} | {_p(r.get('mean_cost_drag'))} | {_p(r.get('mean_net'))} | "
                   f"{r['best']['key']} ({_n(r['best']['t'])}) | {r['verdicts']} |")  # fmt: skip
    out += ["", "## Long vs short", ""]
    for side, r in payload["sides"].items():
        out.append(f"- **{side}**: {r['variants']} variants, verdicts {r['verdicts']}, mean "
                   f"contemporary net {_p(r['mean_contemporary_net'])}, positive share "
                   f"{_p(r['positive_net_share'], 0)}")  # fmt: skip
    out += ["", "## Strategies (non-rejected)", "",
            "| key | verdict | route | n | net | t | q | r180 net | r90 net | indep/day | state | pattern |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for s in payload["strategies"]:
        if s["verdict"] in ("REJECTED",):
            continue
        c = s["contemporary"]
        out.append(f"| {s['key']} | {s['verdict']} | {s.get('route') or ''} | {c.get('n')} | "
                   f"{_p(c.get('net_mean'), 3)} | {_n(c.get('t'))} | {_n(s.get('q_value'))} | "
                   f"{_p(s['recent_180'].get('net_mean'), 3)} | {_p(s['recent_90'].get('net_mean'), 3)} | "
                   f"{_n(s['frequency']['independent_per_day'])} | {s['edge_state'].get('state')} | "
                   f"{s['pattern']['label']} |")  # fmt: skip
    out += [
        "",
        "## Volatility forecast (non-directional)",
        "",
        "| test | n | abs move | excess vs all bars | t | q | answer |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, r in payload["volatility_forecast"].items():
        out.append(f"| {k} | {r.get('n')} | {_p(r.get('abs_move_mean'))} | {_p(r.get('abs_excess_mean'), 3)} | "
                   f"{_n(r.get('t'))} | {_n(r.get('q_value'))} | {r.get('answer')} |")  # fmt: skip
    out += ["", "## Simpler-baseline comparisons", "",
            "| strategy | baseline | freq ratio | complex net | baseline net | in-filter − out | t | prefer |",
            "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for c in payload["contrasts"].values():
        if c["kind"] != "complexity":
            continue
        out.append(f"| {c['strategy']} | {c['baseline']} | {_n(c.get('frequency_ratio'))} | "
                   f"{_p(c.get('complex_net_mean'), 3)} | {_p(c.get('baseline_net_mean'), 3)} | "
                   f"{_p(c.get('in_filter_minus_out'), 3)} | {_n(c.get('t'))} | {c.get('prefer')} |")  # fmt: skip
    out += ["", "## Clusters and shortlist", "", f"Shortlist: {payload['shortlist']}", ""]
    for c in payload["clusters"]:
        out.append(
            f"- {c['cluster_id']}: representative **{c['representative']}**, members {c['members']}"
        )
    out += ["", "## Ensemble opportunity", ""]
    for k, v in payload["ensemble"].items():
        out.append(f"- **{k}**: {v}")
    out += ["", "## Phase 21 replay (diagnostics)", ""]
    for k, v in payload["incubation_replay"].items():
        out.append(f"- {k}: {v}")
    return "\n".join(out) + "\n"
