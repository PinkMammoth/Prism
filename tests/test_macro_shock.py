"""macro_shock: point-in-time event dates (no look-ahead), the hot-CPI calculation on ALFRED
vintages, entry timing, the basket verdict basis, and the no-events path."""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.config import get_settings
from market_signal.perps.backtest import (
    PerpCosts,
    PerpInput,
    basket_asset_events,
    perp_asset_events,
)
from market_signal.perps.macro_events import (
    attach_macro_events,
    cpi_releases,
    dgs2_moves,
    event_table,
)
from market_signal.perps.research import run_perp_research
from market_signal.perps.strategies import get_strategy

COSTS = PerpCosts(4.5, 3.0, 10.0)


def dgs2_rows(start="2022-01-03", n=700, seed=0, shocks=None) -> pd.DataFrame:
    """FRED-style DGS2 rows, stored like the codebase does: available_at = obs_date + 2 days."""
    d = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(seed)
    chg = rng.normal(0, 0.04, n)
    for day, bp in (shocks or {}).items():
        chg[d.get_loc(pd.Timestamp(day))] = bp / 100
    return pd.DataFrame({"series_id": "DGS2", "obs_date": d.date, "value": 2 + np.cumsum(chg),
                         "realtime_start": d.date, "available_at": d.tz_localize("UTC") + pd.Timedelta(days=2),
                         "pit_method": "market_close"})  # fmt: skip


def cpi_vintage(levels: dict[str, float], release: str) -> list[dict]:
    rel = pd.Timestamp(release)
    return [{"series_id": "CPIAUCSL", "obs_date": pd.Timestamp(m).date(), "value": v,
             "realtime_start": rel.date(), "available_at": rel.tz_localize("UTC") + pd.Timedelta(days=1),
             "pit_method": "vintage"} for m, v in levels.items()]  # fmt: skip


def cpi_history(months: pd.DatetimeIndex, mom: float = 0.002) -> dict[str, float]:
    return {str(m.date()): 300 * (1 + mom) ** i for i, m in enumerate(months)}


# --------------------------------------------------------------------------- hot CPI on vintages


def test_hot_cpi_uses_the_vintage_known_at_release_not_later_revisions():
    months = pd.date_range("2023-01-01", "2024-01-01", freq="MS")  # 13 months → 12 m/m before Feb
    base = cpi_history(months)  # 0.2% every month
    feb = base["2024-01-01"] * 1.005  # February prints +0.5%: hot vs a 0.2% trailing mean
    rows = cpi_vintage({**base}, "2024-02-13")  # history up to January, published 2024-02-13
    rows += cpi_vintage({"2024-02-01": feb}, "2024-03-12")
    # April release revises February down to +0.1%; must NOT change the March-12 reading
    rows += cpi_vintage({"2024-02-01": base["2024-01-01"] * 1.001,
                         "2024-03-01": base["2024-01-01"] * 1.001 * 1.002}, "2024-04-10")  # fmt: skip
    rel = cpi_releases(pd.DataFrame(rows)).set_index("release_date")
    feb_rel = rel.loc[pd.Timestamp("2024-03-12")]
    assert np.isclose(feb_rel["mom"], 0.005) and np.isclose(feb_rel["trailing_avg"], 0.002)
    assert feb_rel["surprise"] > 0  # hot, as first reported
    assert feb_rel["known_at"] == pd.Timestamp("2024-03-13", tz="UTC")  # realtime_start + 1 day
    # the March release is judged on the REVISED February (+0.1%), as known on 2024-04-10
    mar = rel.loc[pd.Timestamp("2024-04-10")]
    assert np.isclose(mar["mom"], 0.002)
    # January's release has only 12 earlier months (11 m/m changes): its comparison is missing
    assert np.isnan(rel.loc[pd.Timestamp("2024-02-13"), "surprise"])


