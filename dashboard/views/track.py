"""Track record — live evidence. How did the calls Prism actually made play out?

Scores only scans that were stored at the time (no backfill). Methodology lives in
market_signal/research/track_record.py."""

import pandas as pd
import streamlit as st

from common import banner, db_version, settings, store
from market_signal.research.track_record import (
    mark_independent,
    scan_coverage,
    score_calls,
    summarise,
)
from presenter import pct
from ui import esc, html, inject_css, kv, pill


@st.cache_data(show_spinner="Scoring recorded calls…")
def load(_version: float):
    with store(read_only=True) as s:
        sc = score_calls(s, settings())
        cov = scan_coverage(s)
    if not sc.empty:
        sc = mark_independent(sc)
    return sc, cov


inject_css()
st.title("Track record")
banner()
st.caption(
    "Live evidence: every scan Prism stores is a dated record of what it said. Calls are scored "
    "once their 1- and 3-month windows close. Nothing is backfilled or re-run."
)
sc, cov = load(db_version())
min_n = int(settings().yaml("backtest.yaml")["statistics"]["min_events_for_conclusion"])

if cov["first"] is None:
    st.info(
        "No scans stored yet. Open **Today** or run `uv run market scan` each day to build the record."
    )
    st.stop()
gap = cov["with_scan"] < cov["days"] * 0.7
msg = (f"Scans stored on **{cov['with_scan']} of the last {cov['days']} days** "
       f"(record starts {cov['first']:%d %b %Y}).")  # fmt: skip
if gap:
    st.warning(msg + " Missing days mean missed calls: run `market update && market scan` daily.")
else:
    st.caption(msg)
if sc.empty:
    st.info("No ACTIONABLE or WAIT calls recorded yet.")
    st.stop()

summ = summarise(sc)


def _row(group: str, h: str) -> dict:
    r = summ[(summ["group"] == group) & (summ["horizon"] == h)]
    return r.iloc[0].to_dict() if len(r) else {}


def _n_note(r: dict) -> str:
    n = int(r.get("independent") or 0)
    if n == 0:
        return pill("NO COMPLETED CALLS YET", "unproven")
    if n < min_n:
        return pill(f"TOO EARLY · {n} OF {min_n}", "fair")
    return pill(f"{n} INDEPENDENT CALLS", "good")


def _p(v, signed=True, digits=1):
    return "–" if v is None or pd.isna(v) else pct(v, signed, digits)


blocks = ""
for h in ("1m", "3m"):
    a, w = _row("ACTIONABLE", h), _row("WAIT", h)
    blocks += f"""<div class="section"><h3>ACTIONABLE calls · {h}</h3>{_n_note(a)}
{kv("Mean excess vs random same-class pick", esc(_p(a.get("mean_excess"))))}
{kv("Beat the random pick", esc(_p(a.get("beat_benchmark"), False, 0)))}
{kv("Mean return (net of costs)", esc(_p(a.get("mean_return"))))}
{kv("Random same-class pick", esc(_p(a.get("mean_benchmark"))))}
{kv("Stop / review level hit", esc(_p(a.get("stop_hit_rate"), False, 0)))}
{kv("Calls · still open", esc(f"{a.get('calls', 0)} · {a.get('pending', 0)}"))}</div>"""
    blocks += f"""<div class="section"><h3>WAIT calls · {h}</h3>{_n_note(w)}
{kv("Reached the preferred price", esc(_p(w.get("fill_rate"), False, 0)))}
{kv("Median days to fill", esc("–" if pd.isna(w.get("median_days_to_fill", float("nan"))) else f"{w['median_days_to_fill']:.0f}"))}
{kv("Return after filling (filled calls)", esc(_p(w.get("mean_return_after_fill"))))}
{kv("If bought immediately (all calls)", esc(_p(w.get("mean_chase_return"))))}
{kv("Wait edge (waiting − buying now)", esc(_p(w.get("mean_wait_edge"))))}
{kv("Calls · still open", esc(f"{w.get('calls', 0)} · {w.get('pending', 0)}"))}</div>"""  # fmt: skip
html(f'<div class="grid2">{blocks}</div>')
st.caption(
    f"Statistics use completed, non-overlapping calls only. Below {min_n} per row the numbers are "
    "anecdotes, not evidence. Positive wait edge = the discipline of waiting paid."
)

