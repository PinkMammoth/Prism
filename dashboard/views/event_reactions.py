"""Read-only prospective event cohort results."""

import pandas as pd
import streamlit as st

import common
from market_signal.paper.nimble import engine, event_report, event_spec

st.title("Event reactions · paper")
st.caption(
    "Short-term market reaction evidence. Immediate and confirmed accounts remain separate; no strategy graduates automatically."
)
try:
    with common.store(read_only=True) as store:
        runs = engine.runs(store)
        ids = [r["run_id"] for r in runs if r["policy_id"] == event_spec.policy_id()]
        if not ids:
            st.info("Event cohorts have not been activated.")
        else:
            rid = st.selectbox("Cohort", ids)
            result = event_report.summary(store, rid)
            st.write("Variant:", result["variant"])
            st.dataframe(pd.DataFrame(result["hypotheses"]), hide_index=True)
            st.write("Classification rejections", result["classification_rejections"])
            st.dataframe(pd.DataFrame(result["source_comparison"]), hide_index=True)
            trades = [p for p in event_report.report.trades(store, rid) if p.get("event_entry")]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "asset": p["asset"],
                            "side": p["side"],
                            "state": p["state"],
                            "event_class": p["event_entry"]["family"],
                            "variant": p["event_entry"]["variant"],
                            "source": p["event_entry"]["report"]["source_id"],
                            "event_age_seconds": p.get("event_age_at_entry_seconds"),
                            "entry_latency_seconds": p.get("entry_latency_seconds"),
                            "move_missed": p.get("PRE_ENTRY_REACTION_CAPTURED", {}).get(
                                "signed_mid_move_before_fill"
                            ),
                            "exit": p.get("exit_reason"),
                            "funding": p.get("funding"),
                            "net_pnl": p.get("net_pnl"),
                        }
                        for p in trades
                    ]
                ),
                hide_index=True,
            )
            with st.expander("Baseline comparisons"):
                st.write(
                    "Preserved runs",
                    [r["run_id"] for r in runs if r["policy_id"] != event_spec.policy_id()],
                )
except common.DatabaseBusy:
    st.info("Prism is updating. Refresh shortly.")
