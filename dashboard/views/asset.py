"""Asset decision page (Level 2) with research details (Level 3) one click below.

Summary first — thesis, entry, evidence, caution, what Prism suggests — then the chart,
then every existing research view (score breakdown, zones, setups, pooled research,
this asset's event studies, fundamentals) behind tabs."""

import pandas as pd
import streamlit as st

from charts import line_chart, price_chart
from common import (
    banner,
    cached_scan,
    data_version,
    db_version,
    decision_views,
    nav_link,
    settings,
    store,
)
from market_signal.data.prices import PriceBasis, load_bars
from market_signal.models.domain import Timeframe
from market_signal.presenter import DECISION_MEANING, money, pct
from ui import decision_pill, esc, evidence_pill, html, inject_css, kv, meter, price_block

inject_css()
s = settings()
symbols = [a.symbol for a in s.active_assets()]
default = st.session_state.get("asset", "HYPE" if "HYPE" in symbols else symbols[0])
res = cached_scan(data_version())
views = {v.symbol: v for v in decision_views(res)}

top_l, top_r = st.columns([3, 1], vertical_alignment="bottom")
sym = top_r.selectbox("Asset", symbols, index=symbols.index(default) if default in symbols else 0)
st.session_state["asset"] = sym
a = next((x for x in res.assessments if x.symbol == sym), None)
top_l.title(sym if a is None else f"{sym} · {a.name}")
banner()
if a is None:
    st.error(f"No data for {sym}. Run `uv run market update`.")
    st.stop()
v = views[sym]
ev = v.evidence
asset = s.asset(sym)
nav_link("views/today.py", "Back to Today", icon=":material/arrow_back:")

# ---------------------------------------------------------------- decision summary
cap = f"<p class='small'>{esc(v.cap_note)}</p>" if v.cap_note else ""
html(f"""<div class="hero">
<div class="eyebrow">Prism suggests</div>
<h2>{decision_pill(v.decision)} &nbsp;{esc(v.setup)}</h2>
<p>{esc(DECISION_MEANING[v.decision])}. <span class="muted">{esc(v.short_reason)}.</span></p>{cap}
<p class="small muted">Price {esc(money(a.price))} as of {esc(a.as_of[:16])} UTC · {esc(a.asset_class)} · regime {esc(a.regime.replace("_", " "))}</p>
</div>""")
st.write("")

why = "".join(f"<li>{esc(w)}</li>" for w in v.why)
entry = (
    price_block(v, compact=False)
    + f"<p class='small' style='margin-top:.3rem'><b>Invalidation:</b> {esc(v.entry.invalidation_text)}.</p>"
)
if v.sizing_text:
    entry += f"<p class='small muted'>Suggested size: {esc(v.sizing_text)}. No leverage.</p>"
elif a.sizing is None:
    entry += "<p class='small muted'>No size suggested at this status.</p>"

n = ev.n_independent
evid = (
    kv("Current setup", f"{'–' if v.score is None else f'{v.score:.0f}/100'} {esc(v.strength)}") + meter(v.score)
    + kv("Research verdict", evidence_pill(ev))
    + kv("Independent historical events", esc("–" if n is None else (f"{n} (limited)" if ev.sample_limited else n)))
    + kv(f"{ev.horizon} excess vs random entry", esc(pct(ev.excess)))
    + kv(f"{ev.horizon} hit rate", esc(pct(ev.hit_rate, False, 0)))
    + kv("p-value vs random entry", esc("–" if ev.p_value is None else f"{ev.p_value:.2f}"))
    + kv("Walk-forward test folds positive", esc("–" if not ev.wf_folds else f"{ev.wf_positive}/{ev.wf_folds}"))
    + kv("Data coverage (today's score)", esc(pct(v.coverage, False, 0)))
    + f"<p class='small muted' style='margin-top:.35rem'>{esc(ev.meaning)} Pooled across the research universe"
    + (f", {esc(ev.period[0])} → {esc(ev.period[1])}" if ev.period else "") + ".</p>"
)  # fmt: skip
cautions = list(v.risks) + [
    w[:1].upper() + w[1:] for w in a.warnings if "research verdict" not in w
]
caution = "".join(f"<li>{esc(c)}</li>" for c in dict.fromkeys(cautions)) or "<li>None flagged.</li>"

