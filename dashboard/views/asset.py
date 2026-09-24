"""Page 2 — Asset Research: chart + zones, score breakdown (why), fundamentals, setup history."""

import pandas as pd
import streamlit as st

from charts import line_chart, price_chart
from common import banner, cached_scan, db_version, money, pct, settings, store
from market_signal.data.prices import PriceBasis, load_bars
from market_signal.models.domain import Timeframe

st.title("Asset Research")
banner()
s = settings()
symbols = [a.symbol for a in s.active_assets()]
default = st.session_state.get("asset", "HYPE" if "HYPE" in symbols else symbols[0])
sym = st.selectbox("Asset", symbols, index=symbols.index(default) if default in symbols else 0)
st.session_state["asset"] = sym
res = cached_scan(db_version())
a = next((x for x in res.assessments if x.symbol == sym), None)
if a is None:
    st.error(f"No data for {sym}. Run `uv run market update`.")
    st.stop()
asset = s.asset(sym)

h = st.columns(5)
h[0].metric("Price", money(a.price), help=f"as of {a.as_of[:16]} UTC")
h[1].metric(
    "Score", "–" if a.score is None else f"{a.score:.0f}/100", help=f"coverage {a.coverage:.0%}"
)
h[2].metric("Status", a.status, help=a.status_text)
h[3].metric("Best setup", a.setup.title, a.setup.state, delta_color="off")
h[4].metric("Regime", a.regime)
for w in a.warnings:
    st.warning(w, icon="⚠️")

tf_opts = ["Daily", "Weekly"] + (["4h"] if asset.series_for(Timeframe.H4) else [])
tf = st.radio("Timeframe", tf_opts, horizontal=True)
lookback = st.slider("Bars shown", 60, 1500, 400, step=20)
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

left, right = st.columns([3, 2])
with left:
    st.subheader("Score breakdown — exactly why")
    for c in a.components:
        with st.expander(
            f"**{c.name}** — {c.label()}", expanded=c.name in ("fundamental", "valuation", "entry")
        ):
            for r in c.reasons:
                st.markdown(f"- {r}")
    st.caption(
        "Score = 100 × points / max over AVAILABLE components; N/A components reduce coverage, never count as zero."
    )
with right:
    st.subheader("Price zones")
    z = a.zones

    def rng(v):
        if not v:
            return "–"
        lo, hi = v
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
        *[(f"support: {k}", money(v)) for k, v in z["supports"].items()],
    ], columns=["zone", "price"])  # fmt: skip
    st.dataframe(zt, hide_index=True, use_container_width=True)
    if a.sizing:
        sz = a.sizing
        st.success(f"Suggested ({sz['kind']}, tier {sz['tier']}, regime ×{sz['regime_multiplier']}): "
                   f"**{sz['position_fraction']:.1%} of portfolio**"
                   + (f", risking {sz['portfolio_risk']:.2%} to a stop {sz['stop_distance']:.1%} away" if sz.get("portfolio_risk") else "")
                   + f". {sz['note']}. No leverage.")  # fmt: skip
        for e in sz.get("exit_rules", []):
            st.caption(f"exit if: {e}")

st.subheader("Setups")
st.dataframe(pd.DataFrame([{"setup": x.title, "kind": x.kind, "state": x.state, "detail": x.detail,
                            "entry zone": "–" if not x.entry_zone else f"{x.entry_zone[0]:,.4g}–{x.entry_zone[1]:,.4g}",
                            "stop": x.stop, "research verdict": x.research_verdict or "not run on real data",
                            **{k: v for k, v in (x.conditions or {}).items()}} for x in a.setups]),
             hide_index=True, use_container_width=True)  # fmt: skip

st.subheader(f"Fundamentals ({a.module})")
if a.factors:
    st.dataframe(
        pd.DataFrame(a.factors)[["name", "raw", "score", "role", "kind", "source", "explanation"]],
        hide_index=True,
        use_container_width=True,
    )
for n in a.module_notes:
    st.caption(f"note: {n}")
if fund is not None and not fund.empty:
    f = fund.set_index(pd.DatetimeIndex(fund_idx)).tail(1500)
    c1, c2 = st.columns(2)
    c1.plotly_chart(
        line_chart(f["pe"].dropna(), "P/E", title="P/E (point-in-time)"), use_container_width=True
    )
    c2.plotly_chart(
        line_chart(f["rev_growth"].dropna(), "rev growth", ".0%", "TTM revenue growth (as filed)"),
        use_container_width=True,
    )

st.subheader("Historical setup statistics (this asset, default parameters)")
st.caption("Entry next open; independent (non-overlapping) events; excess vs random entry into this asset. "
           "'Comparable conditions' = events in the same governing regime as today.")  # fmt: skip


@st.cache_data(show_spinner="Running this asset's event studies…")
def asset_history(symbol: str, _version: float) -> pd.DataFrame:
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
            ev = _label_events(ctx, study.events, runs)
            for cond, sub in (
                ("all", ev),
                ("same regime", ev[ev["regime"] == a.regime] if not ev.empty else ev),
            ):
                one = sub[(sub["horizon"] == "1m") & sub["independent"]] if not sub.empty else sub
                out.append({"setup": setup, "conditions": cond, "occurrences": 0 if sub.empty else int(sub[sub["horizon"] == "1m"].shape[0]),
                            "independent (1m)": len(one), "1m median": one["ret"].median() if len(one) else None,
                            "1m hit rate": (one["ret"] > 0).mean() if len(one) else None,
                            "1m excess": one["excess"].mean() if len(one) else None,
                            "avg MAE (1m)": one["mae"].mean() if len(one) else None})  # fmt: skip
    return pd.DataFrame(out)


hist = asset_history(sym, db_version())
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
        "Small single-asset samples are anecdotes, not evidence — see Backtest Lab for pooled, walk-forward results."
    )
