"""Deterministic presentation layer: turns scan assessments + saved research runs into
plain-English decision summaries for the dashboard and the daily brief.

Nothing here computes new analytics or changes a research result. It only *reads* the
structured outputs of the scoring engine (``Assessment``) and of saved research runs
(``research_runs`` → ``summary.json`` / ``events.csv``) and phrases them for humans.
Pure functions (no Streamlit) so they are unit-testable.

Two deliberate presentation rules, both conservative:
- Engine statuses EXCEPTIONAL/STRONG/ACTIONABLE all mean "act now" → decision ACTIONABLE;
  the band is shown separately as the strength of the *current setup*.
- A setup whose research verdict is REJECT is never shown as ACTIONABLE or WAIT: the
  decision is capped at WATCH and the cap is stated. Research is never promoted.
"""

from __future__ import annotations

import contextlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DECISIONS = ("ACTIONABLE", "WAIT", "WATCH", "IGNORE")
_ENGINE_TO_DECISION = {
    "EXCEPTIONAL": "ACTIONABLE",
    "STRONG": "ACTIONABLE",
    "ACTIONABLE": "ACTIONABLE",
    "WAIT": "WAIT",
    "WATCH": "WATCH",
    "IGNORE": "IGNORE",
}
DECISION_MEANING = {
    "ACTIONABLE": "Setup active and price is in the preferred entry zone",
    "WAIT": "Good candidate — wait for the preferred price",
    "WATCH": "Worth monitoring; not a trade yet",
    "IGNORE": "Nothing to do",
}
# score band (after regime adjustment) → strength of the CURRENT setup (not evidence)
_STRENGTH = {
    "EXCEPTIONAL": "EXCEPTIONAL",
    "STRONG": "STRONG",
    "ACTIONABLE": "GOOD",
    "WATCH": "MODERATE",
    "IGNORE": "WEAK",
}
EVIDENCE_LABEL = {
    "PROMISING": "PROMISING",
    "WEAK_POSITIVE": "WEAK POSITIVE",
    "INCONCLUSIVE": "INCONCLUSIVE",
    "INSUFFICIENT_DATA": "INSUFFICIENT DATA",
    "REJECT": "REJECTED",
    None: "NOT RUN",
}
EVIDENCE_MEANING = {
    "PROMISING": "Beat random entry out-of-sample and on a parameter plateau. Promising is not proven.",
    "WEAK_POSITIVE": "Positive vs random entry, but not statistically strong or not robust to parameters.",
    "INCONCLUSIVE": "Mixed results — no demonstrated edge.",
    "INSUFFICIENT_DATA": "Too few independent historical events to judge. Treat as unproven.",
    "REJECT": "Did not beat random entry. Signals from this setup are not acted on.",
    None: "No research run on this setup yet (`market research`). Treat as unproven.",
}
SETUP_SHORT = {
    "quality_pullback": "Quality Pullback",
    "breakout_retest": "Breakout + Retest",
    "rerating": "Fundamental Re-rating",
}
_CONDITION_TEXT = {
    "trend_price_above_sma200": "price above its 200-day average",
    "trend_sma50_above_sma200": "50-day average above the 200-day",
    "trend_sma200_rising": "200-day average rising",
    "momentum_6m_positive": "positive 6-month momentum",
    "pullback_depth": "an orderly pullback from the recent high",
    "near_support_sma50": "price near 50-day-average support",
    "rsi_cooled": "momentum cooled (RSI 30–50)",
    "volatility_not_extreme": "volatility not extreme",
    "fundamentals_ok": "fundamental score above threshold",
    "revenue_growing": "revenue growing",
    "earnings_not_deteriorating": "earnings not deteriorating",
    "profitable": "profitable",
    "valuation_compressed": "P/E in the cheapest 30% of its 5-year range",
    "price_dislocated": "price well below its 52-week high",
    "fundamental_yield_attractive": "buyback yield attractive",
}
_VOTE_TEXT = {
    "above_sma200": "BTC above 200-day average",
    "sma50_above_sma200": "BTC 50-day above 200-day",
    "sma200_rising": "BTC 200-day rising",
    "drawdown": "BTC drawdown from high",
    "vol_extreme": "volatility extreme",
    "breadth": "breadth (share of coins in uptrend)",
    "spy_above_sma200": "S&P 500 above 200-day average",
    "spy_sma50_above_sma200": "S&P 500 50-day above 200-day",
    "qqq_above_sma200": "Nasdaq 100 above 200-day average",
    "vix": "VIX (implied volatility)",
    "credit_widening": "credit spreads",
    "curve_inverted": "yield curve inverted",
    "usd_surge": "US dollar surge",
}


