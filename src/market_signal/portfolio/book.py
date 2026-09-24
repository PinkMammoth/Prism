"""Paper (and optional real/manual) positions + decision journal.

No broker integration: positions are entered manually. Returns, MAE and MFE are computed
from stored daily bars in the RAW price basis (what you actually paid/received); if bars
are missing for the holding period, MAE/MFE are left empty rather than estimated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from market_signal.config import Settings
from market_signal.data.prices import PriceBasis, load_bars
from market_signal.data.store import Store, new_id
from market_signal.models.domain import PositionKind, Timeframe, utcnow


class PortfolioError(ValueError):
    pass


@dataclass
class NewPosition:
    symbol: str
    kind: str
    entry_date: date
    entry_price: float
    quantity: float
    book: str = "paper"
    setup: str | None = None
    fees: float = 0.0
    thesis: str = ""
    evidence: str = ""
    horizon: str = ""
    invalidation_price: float | None = None
    invalidation_note: str = ""
    score_at_entry: float | None = None
    regime_at_entry: str | None = None
    followed_system: bool | None = None
    portfolio_equity: float | None = None  # to compute risk_at_entry


def open_position(store: Store, settings: Settings, p: NewPosition) -> str:
    settings.asset(p.symbol)  # validates symbol
    if p.book not in ("paper", "real"):
        raise PortfolioError("book must be 'paper' or 'real'")
    kind = PositionKind(p.kind.upper())
    if p.entry_price <= 0 or p.quantity <= 0:
        raise PortfolioError("entry_price and quantity must be positive")
    if kind == PositionKind.TRADE:
        if p.invalidation_price is None:
            raise PortfolioError("a TRADE needs an invalidation price (technical stop)")
        if p.invalidation_price >= p.entry_price:
            raise PortfolioError("invalidation must be below entry for a long trade")
    risk = None
    if p.invalidation_price is not None and p.portfolio_equity:
        risk = (p.entry_price - p.invalidation_price) * p.quantity / p.portfolio_equity
    pid = new_id("pos_")
    now = utcnow()
    store.con.execute(
        """INSERT INTO positions (position_id, book, kind, symbol, setup, entry_date, entry_price, quantity, fees,
           thesis, evidence, horizon, invalidation_price, invalidation_note, score_at_entry, regime_at_entry,
           risk_at_entry, followed_system, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [pid, p.book, kind.value, p.symbol.upper(), p.setup, p.entry_date, p.entry_price, p.quantity, p.fees,
         p.thesis, p.evidence, p.horizon, p.invalidation_price, p.invalidation_note, p.score_at_entry,
         p.regime_at_entry, risk, p.followed_system, now, now],
    )  # fmt: skip
    add_journal(store, pid, "entry", f"WHY: {p.thesis}\nEVIDENCE: {p.evidence}\nINVALIDATION: {p.invalidation_note or p.invalidation_price}",
                p.followed_system)  # fmt: skip
    return pid


def excursions(
    store: Store,
    settings: Settings,
    symbol: str,
    entry_date: date,
    end: date | None,
    entry_price: float,
) -> tuple[float | None, float | None, float | None]:
    """(MAE, MFE, last close) from raw daily bars in [entry_date, end]."""
    asset = settings.asset(symbol)
    try:
        bars = load_bars(store, asset, Timeframe.D1, PriceBasis.RAW)
    except ValueError:
        return None, None, None
    if bars.empty:
        return None, None, None
    d = (
        bars["ts"]
        .dt.tz_convert("America/New_York" if asset.calendar.value == "nyse" else "UTC")
        .dt.date
    )
    m = d >= entry_date
    if end is not None:
        m &= d <= end
    w = bars[m]
    if w.empty:
        return None, None, float(bars["close"].iloc[-1])
    return (
        float(w["low"].min() / entry_price - 1),
        float(w["high"].max() / entry_price - 1),
        float(bars["close"].iloc[-1]),
    )


def close_position(
    store: Store,
    settings: Settings,
    position_id: str,
    exit_date: date,
    exit_price: float,
    exit_reason: str,
    outcome: str = "",
    lessons: str = "",
    exit_fees: float = 0.0,
) -> float:
    row = store.query("SELECT * FROM positions WHERE position_id=?", [position_id])
    if row.empty:
        raise PortfolioError(f"unknown position {position_id}")
    r = row.iloc[0]
    if pd.notna(r["exit_date"]):
        raise PortfolioError("position already closed")
    if exit_price <= 0:
        raise PortfolioError("exit price must be positive")
    entry_date = pd.Timestamp(r["entry_date"]).date()
    if exit_date < entry_date:
        raise PortfolioError("exit date before entry date")
    cost = r["entry_price"] * r["quantity"] + (r["fees"] or 0)
    proceeds = exit_price * r["quantity"] - exit_fees
    ret = proceeds / cost - 1
    mae, mfe, _ = excursions(
        store, settings, r["symbol"], entry_date, exit_date, float(r["entry_price"])
    )
    store.con.execute(
        """UPDATE positions SET exit_date=?, exit_price=?, exit_reason=?, realised_return=?, mae=?, mfe=?,
           outcome=?, lessons=?, fees=fees+?, updated_at=? WHERE position_id=?""",
        [
            exit_date,
            exit_price,
            exit_reason,
            ret,
            mae,
            mfe,
            outcome,
            lessons,
            exit_fees,
            utcnow(),
            position_id,
        ],
    )
    add_journal(
        store,
        position_id,
        "exit",
        f"EXIT ({exit_reason}) at {exit_price}: {outcome}\nLESSONS: {lessons}",
        None,
    )
    return float(ret)


