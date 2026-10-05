"""Intraday bar storage, revisions, point-in-time reads, coverage and gap diagnostics.

Storage contract (``perp_intraday_bars``, migration 18):

- one row per (venue ``source``, ``coin``, ``timeframe``, ``open_time``); UTC half-open
  ``[open_time, close_time)``; only CLOSED bars (``first_observed_at >= close_time`` is a
  table CHECK);
- ``first_observed_at``: when Prism first held the closed bar. This is its availability, not
  the theoretical close. ``observed_live`` says whether that was within one interval of the
  close (a live observation) or later (a backfill: the true publication time is unknown);
- a later fetch with different values is a *revision*: the canonical row takes the new values,
  ``revision`` increments, and the superseded values plus the time they were first held go to
  ``perp_intraday_revisions``. Identical refetches change nothing (idempotent);
- ``derivation`` is ``native`` (the provider's own bar). Nothing is aggregated in v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from market_signal.data.store import Store
from market_signal.intraday.grid import (
    expected_opens,
    interval,
    latest_closed_open,
    parse_timeframe,
    utc,
)
from market_signal.models.domain import Timeframe

INGEST_VERSION = "intraday_ingest_v1"
NATIVE = "native"
VALUES = ("open", "high", "low", "close", "volume", "trades")


# --------------------------------------------------------------------------- writes


def upsert_bars(
    store: Store,
    source: str,
    coin: str,
    tf: Timeframe,
    bars: pd.DataFrame,
    *,
    observed_at: datetime,
    run_id: str,
    derivation: str = NATIVE,
    in_transaction: bool = False,
) -> dict[str, int]:
    """Idempotent upsert of CLOSED bars observed at ``observed_at``.

    ``bars``: open_time (UTC, on the grid), open, high, low, close, volume, trades. Bars not
    yet closed at ``observed_at`` are refused here as a last line of defence (the fetcher
    already drops them), and counted as ``forming``. Atomic: its own transaction, or the
    caller's when ``in_transaction``.
    """
    tf = parse_timeframe(tf)
    seen = utc(observed_at)
    if bars.empty:
        return {"inserted": 0, "revised": 0, "unchanged": 0, "forming": 0}
    stage = bars[["open_time", *VALUES]].copy()
    stage["open_time"] = pd.to_datetime(stage["open_time"], utc=True)
    stage["close_time"] = stage["open_time"] + interval(tf)
    forming = int((stage["close_time"] > seen).sum())
    stage = stage[stage["close_time"] <= seen]
    if stage.empty:
        return {"inserted": 0, "revised": 0, "unchanged": 0, "forming": forming}
    if stage["open_time"].duplicated().any():
        raise ValueError(f"{source}/{coin}/{tf}: duplicate open_time in one batch")
    stage["trades"] = stage["trades"].astype("Int64")
    live_cut = seen - interval(tf)
    stage["observed_live"] = stage["close_time"] >= live_cut
    stage.insert(0, "source", source)
    stage.insert(1, "coin", coin)
    stage.insert(2, "timeframe", tf.value)
    con = store.con
    lo, hi = stage["open_time"].min().to_pydatetime(), stage["open_time"].max().to_pydatetime()
    # the literal series/range predicate lets DuckDB prune row groups (there is no index)
    scope = "b.source=? AND b.coin=? AND b.timeframe=? AND b.open_time BETWEEN ? AND ?"
    sargs = [source, coin, tf.value, lo, hi]
    key = "b.open_time=s.open_time"
    changed = " OR ".join(f"b.{c} IS DISTINCT FROM s.{c}" for c in VALUES)
    con.register("_ib", stage)
    own_tx = not in_transaction
    try:
        if own_tx:
            con.execute("BEGIN TRANSACTION")
        revised = con.execute(
            f"""INSERT INTO perp_intraday_revisions
                SELECT b.source, b.coin, b.timeframe, b.open_time, b.close_time, b.revision + 1,
                       b.open, b.high, b.low, b.close, b.volume, b.trades,
                       s.open, s.high, s.low, s.close, s.volume, s.trades,
                       b.updated_at, b.ingest_run_id, ?, ?
                FROM perp_intraday_bars b JOIN _ib s ON {key} WHERE {scope} AND ({changed})""",
            [seen.to_pydatetime(), run_id, *sargs],
        ).fetchone()[0]
        con.execute(
            f"""UPDATE perp_intraday_bars b SET open=s.open, high=s.high, low=s.low,
                close=s.close, volume=s.volume, trades=s.trades, revision=b.revision + 1,
                updated_at=?, ingest_run_id=?
                FROM _ib s WHERE {key} AND {scope} AND ({changed})""",
            [seen.to_pydatetime(), run_id, *sargs],
        )
        inserted = con.execute(
            f"""INSERT INTO perp_intraday_bars
                SELECT s.source, s.coin, s.timeframe, s.open_time, s.close_time, s.open, s.high,
                       s.low, s.close, s.volume, s.trades, ?, ?, ?, s.observed_live, 0, ?, ?
                FROM _ib s WHERE NOT EXISTS
                    (SELECT 1 FROM perp_intraday_bars b WHERE {key} AND {scope})""",
            [derivation, seen.to_pydatetime(), run_id, seen.to_pydatetime(), run_id, *sargs],
        ).fetchone()[0]
        dup = con.execute(
            f"SELECT count(*) - count(DISTINCT open_time) FROM perp_intraday_bars b WHERE {scope}",
            sargs,
        ).fetchone()[0]
        if dup:
            raise RuntimeError(f"{source}/{coin}/{tf}: {dup} duplicate bar(s); write rolled back")
        if own_tx:
            con.execute("COMMIT")
    except Exception:
        if own_tx:
            con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("_ib")
    return {"inserted": int(inserted), "revised": int(revised),
            "unchanged": len(stage) - int(inserted) - int(revised), "forming": forming}  # fmt: skip


def add_coverage(
    store: Store, source: str, coin: str, tf: Timeframe, start, end, at: datetime
) -> None:
    """Record that the provider was successfully asked for every bar opening in
    ``[start, end)`` after those bars closed. Overlapping/adjacent ranges are merged."""
    s, e = utc(start), utc(end)
    if e <= s:
        return
    con = store.con
    args = [source, coin, parse_timeframe(tf).value]
    rows = con.execute(
        "SELECT covered_from, covered_to FROM perp_intraday_coverage WHERE source=? AND coin=? "
        "AND timeframe=? AND covered_from <= ? AND covered_to >= ?",
        [*args, e.to_pydatetime(), s.to_pydatetime()],
    ).fetchall()
    lo = min([s, *[utc(r[0]) for r in rows]])
    hi = max([e, *[utc(r[1]) for r in rows]])
    for r in rows:
        con.execute(
            "DELETE FROM perp_intraday_coverage WHERE source=? AND coin=? AND timeframe=? "
            "AND covered_from=?",
            [*args, r[0]],
        )
    con.execute(
        "INSERT INTO perp_intraday_coverage VALUES (?,?,?,?,?,?)",
        [*args, lo.to_pydatetime(), hi.to_pydatetime(), utc(at).to_pydatetime()],
    )


# --------------------------------------------------------------------------- reads

BAR_COLUMNS = ["open_time", "close_time", *VALUES, "first_observed_at", "observed_live",
               "revision", "updated_at", "derivation"]  # fmt: skip


def load_bars(
    store: Store,
    source: str,
    coin: str,
    tf: Timeframe,
    start=None,
    end=None,
    *,
    known_at=None,
) -> pd.DataFrame:
    """Closed bars of ONE series with ``open_time`` in ``[start, end)``, oldest first.

    ``known_at``: point-in-time view. Only bars Prism had observed by then
    (``first_observed_at <= known_at``), each with the values it held at that instant
    (a later revision is undone using ``perp_intraday_revisions``). Without it: today's
    canonical values (final historical values), which is NOT what Prism knew at the time.
    """
    tf = parse_timeframe(tf)
    where = "b.source=? AND b.coin=? AND b.timeframe=?"
    args: list[Any] = [source, coin, tf.value]
    if start is not None:
        where += " AND b.open_time >= ?"
        args.append(utc(start).to_pydatetime())
    if end is not None:
        where += " AND b.open_time < ?"
        args.append(utc(end).to_pydatetime())
    if known_at is None:
        sql = f"SELECT {', '.join('b.' + c for c in BAR_COLUMNS)} FROM perp_intraday_bars b WHERE {where}"
    else:
        k = utc(known_at).to_pydatetime()
        pick = ", ".join(
            f"CASE WHEN r.revision IS NULL THEN b.{c} ELSE r.old_{c} END AS {c}" for c in VALUES
        )
        sql = f"""
            SELECT b.open_time, b.close_time, {pick}, b.first_observed_at, b.observed_live,
                   CASE WHEN r.revision IS NULL THEN b.revision ELSE r.revision - 1 END AS revision,
                   CASE WHEN r.revision IS NULL THEN b.updated_at ELSE r.old_observed_at END
                       AS updated_at, b.derivation
            FROM perp_intraday_bars b
            LEFT JOIN (SELECT *, row_number() OVER (PARTITION BY source, coin, timeframe, open_time
                                                   ORDER BY revision) AS rn
                       FROM perp_intraday_revisions WHERE revised_at > ?) r
              ON r.rn = 1 AND r.source=b.source AND r.coin=b.coin AND r.timeframe=b.timeframe
                 AND r.open_time=b.open_time
            WHERE {where} AND b.first_observed_at <= ?"""
        args = [k, *args, k]
    df = store.con.execute(sql + " ORDER BY open_time", args).df()
    for c in ("open_time", "close_time", "first_observed_at", "updated_at"):
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- coverage & gaps


@dataclass(frozen=True)
class SeriesPolicy:
    """What 'healthy' means for one venue's series (from config)."""

    live: bool
    retention_bars: int | None  # provider serves only the newest N bars (None: unlimited)
    stale_after: dict[str, pd.Timedelta]


