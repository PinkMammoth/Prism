"""Small HTML/CSS building blocks for the decision views (calm, sparse, readable in light
and dark themes). All text passes through ``esc``; rendered with ``st.html`` so '$' is
never interpreted as LaTeX."""

from __future__ import annotations

from html import escape

import streamlit as st

from presenter import DECISION_MEANING, Evidence, View, money, pct

CSS = """
<style>
.pz { --good:#1f8a5b; --good-bg:rgba(31,138,91,.12); --wait:#b7791f; --wait-bg:rgba(183,121,31,.13);
      --watch:#4a6fa5; --watch-bg:rgba(74,111,165,.13); --bad:#c0453f; --bad-bg:rgba(192,69,63,.12);
      --muted:rgba(128,128,128,.95); --line:rgba(128,128,128,.25); --soft:rgba(128,128,128,.07);
      font-size: 0.95rem; line-height: 1.45; }
.pz * { box-sizing: border-box; }
.pz .pill { display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.72rem; font-weight:650;
            letter-spacing:.04em; white-space:nowrap; vertical-align:middle; }
.pz .p-ACTIONABLE, .pz .p-good { color:var(--good); background:var(--good-bg); }
.pz .p-WAIT, .pz .p-fair { color:var(--wait); background:var(--wait-bg); }
.pz .p-WATCH, .pz .p-unproven { color:var(--watch); background:var(--watch-bg); }
.pz .p-IGNORE { color:var(--muted); background:var(--soft); }
.pz .p-bad { color:var(--bad); background:var(--bad-bg); }
.pz .eyebrow { font-size:.7rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
.pz .muted { color:var(--muted); }
.pz .small { font-size:.82rem; }
.pz .hero { border:1px solid var(--line); border-radius:12px; padding:1rem 1.2rem; background:var(--soft); }
.pz .hero h2 { margin:.1rem 0 .3rem; font-size:1.35rem; font-weight:700; letter-spacing:.01em; padding:0; }
.pz .hero p { margin:.15rem 0; }
.pz .regimes { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:.6rem; }
.pz .regime { border:1px solid var(--line); border-radius:10px; padding:.6rem .85rem; }
.pz .regime .v { font-weight:700; font-size:1.05rem; }
.pz .card-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem .75rem; margin-bottom:.35rem; }
.pz .sym { font-size:1.35rem; font-weight:750; letter-spacing:.01em; }
.pz .grid3 { display:grid; grid-template-columns: 1.35fr 1fr 1fr; gap:1rem 1.4rem; margin-top:.4rem; }
.pz .grid2 { display:grid; grid-template-columns: 1fr 1fr; gap:1rem 1.4rem; }
@media (max-width: 760px) { .pz .grid3, .pz .grid2 { grid-template-columns: 1fr; } }
.pz ul { margin:.2rem 0 0 1.1rem; padding:0; }
.pz li { margin:.1rem 0; }
.pz .kv { display:flex; justify-content:space-between; gap:.75rem; padding:.18rem 0; border-bottom:1px dashed var(--line); }
.pz .kv:last-child { border-bottom:none; }
.pz .kv .k { color:var(--muted); }
.pz .kv .v { font-weight:600; text-align:right; }
.pz .dist { font-weight:650; margin:.25rem 0 .1rem; }
.pz .d-IN_ZONE { color:var(--good); } .pz .d-ABOVE { color:var(--wait); } .pz .d-BELOW { color:var(--bad); }
.pz .risk { margin-top:.25rem; }
.pz .risk b { color:var(--bad); font-weight:650; }
.pz .note { border-left:3px solid var(--line); padding:.2rem .7rem; margin:.4rem 0; }
.pz table.compact { width:100%; border-collapse:collapse; font-size:.88rem; }
.pz table.compact td, .pz table.compact th { padding:.3rem .5rem; border-bottom:1px solid var(--line); text-align:left; }
.pz table.compact th { font-size:.7rem; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); font-weight:650; }
.pz table.compact td.num { text-align:right; font-variant-numeric: tabular-nums; }
.pz .section h3 { font-size:.78rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:var(--muted);
                  margin:.2rem 0 .35rem; padding:0; }
.pz .section p { margin:.1rem 0 .3rem; }
.pz .meter { height:6px; border-radius:3px; background:var(--soft); overflow:hidden; margin:.2rem 0 .1rem; }
.pz .meter > span { display:block; height:100%; background:var(--watch); }
</style>
"""


