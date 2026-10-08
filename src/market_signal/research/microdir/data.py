"""Database side of Phase 24B (read-only): frames, context providers and production evidence.

* **Frames**: ``microstructure.query.load_microstructure`` per coin with
  ``production_only=True`` (nothing before the immutable 24A production cutover; a scratch or
  development database without a cutover yields nothing), ``known_at=as_of`` (only rows
  finalized by then; older revisions as they were), the gap-filled 1-minute grid and
  ``availability="finalized"``. Research never queries the microstructure tables directly.
* **Crowding** (Phase 23, ``positioning_context(strict=True)``): per venue ``crowding_v1``
  skew from observations Prism had actually received by the signal time (first-seen; no
  retrospective hourly alignment).
* **Context** (Phase 23, ``ContextIndex``): events folded as of the signal time from
  ``first_seen_at`` and updates observed by then (never publication time alone, never a
  later revision), mapped to one frozen category.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

import pandas as pd

from market_signal.data.store import Store
from market_signal.microstructure import definitions as md
from market_signal.microstructure import query as mq
from market_signal.research.microdir import spec as sp

POST_EARLY_MIN, POST_LATE_MIN, PRE_MIN = 15, 60, 60


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def load_frames(store: Store, as_of, coins=sp.COINS,
                since=None) -> tuple[dict[str, pd.DataFrame], dict]:  # fmt: skip
    """Per-coin 1-minute frames known at ``as_of`` (post-cutover only) + provenance.
    ``since`` (inspection only) trims the start; checkpoints always load from the cutover."""
    as_of = _ts(as_of)
    co = mq.cutover(store)
    meta: dict = {"cutover": co, "as_of": as_of.isoformat(), "availability": sp.AVAILABILITY,
                  "source_feature_version": md.FEATURE_VERSION, "coins": {}}  # fmt: skip
    if co is None:
        meta["note"] = "no production microstructure cutover: nothing is confirmatory"
        return {}, meta
    start = _ts(co["first_minute"])
    if since is not None:
        start = max(start, _ts(since))
    frames = {}
    h = hashlib.sha256()
    for coin in coins:
        df = mq.load_microstructure(store, coin, start, as_of, known_at=as_of,
                                    availability=sp.AVAILABILITY, production_only=True)  # fmt: skip
        frames[coin] = df
        st = df["status"].value_counts().to_dict() if len(df) else {}
        meta["coins"][coin] = {"minutes": len(df), "status": {k: int(v) for k, v in st.items()}}
        if "content_sha" in df:
            for s in df["content_sha"].fillna("-"):
                h.update(str(s).encode())
    meta["rows_digest"] = h.hexdigest()  # reproduction check for a checkpoint
    return frames, meta


def crowding_provider(store: Store):
    from market_signal.context.positioning import positioning_context

    @lru_cache(maxsize=4096)
    def get(coin: str, t: pd.Timestamp) -> dict:
        try:
            ctx = positioning_context(store, coin, _ts(t).to_pydatetime(), strict=True)
        except Exception:  # an absent Phase 23 table / empty history: unknown, never assumed
            return {"skew": None}
        v = ctx.get("venues") or {}
        hl = ((v.get("hyperliquid") or {}).get("crowding") or {}).get("skew")
        bn = ((v.get("binance") or {}).get("crowding") or {}).get("skew")
        skew = hl if hl not in (None, "unknown") else bn
        return {"skew": skew, "hyperliquid": hl, "binance": bn,
                "ls_account_pct_30d": (v.get("binance") or {}).get("ls_account_pct_30d")}  # fmt: skip

    return get


def context_category(states: list[dict], links: dict[str, str], t: pd.Timestamp) -> str:
    """Frozen priority: pre-tier-1 (<= 60 m) > post 0-15 m > post 15-60 m > active confirmed
    crypto event (direct or market-wide) > none. Pure; ``states`` folded as of ``t``."""
    from market_signal.context.ledger import is_active
    from market_signal.context.macro import macro_state

    m = macro_state(states, t)
    to_next = m.get("minutes_to_next_tier1")
    since = m.get("minutes_since_last_tier1")
    if to_next is not None and 0 < to_next <= PRE_MIN:
        return "pre_tier1_60m"
    if since is not None and 0 <= since < POST_EARLY_MIN:
        return "post_tier1_0_15m"
    if since is not None and POST_EARLY_MIN <= since < POST_LATE_MIN:
        return "post_tier1_15_60m"
    for s in states:
        if s["category"] == "macro" or s["confidence"] not in ("CONFIRMED", "OFFICIAL"):
            continue
        if pd.Timestamp(s["first_seen_at"]) > t:  # defensive: folds are already first-seen
            continue
        if links.get(s["event_id"]) in ("direct", "market_wide") and is_active(s, t):
            return "crypto_event_active"
    return "none"


def context_provider(store: Store):
    from market_signal.context.snapshot import ContextIndex

    try:
        idx = ContextIndex.load(store)
    except Exception:  # Phase 23 tables absent
        return lambda coin, t: "none"
    if idx.events.empty:
        return lambda coin, t: "none"

    @lru_cache(maxsize=8192)
    def get(coin: str, t: pd.Timestamp) -> str:
        t = _ts(t)
        states = idx.states_at(t, asset=coin, window=(t - pd.Timedelta(days=2),
                                                      t + pd.Timedelta(days=2)))  # fmt: skip
        return context_category(states, idx.link_types_at(coin, t), t)

    return get


# --------------------------------------------------------------------------- production evidence


def production_evidence(store: Store, now=None) -> dict:
    """What prospective microstructure exists: cutover, span, status mix per coin, large-print
    warmup, OI hourly cutover and Phase 23 freshness. Read-only."""
    from market_signal.microstructure import health as mh

    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    co = mq.cutover(store)
    out: dict = {"as_of": now.isoformat(), "cutover": co}
    if co is not None:
        first = _ts(co["first_minute"])
        minutes = int((now - first) / pd.Timedelta(minutes=1))
        cov = mh.coverage(store, now, minutes=max(minutes, 1))
        out["collected_hours"] = round(minutes / 60, 2)
        out["collected_days"] = round(minutes / 1440, 3)
        out["coverage"] = cov.to_dict("records") if len(cov) else []
        try:
            lp = store.con.execute(
                "SELECT coin, min(minute_open) FROM microstructure_minutes WHERE feature_version=? "
                "AND lp_threshold IS NOT NULL AND minute_open >= ? GROUP BY 1",
                [md.FEATURE_VERSION, first.to_pydatetime()]).fetchall()  # fmt: skip
            out["large_prints_first_minute"] = {c: _ts(m).isoformat() for c, m in lp}
        except Exception:
            out["large_prints_first_minute"] = {}
        out["large_prints_expected_from"] = (
            first.floor("D") + pd.Timedelta(days=md.LP_MIN_DAYS + 1)
        ).isoformat()
        out["normalization_ready_from"] = (
            first + pd.Timedelta(minutes=sp.SIGNAL_MINUTES * sp.NORM_MIN_WINDOWS)).isoformat()  # fmt: skip
    else:
        out["note"] = ("no production cutover in this database (scratch/development, or the "
                       "Phase 24A collector has not ingested a COMPLETE minute yet)")  # fmt: skip
    try:
        from market_signal.context.positioning import hl_cutover

        out["oi_hourly_cutover"] = hl_cutover(store)
        r = store.con.execute("SELECT max(grid_hour) FROM context_hl_oi_hourly").fetchone()
        out["oi_hourly_last"] = None if not r or r[0] is None else _ts(r[0]).isoformat()
        r = store.con.execute("SELECT max(first_seen_at), count(*) FROM context_events").fetchone()
        out["context_events"] = {"newest_first_seen": None if r[0] is None else _ts(r[0]).isoformat(),
                                 "count": int(r[1])}  # fmt: skip
        r = store.con.execute("SELECT max(ingested_at) FROM context_ls_ratios").fetchone()
        out["binance_ratios_last_ingest"] = None if r[0] is None else _ts(r[0]).isoformat()
    except Exception:
        out["phase23"] = "context tables absent"
    return out


def evaluator(store: Store, *, registered_at=None):
    """``(defn, as_of) -> (payload, meta)`` on the data Prism held at ``as_of``."""
    from market_signal.research.microdir import analysis as an

    def run(defn, as_of):
        frames, meta = load_frames(store, as_of)
        co = meta.get("cutover")
        if registered_at is not None and co is not None:
            meta["registered_at"] = _ts(registered_at).isoformat()
            meta["registered_after_cutover"] = _ts(registered_at) > _ts(co["first_minute"])
        payload = an.evaluate(frames, defn, as_of=_ts(as_of), crowding=crowding_provider(store),
                              context=context_provider(store), data_meta=meta)  # fmt: skip
        return payload, {"rows_digest": meta.get("rows_digest"), "as_of": _ts(as_of).isoformat()}

    return run
