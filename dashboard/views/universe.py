"""Read-only market radar; selection is separate from executable hypothesis readiness."""

import pandas as pd
import streamlit as st

from common import db_version, store
from market_signal.universe.ledger import status


@st.cache_data(ttl=60, show_spinner=False)
def load(version):
    if version == 0:
        return {"last_evaluation": None}
    with store(read_only=True) as s:
        return status(s)


st.title("Hyperliquid market radar")
st.caption(
    "Paper research · permanent core plus bounded satellites · selection spends attention, not capital"
)
try:
    snapshot = load(db_version())
except Exception as exc:
    st.info(f"Universe ledger is not available in this dashboard snapshot: {exc}")
    st.stop()
if snapshot["last_evaluation"] is None:
    st.info("Dynamic universe ranking has not been activated.")
    st.stop()
st.write(snapshot["stage"])
st.caption(
    f"Policy {snapshot['policy_version']} · evaluation {pd.to_datetime(snapshot['at'], unit='s', utc=True)}"
)
if not snapshot["fresh"]:
    st.warning("The ranking snapshot is stale. It cannot authorize new satellite entries.")
columns = st.columns(4)
for column, tier in zip(columns, range(4), strict=True):
    column.metric(
        ("Discovery", "Candidates", "Active / core", "Risk pinned")[tier],
        snapshot.get("discovered_count", len(snapshot["rows"]))
        if tier == 0
        else snapshot["tier_counts"].get(str(tier), 0),
    )
st.caption(
    "Core: "
    + ", ".join(r["asset"] for r in snapshot["rows"] if r["cohort"] == "CORE")
    + " · Active satellites: "
    + ", ".join(
        r["asset"] for r in snapshot["rows"] if r["cohort"] == "SATELLITE" and r["tier"] == 2
    )
    + " · Warmed candidates: "
    + ", ".join(r["asset"] for r in snapshot["rows"] if r["tier"] == 1 and r["warmed"])
)
st.caption("Pinned assets: " + (", ".join(sorted(snapshot["pins"])) or "none"))
rows = []
for r in snapshot["rows"]:
    m, c = r["market"], r["components"]
    available = [h["name"] for h in r.get("hypotheses", {}).get("hypotheses", []) if h["available"]]
    rows.append(
        {
            "Rank": r["rank"],
            "Asset": r["asset"],
            "Cohort": r["cohort"],
            "Tier": r["tier"],
            "Price": m["price"],
            "Volume $": m["volume_usd"],
            "Volume ratio": m["volume_acceleration"],
            "OI $": m["oi_usd"],
            "OI change": m["oi_change"],
            "RV bps": m["rv_bps"],
            "Range bps": m["range_bps"],
            "Funding": m["funding"],
            "Spread bps": m["spread_bps"],
            "Depth $": m["depth_usd"],
            "Score": r["score"],
            "Movement": c["movement"],
            "Participation": c["participation"],
            "Execution": c["execution"],
            "Rich collection": r["warmed"],
            "Warmup": str(m["warmup"]),
            "Pins": ", ".join(r["state"]["pins"]),
            "Reasons": ", ".join(r["execution_reasons"]),
            "Hypothesis history ready": ", ".join(available),
        }
    )
st.dataframe(
    pd.DataFrame(rows),
    hide_index=True,
    width="stretch",
    column_config={
        "Score": st.column_config.NumberColumn(format="%.3f"),
        "Volume $": st.column_config.NumberColumn(format="$%.0f"),
        "OI $": st.column_config.NumberColumn(format="$%.0f"),
    },
)
st.caption(
    "Depth is the lesser side within 10 bps of the executable top. RV uses causal 10-minute snapshots. Hypothesis history readiness still requires current exact feature/data gates at admission."
)
with st.expander("Promotion, demotion and collection changes"):
    st.dataframe(pd.DataFrame(snapshot["changes"]), hide_index=True)
