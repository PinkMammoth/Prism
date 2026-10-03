"""Today — the daily decision view (Level 1). Answers in ~30 seconds: what is the market
doing, is anything actionable, what are the best candidates, at what price, why, how
strong is the evidence, and what would invalidate each idea. Everything deeper is one
click away (asset decision page → research details)."""

import pandas as pd
import streamlit as st

from common import (
    banner,
    cached_scan,
    db_version,
    decision_views,
    nav_link,
    open_asset,
    settings,
    store,
)
from market_signal.data.freshness import check_freshness
from market_signal.presenter import closest_candidate, market_state, triage
from ui import card, compact_table, decision_pill, esc, evidence_pill, html, inject_css, pill

MAX_CARDS = 5

inject_css()
res = cached_scan(db_version())
views = decision_views(res)
act, wait, rest = triage(views)

head, btn = st.columns([5, 1], vertical_alignment="bottom")
head.title("Today")
if btn.button("Re-scan", icon=":material/refresh:", use_container_width=True):
    cached_scan.clear()
    st.rerun()
as_of = pd.Timestamp(res.as_of).strftime("%a %d %b %Y, %H:%M UTC")
st.caption(f"Scan {as_of} · {len(views)} assets scored · Prism never places trades")
banner()

# ---------------------------------------------------------------- 0. is the data current?
with store(read_only=True) as _s:
    fresh = check_freshness(_s, settings())
stale = fresh.data_stale
if stale:
    items = "".join(f"<p>{esc(p)}</p>" for p in fresh.problems(include_scan=False))
    html(f"""<div class="stale-box"><b>DATA IS OUT OF DATE</b>{items}
<p class="small">The answer below is the last known one and may be wrong. Check the scheduled task
(<code>data/daily.log</code>) or run <code>uv run market update</code>, then Re-scan.</p></div>""")
    st.write("")
elif probs := fresh.problems(include_scan=False):
    st.caption("Data note: " + " ".join(probs))
eyebrow = "Last known answer (data out of date)" if stale else "Today's answer"

# ---------------------------------------------------------------- 1. the answer
if not views:
    html(
        '<div class="hero"><h2>No data yet</h2><p>Run <code>uv run market update</code> to load prices, then re-scan.</p></div>'
    )
    st.stop()
if act:
    names = ", ".join(v.symbol for v in act)
    sub = f"{len(wait)} more waiting for a better price." if wait else "Nothing else is close."
    html(f"""<div class="hero"><div class="eyebrow">{esc(eyebrow)}</div>
<h2>{len(act)} ACTIONABLE {"OPPORTUNITY" if len(act) == 1 else "OPPORTUNITIES"}: {esc(names)}</h2>
<p class="muted">{esc(sub)} Act only within your plan and the suggested size; cash remains a valid position.</p></div>""")
else:
    c = closest_candidate(views)
    closest = ""
    if c is not None:
        score = "–" if c.score is None else f"{c.score:.0f}/100"
        closest = (
            "<p style='margin-top:.7rem'><span class='eyebrow'>Closest candidate</span><br>"
            f"<b>{esc(c.symbol)}</b> {decision_pill(c.decision)} {esc(c.setup)} · current setup {esc(score)}"
            f"<br><span class='small'>{esc(c.short_reason)}</span>"
            f"<br><span class='small muted'>Research evidence</span> {evidence_pill(c.evidence)}</p>"
        )
    html(f"""<div class="hero"><div class="eyebrow">{esc(eyebrow)}</div>
<h2>NO ACTIONABLE OPPORTUNITIES TODAY</h2>
<p class="muted">Holding cash is a valid, deliberate result. Nothing meets Prism's entry rules right now.</p>{closest}</div>""")

# ---------------------------------------------------------------- 2. market state
st.write("")
rows = market_state(res.regimes)
tiles = ""
for r in rows:
    cls = {"RISK_ON": "ACTIONABLE", "NEUTRAL": "WATCH", "RISK_OFF": "bad"}.get(
        r["regime"], "IGNORE"
    )
    hw = r["headwinds"]
    support = (
        ("Headwinds: " + ", ".join(hw))
        if hw
        else ("All available inputs supportive" if r["tailwinds"] else "Inputs unavailable")
    )
    tiles += (f"<div class='regime'><div class='eyebrow'>{esc(r['label'])}</div>"
              f"<div class='v'>{pill(r['regime'].replace('_', ' '), cls)}</div>"
              f"<div class='small muted' style='margin-top:.3rem'>{esc(support)}</div></div>")  # fmt: skip
html(
    f"<div class='eyebrow' style='margin-bottom:.35rem'>Market state</div><div class='regimes'>{tiles}</div>"
)
with st.expander("Regime inputs"):
    for r in rows:
        st.markdown(f"**{r['label']}** (governs {r['governs']}) — {r['regime']}")
        st.caption(
            "Supportive: " + (", ".join(r["tailwinds"]) or "none")
            + " · Against: " + (", ".join(r["headwinds"]) or "none")
            + (" · Unavailable: " + ", ".join(r["missing"]) if r["missing"] else "")
        )  # fmt: skip

# ---------------------------------------------------------------- 3. opportunities
featured = (act + wait)[:MAX_CARDS]
if featured:
    st.subheader("Best opportunities")
    st.caption(
        "**Current setup** = how well today's conditions fit (0–100). **Research evidence** = "
        "how that setup has performed historically vs random entry. They are separate: a high "
        "score does not imply a proven edge."
    )
    for i, v in enumerate(featured, 1):
        with st.container(border=True):
            html(card(v, i, stale))
            if st.button(f"Open {v.symbol} decision page", key=f"open_{v.symbol}",
                         icon=":material/arrow_forward:", type="tertiary"):  # fmt: skip
                open_asset(v.symbol)
    more = (act + wait)[MAX_CARDS:]
    if more:
        st.caption("Also waiting: " + ", ".join(f"{v.symbol} ({v.short_reason})" for v in more))

# ---------------------------------------------------------------- 4. no action
st.write("")
watch_n = sum(v.decision == "WATCH" for v in rest)
with st.expander(f"No action — {len(rest)} assets ({watch_n} on watch)", expanded=not featured):
    html(compact_table(rest))

    def _pick() -> None:
        st.session_state["goto"] = st.session_state.get("no_action_pick")
        st.session_state["no_action_pick"] = None

    st.pills("Open an asset", [v.symbol for v in rest], key="no_action_pick", on_change=_pick)
if goto := st.session_state.pop("goto", None):
    open_asset(goto)

st.write("")
legend = " · ".join(f"{decision_pill(d)} {esc(t)}" for d, t in (
    ("ACTIONABLE", "in zone, setup active"), ("WAIT", "good idea, price not there yet"),
    ("WATCH", "monitor only"), ("IGNORE", "nothing to do")))  # fmt: skip
html(f"<div class='small muted'>{legend}</div>")
nav_link(
    "views/track.py",
    "Track record — how Prism's past calls actually played out",
    icon=":material/fact_check:",
)
nav_link(
    "views/market.py",
    "All assets — full ranked table and filters",
    icon=":material/table_rows:",
)