def condition_text(key: str) -> str:
    return _CONDITION_TEXT.get(key, key.replace("_", " "))


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def money(x: Any) -> str:
    v = _num(x)
    if v is None:
        return "–"
    if abs(v) >= 1e4:
        return f"${v:,.0f}"
    if abs(v) >= 1:
        return f"${v:,.2f}"
    return f"${v:,.4g}"


def pct(x: Any, signed: bool = True, digits: int = 1) -> str:
    v = _num(x)
    if v is None:
        return "–"
    return f"{v:+.{digits}%}" if signed else f"{v:.{digits}%}"


# --------------------------------------------------------------------------- evidence


@dataclass
class Evidence:
    """Pooled research evidence for one setup, read from its latest saved research run."""

    setup: str
    verdict: str | None = None
    reasons: list[str] = field(default_factory=list)
    horizon: str = "1m"
    n_events: int | None = None
    n_independent: int | None = None
    excess: float | None = None  # mean excess vs random entry, independent events
    hit_rate: float | None = None
    median: float | None = None
    p_value: float | None = None
    mde: float | None = None
    avg_mae: float | None = None
    wf_positive: int | None = None
    wf_folds: int | None = None
    sensitivity: str | None = None
    period: list[str] | None = None
    class_share: dict[str, float] = field(default_factory=dict)  # independent events by class
    report_path: str | None = None
    created_at: str | None = None
    summary_rows: list[dict] = field(default_factory=list)
    walk_forward: dict | None = None
    sensitivity_detail: dict | None = None
    simulation: dict | None = None
    provenance: dict | None = None
    min_events: int = 30

    @property
    def label(self) -> str:
        return EVIDENCE_LABEL.get(self.verdict, self.verdict or "NOT RUN")

    @property
    def meaning(self) -> str:
        return EVIDENCE_MEANING.get(self.verdict, "")

    @property
    def tone(self) -> str:
        return {"PROMISING": "good", "WEAK_POSITIVE": "fair", "REJECT": "bad"}.get(
            self.verdict or "", "unproven"
        )

    @property
    def sample_limited(self) -> bool:
        return self.n_independent is None or self.n_independent < self.min_events

    def sample_text(self) -> str:
        if self.n_independent is None:
            return "no historical sample"
        s = f"{self.n_independent} independent events"
        return s + (" (limited)" if self.sample_limited else "")

    def concentration_note(self, asset_class: str | None = None) -> str | None:
        if not self.class_share:
            return None
        if asset_class and self.class_share.get(asset_class, 0) == 0:
            covered = ", ".join(sorted(self.class_share))
            return f"No historical {asset_class} events: the evidence comes from {covered} only."
        top, share = max(self.class_share.items(), key=lambda kv: kv[1])
        if share >= 0.7 and len(self.class_share) > 1:
            return f"Evidence is concentrated in {top} ({share:.0%} of independent events)."
        return None


def load_evidence(store, min_events: int = 30) -> dict[str, Evidence]:
    """Latest saved research run per setup name (same lookup the scoring engine uses for
    verdicts), enriched with the primary-horizon row of its saved summary."""
    try:
        df = store.query(
            "SELECT name, created_at, config, summary, report_path FROM research_runs "
            "QUALIFY row_number() OVER (PARTITION BY name ORDER BY created_at DESC) = 1"
        )
    except Exception:
        return {}
    out: dict[str, Evidence] = {}
    for _, r in df.iterrows():
        name = r["name"]
        ev = Evidence(setup=name, min_events=min_events, created_at=str(r["created_at"])[:16])
        try:
            v = json.loads(r["summary"])["verdict"]
            ev.verdict, ev.reasons = v.get("verdict"), list(v.get("reasons") or [])
        except (TypeError, KeyError, ValueError):
            pass
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            ev.horizon = json.loads(r["config"]).get("primary_horizon") or "1m"
        ev.report_path = r["report_path"]
        _enrich_from_report(ev)
        out[name] = ev
    return out


