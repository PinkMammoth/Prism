"""Page 3 — Backtest Lab: run a setup on a chosen universe/period/regime/params."""

import streamlit as st

from charts import diverging_bars, equity_chart, sensitivity_heatmap
from common import banner, is_demo, settings, store
from market_signal.research.report import fmt
from market_signal.research.runner import (
    GROUPS,
    Experiment,
    ResearchContext,
    load_experiment,
    run_experiment,
    save_report,
)
from market_signal.setups.base import get_setup

st.title("Backtest Lab")
banner()
s = settings()
exp_names = sorted(p.stem for p in (s.paths.config / "experiments").glob("*.yaml"))

with st.form("bt"):
    c1, c2 = st.columns(2)
    base_name = c1.selectbox(
        "Start from experiment",
        exp_names,
        index=exp_names.index("quality_pullback") if "quality_pullback" in exp_names else 0,
    )
    base = load_experiment(s, base_name)
    setup_name = c2.selectbox("Setup", ["quality_pullback", "breakout_retest", "rerating", "conditions"],
                              index=["quality_pullback", "breakout_retest", "rerating", "conditions"].index(base.setup))  # fmt: skip
    groups = st.multiselect("Universe groups", list(GROUPS), default=[])
    assets = st.multiselect(
        "…or assets", [a.symbol for a in s.active_assets()], default=base.universe
    )
    d1, d2, d3 = st.columns(3)
    start = d1.text_input("Start (YYYY-MM-DD, blank = all)", base.start or "")
    end = d2.text_input("End", base.end or "")
    regime = d3.multiselect(
        "Only signals in regime",
        ["RISK_ON", "NEUTRAL", "RISK_OFF"],
        default=base.regime_filter or [],
    )
    setup = get_setup(setup_name)
    defaults = setup.default_params("equity")
    st.markdown(
        "**Parameters** (defaults from `config/setups`; changing them here is exploration, not evidence)"
    )
    overrides = {}
    numeric = {
        k: v for k, v in defaults.items() if isinstance(v, int | float) and not isinstance(v, bool)
    }
    cols = st.columns(4)
    for i, (k, v) in enumerate(numeric.items()):
        val = cols[i % 4].number_input(k, value=float(base.params.get(k, v)), format="%.4g")
        if val != float(v):
            overrides[k] = int(val) if isinstance(v, int) else val
    o1, o2, o3 = st.columns(3)
    primary = o1.selectbox(
        "Primary horizon",
        ["1w", "2w", "1m", "3m", "6m"],
        index=["1w", "2w", "1m", "3m", "6m"].index(base.primary_horizon or "1m"),
    )
    do_wf = o2.checkbox("Walk-forward", value=True)
    do_sens = o3.checkbox("Sensitivity grid (slower)", value=False)
    save = st.checkbox("Save report to results/ and research_runs", value=False)
    go = st.form_submit_button("Run backtest", type="primary")

if go:
    universe = list(
        dict.fromkeys(
            [a.symbol for g in groups for a in s.active_assets() if GROUPS[g](a)] + assets
        )
    )
    exp = Experiment(
        name=f"lab_{setup_name}", setup=setup_name, universe=universe, params={**base.params, **overrides},
        params_by_class=base.params_by_class, start=start or None, end=end or None, primary_horizon=primary,
        trade=base.trade, walk_forward=base.walk_forward if do_wf else {}, sensitivity=base.sensitivity if do_sens else {},
        regime_filter=regime or None, raw={**base.raw, "lab_overrides": overrides, "universe": universe},
    )  # fmt: skip
    with st.spinner("Running event study, simulation, splits…"), store() as st_:
        ctx = ResearchContext(st_, s)
        rep = run_experiment(ctx, exp, walk_forward_on=do_wf, sensitivity_on=do_sens)
        path = save_report(ctx, rep) if save else None
    st.session_state["lab_report"] = rep
    if path:
        st.success(f"Saved {path / 'report.md'}")

