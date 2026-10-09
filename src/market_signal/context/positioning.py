"""Positioning context: OI, funding, basis, long/short ratios → crowding and vulnerability.

**Descriptive research context only.** Nothing here is a signal, a veto or a trade, and no
consumer (forward, co-pilot, paper, incubation) reads it.

Terminology (enforced in output): every perp contract has one long and one short, so
aggregate open interest is never "mostly long". Skew is inferred only from funding, basis and
the venue's account/position ratios. Crowding labels end in ``_like`` because they describe
observable conditions, not anyone's intent; nothing claims manipulation or liquidation
targeting, and no liquidation level is derived from OI.

Venues are never merged: each venue's context is computed from its own series only.

Collection added in Phase 23 (data only):
* ``capture_hl_hourly``: a **fixed hourly** Hyperliquid OI/funding/basis capture
  (``context_hl_oi_hourly``, one row per coin per UTC hour, first capture in the hour wins).
  Old irregular ``perp_snapshots`` are untouched; the cutover is the first stored grid hour.
* ``update_binance_ratios``: Binance USD-M long/short account, top-trader position/account and
  taker buy/sell ratios (``context_ls_ratios``), rolling ~30-day backfill like Binance OI.
  Insert-only: the first stored value is kept (``ingested_at`` = Prism's first sighting).

Availability (``strict=True``, the snapshot default): a value counts only once Prism held it
(HL capture time; Binance ``ingested_at`` and the end of its statistics period). Research on
backfilled Binance history may use ``strict=False`` (period end + a conservative lag), which
is assumed-latency and must be labelled as such.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from market_signal.data.http import ProviderError, SchemaError
from market_signal.data.store import Store
from market_signal.models.domain import utcnow

CADENCE_VERSION = "hl_oi_fixed_hourly_v1"
POSITIONING_VERSION = "positioning_context_v1"
CROWDING_VERSION = "crowding_v1"
VULNERABILITY_VERSION = "vulnerability_v1"
BINANCE_LAG = timedelta(minutes=15)  # non-strict mode: period end + this
RATIO_ENDPOINTS = {
    "global_account": "/futures/data/globalLongShortAccountRatio",
    "top_account": "/futures/data/topLongShortAccountRatio",
    "top_position": "/futures/data/topLongShortPositionRatio",
    "taker_volume": "/futures/data/takerlongshortRatio",
}
STALE = {"hyperliquid": timedelta(hours=3), "binance": timedelta(hours=3)}
OI_Z_EXPANDING = 1.5
FUNDING_HIGH, FUNDING_LOW = 0.90, 0.10
TERMINOLOGY = ("Aggregate OI always has equal long and short size; 'crowded_*_like' is inferred "
               "only from funding/basis/ratio skew plus OI change, and describes conditions, "
               "not intent.")  # fmt: skip


# --------------------------------------------------------------------------- collection


def capture_hl_hourly(store: Store, coins: list[str], provider: Any, now: datetime | None = None
                      ) -> dict:  # fmt: skip
    """One fixed-grid capture per coin per UTC hour. Idempotent: a coin already captured in
    this hour is skipped, so retries and duplicate triggers cannot record twice."""
    t = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    hour = t.floor("h")
    have = set(store.con.execute("SELECT coin FROM context_hl_oi_hourly WHERE grid_hour=?",
                                 [hour.to_pydatetime()]).df()["coin"])  # fmt: skip
    need = [c for c in coins if c not in have]
    if not need:
        return {"status": "skipped", "grid_hour": hour.isoformat(), "note": "already captured"}
    run_id = store.start_run("hyperliquid", "context_hl_oi_hourly", "ALL",
                             {"grid_hour": hour.isoformat(), "coins": need})  # fmt: skip
    try:
        ctx = provider.perp_contexts()
    except ProviderError as exc:
        store.finish_run(run_id, status="failed", error=str(exc)[:500],
                         archived=store.archive_raw(provider.drain_raw(), run_id))  # fmt: skip
        return {"status": "failed", "grid_hour": hour.isoformat(), "error": str(exc)[:200]}
    at = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    if at.floor("h") != hour:  # the call straddled the hour: the capture belongs to no grid hour
        store.finish_run(run_id, status="failed", error="capture crossed the hour boundary",
                         archived=store.archive_raw(provider.drain_raw(), run_id))  # fmt: skip
        return {"status": "failed", "grid_hour": hour.isoformat(), "error": "crossed hour"}
    rows = ctx[ctx["coin"].isin(need)]
    cols = ["open_interest", "oi_notional", "mark_px", "oracle_px", "mid_px", "funding_rate",
            "premium", "impact_bid_px", "impact_ask_px", "day_ntl_vlm"]  # fmt: skip
    n = 0
    with store.transaction():
        for r in rows.to_dict("records"):
            vals = [None if r.get(c) is None or pd.isna(r.get(c)) else float(r[c]) for c in cols]
            store.con.execute(
                "INSERT INTO context_hl_oi_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [r["coin"], hour.to_pydatetime(), at.to_pydatetime(), *vals, CADENCE_VERSION, run_id],
            )  # fmt: skip
            n += 1
    store.finish_run(run_id, status="ok", rows_received=len(ctx), rows_written=n,
                     archived=store.archive_raw(provider.drain_raw(), run_id))  # fmt: skip
    missing = sorted(set(need) - set(rows["coin"]))
    return {"status": "ok", "grid_hour": hour.isoformat(), "captured_at": at.isoformat(),
            "written": n, "not_listed": missing}  # fmt: skip


def hl_cutover(store: Store) -> str | None:
    """First fixed-grid hour ever captured: the documented cadence cutover."""
    try:
        r = store.con.execute("SELECT min(grid_hour) FROM context_hl_oi_hourly").fetchone()
    except Exception:
        return None
    return None if not r or r[0] is None else pd.Timestamp(r[0]).tz_convert("UTC").isoformat()


def _ratio_rows(http, metric: str, symbol: str, period: str, stop_ms: int, end_ms: int,
                limit_max: int = 500, max_pages: int = 4) -> list[dict]:  # fmt: skip
    """Page backward by ``endTime`` (Binance returns the latest rows of a wide window)."""
    step = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}[period] * 1000
    out: list[dict] = []
    for _ in range(max_pages):
        limit = max(1, min(limit_max, -(-(end_ms - stop_ms) // step) + 1))
        page = http.get_json(RATIO_ENDPOINTS[metric], params={
            "symbol": symbol, "period": period, "endTime": end_ms, "limit": limit})  # fmt: skip
        if not isinstance(page, list):
            raise SchemaError(f"binance {metric}: expected a list")
        out += page
        if len(page) < limit:
            break
        oldest = min(int(r["timestamp"]) for r in page)
        if oldest <= stop_ms:
            break
        end_ms = oldest - 1
    return out


def parse_ratio(metric: str, rows: list[dict]) -> pd.DataFrame:
    cols = ["observed_at", "long_share", "short_share", "ratio", "buy_volume", "sell_volume"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    need = {"timestamp", "buySellRatio", "buyVol", "sellVol"} if metric == "taker_volume" else \
        {"timestamp", "longAccount", "shortAccount", "longShortRatio"}  # fmt: skip
    if not need <= set(df.columns):
        raise SchemaError(f"binance {metric}: missing {sorted(need - set(df.columns))}")
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else np.nan  # noqa: E731
    out = pd.DataFrame({
        "observed_at": pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True),
        "long_share": num("longAccount") if metric != "taker_volume" else np.nan,
        "short_share": num("shortAccount") if metric != "taker_volume" else np.nan,
        "ratio": num("buySellRatio") if metric == "taker_volume" else num("longShortRatio"),
        "buy_volume": num("buyVol") if metric == "taker_volume" else np.nan,
        "sell_volume": num("sellVol") if metric == "taker_volume" else np.nan,
    })  # fmt: skip
    return out.drop_duplicates("observed_at").sort_values("observed_at").reset_index(drop=True)


def update_binance_ratios(store: Store, symbols: dict[str, str], http, period: str = "1h",
                          history_days: float = 29.5, now: datetime | None = None) -> list[dict]:  # fmt: skip
    """Rolling backfill of the four ratio series per coin. Insert-only (first value kept)."""
    t = pd.Timestamp(now or utcnow()).tz_convert("UTC")
    floor = t - pd.Timedelta(days=history_days)
    out = []
    for coin, sym in sorted(symbols.items()):
        for metric in RATIO_ENDPOINTS:
            last = store.con.execute(
                "SELECT max(observed_at) FROM context_ls_ratios WHERE source='binance' AND coin=? "
                "AND metric=? AND period=?", [coin, metric, period]).fetchone()[0]  # fmt: skip
            stop = floor if last is None else max(floor, pd.Timestamp(last).tz_convert("UTC")
                                                  - pd.Timedelta(hours=3))  # fmt: skip
            run_id = store.start_run("binance", f"ls_ratio_{metric}_{period}", coin,
                                     {"symbol": sym, "stop": stop.isoformat()})  # fmt: skip
            try:
                rows = _ratio_rows(http, metric, sym, period, int(stop.value // 10**6),
                                   int(t.value // 10**6))  # fmt: skip
                df = parse_ratio(metric, rows)
            except ProviderError as exc:
                store.finish_run(run_id, status="failed", error=str(exc)[:300],
                                 archived=store.archive_raw(http.drain(), run_id))  # fmt: skip
                out.append(
                    {"coin": coin, "metric": metric, "status": "failed", "error": str(exc)[:120]}
                )
                continue
            n_before = store.con.execute("SELECT count(*) FROM context_ls_ratios").fetchone()[0]
            if len(df):
                stage = df.assign(source="binance", coin=coin, provider_symbol=sym, metric=metric,
                                  period=period, ingested_at=t.to_pydatetime(), run_id=run_id)  # fmt: skip
                store.con.register("_lsr", stage)
                try:
                    store.con.execute(
                        "INSERT INTO context_ls_ratios SELECT source, coin, provider_symbol, metric, "
                        "period, observed_at, long_share, short_share, ratio, buy_volume, "
                        "sell_volume, ingested_at, run_id FROM _lsr ON CONFLICT DO NOTHING")  # fmt: skip
                finally:
                    store.con.unregister("_lsr")
            n = store.con.execute("SELECT count(*) FROM context_ls_ratios").fetchone()[0] - n_before
            store.finish_run(run_id, status="ok", rows_received=len(df), rows_written=n,
                             archived=store.archive_raw(http.drain(), run_id))  # fmt: skip
            out.append(
                {"coin": coin, "metric": metric, "status": "ok", "received": len(df), "new": n}
            )
    return out


# --------------------------------------------------------------------------- loading (as-of)


def _hl_obs(store: Store, coin: str, t: pd.Timestamp, days: int) -> pd.DataFrame:
    """Hyperliquid captures known at ``t`` (fixed grid + legacy irregular snapshots)."""
    lo = (t - pd.Timedelta(days=days)).to_pydatetime()
    q = """
        SELECT captured_at AS obs_at, open_interest, oi_notional, mark_px, oracle_px, funding_rate,
               premium, 'hourly' AS kind FROM context_hl_oi_hourly
        WHERE coin=? AND captured_at <= ? AND captured_at >= ?
        UNION ALL
        SELECT snapshot_at, open_interest, oi_notional, mark_px, oracle_px, funding_rate, premium,
               'snapshot' FROM perp_snapshots
        WHERE coin=? AND source='hyperliquid' AND snapshot_at <= ? AND snapshot_at >= ?
        ORDER BY obs_at"""
    df = store.con.execute(q, [coin, t.to_pydatetime(), lo, coin, t.to_pydatetime(), lo]).df()
    df = df.rename(columns={"obs_at": "at"})
    if not df.empty:
        df["at"] = pd.to_datetime(df["at"], utc=True)
        df = df.drop_duplicates("at", keep="first")
    return df


def _bn_oi(store: Store, coin: str, t: pd.Timestamp, days: int, strict: bool) -> pd.DataFrame:
    lo = (t - pd.Timedelta(days=days)).to_pydatetime()
    df = store.con.execute(
        "SELECT observed_at, period, open_interest, oi_notional, ingested_at FROM perp_oi_history "
        "WHERE source='binance' AND coin=? AND period='1h' AND observed_at >= ? AND observed_at <= ? "
        "ORDER BY observed_at", [coin, lo, t.to_pydatetime()]).df()  # fmt: skip
    if df.empty:
        return df
    for c in ("observed_at", "ingested_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    avail = df["observed_at"] + pd.Timedelta(hours=1) + (pd.Timedelta(0) if strict else BINANCE_LAG)
    if strict:
        avail = np.maximum(avail, df["ingested_at"])
    # filter by WHEN PRISM KNEW it; index the series by MARKET time (period end), so a late
    # backfill makes the whole known history usable, never values Prism did not yet hold
    df["at"] = df["observed_at"] + pd.Timedelta(hours=1)
    return df[avail <= t].reset_index(drop=True)


def _ratios(store: Store, coin: str, t: pd.Timestamp, days: int, strict: bool) -> pd.DataFrame:
    lo = (t - pd.Timedelta(days=days)).to_pydatetime()
    df = store.con.execute(
        "SELECT metric, observed_at, long_share, short_share, ratio, buy_volume, sell_volume, "
        "ingested_at, period FROM context_ls_ratios WHERE source='binance' AND coin=? AND "
        "observed_at >= ? AND observed_at <= ? ORDER BY observed_at",
        [coin, lo, t.to_pydatetime()]).df()  # fmt: skip
    if df.empty:
        return df
    for c in ("observed_at", "ingested_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    end = df["observed_at"] + pd.Timedelta(
        hours=1
    )  # treated as period-start labelled (conservative)
    avail = np.maximum(end, df["ingested_at"]) if strict else end + BINANCE_LAG
    df["at"] = end  # market time; availability only filters (see ``_bn_oi``)
    return df[avail <= t].reset_index(drop=True)


def _funding(store: Store, coin: str, source: str, t: pd.Timestamp, days: int) -> pd.DataFrame:
    lo = (t - pd.Timedelta(days=days)).to_pydatetime()
    df = store.con.execute(
        "SELECT time, funding_rate, available_at FROM perp_funding WHERE coin=? AND source=? "
        "AND time >= ? AND available_at <= ? ORDER BY time",
        [coin, source, lo, t.to_pydatetime()]).df()  # fmt: skip
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


# --------------------------------------------------------------------------- features


def _value_at(df: pd.DataFrame, col: str, when: pd.Timestamp, tol: pd.Timedelta) -> float | None:
    """Newest value at or before ``when`` and no older than ``tol`` (by elapsed time)."""
    if df.empty:
        return None
    s = df[df["at"] <= when]
    if s.empty or when - s["at"].iloc[-1] > tol or pd.isna(s[col].iloc[-1]):
        return None
    return float(s[col].iloc[-1])


def _pct(x: float | None, hist: pd.Series) -> float | None:
    h = hist.dropna()
    if x is None or len(h) < 20:
        return None
    return float((h < x).mean() + 0.5 * (h == x).mean())


def _oi_block(df: pd.DataFrame, t: pd.Timestamp, stale: pd.Timedelta, step: pd.Timedelta) -> dict:
    """OI level/changes/percentile/z from one venue's observations (base units)."""
    cur = _value_at(df, "open_interest", t, stale)
    out: dict[str, Any] = {"open_interest": cur, "oi_notional": _value_at(df, "oi_notional", t, stale),
                           "age_minutes": None if df.empty else round((t - df["at"].iloc[-1]).total_seconds() / 60, 1)}  # fmt: skip
    for h in (1, 4, 24):
        then = _value_at(
            df, "open_interest", t - pd.Timedelta(hours=h), max(step, pd.Timedelta(hours=h) * 0.25)
        )
        out[f"oi_change_{h}h_pct"] = None if cur is None or not then else (cur / then - 1) * 100
    # distribution of 24h changes over the window, measured by elapsed time on a 1h grid
    if not df.empty and cur is not None:
        grid = pd.date_range(df["at"].iloc[0].ceil("h"), t.floor("h"), freq="h")
        s = df.set_index("at")["open_interest"].sort_index()
        idx = s.index.searchsorted(grid, side="right") - 1
        ok = idx >= 0
        vals = pd.Series(np.nan, index=grid)
        ages = pd.Series(np.nan, index=grid)
        vals[ok] = s.to_numpy()[idx[ok]]
        ages[ok] = (grid[ok] - s.index[idx[ok]]).total_seconds()
        vals[ages > stale.total_seconds()] = np.nan
        ch = (vals / vals.shift(24) - 1) * 100
        prior = ch.iloc[:-1].dropna()
        c24 = out["oi_change_24h_pct"]
        out["oi_level_pct_30d"] = _pct(cur, vals)
        sd = prior.std()
        out["oi_change_24h_z"] = (
            None if c24 is None or len(prior) < 48 or not sd else float((c24 - prior.mean()) / sd)
        )
        out["oi_history_hours"] = int(vals.notna().sum())
    return out


