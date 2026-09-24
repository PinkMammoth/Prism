"""Prism dashboard. Run: uv run streamlit run dashboard/app.py  (append `-- --demo` for synthetic data)."""

from __future__ import annotations

import streamlit as st

import common  # noqa: F401  (path/env setup)

st.set_page_config(page_title="Prism", page_icon="📈", layout="wide")

pages = [
    st.Page("views/market.py", title="Market Dashboard", icon="🧭", default=True),
    st.Page("views/asset.py", title="Asset Research", icon="🔎"),
    st.Page("views/backtest.py", title="Backtest Lab", icon="🧪"),
    st.Page("views/portfolio.py", title="Portfolio / Journal", icon="📒"),
    st.Page("views/hype.py", title="HYPE Monitor", icon="🟢"),
    st.Page("views/data.py", title="Data Health & Alerts", icon="🩺"),
]
st.navigation(pages).run()