def esc(x) -> str:
    return escape(str(x))


def inject_css() -> None:
    st.html(CSS)


def html(body: str) -> None:
    st.html(f'<div class="pz">{body}</div>')


def pill(text: str, cls: str) -> str:
    return f'<span class="pill p-{esc(cls)}">{esc(text)}</span>'


def decision_pill(d: str) -> str:
    return pill(d, d)


def evidence_pill(ev: Evidence) -> str:
    return pill(ev.label, ev.tone)


def kv(k: str, v: str) -> str:
    return f'<div class="kv"><span class="k">{esc(k)}</span><span class="v">{v}</span></div>'


def meter(score: float | None) -> str:
    w = 0 if score is None else max(0, min(100, score))
    return f'<div class="meter"><span style="width:{w:.0f}%"></span></div>'


def evidence_block(v: View) -> str:
    ev = v.evidence
    score = "–" if v.score is None else f"{v.score:.0f}/100"
    sample = ev.sample_text()
    sample_html = (
        f'<span style="color:var(--wait)">{esc(sample)}</span>'
        if ev.sample_limited
        else esc(sample)
    )
    return (
        kv("Current setup", f"{esc(score)} {esc(v.strength)}")
        + meter(v.score)
        + kv("Research evidence", evidence_pill(ev))
        + kv("Data coverage", esc(pct(v.coverage, False, 0)))
        + kv("Sample", sample_html)
    )


def price_block(v: View, compact: bool = True) -> str:
    e = v.entry
    inv = money(e.invalidation) if e.invalidation else "review thesis"
    out = kv("Current price", esc(money(e.current))) + kv("Preferred entry", esc(e.preferred))
    out += f'<div class="dist d-{esc(e.position)}">{esc(e.distance_text)}</div>'
    if compact:
        away = f" ({pct(e.invalidation_distance)})" if e.invalidation_distance is not None else ""
        out += kv("Invalidation", esc(inv + away))
    return out


def card(v: View, rank: int) -> str:
    why = "".join(f"<li>{esc(w)}</li>" for w in v.why)
    risk = esc(v.risks[0]) if v.risks else "No specific risk flagged"
    sizing = (
        f'<div class="small muted" style="margin-top:.45rem">Suggested size: {esc(v.sizing_text)}. No leverage.</div>'
        if v.sizing_text
        else ""
    )
    cap = f'<div class="note small">{esc(v.cap_note)}</div>' if v.cap_note else ""
    return f"""
<div class="card-head">
  <span class="muted">{rank}.</span><span class="sym">{esc(v.symbol)}</span>
  {decision_pill(v.decision)}
  <span class="muted small">{esc(v.setup)} · {esc(v.name)} · {esc(v.asset_class)}</span>
</div>
<div class="small muted">{esc(DECISION_MEANING[v.decision])} — {esc(v.short_reason.lower() if v.decision != "ACTIONABLE" else "act only on your plan")}.</div>
{cap}
<div class="grid3">
  <div><div class="eyebrow">Why Prism ranks it</div><ul>{why}</ul>
       <div class="risk small"><b>Key risk:</b> {risk}</div></div>
  <div><div class="eyebrow">Price</div>{price_block(v)}</div>
  <div><div class="eyebrow">Strength vs evidence</div>{evidence_block(v)}</div>
</div>
{sizing}
"""


def compact_table(views: list[View]) -> str:
    rows = "".join(
        f"<tr><td><b>{esc(v.symbol)}</b></td><td class='num'>{'–' if v.score is None else f'{v.score:.0f}'}</td>"
        f"<td>{decision_pill(v.decision)}</td><td>{esc(v.short_reason)}</td>"
        f"<td class='muted'>{esc(v.evidence.label.lower())}</td></tr>"
        for v in views
    )
    return (
        "<table class='compact'><thead><tr><th>Asset</th><th style='text-align:right'>Score</th><th>Status</th>"
        f"<th>Reason</th><th>Research</th></tr></thead><tbody>{rows}</tbody></table>"
    )