def _funding_block(f: pd.DataFrame, t: pd.Timestamp, periods_per_year: int) -> dict:
    if f.empty:
        return {"funding_24h_mean": None, "funding_pct_90d": None, "funding_annualised": None}
    s = f.set_index("time")["funding_rate"].astype(float)
    recent = s[s.index > t - pd.Timedelta(hours=24)]
    lvl = float(recent.mean()) if len(recent) else None
    daily = s.rolling("24h").mean().resample("1D").last().dropna()
    return {"funding_24h_mean": lvl, "funding_pct_90d": _pct(lvl, daily.iloc[-90:]),
            "funding_annualised": None if lvl is None else lvl * periods_per_year,
            "funding_settlements_24h": len(recent)}  # fmt: skip


def crowding(block: dict) -> dict:
    """``crowding_v1``: descriptive leverage + skew state from one venue's inputs."""
    z, c24 = block.get("oi_change_24h_z"), block.get("oi_change_24h_pct")
    fp = block.get("funding_pct_90d")
    lsp = block.get("ls_account_pct_30d")
    evidence = []
    if z is not None:
        lev = (
            "expanding"
            if z >= OI_Z_EXPANDING
            else "contracting"
            if z <= -OI_Z_EXPANDING
            else "stable"
        )
        evidence.append(f"OI 24h change z={z:+.2f}")
    elif c24 is not None:
        lev = "expanding" if c24 >= 10 else "contracting" if c24 <= -10 else "stable"
        evidence.append(f"OI 24h change {c24:+.1f}% (no z: short history)")
    else:
        lev = "unknown"
    longs = [
        fp is not None and fp >= FUNDING_HIGH,
        lev == "expanding",
        lsp is not None and lsp >= FUNDING_HIGH,
    ]
    shorts = [
        fp is not None and fp <= FUNDING_LOW,
        lev == "expanding",
        lsp is not None and lsp <= FUNDING_LOW,
    ]
    skew_inputs = sum(x is not None for x in (fp, lsp))
    if fp is not None:
        evidence.append(f"funding pct(90d)={fp:.2f}")
    if lsp is not None:
        evidence.append(f"long/short account ratio pct(30d)={lsp:.2f}")
    # a skew label needs a skew measure (funding or ratio) AND two agreeing conditions
    if skew_inputs == 0:
        skew = "unknown"
    elif sum(longs) >= 2 and (longs[0] or longs[2]):
        skew = "crowded_long_like"
    elif sum(shorts) >= 2 and (shorts[0] or shorts[2]):
        skew = "crowded_short_like"
    else:
        skew = "neutral"
    return {"version": CROWDING_VERSION, "leverage": lev, "skew": skew, "evidence": evidence,
            "terminology": TERMINOLOGY}  # fmt: skip


