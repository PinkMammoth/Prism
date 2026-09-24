"""Phase 8: paper portfolio, journal analytics, alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import pytest

from market_signal.models.domain import Timeframe
from market_signal.portfolio.alerts import (
    ConsoleNotifier,
    evaluate_alerts,
    evaluate_rule,
    validate_rule,
)
from market_signal.portfolio.book import (
    NewPosition,
    PortfolioError,
    add_journal,
    close_position,
    journal_stats,
    open_position,
    positions_frame,
)
from tests.conftest import make_daily_crypto


def _load_btc(store, n=60):
    df = make_daily_crypto(n, start="2026-01-01")
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    store.upsert_bars("BTC", Timeframe.D1, "coinbase", df, "r")
    return df


def test_trade_requires_invalidation_below_entry(settings, store):
    with pytest.raises(PortfolioError):
        open_position(store, settings, NewPosition("BTC", "TRADE", date(2026, 1, 5), 100.0, 1.0))
    with pytest.raises(PortfolioError):
        open_position(
            store,
            settings,
            NewPosition("BTC", "TRADE", date(2026, 1, 5), 100.0, 1.0, invalidation_price=110),
        )
    pid = open_position(
        store, settings, NewPosition("BTC", "INVESTMENT", date(2026, 1, 5), 100.0, 1.0)
    )
    assert pid.startswith("pos_")


def test_close_computes_return_mae_mfe_from_bars(settings, store):
    df = _load_btc(store)
    entry = float(df.loc[4, "open"])
    pid = open_position(store, settings, NewPosition("BTC", "TRADE", date(2026, 1, 5), entry, 2.0, fees=1.0,
                                                     invalidation_price=entry * 0.9, portfolio_equity=10_000, setup="quality_pullback",
                                                     followed_system=True))  # fmt: skip
    exit_px = float(df.loc[20, "close"])
    ret = close_position(store, settings, pid, date(2026, 1, 21), exit_px, "time", "ok", "patience")
    assert ret == pytest.approx((exit_px * 2) / (entry * 2 + 1) - 1)
    row = store.query("SELECT * FROM positions WHERE position_id=?", [pid]).iloc[0]
    w = df.iloc[4:21]
    assert row["mae"] == pytest.approx(w["low"].min() / entry - 1)
    assert row["mfe"] == pytest.approx(w["high"].max() / entry - 1)
    assert row["risk_at_entry"] == pytest.approx(entry * 0.1 * 2 / 10_000)
    with pytest.raises(PortfolioError):
        close_position(store, settings, pid, date(2026, 1, 22), exit_px, "time")


def test_journal_stats_answers_the_two_questions(settings, store):
    _load_btc(store)
    for i, (setup, followed, exit_mult) in enumerate([("quality_pullback", True, 1.1), ("quality_pullback", True, 0.95),
                                                      ("breakout_retest", False, 0.9), ("breakout_retest", False, 0.92)]):  # fmt: skip
        pid = open_position(store, settings, NewPosition("BTC", "TRADE", date(2026, 1, 5 + i), 100.0, 1.0,
                                                         invalidation_price=95.0, setup=setup, followed_system=followed))  # fmt: skip
        close_position(store, settings, pid, date(2026, 1, 20), 100.0 * exit_mult, "time")
    add_journal(store, None, "note", "general note", None)
    st = journal_stats(store)
    by = st["by_setup"].set_index("setup")
    assert by.loc["quality_pullback", "hit_rate"] == 0.5
    assert by.loc["breakout_retest", "avg_r"] == pytest.approx(
        ((0.9 - 1) / 0.05 + (0.92 - 1) / 0.05) / 2
    )
    disc = st["by_discipline"].set_index("followed")
    assert disc.loc["violated", "trades"] == 2 and disc.loc["violated", "hit_rate"] == 0.0
    assert st["violations_by_setup"] == {"breakout_retest": 2}


def test_positions_frame_marks_open_positions(settings, store):
    df = _load_btc(store)
    open_position(
        store,
        settings,
        NewPosition("BTC", "TRADE", date(2026, 1, 5), 100.0, 1.0, invalidation_price=90.0),
    )
    pf = positions_frame(store, settings)
    last = float(df["close"].iloc[-1])
    assert pf.iloc[0]["mark"] == pytest.approx(last)
    assert pf.iloc[0]["unrealised_return"] == pytest.approx(last / 100 - 1)
    assert pf.iloc[0]["to_invalidation"] == pytest.approx(last / 90 - 1)


# --------------------------------------------------------------------------- alerts


@dataclass
class FakeAssessment:
    symbol: str
    price: float
    score: float | None
    status: str
    status_text: str = ""
    zones: dict = field(default_factory=dict)


def test_rule_validation():
    with pytest.raises(ValueError):
        validate_rule({"kind": "nope"})
    with pytest.raises(ValueError):
        validate_rule({"kind": "price_below", "symbol": "HYPE"})


def test_rule_evaluation_kinds():
    a = [FakeAssessment("HYPE", 55.0, 78, "WAIT", zones={"accumulate": (50.0, 57.0)}),
         FakeAssessment("MSFT", 400.0, 60, "ACTIONABLE", "in zone")]  # fmt: skip
    assert (
        evaluate_rule({"kind": "price_below", "symbol": "HYPE", "price": 56}, a, {})[0][0] == "HYPE"
    )
    assert evaluate_rule({"kind": "price_above", "symbol": "HYPE", "price": 56}, a, {}) == []
    assert [s for s, _ in evaluate_rule({"kind": "score_gte", "score": 75}, a, {})] == ["HYPE"]
    assert evaluate_rule({"kind": "enters_zone", "symbol": "HYPE", "zone": "accumulate"}, a, {})
    changed = evaluate_rule(
        {"kind": "status_change", "to": ["ACTIONABLE"]}, a, {"MSFT": "WATCH", "HYPE": "WAIT"}
    )
    assert [s for s, _ in changed] == ["MSFT"]
    assert (
        evaluate_rule({"kind": "status_change", "to": ["ACTIONABLE"]}, a, {"MSFT": "ACTIONABLE"})
        == []
    )


def test_alerts_fire_once_within_cooldown(settings, store):
    @dataclass
    class Scan:
        scan_id: str
        assessments: list

    scan = Scan(
        "scan_x", [FakeAssessment("HYPE", 50.0, 80, "WAIT", zones={"accumulate": (45.0, 55.0)})]
    )
    n = ConsoleNotifier()
    first = evaluate_alerts(store, settings, scan, [n])
    assert any("HYPE" in m for m in first) and n.sent
    second = evaluate_alerts(store, settings, scan, [n])
    assert second == []  # cooldown suppresses duplicates
    assert store.query("SELECT count(*) c FROM alert_events").iloc[0]["c"] == len(first)
