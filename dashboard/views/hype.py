"""Page 5 — HYPE Monitor: structural buyback yield, inverse table, assumptions."""

import pandas as pd
import streamlit as st

from charts import line_chart
from common import banner, money, settings, store
from market_signal.fundamentals.hype import hype_valuation_from_store

st.title("HYPE Monitor")
banner()
s = settings()
with store(read_only=True) as st_:
    v = hype_valuation_from_store(st_, s)
    rev = st_.query(
        "SELECT obs_date, value FROM crypto_metrics WHERE symbol='HYPE' AND metric='daily_revenue' AND pit_method='reconstructed' ORDER BY obs_date"
    )
d = v.derived


def g(name):
    return d[name].value if name in d else None


signal_colour = {"STRONGLY_UNDERVALUED": "success", "UNDERVALUED": "success", "FAIR": "info", "OVERVALUED": "warning",
                 "STRONGLY_OVERVALUED": "error"}.get(v.signal, "warning")  # fmt: skip
getattr(st, signal_colour)(f"### Current signal: {v.signal.replace('_', ' ')}\nstructural buyback yield "
                           f"{'n/a' if g('buyback_yield') is None else f'{g('buyback_yield'):.2%}'} "
                           f"(core only {'n/a' if g('core_only_yield') is None else f'{g('core_only_yield'):.2%}'})")  # fmt: skip
for w in v.warnings:
    st.warning(w, icon="⚠️")

m = st.columns(4)
m[0].metric("HYPE price", money(g("price")))
m[1].metric(
    "Circulating supply",
    "–" if g("circulating_supply") is None else f"{g('circulating_supply') / 1e6:,.1f}M",
)
m[2].metric("Market cap", money(g("market_cap")))
m[3].metric("USDC on Hyperliquid", money(g("usdc_on_hyperliquid")))
m = st.columns(4)
m[0].metric("Revenue 30d run-rate", money(g("revenue_30d_annualised")))
m[1].metric("Revenue 90d run-rate", money(g("revenue_90d_annualised")))
m[2].metric(
    "Normalised core revenue", money(g("normalised_revenue")), help=d["normalised_revenue"].source
)
m[3].metric("AQAv2 est. revenue / yr", money(g("aqa_revenue")), help=d["aqa_revenue"].source)
m = st.columns(4)
m[0].metric("Total structural bid / yr", money(g("structural_bid")))
m[1].metric("Buyback yield", "–" if g("buyback_yield") is None else f"{g('buyback_yield'):.2%}")
m[2].metric("Net of contributor selling", "–" if g("net_structural_yield") is None else f"{g('net_structural_yield'):.2%}",
            help="partial: staking emissions unknown")  # fmt: skip
af = next((x for x in v.observed if x.name == "af_hype_balance"), None)
m[3].metric(
    "Assistance Fund HYPE", "–" if af is None or af.value is None else f"{af.value / 1e6:,.1f}M"
)

st.subheader("Fair-value thresholds (price at each yield band edge)")
z = v.zones()
bands = pd.DataFrame(
    [{"zone": k.replace("_", " "), "from": lo, "to": hi} for k, (lo, hi) in z.items()]
)
st.dataframe(bands, hide_index=True, use_container_width=True,
             column_config={"from": st.column_config.NumberColumn(format="$%.2f"), "to": st.column_config.NumberColumn(format="$%.2f")})  # fmt: skip
st.caption(
    "Bands (config/hype.yaml): ≥6% strongly undervalued · 5–6% undervalued · 4–5% fair · 3–4% overvalued · <3% strongly overvalued. Hypotheses, not gospel."
)

st.subheader("What must be true to justify each price? (target yield 4.5%)")
inv = v.inverse_table()
if inv.empty:
    st.info("Needs price, supply and revenue data.")
else:
    st.dataframe(inv.rename(columns={"price": "HYPE price", "required_core_revenue": "required protocol revenue / yr",
                                     "required_usdc": "required USDC (revenue fixed)", "yield_at_price": "structural yield",
                                     "required_bid": "required structural bid"}),
                 hide_index=True, use_container_width=True,
                 column_config={"HYPE price": st.column_config.NumberColumn(format="$%.2f"),
                                "required protocol revenue / yr": st.column_config.NumberColumn(format="compact"),
                                "required USDC (revenue fixed)": st.column_config.NumberColumn(format="compact"),
                                "required structural bid": st.column_config.NumberColumn(format="compact"),
                                "structural yield": st.column_config.NumberColumn(format="percent")})  # fmt: skip

    st.caption(
        "USD, compact notation. A required value of 0 means the current inputs already exceed what that price needs."
    )

with st.expander("Sensitivity: revenue × USDC × reserve yield"):
    sens = v.sensitivity()
    st.dataframe(
        sens,
        hide_index=True,
        use_container_width=True,
        column_config={"buyback_yield": st.column_config.NumberColumn(format="percent")},
    )

if not rev.empty:
    r = pd.Series(rev["value"].to_numpy(), index=pd.to_datetime(rev["obs_date"]))
    st.plotly_chart(line_chart(r.rolling(30).sum() * 365 / 30, "30d annualised",
                               title="Protocol revenue, 30d run-rate — third-party RECONSTRUCTED history (display only, never backtested)"),
                    use_container_width=True)  # fmt: skip

st.subheader("Inputs — observed vs assumed vs derived")
for title, rows in (
    ("Observed", v.observed),
    ("Assumed (config/hype.yaml)", v.assumed),
    ("Derived", list(d.values())),
):
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame([{"name": x.name, "value": x.value, "unit": x.unit, "source": x.source, "as of": x.as_of, "note": x.note}
                               for x in rows]), hide_index=True, use_container_width=True)  # fmt: skip
st.caption(
    "Contributor unlock/claim data: scheduled unlocks are an ASSUMPTION from the published schedule; realised claims are not observable via free APIs in V1. See docs/HYPE_MODEL.md."
)
