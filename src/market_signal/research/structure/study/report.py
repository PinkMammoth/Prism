"""Markdown rendering of a stored Phase 17 result. Presentation only: it reads the payload
and computes nothing new (no statistic, no selection)."""

from __future__ import annotations

from market_signal.research.structure.study.spec import (
    DIRECTIONS,
    HELD,
    LADDER,
    LEVEL_KINDS,
    STRETCH,
)

RUNG_LABEL = {"A_breach": "A breach", "B_failed": "B failed breakout", "C_rejection": "C + rejection",
              "D_shift": "D + structure shift", "E_retest": "E + retest", HELD: "H held breakout",
              STRETCH: "M stretch control"}  # fmt: skip


def bps(x, digits=1) -> str:
    return "–" if x is None else f"{x * 1e4:+.{digits}f}"


def num(x, fmt="{:.2f}") -> str:
    return "–" if x is None else fmt.format(x)


def pval(x) -> str:
    return "–" if x is None else (f"{x:.4f}" if x >= 0.0001 else f"{x:.1e}")


def table(head: list[str], rows: list[list]) -> str:
    out = [
        "| " + " | ".join(head) + " |",
        "|" + "|".join("---" if i == 0 else "---:" for i in range(len(head))) + "|",
    ]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _verdict(res, venue, kind, rung, which):
    return next((v["verdict"] for v in res["verdicts"] if v["venue"] == venue and v["kind"] == kind
                 and v["rung"] == rung and v["direction"] == which), "–")  # fmt: skip


def ladder_table(res: dict, venue: str, kind: str, which: str) -> str:
    v = res["venues"][venue]
    lad = v["ladder"][kind][which]
    delay = {(d["rung"]): d for d in v["entry_delay"] if d.get("kind") == kind and d.get("direction") == which
             and "rung" in d}  # fmt: skip
    rows = []
    for rung in (*LADDER, HELD):
        s = lad[rung]
        p = s.get("paths") or {}
        dl = delay.get(rung, {})
        ci = s.get("excess_ci95") or [None, None]
        rows.append([RUNG_LABEL[rung], s["independent_events"], s["assets_with_events"],
                     bps(s["excess_mean"]), f"[{bps(ci[0], 0)}, {bps(ci[1], 0)}]", bps(s["net_mean"]),
                     num(s["hit_rate"], "{:.1%}"), bps((p.get("mfe") or {}).get("median"), 0),
                     bps((p.get("mae") or {}).get("median"), 0),
                     num(dl.get("hours_after_breach_entry_median"), "{:.0f} h") if rung != "A_breach" else "0 h",
                     num(dl.get("entry_cost_vs_breach_bps_median"), "{:+.0f}") if rung != "A_breach" else "0",
                     pval(s.get("p_value")), pval(s.get("q_value")), _verdict(res, venue, kind, rung, which)])  # fmt: skip
    return table(["stage", "indep. events", "assets", "net excess (bps)", "95% CI", "net (bps)", "hit",
                  "median MFE (bps)", "median MAE (bps)", "entry delay vs A", "price vs A (bps)", "p", "q",
                  "verdict"], rows)  # fmt: skip