def _enrich_from_report(ev: Evidence) -> None:
    if not ev.report_path:
        return
    p = Path(ev.report_path)
    try:
        s = json.loads((p / "summary.json").read_text())
    except (OSError, ValueError):
        return
    ev.summary_rows = s.get("summary") or []
    ev.period = s.get("period")
    ev.walk_forward = s.get("walk_forward")
    ev.sensitivity_detail = s.get("sensitivity")
    ev.simulation = s.get("simulation")
    ev.provenance = s.get("provenance")
    row = next((x for x in ev.summary_rows if x.get("horizon") == ev.horizon), None)
    if row:
        ev.n_events = _int(row.get("n_events"))
        ev.n_independent = _int(row.get("n_independent"))
        ev.excess = _num(row.get("excess_mean_indep"))
        ev.hit_rate = _num(row.get("hit_rate_indep"))
        ev.median = _num(row.get("median_indep"))
        ev.p_value = _num(row.get("p_value_random_entry"))
        ev.mde = _num(row.get("mde_80"))
        ev.avg_mae = _num(row.get("avg_mae"))
    wf = ev.walk_forward or {}
    ev.wf_positive, ev.wf_folds = _int(wf.get("folds_positive")), _int(wf.get("folds_with_events"))
    ev.sensitivity = (ev.sensitivity_detail or {}).get("verdict")
    try:
        import pandas as pd

        e = pd.read_csv(p / "events.csv", usecols=["asset_class", "horizon", "independent"])
        e = e[(e["horizon"] == ev.horizon) & e["independent"].astype(bool)]
        if len(e):
            ev.class_share = (e["asset_class"].value_counts(normalize=True)).to_dict()
    except (OSError, ValueError, KeyError):
        pass


def _int(x: Any) -> int | None:
    v = _num(x)
    return None if v is None else int(v)


# --------------------------------------------------------------------------- per-asset view


@dataclass
class Entry:
    current: float
    zone: tuple[float | None, float] | None
    preferred: str  # "≤ $X" | "$A – $B" | "–"
    position: str  # IN_ZONE | ABOVE | BELOW | NONE
    distance: float | None  # to the nearest zone edge (+ above, − below), 0 inside
    distance_text: str
    invalidation: float | None
    invalidation_text: str
    invalidation_distance: float | None


@dataclass
class View:
    symbol: str
    name: str
    asset_class: str
    decision: str
    engine_status: str
    cap_note: str | None
    setup_key: str
    setup: str
    setup_state: str
    score: float | None
    strength: str
    coverage: float
    evidence: Evidence
    entry: Entry
    thesis: str
    why: list[str]
    risks: list[str]
    sizing_text: str | None
    short_reason: str  # one line for compact lists


def entry_view(a) -> Entry:
    z = a.zones or {}
    price = float(a.price)
    ez = z.get("entry_zone")
    zone = preferred = None
    position, dist = "NONE", None
    if ez and _num(ez[1]):
        lo, hi = (_num(ez[0]) or None), float(ez[1])
        zone = (lo, hi)
        preferred = f"≤ {money(hi)}" if not lo else f"{money(lo)} – {money(hi)}"
        if price > hi:
            position, dist = "ABOVE", price / hi - 1
        elif lo and price < lo:
            position, dist = "BELOW", price / lo - 1
        else:
            position, dist = "IN_ZONE", 0.0
    dist_text = {
        "ABOVE": "At the top of the preferred zone"
        if dist is not None and dist < 0.0005
        else f"{pct(dist, False)} above preferred entry",
        "BELOW": f"{pct(-dist if dist else None, False)} below the entry zone — check support",
        "IN_ZONE": "In the preferred entry zone",
        "NONE": "No entry zone defined",
    }[position]
    inv = _num(z.get("invalidation"))
    basis = str(z.get("invalidation_basis") or "")
    inv_dist = (inv / price - 1) if inv else None
    away = pct(abs(inv_dist), False) if inv_dist is not None and inv_dist < 0 else None
    if inv and basis == "setup stop":
        inv_text = (
            f"Daily close below {money(inv)}"
            + (f" ({away} below price)" if away else "")
            + " breaks the setup"
        )
    elif inv:
        inv_text = (
            f"Review the thesis if price falls below {money(inv)} (200-day average − 1 ATR"
            + (f", {away} below price)" if away else ")")
        )
    elif "already below" in basis:
        inv_text = "Price is already below the thesis-review level (200-day average − 1 ATR): review the thesis before acting"
    else:
        inv_text = "No invalidation level available — do not size"
    return Entry(price, zone, preferred or "–", position, dist, dist_text, inv, inv_text, inv_dist)


