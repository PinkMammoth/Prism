"""Context-conditioned Phase 22 probes: infrastructure only (nothing is rerun or rescored).

The question for a LATER, preregistered study: do the seven Phase 22 INTERESTING variants
(noisy 1h long setups) behave differently when conditioned on genuinely different
information? This module only:

* lists those variants from a stored Phase 22 result payload (``interesting_variants``), and
* attaches point-in-time context to any Phase 22 event frame (``attach_context``): the
  scheduled-catalyst state, linked active events and materiality, HL crowding, and the
  activity state, each as Prism knew it at the signal's availability time ``signal_ns``.

It never changes a Phase 22 threshold, verdict or window, and computes no profitability.
"""

from __future__ import annotations

import pandas as pd

from market_signal.context.macro import macro_state
from market_signal.context.snapshot import ContextIndex, event_view
from market_signal.data.store import Store

PROBE_VERSION = "phase22_context_probe_v1"
CONTEXT_COLUMNS = ("ctx_minutes_to_tier1", "ctx_tier1_within_60m", "ctx_post_event",
                   "ctx_active_events", "ctx_max_materiality", "ctx_active_subcategories",
                   "ctx_hl_skew", "ctx_hl_leverage", "ctx_activity")  # fmt: skip


def interesting_variants(payload: dict) -> list[dict]:
    """The INTERESTING variants of a stored discovery result (by its own verdicts)."""
    return [{"key": s["key"], "strategy_id": s["strategy_id"], "side": s["side"],
             "timeframe": s["timeframe"], "family": s["family"]}
            for s in payload.get("strategies", []) if s.get("verdict") == "INTERESTING"]  # fmt: skip


def attach_context(store: Store, events: pd.DataFrame, *, with_positioning: bool = False,
                   with_activity: bool = False, index: ContextIndex | None = None) -> pd.DataFrame:  # fmt: skip
    """Copy of a Phase 22 event frame (``coin``, ``signal_ns``) with ``ctx_*`` columns.
    Positioning/activity are optional because they query per row (slower)."""
    from market_signal.context.opportunity import opportunity_state
    from market_signal.context.positioning import positioning_context

    idx = index or ContextIndex.load(store)
    out = events.copy()
    cols = {c: [] for c in CONTEXT_COLUMNS}
    for r in events.itertuples():
        t = pd.Timestamp(int(r.signal_ns), tz="UTC")
        states = idx.states_at(t)
        ms = macro_state(states, t)
        lt = idx.link_types_at(r.coin, t)
        act = [event_view(s, t, lt[s["event_id"]]) for s in states if s["event_id"] in lt]
        act = [v for v in act if v["active"] and v["category"] != "macro"]
        cols["ctx_minutes_to_tier1"].append(ms["minutes_to_next_tier1"])
        cols["ctx_tier1_within_60m"].append(ms["tier1_within"]["60m"])
        cols["ctx_post_event"].append(ms["in_post_event_window"])
        cols["ctx_active_events"].append(len(act))
        cols["ctx_max_materiality"].append(max((v["materiality"] for v in act), default=0))
        cols["ctx_active_subcategories"].append(",".join(sorted({v["subcategory"] for v in act})))
        if with_positioning:
            hl = positioning_context(store, r.coin, t.to_pydatetime())["venues"]["hyperliquid"]
            cols["ctx_hl_skew"].append(hl["crowding"]["skew"])
            cols["ctx_hl_leverage"].append(hl["crowding"]["leverage"])
        else:
            cols["ctx_hl_skew"].append(None)
            cols["ctx_hl_leverage"].append(None)
        cols["ctx_activity"].append(opportunity_state(store, r.coin, t.to_pydatetime())["state"]
                                    if with_activity else None)  # fmt: skip
    for c, v in cols.items():
        out[c] = v
    out.attrs["context_probe_version"] = PROBE_VERSION
    return out
