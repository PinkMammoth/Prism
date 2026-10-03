"""Prism dashboard. Run: uv run streamlit run dashboard/app.py  (append `-- --demo` for synthetic data).

Information architecture (progressive disclosure):
  Today          — Level 1: the daily answer in ~30 seconds (default page)
  Asset decision — Level 2: one asset's thesis/entry/evidence/caution; Level 3 research tabs below
  Research       — screener table, Backtest Lab, HYPE monitor
  Records/System — live track record, portfolio & journal, data health & alerts
"""

from __future__ import annotations

import streamlit as st

import common  # noqa: F401  (path/env setup)

st.set_page_config(page_title="Prism", page_icon=":material/change_history:", layout="wide")

pages = {
    "Daily": [
        st.Page("views/today.py", title="Today", icon=":material/today:", default=True),
        st.Page("views/asset.py", title="Asset decision", icon=":material/insights:"),
    ],
    "Research": [
        st.Page("views/market.py", title="All assets", icon=":material/table_rows:"),
        st.Page("views/backtest.py", title="Backtest Lab", icon=":material/science:"),
        st.Page("views/hype.py", title="HYPE Monitor", icon=":material/monitoring:"),
        st.Page("views/perps.py", title="Perps", icon=":material/swap_vert:"),
    ],
    "Records": [
        st.Page("views/track.py", title="Track record", icon=":material/fact_check:"),
        st.Page("views/portfolio.py", title="Portfolio / Journal", icon=":material/book:"),
    ],
    "System": [
        st.Page("views/data.py", title="Data health & alerts", icon=":material/health_and_safety:"),
    ],
}
st.navigation(pages).run()
