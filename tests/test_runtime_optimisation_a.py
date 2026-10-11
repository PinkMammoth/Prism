"""Deterministic adapter oracle, read counts and mutation contracts for Tranche A."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from market_signal.paper.v2 import data, spec
from market_signal.research.lab.common import canonical_json


def baseline():
    name = "market_signal.paper.v2._runtime_a_oracle"
    loader = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / "fixtures/runtime_a/baseline_data.py"
    )
    module = importlib.util.module_from_spec(loader)
    sys.modules[name] = module
    loader.loader.exec_module(module)
    return module


class Reads:
    def __init__(self, con):
        self.con = con
        self.queries = []
        self.rows = 0
        self.writes = 0

    def execute(self, sql, args=None):
        if sql.lstrip().upper().startswith("SELECT"):
            self.queries.append(sql)
        else:
            self.writes += 1
        return ReadResult(self.con.execute(sql, args or []), self)


class ReadResult:
    def __init__(self, cursor, counter):
        self.cursor, self.counter = cursor, counter

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.counter.rows += len(rows)
        return rows

    def fetchone(self):
        row = self.cursor.fetchone()
        self.counter.rows += row is not None
        return row

    def df(self):
        frame = self.cursor.df()
        self.counter.rows += len(frame)
        return frame

    def __getattr__(self, name):
        return getattr(self.cursor, name)


def adapter(module, store, now, rid="replay"):
    reads = Reads(store.con)
    source = module.Sources(SimpleNamespace(con=reads), now)
    source.run_id = rid
    return source, reads


@pytest.mark.parametrize("phase", [22, 23, 24])
@pytest.mark.parametrize("offset", [0, 1, 59, 60])
def test_empty_missing_and_boundary_exact_adapter_parity(store, phase, offset):
    now = pd.Timestamp("2026-10-09T01:00:00Z") + pd.Timedelta(seconds=offset)
    hypotheses = [h for h in spec.bootstrap() if h.source_phase == phase]
    old, before = adapter(baseline(), store, now)
    new, after = adapter(data, store, now)
    a = old.observations(hypotheses, now - pd.Timedelta(days=1), assets=["BTC", "ETH"])
    b = new.observations(hypotheses, now - pd.Timedelta(days=1), assets=["BTC", "ETH"])
    assert canonical_json([asdict(o) for o in a]) == canonical_json([asdict(o) for o in b])
    assert old.dependencies == new.dependencies
    assert len([q for q in before.queries if "SELECT 1 FROM paper_v2" in q]) == 2 * len(hypotheses)
    assert len([q for q in after.queries if "FROM paper_v2_opportunities" in q]) == 1
    if phase == 24:
        assert sum("lab_prospective_results" in q for q in after.queries) == 1
    if phase == 23:
        assert sum("SELECT captured_at,mark_px" in q for q in after.queries) == 2


def test_partial_grid_run_isolation_and_late_insert(store):
    from market_signal.paper.v2 import engine
    from tests.test_exploratory_paper_v2 import evaluate, minute, obs

    engine.register(store, now=minute(0))
    rid = engine.create(store, now=minute(0))["run_id"]
    hypotheses = [h for h in spec.bootstrap() if h.source_phase == 24]
    evaluate(store, rid, at=16, signals=[obs(hypotheses[0], fired=False)])
    for run_id, expected in [(rid, 7), ("other-run", 8)]:
        old, _ = adapter(baseline(), store, minute(16), run_id)
        new, _ = adapter(data, store, minute(16), run_id)
        a = old.observations(hypotheses, minute(0), assets=["BTC"])
        b = new.observations(hypotheses, minute(0), assets=["BTC"])
        assert len(b) == expected
        assert canonical_json([asdict(o) for o in a]) == canonical_json([asdict(o) for o in b])
    # A subsequent invocation must see a newly committed grid member.
    evaluate(store, rid, at=17, signals=[obs(hypotheses[1], fired=False)])
    new.run_id = rid
    assert len(new.observations(hypotheses, minute(0), assets=["BTC"])) == 6


@pytest.mark.parametrize("gap,warmup", [(False, False), (True, False), (False, True)])
def test_populated_frame_and_flags_exact_parity_and_mutation(store, monkeypatch, gap, warmup):
    import numpy as np

    from market_signal.research.discovery.catalogue import IntradayStrategy
    from market_signal.research.discovery.primitives import Frame
    from market_signal.research.discovery.signals import SignalContext, signal_mask
    from market_signal.research.microdir import synthetic
    from market_signal.research.microdir.features import flags, member_mask, windows
    from market_signal.research.microdir.spec import HYPOTHESES

    raw = synthetic.market(synthetic.Scenario("replay", coins=("BTC",), days=8, seed=9))["BTC"]
    now = raw["minute_open"].max() + pd.Timedelta(minutes=1) + pd.Timedelta(seconds=30)
    count = 10 if warmup else 1000
    times = pd.date_range(end=now.floor("h"), periods=count, freq="h")
    close = 100 * np.exp(np.sin(np.arange(count) / 11) * 0.03 + np.arange(count) * 0.00001)
    bars = pd.DataFrame(
        {
            "open_time": times - pd.Timedelta(hours=1),
            "close_time": times,
            "first_observed_at": times,
            "open": close,
            "close": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "volume": 100.0,
        }
    )
    if gap:
        bars = bars.drop(bars.index[-12])
        raw = raw.drop(raw.index[-20])
    if warmup:
        raw = raw.tail(30)
    oracle = baseline()
    for module in (oracle, data):
        monkeypatch.setattr(module, "load_bars", lambda *a, **kw: bars.copy())
        monkeypatch.setattr(module.Sources, "micro", lambda self, coin: raw.copy())
    old, _ = adapter(oracle, store, now)
    new, _ = adapter(data, store, now)
    frame_calls, flag_calls = [], []
    original_frame = Frame.from_series

    def frame(series, **kw):
        f = original_frame(series, **kw)
        frame_calls.append(f)
        return f

    monkeypatch.setattr(Frame, "from_series", frame)
    original_flags = data.flags

    def counted_flags(f):
        values = original_flags(f)
        flag_calls.append(values)
        return values

    monkeypatch.setattr(data, "flags", counted_flags)
    hs = spec.bootstrap()
    before = old.observations(hs, now - pd.Timedelta(days=1), assets=["BTC"])
    frame_calls.clear()
    after = new.observations(hs, now - pd.Timedelta(days=1), assets=["BTC"])
    assert canonical_json([asdict(o) for o in before]) == canonical_json([asdict(o) for o in after])
    assert len(frame_calls) == 1
    assert len(flag_calls) == 1
    f = frame_calls[0]
    from market_signal.intraday.align import Availability
    from market_signal.research.structure.series import BarSeries

    series = BarSeries.from_frame(
        bars, venue="hyperliquid", coin="BTC", timeframe="1h", availability=Availability.observed()
    )
    fresh = original_frame(series)
    _ = fresh.atr, fresh.ret(1)
    for hh in (hh for hh in hs if hh.source_phase == 22):
        ss = IntradayStrategy.model_validate(hh.parameters["strategy"])
        data.technical_ready(fresh, ss)
        signal_mask(SignalContext(tf="1h", f=fresh, ready_at=series.ready_at), ss)
    for h in (h for h in hs if h.source_phase == 22):
        strategy = IntradayStrategy.model_validate(h.parameters["strategy"])
        snapshot = {k: v.copy() for k, v in f._cache.items()}
        a = signal_mask(SignalContext(tf="1h", f=f, ready_at=series.ready_at), strategy)
        b = signal_mask(SignalContext(tf="1h", f=fresh, ready_at=series.ready_at), strategy)
        np.testing.assert_array_equal(a, b)
        for k, v in snapshot.items():
            np.testing.assert_array_equal(f._cache[k], v)
        for k, v in f._cache.items():
            np.testing.assert_array_equal(v, fresh._cache[k])
            with pytest.raises(ValueError):
                v[0] = 0
    features, _ = windows(raw, "BTC")
    snapshot = features.copy(deep=True)
    fl = flag_calls[0]
    frozen = {k: v.copy() for k, v in fl.items()}
    for h in HYPOTHESES:
        for side in ("long", "short"):
            np.testing.assert_array_equal(
                member_mask(features, fl, h, side), member_mask(features, flags(features), h, side)
            )
            pd.testing.assert_frame_equal(features, snapshot)
            for key, value in fl.items():
                np.testing.assert_array_equal(value, frozen[key])
                with pytest.raises(ValueError):
                    value[0] = False
