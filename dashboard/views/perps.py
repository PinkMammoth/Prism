"""Perps (research): funding and open-interest context from Hyperliquid perpetual futures.

Funding / open-interest monitor (context) plus the research verdicts of the pre-registered
perp strategies. A strategy's signals are candidates only if its verdict passed research."""

import pandas as pd
import streamlit as st

from charts import funding_chart, line_chart
from common import banner, db_version, settings, store
from market_signal.perps.data import load_snapshots, perp_config
from market_signal.perps.monitor import funding_history_frame, perp_overview
from ui import esc, evidence_pill, html, inject_css, pill


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


def compact_px(v):
    if v is None or pd.isna(v):
        return "–"
    return f"${v:,.0f}" if v >= 1000 else f"${v:,.2f}" if v >= 1 else f"${v:,.4g}"


STATE_PILL = {"CROWDED LONG": "WAIT", "CROWDED SHORT": "WATCH", "NEUTRAL": "IGNORE"}

inject_css()
st.title("Perps")
banner()
st.caption(
    "Positioning context from Hyperliquid perpetual futures, and the research verdicts of Prism's "
    "pre-registered perp strategies. **The funding table is context, not a signal.** A strategy's "
    "signals only count as candidates once it passes research (PROMISING or WEAK POSITIVE)."
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

# ---------------------------------------------------------------- strategy research
PASSING = ("PROMISING", "WEAK_POSITIVE")


@st.cache_data(show_spinner="Loading perp research…", ttl=900)
def research_state(_version: float):
    from market_signal.perps.backtest import perp_frame
    from market_signal.perps.strategies import STRATEGIES, latest_signals
    from market_signal.presenter import load_evidence

    min_n = int(settings().yaml("backtest.yaml")["statistics"]["min_events_for_conclusion"])
    coins = [str(c).upper() for c in perp_config(settings()).get("coins") or []]
    from market_signal.perps.paper import evaluate_paper

    with store(read_only=True) as s:
        ev = load_evidence(s, min_n)
        frames = {c: perp_frame(s, settings(), c) for c in coins}
        papers = {n: evaluate_paper(s, settings(), n) for n in STRATEGIES}
    rows, sigs = [], []
    for name, strat in STRATEGIES.items():
        e = ev.get(f"perp_{name}")
        rows.append((strat, e, papers[name]))
        for coin, f in frames.items():
            ls = latest_signals(f, strat)
            if ls["side"]:
                sigs.append({"strategy": strat.title, "coin": coin, "side": ls["side"].upper(), "close": ls["close"],
                             "stop": ls["stop"], "verdict": e.verdict if e else None,
                             "as_of": ls["as_of"]})  # fmt: skip
    return rows, sigs


st.subheader("Strategy research")
st.caption(
    "Each strategy is pre-registered (defaults fixed before any result) and judged by the same automatic "
    "criteria as the spot setups: independent events, excess over random entry *on the same side*, a "
    "random-entry p-value, walk-forward folds and parameter sensitivity. Returns include fees, slippage "
    "and funding. Run `uv run market perp-research` to (re)run. **Paper** is the forward test: each day's "
    "live signals are recorded and scored later (`market perp-paper`). It is never backfilled."
)
rows_r, sigs = research_state(db_version())

body = ""


def paper_cell(r) -> str:
    if r.first_check is None:
        return "<span class='muted'>starts with the next update</span>"
    since = f"since {r.first_check:%d %b}"
    if r.independent == 0:
        return f"<span class='muted'>{esc(since)} · {r.signals} signal(s), none completed</span>"
    ex = "–" if r.excess is None else f"{r.excess:+.1%}"
    tag = (
        pill(f"TOO EARLY · {r.independent} OF {r.min_events}", "fair")
        if r.too_early
        else pill(f"{r.independent} DONE", "good")
    )
    return f"{tag} <span class='small'>excess {esc(ex)}</span><br><span class='small muted'>{esc(since)}</span>"


for strat, e, pr in rows_r:
    if e is None:
        body += (f"<tr><td><b>{esc(strat.title)}</b><br><span class='small muted'>{esc(strat.name)}</span></td>"
                 f"<td>{pill('NOT RUN', 'unproven')}</td><td colspan='5' class='muted'>run "
                 f"<code>market perp-research</code></td><td>{paper_cell(pr)}</td></tr>")  # fmt: skip
        continue
    sim = e.simulation or {}
    wf = "–" if not e.wf_folds else f"{e.wf_positive}/{e.wf_folds}"
    body += (
        f"<tr><td><b>{esc(strat.title)}</b><br><span class='small muted'>{esc(strat.name)} · {esc(e.horizon)}</span></td>"
        f"<td>{evidence_pill(e)}</td><td class='num'>{esc(e.n_independent if e.n_independent is not None else '–')}</td>"
        f"<td class='num'>{esc(pct(e.excess, digits=1))}</td>"
        f"<td class='num'>{'–' if e.p_value is None else f'{e.p_value:.2f}'}</td><td class='num'>{esc(wf)}</td>"
        f"<td class='num'>{esc(pct(sim.get('max_drawdown'), digits=1))} · {esc(sim.get('liquidations', 0))} liq.</td>"
        f"<td class='muted small'>{esc(e.created_at or '')}</td><td>{paper_cell(pr)}</td></tr>"
    )
html(
    "<div style='overflow-x:auto'><table class='compact' style='min-width:900px'><thead><tr><th>Strategy</th>"
    "<th>Verdict</th><th style='text-align:right'>Indep. events</th><th style='text-align:right'>Excess</th>"
    "<th style='text-align:right'>p</th><th style='text-align:right'>Walk-fwd +</th>"
    f"<th style='text-align:right'>Simulation</th><th>Run</th><th>Paper (live, not traded)</th></tr></thead>"
    f"<tbody>{body}</tbody></table></div>"
)
with st.expander("Hypotheses being tested"):
    for strat, _, _ in rows_r:
        st.markdown(f"**{strat.title}** (`{strat.name}`): {strat.hypothesis}")
    st.caption(
        "Full reports: `results/perps/<strategy>/<run>/report.md`. Method: docs/PERPS_BACKTEST.md."
    )

st.markdown("**What each strategy says today**")
if not sigs:
    st.caption("No strategy has a signal on the latest closed bar.")
else:
    lines = ""
    for g in sigs:
        ok = g["verdict"] in PASSING
        status = (pill("CANDIDATE", "ACTIONABLE") + " <span class='small muted'>strategy passed research</span>" if ok
                  else pill("RESEARCH ONLY", "IGNORE") + f" <span class='small muted'>verdict {esc((g['verdict'] or 'not run').replace('_', ' ').lower())}</span>")  # fmt: skip
        lines += (f"<tr><td>{esc(g['strategy'])}</td><td><b>{esc(g['coin'])}</b></td><td>{esc(g['side'])}</td>"
                  f"<td class='num'>{esc(compact_px(g['close']))}</td><td class='num'>{esc(compact_px(g['stop']))}</td>"
                  f"<td>{status}</td></tr>")  # fmt: skip
    html("<div style='overflow-x:auto'><table class='compact' style='min-width:640px'><thead><tr><th>Strategy</th><th>Coin</th>"
         "<th>Side</th><th style='text-align:right'>Close</th><th style='text-align:right'>Stop</th><th>Status</th></tr>"
         f"</thead><tbody>{lines}</tbody></table></div>")  # fmt: skip
    st.caption(
        "A signal from a strategy that hasn't passed research is shown for transparency only. Don't trade it."
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
