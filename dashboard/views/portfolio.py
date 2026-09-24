"""Page 4 — Portfolio / Journal: manual paper/real positions, decisions, lessons."""

from datetime import date

import streamlit as st

from common import banner, settings, store
from market_signal.portfolio.book import (
    NewPosition,
    add_journal,
    close_position,
    journal_stats,
    latest_scores,
    open_position,
    positions_frame,
)

st.title("Portfolio / Journal")
banner()
s = settings()

with store() as st_:
    pf = positions_frame(st_, s)
    stats = journal_stats(st_)
    scores = latest_scores(st_)
    jr = st_.query(
        "SELECT created_at, position_id, kind, followed_system, text FROM journal_entries ORDER BY created_at DESC LIMIT 200"
    )

tab_pos, tab_new, tab_close, tab_journal, tab_stats = st.tabs(
    ["Positions", "New position", "Close position", "Journal", "What am I good at?"]
)
with tab_pos:
    if pf.empty:
        st.info("No positions yet. Paper-trade the system's signals before risking capital.")
    else:
        book = st.radio("Book", ["all", "paper", "real"], horizontal=True)
        view = pf if book == "all" else pf[pf["book"] == book]
        cols = ["status", "book", "kind", "symbol", "setup", "entry_date", "entry_price", "quantity", "cost_basis", "mark",
                "unrealised_return", "realised_return", "score_at_entry", "current_score", "regime_at_entry",
                "invalidation_price", "to_invalidation", "mae_to_date", "mfe_to_date", "thesis", "horizon", "position_id"]  # fmt: skip
        st.dataframe(view[cols], hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.NumberColumn(format="percent") for c in
                                    ("unrealised_return", "realised_return", "to_invalidation", "mae_to_date", "mfe_to_date")})  # fmt: skip

with tab_new, st.form("open"):
    c1, c2, c3, c4 = st.columns(4)
    sym = c1.selectbox("Asset", [a.symbol for a in s.active_assets()])
    kind = c2.selectbox(
        "Kind",
        ["TRADE", "INVESTMENT"],
        help="TRADE: technical invalidation. INVESTMENT: thesis-based exits.",
    )
    book = c3.selectbox("Book", ["paper", "real"])
    setup = c4.selectbox(
        "Setup", ["quality_pullback", "breakout_retest", "rerating", "discretionary"]
    )
    c5, c6, c7, c8 = st.columns(4)
    price = c5.number_input("Entry price", min_value=0.0, format="%.6g")
    qty = c6.number_input("Quantity", min_value=0.0, format="%.6g")
    stop = c7.number_input("Invalidation price (0 = none)", min_value=0.0, format="%.6g")
    equity = c8.number_input("Portfolio equity (for risk %)", min_value=0.0, value=0.0)
    thesis = st.text_area("Why are we entering?")
    evidence = st.text_area("What evidence supports it?")
    horizon = st.text_input("Intended horizon", "1–3 months")
    followed = st.checkbox(
        "This follows the system (setup active, in zone, sized per the model)", value=True
    )
    entry_date = st.date_input("Entry date", date.today())
    if st.form_submit_button("Record position", type="primary"):
        try:
            with store() as st_:
                pid = open_position(st_, s, NewPosition(sym, kind, entry_date, price, qty, book, None if setup == "discretionary" else setup,
                                                        thesis=thesis, evidence=evidence, horizon=horizon,
                                                        invalidation_price=stop or None, score_at_entry=scores.get(sym),
                                                        followed_system=followed, portfolio_equity=equity or None))  # fmt: skip
            st.success(f"Recorded {pid}")
        except ValueError as exc:
            st.error(str(exc))

with tab_close:
    open_ = pf[pf["status"] == "open"] if not pf.empty else pf
    if open_.empty:
        st.info("No open positions.")
    else:
        with st.form("close"):
            pid = st.selectbox(
                "Position",
                open_["position_id"],
                format_func=lambda p: f"{p} {open_.set_index('position_id').loc[p, 'symbol']}",
            )
            c1, c2, c3 = st.columns(3)
            px = c1.number_input("Exit price", min_value=0.0, format="%.6g")
            reason = c2.selectbox("Exit reason", ["stop", "target", "time", "thesis_invalidated", "fundamentals_deteriorated",
                                                  "valuation_excessive", "better_opportunity", "concentration", "other"])  # fmt: skip
            xd = c3.date_input("Exit date", date.today())
            outcome = st.text_area("Outcome")
            lessons = st.text_area("Lessons")
            if st.form_submit_button("Close position"):
                try:
                    with store() as st_:
                        ret = close_position(st_, s, pid, xd, px, reason, outcome, lessons)
                    st.success(f"Closed: realised {ret:+.2%} (MAE/MFE computed from stored bars)")
                except ValueError as exc:
                    st.error(str(exc))

with tab_journal:
    with st.form("note"):
        pid = st.selectbox(
            "Position (optional)", ["—"] + (list(pf["position_id"]) if not pf.empty else [])
        )
        text = st.text_area("Note / review")
        fs = st.radio("Did I follow the system?", ["n/a", "yes", "no"], horizontal=True)
        if st.form_submit_button("Add note") and text:
            with store() as st_:
                add_journal(
                    st_,
                    None if pid == "—" else pid,
                    "note",
                    text,
                    {"yes": True, "no": False}.get(fs),
                )
            st.success("Noted.")
    st.dataframe(jr, hide_index=True, use_container_width=True)

with tab_stats:
    if not stats.get("closed"):
        st.info(
            "Close some positions to see which setups you are actually good at and where you break your own rules."
        )
    else:
        st.subheader("Which setups am I good at?")
        st.dataframe(stats["by_setup"], hide_index=True, use_container_width=True)
        st.subheader("Where do I violate my own rules?")
        st.dataframe(stats["by_discipline"], hide_index=True, use_container_width=True)
        st.write("Violations by setup:", stats["violations_by_setup"] or "none")
        st.write("Exit reasons:", stats["exit_reasons"])