def vulnerability(block: dict, catalyst_24h: bool | None) -> dict:
    """``vulnerability_v1``: which observable ingredients of positioning pain are present.
    Counts only; never a trade, never a liquidation level."""
    fp, z = block.get("funding_pct_90d"), block.get("oi_change_24h_z")
    px, lsp = block.get("price_change_24h_pct"), block.get("ls_account_pct_30d")
    rising = z is not None and z >= OI_Z_EXPANDING

    def side(sign: int) -> dict:
        comp = {
            "funding_extreme": None if fp is None else (fp >= FUNDING_HIGH if sign > 0 else fp <= FUNDING_LOW),
            "oi_rapid_growth": None if z is None else rising,
            "price_failing_to_follow": None if px is None or z is None else rising and (px * sign) <= 0.5,
            "ratio_skew": None if lsp is None else (lsp >= FUNDING_HIGH if sign > 0 else lsp <= FUNDING_LOW),
            "catalyst_within_24h": catalyst_24h,
        }  # fmt: skip
        # Comparisons with NumPy-backed prices yield numpy.bool_, which JSON rejects.
        comp = {k: None if v is None else bool(v) for k, v in comp.items()}
        avail = [v for v in comp.values() if v is not None]
        score = sum(bool(v) for v in avail)
        level = (
            "unknown"
            if len(avail) < 3
            else "high"
            if score >= 3
            else "elevated"
            if score == 2
            else "low"
        )
        return {"components": comp, "present": score, "available": len(avail), "level": level}

    return {"version": VULNERABILITY_VERSION, "long_side": side(+1), "short_side": side(-1),
            "note": "ingredients of positioning stress only; not a direction or a trade"}  # fmt: skip


