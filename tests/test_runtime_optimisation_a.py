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

    def execute(self, sql, args=None):
        if sql.lstrip().upper().startswith("SELECT"):
            self.queries.append(sql)
        return self.con.execute(sql, args or [])


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