def thesis_text(a) -> str:
    s = a.setup
    key, state = s.setup, s.state
    missing = [condition_text(k) for k, v in (s.conditions or {}).items() if not v]
    miss = (" Still missing: " + ", ".join(missing) + ".") if missing else ""
    if key == "breakout_retest":
        lvl = money(s.ideal_entry)
        if state == "ACTIVE":
            return f"Price broke out of a tight consolidation and has retested the breakout level ({lvl}) without failing."
        if state == "WAIT_RETEST":
            return f"Price broke out above {lvl}. Prism waits for an orderly pullback to retest that level rather than chasing."
    if key == "quality_pullback":
        if state == "ACTIVE":
            return "Long-term uptrend intact; price has pulled back in an orderly way to support near the 50-day average and momentum has cooled."
        if state == "NEAR":
            return "A quality pullback in an uptrend is forming but not complete." + miss
    if key == "rerating":
        cheap = "buyback yield" if a.symbol == "HYPE" else "valuation history"
        if state == "ACTIVE":
            return f"Fundamentals hold up while the price has de-rated versus its own {cheap} — a potential re-rating (investment) candidate."
        if state == "NEAR":
            return "A fundamental re-rating candidate is forming." + miss
    if state in ("NONE", "N/A"):
        return f"No setup is active ({s.detail})."
    return f"{s.title}: {s.detail}."


_COMP_GOOD = {
    "fundamental": "Strong fundamentals",
    "valuation": "Cheap versus its own history",
    "structure": "Long-term uptrend intact",
    "entry": "Good entry timing",
    "macro": "Supportive market regime",
    "catalyst": "Positive catalyst on record",
}
_COMP_BAD = {
    "fundamental": "Weak fundamentals",
    "valuation": "Expensive versus its own history",
    "structure": "Long-term trend is weak or down",
    "entry": "Poor entry timing (extended or outside the zone)",
    "macro": "Unsupportive market regime",
    "liquidity": "Thin liquidity",
}


def _ratio(c) -> float | None:
    return None if c.points is None or not c.max_points else c.points / c.max_points


def why_ranked(a, limit: int = 3) -> list[str]:
    out = []
    if a.setup.state == "ACTIVE":
        out.append(f"{SETUP_SHORT.get(a.setup.setup, a.setup.title)} setup is active")
    comps = sorted(
        (c for c in a.components if c.name in _COMP_GOOD and (_ratio(c) or 0) >= 0.7),
        key=lambda c: -(c.points or 0),
    )
    for c in comps:
        detail = ""
        if c.name == "valuation" and c.reasons:
            detail = c.reasons[0].split(" (")[0].replace("_", " ")
        elif c.name == "macro":
            detail = a.regime.replace("_", " ")
        out.append(_COMP_GOOD[c.name] + (f": {detail}" if detail else ""))
    return out[:limit] or ["No strong positive drivers"]


def risks_ranked(a, ev: Evidence, entry: Entry, limit: int = 4) -> list[str]:
    out = []
    if entry.invalidation is None and "already below" in (a.zones.get("invalidation_basis") or ""):
        out.append("Price is already below its thesis-review level")
    for c in sorted(a.components, key=lambda c: _ratio(c) if _ratio(c) is not None else 9):
        r = _ratio(c)
        if r is not None and r <= 0.35 and c.name in _COMP_BAD:
            out.append(_COMP_BAD[c.name])
    entry_c = next((c for c in a.components if c.name == "entry"), None)
    for r in entry_c.reasons if entry_c else []:
        if r.startswith("+0 extended"):
            out.append("Price stretched above its 50-day average")
        elif r.startswith("+0 RSI") and "overbought" in r:
            out.append(f"Momentum overbought ({r.removeprefix('+0 ').removesuffix(' overbought')})")
        elif r.startswith("−") and "4h" in r:
            out.append("Short-term extended on the 4-hour chart")
    if a.coverage < 0.6:
        out.append(f"Limited data: only {a.coverage:.0%} of the score inputs are available")
    if ev.verdict != "PROMISING":
        out.append(f"Historical edge not demonstrated (research: {ev.label.lower()})")
    note = ev.concentration_note(a.asset_class)
    if note:
        out.append(note)
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq[:limit]


def decision_for(a, ev: Evidence) -> tuple[str, str | None]:
    d = _ENGINE_TO_DECISION.get(a.status, "WATCH")
    if ev.verdict == "REJECT" and d in ("ACTIONABLE", "WAIT"):
        return "WATCH", (
            f"Engine status {a.status}, shown as WATCH: research rejected the "
            f"{SETUP_SHORT.get(a.setup.setup, a.setup.title)} setup."
        )
    return d, None


