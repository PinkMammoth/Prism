"""Standalone read-only paper dashboard: streamlit run dashboard/paper_app.py."""

import streamlit as st

import common  # noqa: F401 — establish the project source path before rendering

st.set_page_config(
    page_title="Prism · Paper traders", page_icon=":material/compare_arrows:", layout="wide"
)
st.navigation([st.Page("views/paper.py", title="Paper traders", default=True)]).run()