html(f"""<div class="grid2">
<div class="section"><h3>Thesis</h3><p>{esc(v.thesis)}</p><h3 style="margin-top:.8rem">Why it ranks</h3><ul>{why}</ul></div>
<div class="section"><h3>Entry</h3>{entry}</div>
<div class="section"><h3>Evidence</h3>{evid}</div>
<div class="section"><h3>Caution</h3><ul>{caution}</ul></div>
</div>""")  # fmt: skip

# ---------------------------------------------------------------- chart
st.write("")
st.subheader("Chart")
tf_opts = ["Daily", "Weekly"] + (["4h"] if asset.series_for(Timeframe.H4) else [])
c1, c2 = st.columns([1, 2])
tf = c1.radio("Timeframe", tf_opts, horizontal=True)
lookback = c2.slider("Bars shown", 60, 1500, 400, step=20)
with store(read_only=True) as st_:
    from market_signal.features import FeatureStore

    fs = FeatureStore(st_, s)
    if tf == "Daily":
        feat = fs(sym)
        bars = feat
    elif tf == "Weekly":
        bars = load_bars(st_, asset, Timeframe.W1, PriceBasis.SPLIT)
        bars = bars[bars["complete"]] if "complete" in bars else bars
        feat = bars.assign(
            sma_10=bars["close"].rolling(10).mean(), sma_40=bars["close"].rolling(40).mean()
        )
    else:
        feat = fs(sym, Timeframe.H4)
        bars = feat
    fund = None
    if asset.asset_class.value == "equity" and fs(sym) is not None:
        from market_signal.fundamentals.equity import pit_fundamental_features

        fund = pit_fundamental_features(st_, s, asset, fs(sym))
        fund_idx = fs(sym)["close_time"]
if bars is None or bars.empty:
    st.info(f"No {tf} data stored for {sym} (4h is optional).")
else:
    b = bars.tail(lookback)
    mas = (10, 40) if tf == "Weekly" else (20, 50, 200)
    st.plotly_chart(price_chart(b, feat.tail(lookback) if feat is not None else None, a.zones if tf != "4h" else None,
                                f"{sym} {tf.lower()} (split-adjusted)", mas), use_container_width=True)  # fmt: skip

# ---------------------------------------------------------------- research details
st.subheader("Research details")
st.caption("The full working behind the summary above.")
t_score, t_zones, t_setups, t_research, t_hist, t_fund = st.tabs(
    [
        "Score breakdown",
        "Price zones",
        "Setups",
        "Setup research",
        "This asset's history",
        "Fundamentals",
    ]
)

with t_score:
    st.caption(
        f"Score {'–' if a.score is None else f'{a.score:.0f}'}/100 at {a.coverage:.0%} coverage · "
        f"engine status {a.status} ({a.status_text}). Score = 100 × points / max over AVAILABLE "
        "components; N/A components reduce coverage, never count as zero."
    )
    for c in a.components:
        with st.expander(
            f"**{c.name}** — {c.label()}", expanded=c.name in ("fundamental", "valuation", "entry")
        ):
            for r in c.reasons:
                st.markdown(f"- {r}".replace("$", "\\$"))
    for w in a.warnings:
        st.caption(f"engine warning: {w}")