def short_reason(a, decision: str, entry: Entry, ev: Evidence, cap: str | None) -> str:
    if cap:
        return "Setup rejected by research"
    if decision == "ACTIONABLE":
        return "In preferred entry zone"
    if decision == "WAIT":
        if a.setup.state == "WAIT_RETEST":
            return "Waiting for the breakout retest"
        if entry.position == "IN_ZONE":
            missing = [condition_text(k) for k, v in (a.setup.conditions or {}).items() if not v]
            return "In zone, setup not confirmed" + (f" — needs {missing[0]}" if missing else "")
        return entry.distance_text if entry.position != "NONE" else "Waiting for entry"
    t = a.status_text or ""
    if "data coverage" in t:
        return f"Limited data ({a.coverage:.0%} coverage)"
    if "no active setup" in t or a.setup.state in ("NONE", "N/A"):
        return "No active setup"
    if a.setup.state == "NEAR":
        missing = [condition_text(k) for k, v in (a.setup.conditions or {}).items() if not v]
        return "Setup forming — needs " + (missing[0] if missing else "confirmation")
    if a.setup.state == "WAIT_RETEST":
        return "Breakout not yet retested"
    if decision == "IGNORE":
        return "Score below watch threshold"
    return "Score below action threshold"


def sizing_text(a) -> str | None:
    sz = a.sizing
    if not sz:
        return None
    frac = _num(sz.get("position_fraction")) or 0
    if frac <= 0:
        return f"Do not size: {sz.get('note')}"
    tier = str(sz.get("tier", "")).lower()
    if sz.get("kind") == "INVESTMENT":
        return f"Up to {frac:.1%} of portfolio ({tier} tier, investment; thesis-based exits)"
    risk = _num(sz.get("portfolio_risk"))
    return f"Up to {frac:.1%} of portfolio, risking {pct(risk, False, 2)} to the stop ({tier} tier)"


def build_view(a, evidence: dict[str, Evidence]) -> View:
    ev = evidence.get(a.setup.setup) or Evidence(a.setup.setup, verdict=a.setup.research_verdict)
    # the engine's verdict string is authoritative for this scan
    if a.setup.research_verdict != ev.verdict:
        ev = Evidence(a.setup.setup, verdict=a.setup.research_verdict)
    decision, cap = decision_for(a, ev)
    entry = entry_view(a)
    return View(
        symbol=a.symbol,
        name=a.name,
        asset_class=a.asset_class,
        decision=decision,
        engine_status=a.status,
        cap_note=cap,
        setup_key=a.setup.setup,
        setup=SETUP_SHORT.get(a.setup.setup, a.setup.title)
        if a.setup.state not in ("NONE", "N/A")
        else "No active setup",
        setup_state=a.setup.state,
        score=a.score,
        strength=_STRENGTH.get(a.band, a.band),
        coverage=a.coverage,
        evidence=ev,
        entry=entry,
        thesis=thesis_text(a),
        why=why_ranked(a),
        risks=risks_ranked(a, ev, entry),
        sizing_text=sizing_text(a) if decision in ("ACTIONABLE", "WAIT") else None,
        short_reason=short_reason(a, decision, entry, ev, cap),
    )


def triage(views: list[View]) -> tuple[list[View], list[View], list[View]]:
    """(actionable, waiting, no-action) — each kept in the engine's ranking order."""
    act = [v for v in views if v.decision == "ACTIONABLE"]
    wait = [v for v in views if v.decision == "WAIT"]
    rest = [v for v in views if v.decision in ("WATCH", "IGNORE")]
    return act, wait, rest


def closest_candidate(views: list[View]) -> View | None:
    """When nothing is actionable: the WAIT nearest its preferred entry, else the best
    WATCH by score."""
    wait = [v for v in views if v.decision == "WAIT" and v.entry.distance is not None]
    if wait:
        return min(wait, key=lambda v: abs(v.entry.distance or 0))
    watch = [v for v in views if v.decision == "WATCH" and v.score is not None]
    return max(watch, key=lambda v: v.score or 0) if watch else None


# --------------------------------------------------------------------------- market state


def market_state(regimes: dict) -> list[dict]:
    rows = []
    for key, label, governs in (
        ("crypto", "Crypto", "crypto"),
        ("macro", "Equities & macro", "equities, ETFs, commodities"),
    ):
        r = regimes.get(key) or {}
        votes = r.get("votes") or {}
        up = [_VOTE_TEXT.get(k, k) for k, v in votes.items() if v is not None and v > 0]
        down = [_VOTE_TEXT.get(k, k) for k, v in votes.items() if v is not None and v < 0]
        missing = [_VOTE_TEXT.get(k, k) for k, v in votes.items() if v is None]
        rows.append({
            "key": key, "label": label, "governs": governs, "regime": r.get("regime", "UNKNOWN"),
            "tailwinds": up, "headwinds": down, "missing": missing, "coverage": r.get("coverage"),
        })  # fmt: skip
    return rows