def render(payload: dict, meta: dict | None = None) -> str:
    out = [f"# Phase 17 result `{payload['study_id']}`", "",
           f"**{payload['evidence_class']}.** {payload['statement']}", "",
           f"> {payload['availability']['statement']} (assumed latency "
           f"{payload['availability']['assumed_latency_s']:.0f} s)", ""]  # fmt: skip
    for name, res in payload["architectures"].items():
        out += [f"## {name} architecture: {res['timeframes']['structure']} levels → "
                f"{res['timeframes']['event']} events (confirm {res['timeframes']['confirm']}, "
                f"resolve {res['timeframes']['resolve'] or 'none'}); primary horizon "
                f"{res['primary_horizon_bars']} bars", ""]  # fmt: skip
        for venue, v in res["venues"].items():
            if v.get("empty"):
                out += [f"### {venue}: no events", ""]
                continue
            fp = v["family_primary"]
            out += [f"### {venue} (events {v['event_window'][0][:10]} → {v['event_window'][1][:10]}); "
                    f"primary family m = {fp['m']} of {fp['preregistered']} preregistered", ""]  # fmt: skip
            for kind in LEVEL_KINDS:
                for which in DIRECTIONS:
                    out += [
                        f"#### {kind} levels, {which}",
                        "",
                        ladder_table(res, venue, kind, which),
                        "",
                    ]
            if "M_stretch" in (v["ladder"].get("none") or {}).get("reversal", {}):
                rows = []
                for which in DIRECTIONS:
                    s = v["ladder"]["none"][which]["M_stretch"]
                    rows.append([which, s["independent_events"], bps(s["excess_mean"]), bps(s["net_mean"]),
                                 pval(s.get("p_value")), pval(s.get("q_value")),
                                 _verdict(res, venue, "none", "M_stretch", which)])  # fmt: skip
                out += ["#### stretch control (no level narrative)", "",
                        table(["direction", "indep.", "net excess", "net", "p", "q", "verdict"], rows), ""]  # fmt: skip
            out += _sections(v)
        if res.get("cross_venue"):
            rows = [[r["kind"], RUNG_LABEL[r["rung"]], r["direction"], r["label"],
                     *[bps(r.get(f"{x}_excess")) for x in res["venues"]],
                     *[pval(r.get(f"{x}_p")) for x in res["venues"]]] for r in res["cross_venue"]]  # fmt: skip
            out += ["### cross-venue (never pooled)", "",
                    table(["kind", "stage", "direction", "label", *[f"{x} excess" for x in res["venues"]],
                           *[f"{x} p" for x in res["venues"]]], rows), ""]  # fmt: skip
        counts: dict = {}
        for r in res["verdicts"]:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        out += [
            "### verdict counts",
            "",
            table(["verdict", "hypotheses"], [[k, n] for k, n in counts.items()]),
            "",
        ]
    if meta:
        out += ["## run", "", "```", str(meta), "```"]
    return "\n".join(out)


def _cell(key: str, x) -> str:
    if isinstance(x, dict):
        return ", ".join(f"{a}:{b}" for a, b in x.items()) or "–"
    if x is None:
        return "–"
    if isinstance(x, float):
        if "excess" in key:
            return bps(x)  # fractions -> bps
        return f"{x:.3g}"
    return str(x)