with t_zones:
    z = a.zones

    def rng(x):
        if not x:
            return "–"
        lo, hi = x
        return f"{money(lo) if lo else '…'} – {money(hi) if hi else '…'}"

    zt = pd.DataFrame([
        ("current", money(z["current"])),
        (f"fair value ({z.get('zone_basis') or 'no valuation model'})", rng(z.get("fair"))),
        ("accumulate", rng(z.get("accumulate"))),
        ("strong buy / dislocation", rng(z.get("strong_buy"))),
        ("entry zone", rng(z.get("entry_zone"))),
        ("ideal entry", money(z.get("ideal_entry"))),
        ("distance to ideal entry", pct(z.get("distance_to_ideal"))),
        (f"invalidation ({z['invalidation_basis']})", money(z.get("invalidation"))),
        *[(f"support: {k}", money(x)) for k, x in z["supports"].items()],
    ], columns=["zone", "price"])  # fmt: skip
    st.dataframe(zt, hide_index=True, use_container_width=True)
    if a.sizing:
        sz = a.sizing
        st.markdown((f"Sizing detail ({sz['kind']}, tier {sz['tier']}, regime ×{sz['regime_multiplier']}): "
                     f"**{sz['position_fraction']:.1%} of portfolio**"
                     + (f", risking {sz['portfolio_risk']:.2%} to a stop {sz['stop_distance']:.1%} away" if sz.get("portfolio_risk") else "")
                     + f". {sz['note']}. No leverage.").replace("$", "\\$"))  # fmt: skip
        for e in sz.get("exit_rules", []):
            st.caption(f"exit if: {e}")

with t_setups:
    st.dataframe(pd.DataFrame([{"setup": x.title, "kind": x.kind, "state": x.state, "detail": x.detail,
                                "entry zone": "–" if not x.entry_zone else f"{x.entry_zone[0]:,.4g}–{x.entry_zone[1]:,.4g}",
                                "stop": x.stop, "research verdict": x.research_verdict or "not run on real data",
                                **{k: val for k, val in (x.conditions or {}).items()}} for x in a.setups]),
                 hide_index=True, use_container_width=True)  # fmt: skip

with t_research:
    st.markdown(f"**{a.setup.title}** — pooled research run, primary horizon **{ev.horizon}**")
    if not ev.summary_rows:
        st.info(
            "No saved research run for this setup. Run `uv run market research` (or the Backtest Lab) to produce one."
        )
    else:
        st.markdown(f"Verdict **{ev.label}** — " + "; ".join(ev.reasons))
        note = ev.concentration_note(a.asset_class)
        if note:
            st.caption(note)
        cols = ["horizon", "n_events", "n_independent", "mean_indep", "median_indep", "hit_rate_indep", "baseline_mean",
                "excess_mean_indep", "excess_t_indep", "p_value_random_entry", "mde_80", "avg_mae", "avg_mfe", "verdict_sample"]  # fmt: skip
        summ = pd.DataFrame(ev.summary_rows)
        st.dataframe(summ[[c for c in cols if c in summ]], hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.NumberColumn(format="percent") for c in cols
                                    if c.endswith(("mean_indep", "median_indep", "hit_rate_indep", "baseline_mean", "mde_80", "avg_mae", "avg_mfe"))})  # fmt: skip
        if ev.class_share:
            st.caption(
                "Independent events by asset class: "
                + ", ".join(f"{k} {x:.0%}" for k, x in ev.class_share.items())
            )
        r1, r2 = st.columns(2)
        with r1:
            st.markdown("**Walk-forward**")
            st.json(ev.walk_forward or {}, expanded=False)
            st.markdown(f"**Parameter sensitivity** — {ev.sensitivity or 'not run'}")
            st.json(ev.sensitivity_detail or {}, expanded=False)
        with r2:
            st.markdown("**Portfolio simulation**")
            st.json(ev.simulation or {}, expanded=False)
            st.markdown("**Provenance**")
            st.json(ev.provenance or {}, expanded=False)
        st.caption(f"Run {ev.created_at} · report: {ev.report_path}/report.md")
    nav_link(
        "views/backtest.py",
        "Re-run or explore in the Backtest Lab",
        icon=":material/science:",
    )

