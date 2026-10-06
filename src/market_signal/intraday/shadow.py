"""Shadow execution timing (observational only; never alters the paper run).

For every paper ENTRY order submitted by the paper engine, record what an intraday execution
reference would have been and when Prism could have known it, under the frozen rule
``intraday_exec_timing_v1``. The paper engine never reads this table; fills, PnL, events and
notifications of the paper run are unchanged (it keeps its Phase 12 daily-open fill model).

Rule ``intraday_exec_timing_v1`` (frozen; a change is a new version):

- ``intended_entry_at``: the legacy reference time, the open of the order's fill day
  (``fill_bar_close - 1 day``, i.e. the signal bar's close);
- ``decision_at``: when the ``order_submitted`` event was recorded (Prism cannot trade before
  it has decided);
- reference bar: the first stored native Hyperliquid **15m** bar of the order's venue/coin with
  ``open_time >= max(intended_entry_at, decision_at)``; reference price = its open;
- ``ref_observed_at``: that bar's ``first_observed_at`` (closed-bar model: Prism holds a bar's
  values only once it has closed and been fetched); latency = ref_observed_at -
  intended_entry_at; ``timely`` = latency <= 1 hour;
- prospective only: recorded only while ``now - intended_entry_at <= 48 h``. Old orders are
  never backfilled; a missed observation stays missed.

The legacy reference (the daily open the paper engine actually used) is NOT copied here: the
report joins it from the immutable ``order_filled`` event when it exists.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd

from market_signal.data.store import Store
from market_signal.intraday.grid import utc
from market_signal.models.domain import utcnow
from market_signal.research.lab.common import canonical_json, content_id

RULE_VERSION = "intraday_exec_timing_v1"
RULE = {
    "version": RULE_VERSION,
    "timeframe": "15m",
    "derivation": "native",
    "reference": "open of first stored bar with open_time >= max(intended_entry_at, decision_at)",
    "availability": "first_observed_at of the reference bar",
    "timely_within_seconds": 3600,
    "prospective_window_hours": 48,
    "effect_on_paper_run": "none (observational)",
}
DAY = timedelta(days=1)


def _entry_orders(store: Store) -> list[dict]:
    rows = store.con.execute(
        "SELECT e.run_id, e.recorded_at, e.payload, r.definition FROM paper_events e "
        "JOIN paper_runs r USING (run_id) WHERE e.event_type='order_submitted' ORDER BY e.run_id, e.seq"
    ).fetchall()
    out = []
    for run_id, recorded_at, payload, definition in rows:
        p = json.loads(payload)
        o = p.get("order") or {}
        if o.get("purpose") != "entry":
            continue
        cohort = json.loads(definition).get("cohort") or []
        source = next((m["source"] for m in cohort if o["symbol"] in m.get("assets", [])), None)
        out.append({"run_id": run_id, "order_id": o["order_id"], "symbol": o["symbol"],
                    "side": int(o["position_side"]), "source": source,
                    "intended_entry_at": utc(o["fill_bar_close"]) - DAY,
                    "decision_at": utc(recorded_at)})  # fmt: skip
    return out


def record(store: Store, now=None) -> list[dict]:
    """Insert shadow observations that have become available. Idempotent; read-only towards
    every paper table. Returns the rows written."""
    now = utc(now or utcnow())
    done = {(r[0], r[1]) for r in store.con.execute(
        "SELECT paper_run_id, order_id FROM intraday_execution_shadow WHERE rule_version=?", [RULE_VERSION]
    ).fetchall()}  # fmt: skip
    written = []
    window = timedelta(hours=RULE["prospective_window_hours"])
    for o in _entry_orders(store):
        if (o["run_id"], o["order_id"]) in done or o["source"] is None:
            continue
        if now - o["intended_entry_at"] > window:
            continue  # prospective only: never reconstructed after the fact
        actionable = max(o["intended_entry_at"], o["decision_at"])
        ref = store.con.execute(
            "SELECT open_time, open, first_observed_at FROM perp_intraday_bars WHERE source=? AND "
            "coin=? AND timeframe='15m' AND derivation='native' AND open_time >= ? AND "
            "first_observed_at <= ? ORDER BY open_time LIMIT 1",
            [o["source"], o["symbol"], actionable.to_pydatetime(), now.to_pydatetime()],
        ).fetchone()
        if ref is None:
            continue
        nxt = store.con.execute(  # the reference must be the FIRST bar at/after actionable
            "SELECT count(*) FROM perp_intraday_bars WHERE source=? AND coin=? AND timeframe='15m' "
            "AND open_time >= ? AND open_time < ?",
            [o["source"], o["symbol"], actionable.to_pydatetime(), ref[0]],
        ).fetchone()[0]
        if nxt or utc(ref[0]) - actionable >= timedelta(minutes=15):
            continue  # the true first bar is missing: do not substitute a later one
        aligned = store.con.execute(
            "SELECT open, first_observed_at FROM perp_intraday_bars WHERE source=? AND coin=? AND "
            "timeframe='15m' AND open_time=?",
            [o["source"], o["symbol"], o["intended_entry_at"].to_pydatetime()],
        ).fetchone()
        ref_open, ref_px, ref_seen = utc(ref[0]), float(ref[1]), utc(ref[2])
        latency = (ref_seen - o["intended_entry_at"]).total_seconds()
        payload = {
            "rule": RULE,
            "actionable_at": actionable.isoformat(),
            "aligned_15m_open_at_intended_entry": None if aligned is None else float(aligned[0]),
            "aligned_observed_at": None if aligned is None else utc(aligned[1]).isoformat(),
            "notification_possible_at": max(o["decision_at"], ref_seen).isoformat(),
        }
        sid = content_id(
            "ishadow_", {"rule": RULE_VERSION, "run": o["run_id"], "order": o["order_id"]}
        )
        store.con.execute(
            "INSERT INTO intraday_execution_shadow VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            [sid, RULE_VERSION, o["run_id"], o["order_id"], o["symbol"], o["source"], o["side"],
             o["intended_entry_at"].to_pydatetime(), o["decision_at"].to_pydatetime(),
             ref_open.to_pydatetime(), ref_px, ref_seen.to_pydatetime(), latency,
             latency <= RULE["timely_within_seconds"], now.to_pydatetime(), canonical_json(payload)],
        )  # fmt: skip
        written.append({"order_id": o["order_id"], "symbol": o["symbol"], "ref_price": ref_px,
                        "latency_seconds": latency})  # fmt: skip
    return written


def report(store: Store, now=None) -> dict:
    """Shadow rows joined with the legacy daily-open fill and legacy notification time."""
    now = utc(now or utcnow())
    orders = _entry_orders(store)
    rows = store.query(
        "SELECT * FROM intraday_execution_shadow WHERE rule_version=? ORDER BY intended_entry_at",
        [RULE_VERSION],
    )
    fills = {}
    for (payload,) in store.con.execute(
        "SELECT payload FROM paper_events WHERE event_type='order_filled'"
    ).fetchall():
        p = json.loads(payload)
        fills[p["order_id"]] = p["fill"]
    opened = {}
    for payload, recorded_at in store.con.execute(
        "SELECT payload, recorded_at FROM paper_events WHERE event_type='position_opened'"
    ).fetchall():
        opened[json.loads(payload)["order_id"]] = utc(recorded_at)
    out = []
    for _, r in rows.iterrows():
        legacy = fills.get(r["order_id"], {}).get("reference_price")
        diff = None if legacy is None else r["side"] * (r["ref_price"] - legacy) / legacy * 1e4
        p = json.loads(r["payload"])
        legacy_note = opened.get(r["order_id"])
        possible = utc(p["notification_possible_at"])
        out.append({
            "order_id": r["order_id"], "symbol": r["symbol"], "side": int(r["side"]),
            "intended_entry_at": utc(r["intended_entry_at"]), "decision_at": utc(r["decision_at"]),
            "legacy_daily_open": legacy, "shadow_ref_open_time": utc(r["ref_open_time"]),
            "shadow_ref_price": float(r["ref_price"]), "adverse_bps_vs_legacy": diff,
            "observation_latency_min": round(r["latency_seconds"] / 60, 1), "timely": bool(r["timely"]),
            "notification_possible_at": possible, "legacy_notification_event_at": legacy_note,
            "notification_lead_h": None if legacy_note is None else round((legacy_note - possible).total_seconds() / 3600, 2),
        })  # fmt: skip
    recorded = {(r["paper_run_id"], r["order_id"]) for _, r in rows.iterrows()}
    pending = [o for o in orders if (o["run_id"], o["order_id"]) not in recorded]
    window = timedelta(hours=RULE["prospective_window_hours"])
    return {"rule": RULE, "entry_orders": len(orders), "recorded": out,
            "pending": [o["order_id"] for o in pending if now - o["intended_entry_at"] <= window],
            "missed": [o["order_id"] for o in pending if now - o["intended_entry_at"] > window]}  # fmt: skip


def summary_frame(rep: dict) -> pd.DataFrame:
    return pd.DataFrame(rep["recorded"])