def _sections(v: dict) -> list[str]:
    out = []
    tr = [r for r in v.get("transitions", [])]
    if tr:
        rows = [[r["kind"], r["direction"], f"{RUNG_LABEL[r['from']][:1]}→{RUNG_LABEL[r['to']][:1]}",
                 r["independent_from"], r["independent_to"], num(r.get("retention"), "{:.0%}"),
                 bps(r.get("excess_change")),
                 f"[{bps((r.get('excess_change_ci95') or [None])[0], 0)}, {bps((r.get('excess_change_ci95') or [None, None])[1], 0)}]",
                 num(r.get("hit_change"), "{:+.1%}"), num(r.get("entry_delay_hours_median"), "{:.0f} h"),
                 num(r.get("entry_cost_bps_median"), "{:+.0f}")] for r in tr]  # fmt: skip
        out += ["#### incremental value of each layer", "",
                table(["kind", "direction", "step", "indep. before", "indep. after", "retention",
                       "Δ excess (bps)", "95% CI", "Δ hit", "entry delay", "entry cost (bps)"], rows), ""]  # fmt: skip
    cv = v.get("conditional_vs_executable") or []
    if cv:
        rows = [[r["kind"], r["direction"], RUNG_LABEL[r["rung"]], r["independent_events"],
                 bps(r.get("conditional_excess_from_A_breach_entry")),
                 bps(r.get("conditional_excess_from_B_failed_entry")), bps(r.get("executable_excess_mean"))]
                for r in cv]  # fmt: skip
        out += ["#### conditional (non-executable) vs executable excess", "",
                table(["kind", "direction", "stage", "indep.", "from A entry*", "from B entry*",
                       "from own entry"], rows),
                "", "*conditional: selects chains using information not yet available at that entry.", ""]  # fmt: skip
    fc = v.get("family_contrasts")
    if fc:
        rows = [[r["kind"], r["contrast"], r["n_first"], r["n_second"], bps(r.get("excess_first")),
                 bps(r.get("excess_second")), pval(r.get("p_value")), pval(r.get("q_value"))]
                for r in fc["members"]]  # fmt: skip
        out += [f"#### contrasts family (two-sided, m = {fc['m']})", "",
                table(["kind", "contrast", "n first", "n second", "excess first", "excess second", "p", "q"], rows), ""]  # fmt: skip
    for key, title in (
        ("shift_ablation", "structure-shift ablation"),
        ("retest_ablation", "retest ablation"),
    ):
        if v.get(key):
            rows = [[r["kind"], r.get("direction", "reversal"),
                     *[_cell(k, x) for k, x in r.items() if k not in ("kind", "direction")]] for r in v[key]]  # fmt: skip
            head = ["kind", "direction", *[k for k in v[key][0] if k not in ("kind", "direction")]]
            out += [f"#### {title}", "", table(head, rows), ""]
    rc = v.get("rejection_continuous") or []
    if rc:
        rows = [[r["kind"], r["metric"], r.get("n", "–"), num(r.get("spearman"), "{:+.3f}"),
                 " / ".join(bps(x, 0) for x in r.get("quintile_excess", [])) or
                 "; ".join(f"same_bar={k}: {bps(g['excess_mean'])} (n={g['n']})" for k, g in r.get("groups", {}).items())]
                for r in rc]  # fmt: skip
        out += ["#### continuous rejection metrics (B, reversal; descriptive)", "",
                table(["kind", "metric", "n", "Spearman", "quintile excess (bps, low→high)"], rows), ""]  # fmt: skip
    lc = v.get("level_comparison")
    if lc:
        rows = [[r["rung"], f"{r['a']} vs {r['b']}", r["a_events"], r["b_events"], r["shared_entries"],
                 num(r.get("share_of_a_in_b"), "{:.0%}")] for r in lc["overlap"]]  # fmt: skip
        out += [
            "#### level types: overlap",
            "",
            table(["stage", "pair", "a", "b", "shared", "share of a"], rows),
            "",
        ]
        rows = [[r["rung"], r["direction"], f"{r['kind']} − prior_extreme", r["n_kind"], r["n_prior"],
                 bps(r.get("difference")), f"[{bps((r.get('difference_ci95') or [None])[0], 0)}, "
                 f"{bps((r.get('difference_ci95') or [None, None])[1], 0)}]"] for r in lc["differences"]]  # fmt: skip
        out += ["#### level types: excess difference", "",
                table(["stage", "direction", "difference", "n", "n prior", "Δ excess", "95% CI"], rows), ""]  # fmt: skip
    rp = []
    for kind, dirs in v["ladder"].items():
        for rung, s in dirs.get("reversal", {}).items():
            r = (s.get("r_paths") or {}).get("targets") or {}
            for k, t in r.items():
                rp.append([kind, RUNG_LABEL[rung], k, t["events"], t["favourable_first"], t["stop_first"],
                           t["ambiguous_raw"], t["resolved_by_child_bars"], t["unresolved"],
                           f"{num(t['p_target_first_lower'], '{:.1%}')}–{num(t['p_target_first_upper'], '{:.1%}')}",
                           f"{num(t['net_r_mean_lower'], '{:+.2f}')}…{num(t['net_r_mean_upper'], '{:+.2f}')}"])  # fmt: skip
    if rp:
        out += ["#### R paths (reversal, +kR before −1R at the primary horizon, net of costs)", "",
                table(["kind", "stage", "target", "events", "target first", "stop first", "ambiguous (OHLC)",
                       "resolved by 15m", "unresolved", "P(target first) bounds", "net R/trade bounds"], rp), ""]  # fmt: skip
    fs = v.get("family_subgroups")
    if fs:
        rows = [[r["kind"], RUNG_LABEL[r["rung"]], r["cell"], r["independent_events"], bps(r.get("excess_mean")),
                 pval(r.get("p_value")), pval(r.get("q_value"))] for r in fs["members"]]  # fmt: skip
        out += [f"#### regime / volatility subgroups (reversal, two-sided, m = {fs['m']})", "",
                table(["kind", "stage", "cell", "indep.", "excess (bps)", "p", "q"], rows), ""]  # fmt: skip
    se = v.get("sensitivity") or []
    if se:
        rows = [[r["kind"], RUNG_LABEL[r["rung"]], r["direction"], r["verdict"], num(r.get("sign_agreement"), "{:.0%}"),
                 f"{bps(r['excess_range'][0], 0)}…{bps(r['excess_range'][1], 0)}"] for r in se]  # fmt: skip
        out += ["#### parameter neighbourhood (one-at-a-time; descriptive)", "",
                table(["kind", "stage", "direction", "plateau verdict", "sign agreement", "excess range (bps)"], rows), ""]  # fmt: skip
    return out