with t_hist:
    st.caption("Entry next open; independent (non-overlapping) events; excess vs random entry into this asset. "
               "'Same regime' = events in the same governing regime as today.")  # fmt: skip

    @st.cache_data(show_spinner="Running this asset's event studies…")
    def asset_history(symbol: str, regime: str, _version: float) -> pd.DataFrame:
        from market_signal.backtest.events import run_event_study
        from market_signal.research.runner import (
            Experiment,
            ResearchContext,
            _asset_events,
            _label_events,
            evaluate_universe,
        )

        out = []
        with store() as st_:
            ctx = ResearchContext(st_, settings())
            for setup in ("quality_pullback", "breakout_retest", "rerating"):
                exp = Experiment(name=f"asset_{setup}", setup=setup, universe=[symbol])
                runs = evaluate_universe(ctx, exp)
                if not runs:
                    continue
                study = run_event_study(_asset_events(ctx, runs), "1m", n_boot=0)
                evs = _label_events(ctx, study.events, runs)
                for cond, sub in (
                    ("all", evs),
                    ("same regime", evs[evs["regime"] == regime] if not evs.empty else evs),
                ):
                    one = (
                        sub[(sub["horizon"] == "1m") & sub["independent"]] if not sub.empty else sub
                    )
                    out.append({"setup": setup, "conditions": cond, "occurrences": 0 if sub.empty else int(sub[sub["horizon"] == "1m"].shape[0]),
                                "independent (1m)": len(one), "1m median": one["ret"].median() if len(one) else None,
                                "1m hit rate": (one["ret"] > 0).mean() if len(one) else None,
                                "1m excess": one["excess"].mean() if len(one) else None,
                                "avg MAE (1m)": one["mae"].mean() if len(one) else None})  # fmt: skip
        return pd.DataFrame(out)

    if st.toggle("Run this asset's event studies", key=f"hist_{sym}"):
        hist = asset_history(sym, a.regime, db_version())
        if hist.empty:
            st.info("Not enough history for setup statistics.")
        else:
            for _, r in hist[hist["conditions"] == "all"].iterrows():
                if r["occurrences"]:
                    st.markdown((f"- **{r['setup']}** occurred **{r['occurrences']}** times ({r['independent (1m)']} independent): "
                                f"1m median {pct(r['1m median'])}, hit rate {pct(r['1m hit rate'], False)}, "
                                f"excess vs random entry {pct(r['1m excess'])}, avg MAE {pct(r['avg MAE (1m)'])}.").replace("$", "\\$"))  # fmt: skip
            st.dataframe(hist, hide_index=True, use_container_width=True,
                         column_config={c: st.column_config.NumberColumn(format="percent") for c in ("1m median", "1m hit rate", "1m excess", "avg MAE (1m)")})  # fmt: skip
            st.caption(
                "Small single-asset samples are anecdotes, not evidence — see Setup research for pooled, walk-forward results."
            )

with t_fund:
    st.caption(f"Module: {a.module}")
    if a.factors:
        st.dataframe(
            pd.DataFrame(a.factors)[
                ["name", "raw", "score", "role", "kind", "source", "explanation"]
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No fundamental factors for this asset.")
    for note in a.module_notes:
        st.caption(f"note: {note}")
    if fund is not None and not fund.empty:
        f = fund.set_index(pd.DatetimeIndex(fund_idx)).tail(1500)
        f1, f2 = st.columns(2)
        f1.plotly_chart(
            line_chart(f["pe"].dropna(), "P/E", title="P/E (point-in-time)"),
            use_container_width=True,
        )
        f2.plotly_chart(
            line_chart(
                f["rev_growth"].dropna(), "rev growth", ".0%", "TTM revenue growth (as filed)"
            ),
            use_container_width=True,
        )
    if sym == "HYPE":
        nav_link("views/hype.py", "HYPE valuation monitor", icon=":material/monitoring:")