def _runs(missing: pd.DatetimeIndex, step: pd.Timedelta) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous runs of missing open times as half-open [first_open, last_open + step)."""
    out: list[list[pd.Timestamp]] = []
    for t in missing:
        if out and t == out[-1][1]:
            out[-1][1] = t + step
        else:
            out.append([t, t + step])
    return [(a, b) for a, b in out]


def gaps(
    store: Store, source: str, coin: str, tf: Timeframe, policy: SeriesPolicy, now, since=None
) -> pd.DataFrame:
    """Missing grid intervals between the first stored bar and the newest closed bar.

    Category per missing run:
    - ``provider_missing``: Prism successfully asked the provider for it after it closed and
      none was served (exchange downtime / no trades), not a local failure;
    - ``recoverable``: never fetched after closing, and still inside the provider's retention
      (a local miss; ``market bars backfill`` fills it);
    - ``permanent``: never fetched and already outside the provider's retention.
    The tail after the newest stored bar is staleness (see ``coverage``), not a gap.
    """
    tf = parse_timeframe(tf)
    step = interval(tf)
    first_last = store.con.execute(
        "SELECT min(open_time), max(open_time) FROM perp_intraday_bars WHERE source=? AND coin=? "
        "AND timeframe=?",
        [source, coin, tf.value],
    ).fetchone()
    cols = ["start", "end", "bars", "category"]
    if first_last[0] is None:
        return pd.DataFrame(columns=cols)
    lo = utc(first_last[0]) if since is None else max(utc(first_last[0]), utc(since))
    hi = utc(first_last[1]) + step
    have = pd.DatetimeIndex(
        pd.to_datetime(
            store.con.execute(
                "SELECT open_time FROM perp_intraday_bars WHERE source=? AND coin=? AND "
                "timeframe=? AND open_time >= ? AND open_time < ?",
                [source, coin, tf.value, lo.to_pydatetime(), hi.to_pydatetime()],
            ).df()["open_time"],
            utc=True,
        )
    )
    missing = expected_opens(lo, hi, tf).difference(have)
    if missing.empty:
        return pd.DataFrame(columns=cols)
    covered = [
        (utc(a), utc(b))
        for a, b in store.con.execute(
            "SELECT covered_from, covered_to FROM perp_intraday_coverage WHERE source=? AND "
            "coin=? AND timeframe=? ORDER BY covered_from",
            [source, coin, tf.value],
        ).fetchall()
    ]
    floor = (
        None
        if policy.retention_bars is None
        else latest_closed_open(now, tf) - (policy.retention_bars - 1) * step
    )

    def category(t: pd.Timestamp) -> str:
        if any(a <= t < b for a, b in covered):
            return "provider_missing"
        return "recoverable" if floor is None or t >= floor else "permanent"

    rows = []
    for t in missing:
        c = category(t)
        if rows and rows[-1]["end"] == t and rows[-1]["category"] == c:
            rows[-1]["end"] = t + step
            rows[-1]["bars"] += 1
        else:
            rows.append({"start": t, "end": t + step, "bars": 1, "category": c})
    return pd.DataFrame(rows, columns=cols)


def coverage(
    store: Store, series: list[tuple[str, str, Timeframe, SeriesPolicy]], now
) -> pd.DataFrame:
    """One row per (venue, coin, timeframe): extent, expected vs actual count, duplicates /
    off-grid rows, gaps by category, revisions, newest bar age and a status.

    Status: NO DATA; STALE (live series whose newest closed bar is older than the timeframe's
    threshold); GAPS (recoverable/permanent missing bars); OK. A non-live (history-only)
    venue is HISTORICAL instead of being judged stale. Provider-missing bars are reported but
    are not a local failure."""
    now = utc(now)
    out = []
    for source, coin, tf, pol in series:
        tf = parse_timeframe(tf)
        step = interval(tf)
        r = store.con.execute(
            """SELECT count(*), count(DISTINCT open_time), min(open_time), max(open_time),
                      max(close_time), max(first_observed_at), sum(CASE WHEN revision > 0 THEN 1 ELSE 0 END),
                      sum(CASE WHEN close_time <> open_time + to_microseconds(?) THEN 1 ELSE 0 END),
                      sum(CASE WHEN epoch_us(open_time) % ? <> 0 THEN 1 ELSE 0 END),
                      sum(CASE WHEN observed_live THEN 1 ELSE 0 END),
                      sum(CASE WHEN volume = 0 THEN 1 ELSE 0 END)
               FROM perp_intraday_bars WHERE source=? AND coin=? AND timeframe=?""",
            [
                int(step.total_seconds() * 1e6),
                int(step.total_seconds() * 1e6),
                source,
                coin,
                tf.value,
            ],
        ).fetchone()
        (
            n,
            distinct,
            first,
            last,
            last_close,
            last_seen,
            revised,
            bad_close,
            off_grid,
            live_n,
            zero_v,
        ) = r
        row = {"venue": source, "coin": coin, "timeframe": tf.value, "rows": int(n),
               "first": None if first is None else utc(first), "last": None if last is None else utc(last),
               "expected": 0, "missing": 0, "provider_missing": 0, "recoverable": 0, "permanent": 0,
               "duplicates": int(n - distinct), "malformed": int((bad_close or 0) + (off_grid or 0)),
               "revised": int(revised or 0), "observed_live": int(live_n or 0), "zero_volume": int(zero_v or 0),
               "age_min": None, "last_observed": None if last_seen is None else utc(last_seen),
               "live": pol.live, "status": "NO DATA"}  # fmt: skip
        if n:
            row["expected"] = int((utc(last) - utc(first)) / step) + 1
            g = gaps(store, source, coin, tf, pol, now)
            for cat in ("provider_missing", "recoverable", "permanent"):
                row[cat] = int(g.loc[g["category"] == cat, "bars"].sum()) if len(g) else 0
            row["missing"] = row["provider_missing"] + row["recoverable"] + row["permanent"]
            age = now - utc(last_close)
            row["age_min"] = round(age.total_seconds() / 60, 1)
            stale = pol.live and age > pol.stale_after.get(tf.value, 3 * step)
            if stale:
                row["status"] = "STALE"
            elif row["duplicates"] or row["malformed"]:
                row["status"] = "INVALID"
            elif row["recoverable"] or row["permanent"]:
                row["status"] = "GAPS"
            else:
                row["status"] = "OK" if pol.live else "HISTORICAL"
        out.append(row)
    return pd.DataFrame(out)


def storage_footprint(store: Store) -> pd.DataFrame:
    """Row counts per table/venue/timeframe (for the storage report)."""
    return store.query(
        """SELECT source AS venue, timeframe, count(*) AS rows, count(DISTINCT coin) AS coins,
                  min(open_time) AS first, max(open_time) AS last
           FROM perp_intraday_bars GROUP BY 1, 2 ORDER BY 1, 2"""
    )