st.subheader("By research verdict at the time of the call")
by_v = summarise(sc, by="verdict")
if not by_v.empty:
    by_v["verdict"] = by_v["verdict"].fillna("NOT RUN")
    by_v = by_v.sort_values(["verdict", "horizon"])
    a = by_v[by_v["group"] == "ACTIONABLE"][
        ["verdict", "horizon", "calls", "independent", "mean_excess", "beat_benchmark"]
    ]
    a = a.rename(columns={"mean_excess": "excess", "beat_benchmark": "beat pick"})
    w = by_v[by_v["group"] == "WAIT"][
        ["verdict", "horizon", "calls", "independent", "fill_rate", "mean_wait_edge"]
    ]
    w = w.rename(columns={"fill_rate": "filled", "mean_wait_edge": "wait edge"})
    pc = st.column_config.NumberColumn(format="percent")
    c1, c2 = st.columns(2)
    c1.caption("ACTIONABLE")
    c1.dataframe(a, hide_index=True, use_container_width=True,
                 column_config={"excess": pc, "beat pick": pc})  # fmt: skip
    c2.caption("WAIT")
    c2.dataframe(w, hide_index=True, use_container_width=True,
                 column_config={"filled": pc, "wait edge": pc})  # fmt: skip
    st.caption(
        "REJECT-verdict calls are recorded with the engine's status; the Today page shows them as WATCH. "
        "If they keep underperforming here, that cap is earning its keep."
    )

st.subheader("Open calls")
st.caption(
    "Windows not yet closed. A call enters at the next bar's open, so the newest calls are not entered yet."
)
open_ = sc[(sc["state"] == "pending") & (sc["horizon"] == "1m")]
if open_.empty:
    st.caption("None.")
else:
    open_ = open_.assign(
        bar=pd.to_datetime(open_["bar"]).dt.strftime("%Y-%m-%d"),
        to_date=pd.to_numeric(open_["to_date"], errors="coerce").map(
            lambda v: "not entered yet" if pd.isna(v) else pct(v)
        ),
        filled=open_["filled"].map({True: "yes", False: "no"}).fillna(""),
    )
    st.dataframe(
        open_[["bar", "symbol", "group", "status", "setup", "verdict", "price", "zone_hi", "to_date", "filled"]]
        .rename(columns={"bar": "called on", "zone_hi": "preferred ≤", "to_date": "return to date"})
        .sort_values("called on", ascending=False),
        hide_index=True, use_container_width=True,
        column_config={"price": st.column_config.NumberColumn(format="$%.4g"),
                       "preferred ≤": st.column_config.NumberColumn(format="$%.4g")},
    )  # fmt: skip

with st.expander("Every scored call"):
    st.dataframe(sc.sort_values(["bar", "symbol", "horizon"], ascending=[False, True, True]),
                 hide_index=True, use_container_width=True)  # fmt: skip

with st.expander("How calls are scored"):
    st.markdown(
        "- **A call** is the first scanned bar on which an asset became ACTIONABLE (incl. STRONG / "
        "EXCEPTIONAL) or WAIT. Staying in that state on later scans is the same call; re-scans of "
        "the same bar count once.\n"
        "- **Timing** matches the backtests: entry at the next bar's open plus costs, exit at the "
        "close 1 or 3 months of bars later minus costs, total-return prices. Unfinished windows "
        "stay *open* and are excluded.\n"
        "- **Random pick:** the average net return over the same window of every asset of the "
        "same class scanned that day. Excess = call − random pick, so a rising market doesn't "
        "flatter the record.\n"
        "- **WAIT:** filled if the price traded at or below the top of the entry zone within the "
        "window. Wait edge = return after filling (0 if never filled: you held cash) − return "
        "from buying at the next open anyway.\n"
        "- **Independent:** calls on the same asset whose windows overlap are counted once.".replace(
            "$", "\\$"
        )
    )