def add_journal(
    store: Store,
    position_id: str | None,
    kind: str,
    text: str,
    followed_system: bool | None,
    tags: list[str] | None = None,
) -> str:
    eid = new_id("jr_")
    store.con.execute(
        "INSERT INTO journal_entries VALUES (?,?,?,?,?,?,?)",
        [eid, position_id, utcnow(), kind, text, followed_system, json.dumps(tags or [])],
    )
    return eid


def positions_frame(store: Store, settings: Settings, book: str | None = None) -> pd.DataFrame:
    sql = (
        "SELECT * FROM positions" + (" WHERE book=?" if book else "") + " ORDER BY entry_date DESC"
    )
    df = store.query(sql, [book] if book else [])
    if df.empty:
        return df
    latest = latest_scores(store)
    marks, unreal, cur_score, dist_inv, mae_now, mfe_now = [], [], [], [], [], []
    for _, r in df.iterrows():
        open_ = pd.isna(r["exit_date"])
        mae, mfe, last = excursions(store, settings, r["symbol"], pd.Timestamp(r["entry_date"]).date(),
                                    None if open_ else pd.Timestamp(r["exit_date"]).date(), float(r["entry_price"]))  # fmt: skip
        marks.append(last if open_ else r["exit_price"])
        unreal.append((last / r["entry_price"] - 1) if open_ and last else np.nan)
        cur_score.append(latest.get(r["symbol"]))
        inv = r["invalidation_price"]
        dist_inv.append((last / inv - 1) if open_ and last and pd.notna(inv) and inv else np.nan)
        mae_now.append(mae if open_ else r["mae"])
        mfe_now.append(mfe if open_ else r["mfe"])
    df["mark"] = marks
    df["cost_basis"] = df["entry_price"] * df["quantity"] + df["fees"].fillna(0)
    df["unrealised_return"] = unreal
    df["current_score"] = cur_score
    df["to_invalidation"] = dist_inv
    df["mae_to_date"], df["mfe_to_date"] = mae_now, mfe_now
    df["status"] = np.where(df["exit_date"].isna(), "open", "closed")
    return df


def latest_scores(store: Store) -> dict[str, float]:
    try:
        df = store.query(
            """SELECT symbol, score FROM scan_results WHERE scan_id =
               (SELECT scan_id FROM scan_runs ORDER BY created_at DESC LIMIT 1)"""
        )
    except Exception:
        return {}
    return {r["symbol"]: r["score"] for _, r in df.iterrows()}


def journal_stats(store: Store) -> dict[str, Any]:
    """'Which setups am I good at?' and 'where do I break my own rules?'"""
    closed = store.query("SELECT * FROM positions WHERE exit_date IS NOT NULL")
    out: dict[str, Any] = {"closed": len(closed)}
    if closed.empty:
        return out
    closed["setup"] = closed["setup"].fillna("discretionary")
    closed["r_multiple"] = [
        (r["realised_return"] / ((r["entry_price"] - r["invalidation_price"]) / r["entry_price"]))
        if pd.notna(r["invalidation_price"]) and r["entry_price"] > r["invalidation_price"]
        else np.nan
        for _, r in closed.iterrows()
    ]
    by_setup = closed.groupby(["setup", "kind"]).agg(
        trades=("position_id", "count"), hit_rate=("realised_return", lambda x: float((x > 0).mean())),
        avg_return=("realised_return", "mean"), median_return=("realised_return", "median"),
        avg_r=("r_multiple", "mean"), worst_mae=("mae", "min"),
    ).reset_index()  # fmt: skip
    out["by_setup"] = by_setup
    rules = closed.assign(
        followed=closed["followed_system"]
        .map({True: "followed", False: "violated"})
        .fillna("unrecorded")
    )
    out["by_discipline"] = rules.groupby("followed").agg(
        trades=("position_id", "count"), hit_rate=("realised_return", lambda x: float((x > 0).mean())),
        avg_return=("realised_return", "mean"),
    ).reset_index()  # fmt: skip
    out["violations_by_setup"] = (
        rules[rules["followed"] == "violated"].groupby("setup").size().to_dict()
    )
    out["exit_reasons"] = closed["exit_reason"].value_counts().to_dict()
    return out
