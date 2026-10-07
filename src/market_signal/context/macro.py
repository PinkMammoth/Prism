"""Scheduled-catalyst state and objective release surprise (context only; never a direction).

``macro_state(states, t)`` answers "is a major scheduled catalyst coming, or just past?" from
the events Prism knew at ``t`` (pass ``ledger.states_asof(store, t, ...)``). It exposes the
conditioning flags future research needs (event in the next 5m/15m/30m/1h, post-event
window). It is **not a veto**: nothing here stops or gates trading.

``surprise(state, history)`` reports the release fact only: actual − consensus, its sign, a
standardised size when enough earlier surprises of the same series exist, and the revision to
the previous print. A consensus Prism first saw *after* the release time is never used (it is
reported as ``consensus_after_release``). No rule maps a surprise to a market direction.
"""

from __future__ import annotations

import statistics
from datetime import datetime

import pandas as pd

from market_signal.context.ledger import relevance_window
from market_signal.context.taxonomy import MACRO_SENSITIVITY, RELEVANCE, TIER1_MACRO, Category

MACRO_STATE_VERSION = "macro_state_v1"
SURPRISE_VERSION = "macro_surprise_v1"
BLACKOUT_MINUTES = (5, 15, 30, 60)
STANDARDISE_MIN_OBS = 8
CRYPTO_SCHEDULED = ("token_unlock", "listing", "delisting", "protocol_upgrade",
                    "governance_result", "exchange_maintenance")  # fmt: skip


def _ts(x) -> pd.Timestamp | None:
    return None if x is None else pd.Timestamp(x).tz_convert("UTC")


def _tier1(st: dict) -> bool:
    a = st.get("attributes") or {}
    return st["subcategory"] in TIER1_MACRO or (
        a.get("kind") == "macro" and a.get("importance") == 1
    )


def _brief(st: dict, t: pd.Timestamp) -> dict:
    et = _ts(st["event_time"])
    a = st.get("attributes") or {}
    return {"event_id": st["event_id"], "subcategory": st["subcategory"], "title": st["title"],
            "event_time": st["event_time"], "minutes": round((et - t).total_seconds() / 60, 1),
            "time_precision": a.get("time_precision", "exact"),
            "sensitivity": list(MACRO_SENSITIVITY.get(st["subcategory"], ())),
            "confidence": st["confidence"]}  # fmt: skip


def macro_state(states: list[dict], t: datetime) -> dict:
    """Pre/post-event context at ``t`` from events known at ``t``."""
    t = pd.Timestamp(t).tz_convert("UTC")
    sched = [s for s in states if s["scheduled"] and s["event_time"] and s["confidence"] != "DENIED"
             and pd.Timestamp(s["first_seen_at"]) <= t]  # fmt: skip
    macro = [s for s in sched if s["category"] == Category.MACRO.value]
    t1 = [s for s in macro if _tier1(s)]
    fut = sorted((s for s in t1 if _ts(s["event_time"]) > t), key=lambda s: s["event_time"])
    past = sorted((s for s in t1 if _ts(s["event_time"]) <= t), key=lambda s: s["event_time"])
    nxt = _brief(fut[0], t) if fut else None
    last = _brief(past[-1], t) if past else None
    win24 = [s for s in sched if t < _ts(s["event_time"]) <= t + pd.Timedelta(hours=24)]
    sens = {x for s in win24 for x in MACRO_SENSITIVITY.get(s["subcategory"], ())}
    post = [s for s in macro if _ts(s["event_time"]) <= t < _ts(s["event_time"])
            + RELEVANCE[s["subcategory"]].post]  # fmt: skip
    mins_to = nxt["minutes"] if nxt else None
    return {
        "version": MACRO_STATE_VERSION,
        "as_of": t.isoformat(),
        "next_tier1": nxt,
        "last_tier1": last,
        "minutes_to_next_tier1": mins_to,
        "minutes_since_last_tier1": None if last is None else -last["minutes"],
        "tier1_within": {f"{m}m": mins_to is not None and mins_to <= m for m in BLACKOUT_MINUTES},
        "in_post_event_window": bool(post),
        "post_event": [_brief(s, t) for s in post],
        "next_24h": [_brief(s, t) for s in sorted(win24, key=lambda s: s["event_time"])],
        "sensitive_next_24h": {
            "usd": "usd" in sens, "jpy": "jpy" in sens, "global_rates": "global_rates" in sens,
            "crypto_specific": any(s["subcategory"] in CRYPTO_SCHEDULED for s in win24),
        },
        "note": "context only: scheduled-catalyst proximity is a conditioning variable, not a veto "
                "and not a direction",
    }  # fmt: skip


def surprise(state: dict, history: list[dict] | None = None) -> dict:
    """Objective surprise of a released value as known in ``state`` (an as-of fold).

    ``history``: earlier releases of the same ``series_key`` (as-of folds); their surprises
    standardise this one when at least ``STANDARDISE_MIN_OBS`` exist."""
    a = state.get("attributes") or {}
    seen = state.get("attr_seen") or {}
    et = _ts(state.get("event_time"))
    out = {"version": SURPRISE_VERSION, "event_id": state["event_id"],
           "series_key": a.get("series_key"), "unit": a.get("unit"), "actual": a.get("actual"),
           "consensus": None, "previous": a.get("previous"), "surprise": None,
           "abs_surprise": None, "direction_vs_consensus": None, "standardised": None,
           "n_history": 0, "vs_previous": None, "revision_impact": None, "status": "pending",
           "actual_first_seen_at": seen.get("actual"),
           "consensus_first_seen_at": seen.get("consensus")}  # fmt: skip
    if a.get("actual") is None:
        return out
    out["status"] = "released"
    if a.get("previous") is not None:
        out["vs_previous"] = a["actual"] - a["previous"]
    if a.get("revision_of_previous") is not None and a.get("previous") is not None:
        out["revision_impact"] = a["revision_of_previous"] - a["previous"]
    c = a.get("consensus")
    cseen = _ts(seen.get("consensus"))
    if c is None:
        out["status"] = "released_no_consensus"
        return out
    if et is not None and cseen is not None and cseen > et:
        out["status"] = "consensus_after_release"
        return out
    s = a["actual"] - c
    out.update(consensus=c, surprise=s, abs_surprise=abs(s),
               direction_vs_consensus="above" if s > 0 else "below" if s < 0 else "inline")  # fmt: skip
    prior = []
    for h in history or []:
        if h["event_id"] == state["event_id"] or (et and _ts(h.get("event_time")) >= et):
            continue
        r = surprise(h, None)
        if r["surprise"] is not None:
            prior.append(r["surprise"])
    out["n_history"] = len(prior)
    if len(prior) >= STANDARDISE_MIN_OBS:
        sd = statistics.pstdev(prior)
        out["standardised"] = s / sd if sd > 0 else None
    return out


def window_flags(state: dict, t: datetime) -> dict:
    """Where ``t`` sits relative to one scheduled event (for event-conditioned research)."""
    t = pd.Timestamp(t).tz_convert("UTC")
    et = _ts(state["event_time"])
    start, end = relevance_window(state)
    m = (et - t).total_seconds() / 60 if et is not None else None
    return {"minutes_to_event": m, "pre": m is not None and m > 0, "post": m is not None and m <= 0,
            "active": start <= t < end,
            **{f"within_{w}m": m is not None and 0 < m <= w for w in BLACKOUT_MINUTES}}  # fmt: skip
