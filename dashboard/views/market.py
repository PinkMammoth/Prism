"""Page 1 — Market Dashboard: regime, ranked opportunities, headline."""

import pandas as pd
import streamlit as st

from common import banner, cached_scan, db_version, money, pct

st.title("Market Dashboard")
banner()
res = cached_scan(db_version())
if st.button("Re-scan now"):
    cached_scan.clear()
    st.rerun()

c1, c2, c3 = st.columns([1, 1, 2])
for col, key, label in ((c1, "crypto", "Crypto regime"), (c2, "macro", "Macro / equities regime")):
    r = res.regimes[key]
    col.metric(label, r["regime"], help=f"score {r.get('score')}, coverage {r.get('coverage')}")
    votes = r.get("votes") or {}
    col.caption(
        " · ".join(
            f"{k.replace('_', ' ')} {'n/a' if v is None else f'{v:+.0f}'}" for k, v in votes.items()
        )
    )
c3.info(res.headline)
st.caption(
    f"Scan {res.scan_id} at {res.as_of}. Cash is a valid position: statuses below 'ACTIONABLE' are not trades."
)

rows = []
for a in res.assessments:
    comp = {c.name: c for c in a.components}
    rows.append({
        "asset": a.symbol, "class": a.asset_class, "setup": a.setup.title, "setup state": a.setup.state,
        "score": None if a.score is None else round(a.score), "coverage": a.coverage,
        "fundamental": comp["fundamental"].label(), "valuation": comp["valuation"].label(),
        "trend": comp["structure"].label(), "entry": comp["entry"].label(), "macro": comp["macro"].label(),
        "price": a.price, "ideal entry": a.zones.get("ideal_entry"), "to ideal": a.zones.get("distance_to_ideal"),
        "status": a.status, "why": a.status_text, "research verdict": a.setup.research_verdict or "not run",
    })  # fmt: skip
df = pd.DataFrame(rows)
f1, f2, f3 = st.columns(3)
classes = f1.multiselect(
    "Asset class", sorted(df["class"].unique()), default=sorted(df["class"].unique())
)
statuses = f2.multiselect("Status", ["EXCEPTIONAL", "STRONG", "ACTIONABLE", "WAIT", "WATCH", "IGNORE"],
                          default=["EXCEPTIONAL", "STRONG", "ACTIONABLE", "WAIT", "WATCH"])  # fmt: skip
min_score = f3.slider("Minimum score", 0, 100, 0)
view = df[
    df["class"].isin(classes) & df["status"].isin(statuses) & (df["score"].fillna(0) >= min_score)
]

st.subheader("Top opportunities")
event = st.dataframe(
    view, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
    column_config={
        "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%d"),
        "coverage": st.column_config.NumberColumn(format="percent"),
        "price": st.column_config.NumberColumn(format="$%.4g"),
        "ideal entry": st.column_config.NumberColumn(format="$%.4g"),
        "to ideal": st.column_config.NumberColumn(format="percent"),
    },
)  # fmt: skip
sel = event.selection.rows if event and event.selection else []
if sel:
    sym = view.iloc[sel[0]]["asset"]
    st.session_state["asset"] = sym
    st.page_link(
        "views/asset.py",
        label=f"Open {sym} research → exactly why it scored {view.iloc[sel[0]]['score']}",
        icon="🔎",
    )
else:
    st.caption("Select a row to open the asset's research page.")

top = [a for a in res.assessments if a.status in ("EXCEPTIONAL", "STRONG", "ACTIONABLE", "WAIT")][
    :5
]
if top:
    st.subheader("Summary")
    for i, a in enumerate(top, 1):
        z = a.zones.get("entry_zone")
        entry = (
            ""
            if not z
            else (
                f" · preferred entry {money(z[0])}–{money(z[1])}"
                if z[0]
                else f" · preferred entry ≤ {money(z[1])}"
            )
        )
        line = (f"**{i}. {a.symbol}** — score **{a.score:.0f}** · {a.setup.title} · current {money(a.price)}{entry}"
                f" · status **{a.status}** ({a.status_text}) · distance to ideal {pct(a.zones.get('distance_to_ideal'))}")  # fmt: skip
        st.markdown(line.replace("$", "\\$"))  # '$' would otherwise start LaTeX math