rep = st.session_state.get("lab_report")
if rep is None:
    st.info(
        "Configure and run a backtest. Results include baselines, random-entry p-values, walk-forward and sensitivity."
    )
    st.stop()
if is_demo():
    st.warning("Synthetic data: this validates the engine, not a strategy.")
v = rep.verdict
colour = {"PROMISING": "success", "WEAK_POSITIVE": "info", "REJECT": "error"}.get(
    v["verdict"], "warning"
)
getattr(st, colour)(f"**Verdict: {v['verdict']}** — {'; '.join(v['reasons'])}")
if overrides_ := rep.experiment.raw.get("lab_overrides"):
    st.caption(
        f"Non-default parameters {overrides_}: treat as exploration; confirm as a new named experiment."
    )

st.subheader("Event study")
cols = ["horizon", "n_events", "n_independent", "mean_indep", "median_indep", "hit_rate_indep", "baseline_mean",
        "excess_mean_indep", "excess_t_indep", "p_value_random_entry", "mde_80", "avg_mae", "avg_mfe", "verdict_sample"]  # fmt: skip
summ = rep.study.summary[[c for c in cols if c in rep.study.summary]]
st.dataframe(summ, hide_index=True, use_container_width=True,
             column_config={c: st.column_config.NumberColumn(format="percent") for c in cols if c.endswith(("mean_indep", "median_indep", "hit_rate_indep", "baseline_mean", "mde_80", "avg_mae", "avg_mfe"))})  # fmt: skip
prim = rep.experiment.primary_horizon or "1m"
ba = rep.study.by_asset
if not ba.empty:
    pa = ba[(ba["horizon"] == prim) & (ba["n_independent"] > 0)]
    st.plotly_chart(
        diverging_bars(
            pa,
            "symbol",
            "excess_mean_indep",
            f"Excess vs random entry by asset ({prim}, independent events)",
        ),
        use_container_width=True,
    )

st.subheader("Portfolio simulation")
m = rep.sim.metrics
k = st.columns(6)
for i, (label, key, is_pct) in enumerate([("Trades", "trades", False), ("Hit rate", "hit_rate", True), ("Expectancy (R)", "expectancy_r", False),
                                          ("CAGR", "cagr", True), ("Sharpe", "sharpe", False), ("Max drawdown", "max_drawdown", True)]):  # fmt: skip
    val = m.get(key)
    k[i].metric(
        label, "–" if val is None or val != val else (f"{val:.1%}" if is_pct else f"{val:.3g}")
    )
if not rep.sim.equity.empty:
    st.plotly_chart(equity_chart(rep.sim.equity), use_container_width=True)
st.caption(
    f"exits: {m.get('exit_reasons')} · skipped: {m.get('skipped')} · time in market {fmt(m.get('time_in_market'))} · turnover/yr {fmt(m.get('turnover_per_year'))}"
)
if not rep.sim.trades.empty:
    with st.expander(f"Trades ({len(rep.sim.trades)})"):
        st.dataframe(rep.sim.trades, hide_index=True, use_container_width=True)

st.subheader("Regime & condition splits")
tabs = st.tabs(list(rep.splits))
for t, tbl in zip(tabs, rep.splits.values(), strict=True):
    t.dataframe(tbl, hide_index=True, use_container_width=True)

if rep.wf_table is not None:
    st.subheader("Walk-forward (train → choose ≤2 params → test unseen)")
    st.dataframe(rep.wf_table, hide_index=True, use_container_width=True)
    st.json(rep.wf_summary, expanded=False)
if rep.sens_table is not None:
    st.subheader(f"Parameter sensitivity — {(rep.sens_verdict or {}).get('verdict')}")
    axes = list(rep.experiment.sensitivity.get("axes", {}))
    st.plotly_chart(
        sensitivity_heatmap(rep.sens_table, axes[0], axes[1] if len(axes) > 1 else None),
        use_container_width=True,
    )
    st.caption(
        "Good strategies live on plateaus: neighbours of the default should keep the sign of its excess."
    )
