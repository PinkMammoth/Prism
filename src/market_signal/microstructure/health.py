"""Collector and data health (read-only). A dead socket must not look healthy: health is judged
from the collector's own status file (written every few seconds while it runs) AND from the
finalized minutes in the database, so a silent collector, a stale feed or a stuck ingest are
each visible on their own."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd

from market_signal.data.store import Store
from market_signal.microstructure import definitions as d
from market_signal.microstructure.spool import Spool
from market_signal.models.domain import utcnow

STATUS_STALE_S = 60  # status.json older than this: the collector is not running
DISCONNECTED_ALERT_S = 300  # transient reconnects are normal; a 5-minute outage is not
FINAL_STALE_S = 180  # no finalized minute for this long while running
INGEST_LAG_S = 45 * 60  # the ingest job runs every 15 minutes
GAP_WINDOW_MIN = 60
GAP_FRACTION_ALERT = 0.20
TRADES_SILENT_S = 30 * 60  # informational only: a quiet market is legitimate


def collector_state(spool: Spool, now: datetime | None = None) -> dict:
    now_ms = int(pd.Timestamp(now or utcnow()).value // 1_000_000)
    st = spool.read_status()
    if st is None:
        return {"state": "NOT RUNNING", "status": None, "issues": ["no collector status file"],
                "warnings": []}  # fmt: skip
    issues, warnings = [], []
    age = (now_ms - st["written_ms"]) / 1000
    state = "RUNNING"
    if age > STATUS_STALE_S:
        state = "DOWN"
        issues.append(f"collector status is {age:.0f}s old (process not running)")
    else:
        if not st.get("connected"):
            since = st.get("disconnected_since_ms")
            dur = (now_ms - since) / 1000 if since else None
            state = "DISCONNECTED"
            if dur is None or dur > DISCONNECTED_ALERT_S:
                issues.append(f"disconnected{f' for {dur / 60:.1f} min' if dur else ''}: "
                              f"{st.get('last_error')}")  # fmt: skip
        else:
            for c, v in (st.get("coins") or {}).items():
                missing = set(d.FEEDS) - set(v.get("subscribed") or [])
                if missing:
                    issues.append(f"{c}: subscription missing {sorted(missing)}")
                b = v.get("last_book5_age_s")
                if b is None or b > 30:
                    issues.append(f"{c}: book feed stale ({b}s)")
                t = v.get("last_trade_age_s")
                if t is not None and t > TRADES_SILENT_S:
                    warnings.append(
                        f"{c}: no trade for {t / 60:.0f} min (quiet market or silent feed)"
                    )
        if st.get("pending_records") or st.get("dropped_records"):
            issues.append(f"spool write failure: {st.get('pending_records')} pending, "
                          f"{st.get('dropped_records')} dropped ({st.get('last_error')})")  # fmt: skip
        finals = [v.get("last_finalized_minute") for v in (st.get("coins") or {}).values()]
        finals = [pd.Timestamp(f) for f in finals if f]
        up = (now_ms - st["started_ms"]) / 1000
        if up > FINAL_STALE_S and (
            not finals
            or (pd.Timestamp(now or utcnow()) - max(finals)).total_seconds() > FINAL_STALE_S
        ):
            issues.append("no finalized minute recently")
        if st.get("raw_paused"):
            warnings.append("raw archive paused (low disk); aggregates continue")
    return {"state": state, "status": st, "issues": issues, "warnings": warnings,
            "status_age_s": round(age, 1)}  # fmt: skip


def coverage(store: Store, now: datetime | None = None, minutes: int = 1440,
             feature_version: str = d.FEATURE_VERSION) -> pd.DataFrame:  # fmt: skip
    """Per coin: minute status counts over the last ``minutes`` (MISSING = expected minute with
    no row, counted from the coin's first stored minute), newest minute and its age."""
    now_ts = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    lo = now_ts.floor("min") - pd.Timedelta(minutes=minutes)
    try:
        df = store.con.execute(
            "SELECT coin, status, count(*) n, min(minute_open) first_m FROM "
            "microstructure_minutes WHERE feature_version=? AND minute_open >= ? GROUP BY 1, 2",
            [feature_version, lo.to_pydatetime()]).df()  # fmt: skip
        newest = dict(store.con.execute(
            "SELECT coin, max(minute_open) FROM microstructure_minutes WHERE feature_version=? "
            "GROUP BY 1", [feature_version]).fetchall())  # fmt: skip
    except duckdb.CatalogException:  # schema < 23
        return pd.DataFrame()
    rows = []
    for coin in sorted(set(df["coin"]) | set(newest)):
        g = df[df["coin"] == coin]
        counts = {s: int(g.loc[g["status"] == s, "n"].sum()) for s in d.STATUSES}
        first = pd.Timestamp(g["first_m"].min()).tz_convert("UTC") if len(g) else None
        expected = 0 if first is None else int((now_ts.floor("min") - pd.Timedelta(minutes=1) - first)
                                               / pd.Timedelta(minutes=1)) + 1  # fmt: skip
        last = newest.get(coin)
        last = None if last is None else pd.Timestamp(last).tz_convert("UTC")
        rows.append({"coin": coin, **counts, "MISSING": max(expected - sum(counts.values()), 0),
                     "complete_frac": round(counts["COMPLETE"] / expected, 4) if expected > 0 else None,
                     "newest_minute": None if last is None else last.isoformat(),
                     "newest_age_min": None if last is None else round((now_ts - last).total_seconds() / 60, 1)})  # fmt: skip
    return pd.DataFrame(rows)


def evaluate(store: Store | None, spool: Spool, now: datetime | None = None) -> dict:
    """Overall verdict for `market microstructure health --check` and the runtime job."""
    col = collector_state(spool, now)
    issues = list(col["issues"])
    warnings = list(col["warnings"])
    cov = recent = pd.DataFrame()
    if store is not None:
        cov = coverage(store, now)
        recent = coverage(store, now, minutes=GAP_WINDOW_MIN)
        for r in cov.itertuples():
            if r.newest_age_min is not None and r.newest_age_min * 60 > INGEST_LAG_S:
                issues.append(f"{r.coin}: newest stored minute is {r.newest_age_min:.0f} min old")
        dup = store.con.execute(
            "SELECT count(*) FROM (SELECT 1 FROM microstructure_minutes WHERE minute_open >= ? "
            "GROUP BY feature_version, coin, minute_open HAVING count(*) > 1)",
            [(pd.Timestamp(now or utcnow()) - pd.Timedelta(days=2)).to_pydatetime()],
        ).fetchone()[0]
        if dup:
            issues.append(f"{dup} duplicate minute keys stored in the last 2 days (ingest bug)")
        for r in recent.itertuples():
            total = sum(getattr(r, s) for s in d.STATUSES) + r.MISSING
            bad = total - r.COMPLETE
            if total >= 30 and bad / total > GAP_FRACTION_ALERT:
                issues.append(
                    f"{r.coin}: {bad}/{total} of the last {GAP_WINDOW_MIN} min not COMPLETE"
                )
    return {"healthy": not issues, "collector": col["state"], "issues": issues,
            "warnings": warnings, "coverage_24h": cov.to_dict("records") if len(cov) else [],
            "coverage_1h": recent.to_dict("records") if len(recent) else []}  # fmt: skip


def status_row(store: Store, spool: Spool, now: datetime | None = None) -> list[dict]:
    """The concise `market status` row, shown only once the capability is deployed (a
    collector status file or stored minutes exist)."""
    col = collector_state(spool, now)
    cov = coverage(store, now)
    if col["status"] is None and cov.empty:
        return []
    from market_signal.microstructure.query import cutover

    co = cutover(store)
    st = col["status"] or {}
    coins = st.get("coins") or {}
    connected = sum(1 for v in coins.values() if set(d.FEEDS) <= set(v.get("subscribed") or []))
    newest = None if cov.empty else cov["newest_minute"].dropna().max()
    gaps = (
        0
        if cov.empty
        else int(cov[["PARTIAL", "TRADE_ONLY", "BOOK_ONLY", "GAP", "MISSING"]].sum().sum())
    )
    ev = evaluate(store, spool, now)
    state = (
        "OK"
        if ev["healthy"]
        else ("STALE" if col["state"] in ("DOWN", "NOT RUNNING") else "ATTENTION")
    )
    detail = (f"collector {col['state'].lower()}, {connected}/{len(coins) or len(d.COINS)} assets subscribed; "
              f"latest stored minute {str(newest)[5:16] if newest else 'none'}; "
              f"{gaps} non-complete min/24h; "
              f"cutover {str(co['first_minute'])[:16] if co else 'not yet'}")  # fmt: skip
    if ev["issues"]:
        detail += "; " + ev["issues"][0]
    return [{"component": "Microstructure", "state": state, "detail": detail}]
