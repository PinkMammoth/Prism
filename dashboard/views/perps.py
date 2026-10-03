"""Perps (research): funding and open-interest context from Hyperliquid perpetual futures.

Phase 1 of the perp work: data and a monitor only. No perp strategy has been tested, so
nothing here is a trade signal."""

import pandas as pd
import streamlit as st

from charts import funding_chart, line_chart
from common import banner, db_version, settings, store
from market_signal.perps.data import load_snapshots, perp_config
from market_signal.perps.monitor import funding_history_frame, perp_overview
from ui import esc, html, inject_css, pill


@st.cache_data(show_spinner=False, ttl=900)
def load(_version: float):
    with store(read_only=True) as s:
        return perp_overview(s, settings())


def pct(v, signed=True, digits=0):
    return "–" if v is None or pd.isna(v) else (f"{v:+.{digits}%}" if signed else f"{v:.{digits}%}")


def compact(v):
    if v is None or pd.isna(v):
        return "–"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


STATE_PILL = {"CROWDED LONG": "WAIT", "CROWDED SHORT": "WATCH", "NEUTRAL": "IGNORE"}

inject_css()
st.title("Perps")
banner()
st.caption(
    "Positioning context from Hyperliquid perpetual futures. **No perp strategy has been tested "
    "yet**, so nothing on this page is a trade signal. Whether crowded funding predicts anything "
    "is the question the research phase will answer."
)
rows = load(db_version())
if not rows:
    st.info("No perp coins configured (config/perps.yaml).")
    st.stop()
if all(v.last_funding_time is None for v in rows):
    st.info("No perp data yet. Run `uv run market update` (or `--only perps`).")
    st.stop()
stale = [v.coin for v in rows if v.funding_stale]
if stale:
    st.warning(
        f"Funding data is out of date for {', '.join(stale)}. Run `uv run market update --only perps`."
    )

body = "".join(
    f"<tr><td><b>{esc(v.coin)}</b></td><td class='num'>{esc(pct(v.funding_avg_ann))}</td>"
    f"<td class='num'>{esc(pct(v.funding_30d_ann))}</td><td class='num'>{esc(pct(v.percentile, False))}</td>"
    f"<td>{pill(v.state, STATE_PILL.get(v.state, 'unproven'))}</td><td class='muted'>{esc(v.who_pays)}</td>"
    f"<td class='num'>{esc(compact(v.oi_notional))}</td><td class='num'>{esc(pct(v.oi_change_7d))}</td>"
    f"<td class='num'>{'–' if v.max_leverage is None else f'{v.max_leverage:.0f}x'}</td></tr>"
    for v in rows
)
html(
    "<div style='overflow-x:auto'><table class='compact' style='min-width:720px'><thead><tr><th>Coin</th><th style='text-align:right'>7d funding</th>"
    "<th style='text-align:right'>30d</th><th style='text-align:right'>Percentile (1y)</th><th>State</th>"
    "<th>Who pays</th><th style='text-align:right'>Open interest</th><th style='text-align:right'>OI 7d</th>"
    f"<th style='text-align:right'>Max lev.</th></tr></thead><tbody>{body}</tbody></table></div>"
)
st.caption(
    "Funding is annualised (hourly rate × 8,760); positive means longs pay shorts. Percentile = this "
    "coin's 7-day average funding against its own past year: ≥ 90th is *crowded long*, ≤ 10th "
    "*crowded short*. Max leverage is the venue's limit, not a recommendation."
)

with st.expander("How to read this"):
    st.markdown(
        "- **Funding** keeps a perpetual's price near spot. When many traders are long with leverage, "
        "longs pay shorts (positive funding), and the reverse when shorts dominate.\n"
        "- **Crowded** readings show where leveraged positioning is unusually one-sided *for that coin*. "
        "Crowding can unwind sharply (squeezes), but it can also persist for weeks. That is why it's "
        "context here, not a signal.\n"
        "- **Open interest** is the total value of open perp positions. Rising OI with extreme funding "
        "means more leverage is piling into one side. OI history only exists from Prism's first daily "
        "snapshot onwards (Hyperliquid has no free OI history).\n"
        "- **Cost of holding:** at +20% annualised funding, a long pays about 0.4% per week."
    )

st.subheader("Funding history")
coins = [v.coin for v in rows]
coin = st.pills("Coin", coins, default=coins[0], key="perp_coin") or coins[0]
v = next(x for x in rows if x.coin == coin)
with store(read_only=True) as s:
    hist = funding_history_frame(
        s,
        settings(),
        coin,
        days=int((perp_config(settings()).get("monitor") or {}).get("window_days", 365)),
    )
    snaps = load_snapshots(s, coin)
if hist.empty:
    st.info(f"No funding history stored for {coin}.")
else:
    f = hist["funding_ann"]
    lo, hi = (float(f.quantile(0.1)), float(f.quantile(0.9))) if len(f) >= 30 else (None, None)
    st.plotly_chart(
        funding_chart(f, lo, hi, f"{coin}: 7-day average funding, annualised"),
        use_container_width=True,
    )
    st.caption(f"Latest settled hourly rate, annualised: {pct(v.funding_now_ann, digits=1)} "
               f"(as of {v.last_funding_time:%d %b %H:%M} UTC). {v.history_days:.0f} days of history loaded.")  # fmt: skip
if snaps is not None and len(snaps) >= 2:
    oi = pd.Series(
        snaps["oi_notional"].to_numpy(), index=pd.to_datetime(snaps["snapshot_at"], utc=True)
    )
    scale, unit = (1e9, "bn") if oi.max() >= 1e9 else (1e6, "m")
    st.plotly_chart(line_chart(oi / scale, f"open interest ($ {unit})", "",
                               f"{coin}: open interest ($ {unit}), daily snapshots"), use_container_width=True)  # fmt: skip
elif snaps is not None and len(snaps) == 1:
    st.caption(
        "Open-interest history starts with today's snapshot; a chart appears after a few days."
    )