def test_hot_cpi_with_a_missing_month_stays_missing():
    months = pd.date_range("2023-01-01", "2024-01-01", freq="MS")
    hist = cpi_history(months)
    del hist["2023-06-01"]  # a month that was never published (e.g. a shutdown)
    rows = cpi_vintage(hist, "2024-01-20") + cpi_vintage({"2024-02-01": 999.0}, "2024-03-12")
    rel = cpi_releases(pd.DataFrame(rows)).set_index("release_date")
    assert np.isnan(rel.loc[pd.Timestamp("2024-03-12"), "surprise"])  # never imputed


# --------------------------------------------------------------------------- PIT event dates


def test_event_table_has_no_look_ahead():
    """Every event computed from all data equals the same event computed from only the data
    available at its known_at (truncation invariance)."""
    d = dgs2_rows(shocks={"2023-06-14": 30, "2023-09-08": -25})
    months = pd.date_range("2021-01-01", "2023-12-01", freq="MS")
    rows = []
    for i in range(13, len(months)):  # each release adds a month and revises the one before
        m = months[i]
        rel = pd.Timestamp(m + pd.DateOffset(months=1, days=12)) + pd.offsets.BDay(0)
        lv = cpi_history(months[: i + 1], 0.003)
        lv[str(months[i - 1].date())] *= 1.0004
        rows += cpi_vintage(lv if i == 13 else {str(months[i - 1].date()): lv[str(months[i - 1].date())],
                                                 str(m.date()): lv[str(m.date())]}, str(rel.date()))  # fmt: skip
    c = pd.DataFrame(rows)
    full = event_table(d, c).set_index("event_date")
    assert full["cpi_surprise"].notna().sum() >= 8
    for t in pd.to_datetime(["2023-03-20", "2023-06-17", "2023-09-12", "2023-11-15"], utc=True):
        part = event_table(
            d[d["available_at"] <= t], c[pd.to_datetime(c["available_at"], utc=True) <= t]
        )
        part = part[part["known_at"] <= t].set_index("event_date")
        common = full.loc[part.index]
        assert (common["known_at"] <= t).all()
        pd.testing.assert_frame_equal(common, part, check_dtype=False)


def test_known_at_is_the_later_of_the_stored_lag_and_fred_publication():
    mv = dgs2_moves(dgs2_rows(start="2024-01-01", n=500)).set_index("obs_date")
    thu, fri = pd.Timestamp("2024-08-15"), pd.Timestamp("2024-08-16")
    assert mv.loc[thu, "known_at"] == pd.Timestamp("2024-08-17", tz="UTC")  # Thu + 2 days
    assert mv.loc[fri, "known_at"] == pd.Timestamp("2024-08-20", tz="UTC")  # Fri: posted Mon → Tue
    # σ uses only the year BEFORE the move: a huge shock doesn't inflate its own yardstick
    big = dgs2_moves(dgs2_rows(start="2024-01-01", n=500, shocks={"2025-03-04": 40})).set_index(
        "obs_date"
    )
    assert np.isclose(big.loc[pd.Timestamp("2025-03-04"), "sigma_bp"],
                      mv.loc[pd.Timestamp("2025-03-04"), "sigma_bp"])  # fmt: skip
    assert big.loc[pd.Timestamp("2025-03-04"), "z"] > 5


def test_signal_fires_on_the_bar_closing_at_availability_and_enters_next_open():
    d = dgs2_rows(start="2023-01-02", n=400, shocks={"2024-04-11": 35})  # a Thursday
    ev = event_table(d, pd.DataFrame())
    ts = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
    f = pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": 100.0, "high": 101.0,
                      "low": 99.0, "close": 100.0, "volume": 1.0, "funding_day": 0.0})  # fmt: skip
    f = attach_macro_events(f, ev)
    s = get_strategy("macro_shock").signals(f)
    hit = f.loc[s.short, "ts"]
    assert pd.Timestamp("2024-04-12", tz="UTC") in set(hit)  # bar closing Sat 00:00 = known_at
    i = f.index[f["ts"] == pd.Timestamp("2024-04-12", tz="UTC")][0]
    assert f.loc[i, "ms_known_at"] == f.loc[i, "close_time"]
    assert f.loc[i + 1, "ts"] == f.loc[i, "ms_known_at"]  # entry: the open AT availability
    assert not s.short.loc[: i - 1][
        f["ts"].loc[: i - 1] >= pd.Timestamp("2024-04-11", tz="UTC")
    ].any()
    assert not s.long.any()  # primary hypothesis is short-only


