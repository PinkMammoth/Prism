"""Live track record: call episodes, next-open timing, same-class benchmark, WAIT fills."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.research import track_record as tr

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db
    from market_signal.features import FeatureStore

    db = tmp_path_factory.mktemp("track") / "demo.duckdb"
    old = {k: os.environ.get(k) for k in ("PRISM_DB_PATH", "PRISM_SOURCE_OVERRIDE", "PRISM_HOME")}
    os.environ.update(
        PRISM_DB_PATH=str(db), PRISM_SOURCE_OVERRIDE="synthetic", PRISM_HOME=str(REPO)
    )
    s = get_settings(REPO)
    store = Store(db)
    build_demo_db(s, store, years=3, seed=11)
    yield s, store, FeatureStore(store, s)
    store.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _scan(store, feats, i_from_end: int, statuses: dict[str, str], zone_hi=None, created=None):
    """Persist a scan of bar (len - i_from_end) for each crypto symbol given."""
    any_feat = next(iter(feats.values()))
    bar = pd.Timestamp(any_feat["close_time"].iloc[len(any_feat) - i_from_end])
    sid = f"scan_{bar:%Y%m%d}_{created or 0}"
    created_at = bar + pd.Timedelta(hours=1 + (created or 0))
    store.con.execute("INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?)",
                      [sid, bar, created_at, "h", None, "{}", "{}"])  # fmt: skip
    for sym, status in statuses.items():
        f = feats[sym]
        price = float(f.loc[f["close_time"] == bar, "close"].iloc[0])
        payload = {"asset_class": "crypto", "as_of": str(bar), "band": "STRONG",
                   "setup": {"state": "ACTIVE", "research_verdict": "INSUFFICIENT_DATA"},
                   "zones": {"entry_zone": [0.0, (zone_hi or {}).get(sym, price * 1.01)],
                             "invalidation": price * 0.5}}  # fmt: skip
        store.con.execute("INSERT INTO scan_results VALUES (?,?,?,?,?,?,?,?)",
                          [sid, sym, "breakout_retest", 80.0, 0.8, status, price, json.dumps(payload)])  # fmt: skip
    return bar


def test_episodes_benchmark_and_wait(env):
    s, store, fs = env
    syms = ["BTC", "ETH", "SOL"]
    feats = {k: fs(k) for k in syms}
    other = {"ETH": "IGNORE", "SOL": "WATCH"}
    # BTC actionable on 3 consecutive bars (re-scanned twice on the first) = ONE call
    b0 = _scan(store, feats, 200, {"BTC": "STRONG", **other})
    _scan(store, feats, 200, {"BTC": "STRONG", **other}, created=1)
    _scan(store, feats, 199, {"BTC": "ACTIONABLE", **other})
    _scan(store, feats, 198, {"BTC": "STRONG", **other})
    # SOL: a WAIT that fills (preferred price = the lowest low in the next 30 bars)
    f_sol = feats["SOL"]
    t_sol = len(f_sol) - 150
    fill_px = float(f_sol["low"].iloc[t_sol + 1 : t_sol + 31].min()) * 1.0001
    _scan(
        store, feats, 150, {"SOL": "WAIT", "BTC": "WATCH", "ETH": "WATCH"}, zone_hi={"SOL": fill_px}
    )
    # ETH: a WAIT that can never fill (preferred far below), and a pending call at the end
    _scan(store, feats, 120, {"ETH": "WAIT", "BTC": "WATCH", "SOL": "WATCH"}, zone_hi={"ETH": 1e-9})
    _scan(store, feats, 5, {"BTC": "EXCEPTIONAL", "ETH": "IGNORE", "SOL": "IGNORE"})

    calls = tr.extract_calls(tr.load_scan_rows(store))
    assert list(zip(calls["symbol"], calls["group"], strict=True)) == [
        ("BTC", "ACTIONABLE"), ("SOL", "WAIT"), ("ETH", "WAIT"), ("BTC", "ACTIONABLE")]  # fmt: skip
    assert calls.iloc[0]["days_in_state"] == 3 and calls.iloc[0]["bar"] == b0

    sc = tr.score_calls(store, s)
    btc = sc[(sc["symbol"] == "BTC") & (sc["bar"] == b0) & (sc["horizon"] == "1m")].iloc[0]
    # hand check: entry next open + costs, exit close of t+30 − costs (crypto: 10+10 bps/side)
    c = 0.002

    def ret(f, t):
        return f["tr_close"].iloc[t + 30] * (1 - c) / (f["tr_open"].iloc[t + 1] * (1 + c)) - 1

    t = len(feats["BTC"]) - 200
    assert btc["state"] == "complete"
    assert btc["ret"] == pytest.approx(ret(feats["BTC"], t))
    assert btc["bench"] == pytest.approx(
        np.mean([ret(feats[k], len(feats[k]) - 200) for k in syms])
    )
    assert btc["excess"] == pytest.approx(btc["ret"] - btc["bench"])
    assert btc["stop_hit"] is False or btc["stop_hit"] == False  # noqa: E712

    sol = sc[(sc["symbol"] == "SOL") & (sc["horizon"] == "1m")].iloc[0]
    assert sol["filled"] == True and 1 <= sol["days_to_fill"] <= 30  # noqa: E712
    assert sol["wait_edge"] == pytest.approx(sol["ret_after_fill"] - sol["ret"])
    eth = sc[(sc["symbol"] == "ETH") & (sc["horizon"] == "1m")].iloc[0]
    assert eth["filled"] == False and eth["wait_edge"] == pytest.approx(-eth["ret"])  # noqa: E712

    late = sc[(sc["symbol"] == "BTC") & (sc["bar"] != b0)]
    assert set(late["state"]) == {"pending"} and late["ret"].isna().all()

    summ = tr.summarise(sc)
    act1 = summ[(summ["group"] == "ACTIONABLE") & (summ["horizon"] == "1m")].iloc[0]
    assert act1["calls"] == 2 and act1["pending"] == 1 and act1["independent"] == 1
    assert act1["mean_excess"] == pytest.approx(btc["excess"])
    wait1 = summ[(summ["group"] == "WAIT") & (summ["horizon"] == "1m")].iloc[0]
    assert wait1["fill_rate"] == pytest.approx(0.5)


def test_overlapping_calls_are_not_independent():
    bars = pd.to_datetime(["2026-01-01", "2026-01-10", "2026-03-01"], utc=True)
    df = pd.DataFrame({"symbol": "BTC", "group": "ACTIONABLE", "horizon": "1m", "bars": 30,
                       "asset_class": "crypto", "bar": bars, "state": "complete"})  # fmt: skip
    assert tr.mark_independent(df)["independent"].tolist() == [True, False, True]


def test_no_scans_is_empty(env):
    from market_signal.data.store import Store

    s, store, _ = env
    assert tr.extract_calls(pd.DataFrame()).empty
    empty = Store(store.path.parent / "empty.duckdb")
    try:
        assert tr.score_calls(empty, s).empty
        assert tr.scan_coverage(empty)["with_scan"] == 0
    finally:
        empty.close()