def positioning_context(store: Store, coin: str, t: datetime, *, strict: bool = True,
                        catalyst_24h: bool | None = None, window_days: int = 30) -> dict:  # fmt: skip
    """Per-venue positioning context known at ``t``. Venues are never merged."""
    t = pd.Timestamp(t).tz_convert("UTC")
    coin = coin.upper()
    venues: dict[str, dict] = {}
    # Hyperliquid
    hl = _hl_obs(store, coin, t, window_days)
    blk = (
        _oi_block(hl, t, STALE["hyperliquid"], pd.Timedelta(hours=1))
        if not hl.empty
        else {"open_interest": None}
    )
    if not hl.empty:
        last = hl.iloc[-1]
        stale = t - last["at"] > STALE["hyperliquid"]
        blk |= {"mark_px": None if stale else last["mark_px"],
                "basis_mark_vs_oracle_bps": None if stale or not last["oracle_px"] else
                (last["mark_px"] / last["oracle_px"] - 1) * 1e4,
                "predicted_funding_hourly": None if stale else last["funding_rate"],
                "stale": bool(stale), "source_kinds": sorted(set(hl["kind"]))}  # fmt: skip
        then = _value_at(hl, "mark_px", t - pd.Timedelta(hours=24), pd.Timedelta(hours=6))
        blk["price_change_24h_pct"] = (
            None if stale or not then else (last["mark_px"] / then - 1) * 100
        )
    blk |= _funding_block(_funding(store, coin, "hyperliquid", t, 120), t, 8760)
    blk["crowding"] = crowding(blk)
    blk["vulnerability"] = vulnerability(blk, catalyst_24h)
    venues["hyperliquid"] = blk
    # Binance (OI + ratios + funding where stored)
    bn = _bn_oi(store, coin, t, window_days, strict)
    rat = _ratios(store, coin, t, window_days, strict)
    if not bn.empty or not rat.empty:
        b = (
            _oi_block(bn, t, STALE["binance"], pd.Timedelta(hours=1))
            if not bn.empty
            else {"open_interest": None}
        )
        for metric in ("global_account", "top_account", "top_position", "taker_volume"):
            r = rat[rat["metric"] == metric] if not rat.empty else rat
            if r is None or r.empty or t - r["at"].iloc[-1] > STALE["binance"]:
                b[metric] = None
                continue
            last = r.iloc[-1]
            b[metric] = {"ratio": float(last["ratio"]), "observed_at": last["observed_at"].isoformat(),
                         **({"long_share": float(last["long_share"]), "short_share": float(last["short_share"])}
                            if metric != "taker_volume" else
                            {"buy_volume": float(last["buy_volume"]), "sell_volume": float(last["sell_volume"])})}  # fmt: skip
        ga = rat[rat["metric"] == "global_account"] if not rat.empty else rat
        b["ls_account_pct_30d"] = (None if b.get("global_account") is None else
                                   _pct(b["global_account"]["ratio"], ga["ratio"].iloc[:-1]))  # fmt: skip
        b |= _funding_block(_funding(store, coin, "binance", t, 120), t, 1095)
        b["crowding"] = crowding(b)
        b["vulnerability"] = vulnerability(b, catalyst_24h)
        b["availability"] = "strict_first_seen" if strict else "assumed_latency"
        venues["binance"] = b
    return {"version": POSITIONING_VERSION, "coin": coin, "as_of": t.isoformat(), "venues": venues,
            "terminology": TERMINOLOGY}  # fmt: skip


def binance_ratio_symbols(settings) -> dict[str, str]:
    from market_signal.perps.open_interest import oi_config

    return oi_config(settings).symbols


__all__ = ["CADENCE_VERSION", "capture_hl_hourly", "crowding", "hl_cutover", "parse_ratio",
           "positioning_context", "update_binance_ratios", "vulnerability"]  # fmt: skip