def test_hot_cpi_needs_both_the_surprise_and_the_yield_rise():
    f = pd.DataFrame({"close": 100.0, "high": 101.0, "low": 99.0,
                      "ms_dgs2_bp": [6.0, 4.0, 6.0, np.nan], "ms_dgs2_z": [0.5, 0.5, 0.5, np.nan],
                      "ms_cpi_surprise": [0.001, 0.001, -0.001, 0.002]})  # fmt: skip
    st = get_strategy("macro_shock")
    assert st.signals(f).short.tolist() == [True, False, False, False]
    assert st.signals(f, {"triggers": "rate"}).short.tolist() == [False] * 4
    m = st.signals(f.assign(ms_dgs2_z=[-3.0, 0.0, 3.0, np.nan]), {"mode": "mirror"})
    assert m.long.tolist() == [True, False, False, False] and not m.short.any()


# --------------------------------------------------------------------------- basket & empty path


def flat_frame(seed, n=500):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    o = np.r_[c[0], c[:-1]]
    ts = pd.date_range("2023-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o,
                         "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
                         "volume": 1.0, "funding_day": 0.0001})  # fmt: skip


def test_basket_counts_a_shared_event_date_once():
    frames = [flat_frame(k) for k in range(4)]
    sig = pd.Series(False, index=frames[0].index)
    sig.iloc[[100, 200, 300]] = True
    per_coin = []
    for k, f in enumerate(frames):
        per_coin += perp_asset_events(f"C{k}", f, {"1w": 7}, COSTS, short_signal=sig)
    (b,) = basket_asset_events(per_coin)
    assert b.symbol == "BASKET:short" and int(b.signal.sum()) == 3
    want = np.mean([a.fwd["ret_1w"].iloc[100] for a in per_coin])
    assert np.isclose(b.fwd["ret_1w"].iloc[100], want)


def test_no_macro_data_gives_insufficient_data_not_a_crash():
    s = get_settings()
    inputs = [
        PerpInput(c, flat_frame(k), COSTS, 20.0) for k, c in enumerate("ABC")
    ]  # no ms_* columns
    rep = run_perp_research(None, s, "macro_shock", inputs=inputs)
    assert rep.verdict["verdict"] == "INSUFFICIENT_DATA"
    assert int(rep.study.summary.set_index("horizon").loc["1w", "n_independent"]) == 0
    assert rep.sim.metrics.get("trades", 0) == 0
    # macro columns present but no event qualifies: same answer
    empty = [PerpInput(a.coin, attach_macro_events(a.frame, event_table(pd.DataFrame(), pd.DataFrame())),
                       COSTS, 20.0) for a in inputs]  # fmt: skip
    assert (
        run_perp_research(None, s, "macro_shock", inputs=empty).verdict["verdict"]
        == "INSUFFICIENT_DATA"
    )


def test_macro_shock_research_runs_with_events_and_reports_breakdowns():
    s = get_settings()
    d = dgs2_rows(start="2022-01-03", n=650, seed=3)
    ev = event_table(d, pd.DataFrame())
    inputs = [
        PerpInput(c, attach_macro_events(flat_frame(k), ev), COSTS, 20.0)
        for k, c in enumerate("ABC")
    ]
    rep = run_perp_research(None, s, "macro_shock", inputs=inputs)
    row = rep.study.summary.set_index("horizon").loc["1w"]
    assert set(rep.study.by_asset["symbol"]) <= {"BASKET:short"}
    n_dates = int(
        pd.concat(
            [a.frame.loc[a.frame["ms_dgs2_z"] >= 2, "ms_event_date"] for a in inputs]
        ).nunique()
    )
    assert 0 < int(row["n_events"]) <= n_dates  # one observation per date, not per coin
    assert rep.coin_study is not None and len(rep.coin_study.events) > len(rep.study.events)
    assert list(rep.breakdowns["label"]) == list(get_strategy("macro_shock").breakdowns)
