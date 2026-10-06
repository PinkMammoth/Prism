"""Point-in-time context: ``context_snapshot(store, asset, t)`` — only what Prism knew at ``t``.

Every input is filtered by Prism's own knowledge time:
  events/updates/links  ``first_seen_at`` / ``observed_at`` / ``linked_at`` <= t
  positioning           capture time (HL) or ingested_at + period end (Binance, strict)
  activity              1h bars with ``first_observed_at`` <= t, revisions undone
  market                FRED values with ``available_at`` <= t; perp bars known by t
  freshness             provider runs finished by t

The payload is canonical JSON; ``snapshot_id`` is its content hash, so recomputing a
snapshot for the same ``t`` from an unchanged history reproduces the same id (the
reproducibility check for "what did Prism know at 14:37 UTC?"). ``record=True`` also appends
it to ``context_snapshots`` (immutable) so later theses can cite it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from market_signal.context import ledger
from market_signal.context.macro import macro_state
from market_signal.context.taxonomy import SUBCATEGORIES, Confidence, materiality, severity
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.research.lab.common import canonical_json, content_id

SNAPSHOT_VERSION = "context_snapshot_v1"
RECENT_DAYS = 7
SCHEDULE_HORIZON_DAYS = 45


def _ts(x) -> pd.Timestamp | None:
    return None if x is None else pd.Timestamp(x).tz_convert("UTC")


# --------------------------------------------------------------------------- in-memory index


@dataclass
class ContextIndex:
    """All events, updates and links loaded once; ``states_at(t)`` folds in memory. Use for
    research over many timestamps (event studies, Phase 22 probes) without re-querying."""

    events: pd.DataFrame
    updates: pd.DataFrame
    links: pd.DataFrame

    @classmethod
    def load(cls, store: Store) -> ContextIndex:
        ev = store.con.execute("SELECT * FROM context_events ORDER BY first_seen_at, event_id").df()
        up = store.con.execute("SELECT * FROM context_event_updates ORDER BY event_id, seq").df()
        ln = store.con.execute("SELECT * FROM context_asset_links").df()
        for df, cols in ((ev, ("first_seen_at",)), (up, ("observed_at",)), (ln, ("linked_at",))):
            for c in cols:
                if len(df):
                    df[c] = pd.to_datetime(df[c], utc=True)
        return cls(ev, up, ln)

    def states_at(self, t: datetime, asset: str | None = None,
                  window: tuple[datetime, datetime] | None = None) -> list[dict]:  # fmt: skip
        """Folds at ``t``; ``window`` pre-filters on the ORIGINAL anchor (event_time, else
        first_seen) for speed — callers re-filter on the folded anchor."""
        t = _ts(t)
        ev = self.events[self.events["first_seen_at"] <= t] if len(self.events) else self.events
        if window is not None and len(ev):
            pad = pd.Timedelta(days=30)  # an update may move event_time; keep a margin
            anchor = pd.to_datetime(ev["event_time"], utc=True).fillna(ev["first_seen_at"])
            ev = ev[(anchor >= _ts(window[0]) - pad) & (anchor <= _ts(window[1]) + pad)]
        if asset is not None and len(ev):
            ids = set(self.links.loc[(self.links["asset"] == asset.upper())
                                     & (self.links["linked_at"] <= t), "event_id"])  # fmt: skip
            ev = ev[ev["event_id"].isin(ids) | ev["category"].eq("macro")]
        out = []
        up = self.updates[self.updates["observed_at"] <= t] if len(self.updates) else self.updates
        by = dict(tuple(up.groupby("event_id"))) if len(up) else {}
        for r in ev.to_dict("records"):
            st = ledger._base_state(r)
            for u in by[r["event_id"]].itertuples() if r["event_id"] in by else ():
                st = ledger._apply(st, u)
            out.append(st)
        return out

    def link_types_at(self, asset: str, t: datetime) -> dict[str, str]:
        t = _ts(t)
        ln = self.links[(self.links["asset"] == asset.upper()) & (self.links["linked_at"] <= t)]
        rank = {"direct": 0, "ecosystem": 1, "market_wide": 2}
        out: dict[str, str] = {}
        for r in ln.itertuples():
            if r.event_id not in out or rank[r.link_type] < rank[out[r.event_id]]:
                out[r.event_id] = r.link_type
        return out


# --------------------------------------------------------------------------- event views


def event_view(st: dict, t: pd.Timestamp, link: str) -> dict:
    a = st.get("attributes") or {}
    m = materiality(st["subcategory"], Confidence(st["confidence"]), link=link, scope=st["scope"],
                    loss_usd=a.get("loss_usd_estimate"))  # fmt: skip
    start, end = ledger.relevance_window(st)
    pub, seen = _ts(st["published_at"]), _ts(st["first_seen_at"])
    proc = _ts(st["processed_at"])
    return {
        "event_id": st["event_id"], "category": st["category"], "subcategory": st["subcategory"],
        "title": st["title"], "confidence": st["confidence"], "link": link,
        "materiality": m["score"], "severity": severity(m["score"]),
        "scheduled": st["scheduled"], "event_time": st["event_time"],
        "published_at": st["published_at"], "first_seen_at": st["first_seen_at"],
        "last_updated_at": st["last_updated_at"], "active": ledger.is_active(st, t),
        "relevance": {"start": start.isoformat(), "end": end.isoformat()},
        "sources": len(st["sources"]), "observation_mode": st["observation_mode"],
        "information_latency_sec": None if pub is None else round((seen - pub).total_seconds(), 1),
        "processing_latency_ms": None if proc is None else round((proc - seen).total_seconds() * 1000, 1),
    }  # fmt: skip


# --------------------------------------------------------------------------- market context


def market_context(store: Store, t: datetime, coins: list[str] | None = None) -> dict:
    """BTC direction, perp breadth and the FRED rates/vol/USD values known at ``t``."""
    t = _ts(t)
    coins = coins or ["BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE"]
    rets: dict[str, float | None] = {}
    for c in coins:
        try:
            df = store.con.execute(
                "SELECT close_time, close FROM perp_intraday_bars WHERE source='hyperliquid' AND coin=? "
                "AND timeframe='1h' AND first_observed_at <= ? AND close_time >= ? ORDER BY close_time",
                [c, t.to_pydatetime(), (t - pd.Timedelta(hours=30)).to_pydatetime()]).df()  # fmt: skip
        except Exception:
            df = pd.DataFrame()
        if len(df) < 2:
            rets[c] = None
            continue
        df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
        last = df.iloc[-1]
        base = df[df["close_time"] <= last["close_time"] - pd.Timedelta(hours=24)]
        rets[c] = None if base.empty or t - last["close_time"] > pd.Timedelta(hours=3) else \
            float(last["close"] / base["close"].iloc[-1] - 1) * 100  # fmt: skip
    known = [v for v in rets.values() if v is not None]
    macro = {}
    for sid in ("DGS2", "DGS10", "VIXCLS", "DTWEXBGS", "DEXJPUS", "SP500", "DCOILWTICO"):
        try:
            r = store.con.execute(
                "SELECT obs_date, value FROM macro_observations WHERE series_id=? AND available_at <= ? "
                "AND value IS NOT NULL ORDER BY obs_date DESC, realtime_start DESC LIMIT 6",
                [sid, t.to_pydatetime()]).df()  # fmt: skip
        except Exception:
            r = pd.DataFrame()
        macro[sid] = None if r.empty else {
            "value": float(r["value"].iloc[0]), "obs_date": str(r["obs_date"].iloc[0]),
            "change_5obs": float(r["value"].iloc[0] - r["value"].iloc[-1]) if len(r) >= 6 else None}  # fmt: skip
    btc = rets.get("BTC")
    return {"btc_24h_pct": btc,
            "btc_direction_24h": None if btc is None else "up" if btc > 0 else "down" if btc < 0 else "flat",
            "breadth_24h_up_share": None if len(known) < 3 else sum(v > 0 for v in known) / len(known),
            "perp_24h_pct": rets, "fred_known": macro,
            "unavailable": ["equity index futures intraday", "DXY (proxy: FRED DTWEXBGS)",
                            "USDJPY intraday", "gold"]}  # fmt: skip


# --------------------------------------------------------------------------- snapshot


def context_snapshot(store: Store, asset: str, t: datetime | None = None, *, record: bool = False,
                     stale_hours: dict[str, float] | None = None,
                     index: ContextIndex | None = None,
                     include_positioning: bool = True) -> dict:  # fmt: skip
    from market_signal.context.opportunity import opportunity_state
    from market_signal.context.positioning import positioning_context
    from market_signal.context.providers.base import provider_health

    t = _ts(t or utcnow())
    asset = asset.upper()
    if index is not None:
        states = index.states_at(
            t, window=(t - pd.Timedelta(days=30), t + pd.Timedelta(days=SCHEDULE_HORIZON_DAYS))
        )
        lt = index.link_types_at(asset, t)
    else:
        states = ledger.states_asof(store, t.to_pydatetime(), since=(t - pd.Timedelta(days=60)).to_pydatetime(),
                                    until=(t + pd.Timedelta(days=SCHEDULE_HORIZON_DAYS + 30)).to_pydatetime())  # fmt: skip
        links = ledger.links_for(store, asset, t.to_pydatetime())
        lt = {e: ledger.link_type_for(links, e) for e in set(links["event_id"])}
    lo, hi = t - pd.Timedelta(days=30), t + pd.Timedelta(days=SCHEDULE_HORIZON_DAYS)
    states = [s for s in states if lo <= _ts(s["event_time"] or s["first_seen_at"]) <= hi]
    linked = [s for s in states if s["event_id"] in lt]
    views = [event_view(s, t, lt[s["event_id"]]) for s in linked]
    active = sorted(
        (v for v in views if v["active"]), key=lambda v: (-v["materiality"], v["event_id"])
    )
    recent = sorted((v for v in views if not v["scheduled"] and _ts(v["first_seen_at"])
                     >= t - pd.Timedelta(days=RECENT_DAYS)),
                    key=lambda v: (v["first_seen_at"], v["event_id"]), reverse=True)  # fmt: skip
    upcoming = sorted((v for v in views if v["scheduled"] and v["event_time"]
                       and _ts(v["event_time"]) > t), key=lambda v: (v["event_time"], v["event_id"]))  # fmt: skip
    mstate = macro_state(states, t)
    high_active = any(v["materiality"] >= 70 for v in active)
    nxt = mstate["minutes_to_next_tier1"]
    catalyst_24h = high_active or (nxt is not None and nxt <= 24 * 60)
    pos = (
        positioning_context(store, asset, t.to_pydatetime(), catalyst_24h=catalyst_24h)
        if include_positioning
        else None
    )
    opp = opportunity_state(store, asset, t.to_pydatetime())
    health = provider_health(store, stale_hours or {}, now=t.to_pydatetime()) if stale_hours else []
    warnings = [f"provider {h['provider']} {h['state']}" for h in health if h["state"] != "OK"]
    if opp["state"] == "unknown":
        warnings.append("activity state unknown (no recent 1h bars known)")
    for venue, b in ((pos or {}).get("venues") or {}).items():
        if b.get("open_interest") is None:
            warnings.append(f"{venue}: no fresh OI known (inputs older than the stale limit)")
        if b.get("funding_24h_mean") is None:
            warnings.append(f"{venue}: no settled funding known in the last 24h")
        if b.get("open_interest") is not None and b.get("oi_change_24h_pct") is None:
            warnings.append(f"{venue}: OI history too short for a 24h change")
    body = {
        "version": SNAPSHOT_VERSION, "asset": asset, "as_of": t.isoformat(),
        "knowledge_rule": "only information Prism had observed at or before as_of",
        "active_events": active, "recent_events": recent[:25], "upcoming_linked": upcoming[:10],
        "next_scheduled_catalyst": upcoming[0] if upcoming else None,
        "macro": mstate, "positioning": pos, "activity": opp,
        "market": market_context(store, t.to_pydatetime()),
        "freshness": {"providers": health, "warnings": warnings},
        "counts": {"known_events": len(states), "linked": len(linked), "active": len(active)},
        "taxonomy": {"categories": sorted({c.value for c in SUBCATEGORIES.values()})},
        "semantics": "context only: no direction, no recommendation, no veto",
    }  # fmt: skip
    body = json.loads(canonical_json(body))
    sid = content_id("ctxsnap_", body)
    snap = {"snapshot_id": sid, **body}
    if record:
        store.con.execute("INSERT INTO context_snapshots VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                          [sid, asset, t.to_pydatetime(), max(utcnow(), t.to_pydatetime()),
                           SNAPSHOT_VERSION, canonical_json(body)])  # fmt: skip
    return snap


def brief(store: Store, assets: list[str], t: datetime | None = None,
          stale_hours: dict[str, float] | None = None) -> dict:  # fmt: skip
    """Human/agent context brief: upcoming macro, active crypto events, unusual positioning,
    elevated activity, data warnings. No recommendations."""
    t = _ts(t or utcnow())
    idx_states = ledger.states_asof(store, t.to_pydatetime(), since=(t - pd.Timedelta(days=30)).to_pydatetime(),
                                    until=(t + pd.Timedelta(days=SCHEDULE_HORIZON_DAYS)).to_pydatetime())  # fmt: skip
    ms = macro_state(idx_states, t)
    crypto = []
    for s in idx_states:
        if s["category"] == "macro" or not ledger.is_active(s, t):
            continue
        crypto.append(event_view(s, t, "market_wide" if s["scope"] == "systemic" else "direct"))
    crypto.sort(key=lambda v: -v["materiality"])
    per_asset = []
    for a in assets:
        snap = context_snapshot(store, a, t.to_pydatetime(), stale_hours=None)
        pos = snap["positioning"] or {"venues": {}}
        per_asset.append({"asset": a, "activity": snap["activity"]["state"],
                          "crowding": {v: b["crowding"]["skew"] + "/" + b["crowding"]["leverage"]
                                       for v, b in pos["venues"].items()},
                          "vulnerability": {v: {"long_side": b["vulnerability"]["long_side"]["level"],
                                                "short_side": b["vulnerability"]["short_side"]["level"]}
                                            for v, b in pos["venues"].items()},
                          "active_linked_events": len(snap["active_events"])})  # fmt: skip
    from market_signal.context.providers.base import provider_health

    health = provider_health(store, stale_hours or {}, now=t.to_pydatetime()) if stale_hours else []
    return {"as_of": t.isoformat(), "macro_next_24h": ms["next_24h"], "next_tier1": ms["next_tier1"],
            "post_event": ms["post_event"], "active_crypto_events": crypto[:15],
            "unusual_positioning": [p for p in per_asset if any(
                not v.startswith("neutral") and not v.startswith("unknown") for v in p["crowding"].values())],
            "elevated_activity": [p["asset"] for p in per_asset if p["activity"] == "elevated"],
            "assets": per_asset,
            "warnings": [f"provider {h['provider']} {h['state']}" for h in health if h["state"] != "OK"],
            "semantics": "context only: no trade recommendations"}  # fmt: skip
