"""Data health (freshness, quality issues, failed runs) and alerts."""

import json

import pandas as pd
import streamlit as st

from common import banner, store
from market_signal.models.domain import utcnow
from market_signal.portfolio.alerts import add_rule

st.title("Data Health & Alerts")
banner()
now = pd.Timestamp(utcnow())
with store(read_only=True) as st_:
    inv = st_.series_inventory()
    macro = st_.query(
        "SELECT series_id, max(obs_date) last_obs, max(available_at) last_avail, any_value(pit_method) pit FROM macro_observations GROUP BY 1 ORDER BY 1"
    )
    issues = st_.query(
        "SELECT symbol, timeframe, source, check_name, severity, count(*) n, max(detail) example FROM data_quality_issues GROUP BY ALL ORDER BY severity, symbol"
    )
    runs = st_.query(
        "SELECT provider, dataset, entity, started_at, status, rows_written, error FROM ingestion_runs ORDER BY started_at DESC LIMIT 100"
    )
    changes = st_.query("SELECT * FROM series_changes")
    rules = st_.query("SELECT rule_id, kind, symbol, params FROM alert_rules WHERE active")
    events = st_.query(
        "SELECT fired_at, symbol, message FROM alert_events ORDER BY fired_at DESC LIMIT 100"
    )


def age(ts) -> str:
    h = (now - pd.Timestamp(ts)).total_seconds() / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.1f}d ago"


st.subheader("Freshness")
if inv.empty:
    st.error("No price data stored. Run `uv run market update`.")
else:
    inv["last close"] = inv["last_close"].map(age)
    inv["updated"] = inv["last_ingested"].map(age)
    st.dataframe(
        inv[["symbol", "timeframe", "source", "n_bars", "first_ts", "last close", "updated"]],
        hide_index=True,
        use_container_width=True,
    )
if not macro.empty:
    macro["available since"] = macro["last_avail"].map(age)
    st.dataframe(
        macro[["series_id", "last_obs", "available since", "pit"]],
        hide_index=True,
        use_container_width=True,
    )
if not changes.empty:
    st.warning("Provider changes detected — series kept separate, never stitched.")
    st.dataframe(changes, hide_index=True)
st.subheader("Data-quality issues")
if issues.empty:
    st.success("None recorded.")
else:
    st.dataframe(issues, hide_index=True, use_container_width=True)
st.subheader("Ingestion runs (provenance)")
st.dataframe(runs, hide_index=True, use_container_width=True)

st.subheader("Alerts")
if events.empty:
    st.caption("No alerts fired yet.")
else:
    st.dataframe(events, hide_index=True, use_container_width=True)
st.dataframe(rules, hide_index=True, use_container_width=True)
with st.form("rule"):
    txt = st.text_input("New rule (JSON)", '{"kind": "price_below", "symbol": "HYPE", "price": 56}')
    if st.form_submit_button("Add rule"):
        try:
            with store() as st_:
                st.success(add_rule(st_, json.loads(txt)))
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(str(exc))
