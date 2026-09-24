"""Markdown rendering of research reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from market_signal.research.runner import ResearchReport

PCT_COLS = {
    "mean", "median", "hit_rate", "mean_indep", "median_indep", "hit_rate_indep", "excess_mean_indep",
    "excess_median_indep", "avg_mae", "p10_mae", "avg_mfe", "baseline_mean", "excess_mean", "excess_median",
    "test_excess_mean", "test_excess_median", "test_hit", "default_test_excess_mean", "train_objective", "mde_80",
}  # fmt: skip


def fmt(v, col: str = "") -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "–"
    if isinstance(v, float | np.floating):
        if col in PCT_COLS:
            return (
                f"{v:+.2%}"
                if "excess" in col
                or col in ("mean", "median", "mean_indep", "median_indep", "baseline_mean")
                else f"{v:.1%}"
            )
        return f"{v:.3g}"
    return str(v)


def md_table(df: pd.DataFrame | None, cols: list[str] | None = None, max_rows: int = 60) -> str:
    if df is None or df.empty:
        return "_(none)_\n"
    d = df[cols] if cols else df
    d = d.head(max_rows)
    head = "| " + " | ".join(map(str, d.columns)) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    rows = [
        "| " + " | ".join(fmt(v, c) for c, v in zip(d.columns, r, strict=True)) + " |"
        for r in d.itertuples(index=False)
    ]
    return "\n".join([head, sep, *rows]) + "\n"


SUMMARY_COLS = ["horizon", "n_events", "n_independent", "mean_indep", "median_indep", "hit_rate_indep",
                "baseline_mean", "excess_mean_indep", "excess_t_indep", "p_value_random_entry", "mde_80", "avg_mae", "avg_mfe",
                "verdict_sample"]  # fmt: skip


def render_markdown(rep: ResearchReport) -> str:
    e = rep.experiment
    lines = [
        f"# Research report — {e.name}",
        "",
        f"- **Setup:** `{e.setup}`  ",
        f"- **Verdict:** **{rep.verdict['verdict']}** — {'; '.join(rep.verdict['reasons'])}",
        f"- **Universe:** {', '.join(e.universe)}",
        f"- **Signal period:** {rep.period[0]} → {rep.period[1]}",
        f"- **Run:** `{rep.run_id}` at {rep.created_at}; config hash `{rep.provenance['config_hash']}`; "
        f"git `{(rep.provenance.get('git_commit') or 'n/a')[:10]}`",
        "",
        f"> {e.description.strip()}" if e.description else "",
        "",
        "## Event study (entry next open, exit at close after h bars, net of costs)",
        "",
        "Excess = return minus the same asset's unconditional forward return over eligible bars.",
        "`independent` = non-overlapping events (gap >= horizon). p-value = random-entry null.",
        "`mde_80` = smallest excess this sample could detect (one-sided 5%, 80% power): smaller true edges are invisible here.",
        "",
        md_table(rep.study.summary, [c for c in SUMMARY_COLS if c in rep.study.summary.columns]),
    ]
    for note in rep.study.notes:
        lines.append(f"- {note}")
    prim = e.primary_horizon or "1m"
    lines += ["", f"### By asset ({prim})", ""]
    ba = rep.study.by_asset
    lines.append(md_table(ba[ba["horizon"] == prim] if not ba.empty else ba,
                          ["symbol", "n_independent", "mean_indep", "hit_rate_indep", "excess_mean_indep", "excess_t_indep", "avg_mae"]
                          if not ba.empty else None))  # fmt: skip
    lines += ["", f"### By asset class ({prim})", ""]
    bc = rep.study.by_class
    lines.append(md_table(bc[bc["horizon"] == prim] if not bc.empty else bc,
                          ["asset_class", "n_independent", "mean_indep", "hit_rate_indep", "excess_mean_indep", "excess_t_indep"]
                          if not bc.empty else None))  # fmt: skip
    lines += ["", f"## Condition splits ({prim}, independent events)", ""]
    for k, t in rep.splits.items():
        lines += [f"### {k}", "", md_table(t)]
    m = rep.sim.metrics
    lines += ["", "## Portfolio simulation", "",
              "Next-open fills, stops intrabar (gap → open), stop-first on ambiguity, regime risk multiplier, no leverage.", ""]  # fmt: skip
    keys = ["trades", "hit_rate", "avg_return", "median_return", "payoff_ratio", "expectancy", "expectancy_r",
            "avg_holding_bars", "avg_mae", "worst_mae", "avg_mfe", "cagr", "sharpe", "sortino", "max_drawdown",
            "max_drawdown_peak", "max_drawdown_trough", "time_in_market", "avg_gross_exposure", "turnover_per_year",
            "exit_reasons", "skipped"]  # fmt: skip
    lines += ["| metric | value |", "|---|---|"]
    for k in keys:
        if k in m:
            v = m[k]
            if isinstance(v, float) and k in ("hit_rate", "avg_return", "median_return", "expectancy", "avg_mae",
                                               "worst_mae", "avg_mfe", "cagr", "max_drawdown", "time_in_market", "avg_gross_exposure"):  # fmt: skip
                s = f"{v:+.2%}" if np.isfinite(v) else "–"
            else:
                s = fmt(v)
            lines.append(f"| {k} | {s} |")
    lines += ["", "## Walk-forward (train → select ≤2 params → test on unseen period)", ""]
    if rep.wf_table is not None:
        lines.append(md_table(rep.wf_table))
        lines += ["", "```", *(f"{k}: {fmt(v)}" for k, v in (rep.wf_summary or {}).items()), "```"]
    else:
        lines.append("_(not run)_")
    lines += ["", "## Parameter sensitivity", ""]
    if rep.sens_table is not None:
        lines.append(md_table(rep.sens_table))
        lines += [
            "",
            f"Plateau verdict: **{(rep.sens_verdict or {}).get('verdict')}** — {rep.sens_verdict}",
        ]
    else:
        lines.append("_(not run)_")
    lines += [
        "",
        "## Provenance",
        "",
        "| symbol | source | bars | first | last | content hash |",
        "|---|---|---|---|---|---|",
    ]
    for d in rep.provenance["data"]:
        lines.append(
            f"| {d['symbol']} | {d['source']} | {d['n']} | {d['first'][:10]} | {d['last'][:10]} | {d['hash']} |"
        )
    return "\n".join(lines) + "\n"
