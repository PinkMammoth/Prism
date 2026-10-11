"""Raw input reuse gates: science clocks, positioning ties and nullable fold inputs."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd

from market_signal.paper.nimble import data as nimble
from market_signal.paper.v2 import data, spec
from market_signal.research.lab.common import canonical_json
from tests.test_runtime_optimisation_a import Reads, adapter, baseline, reference


def test_science_completed_boundary_decoded_once_and_members_unchanged(store, settings):
    from tests.test_exploratory_paper_v2 import (
        minute,
    )
    from tests.test_exploratory_paper_v2 import (
        test_compact_scientific_checkpoint_count_and_completion_time as seed,
    )

    seed(store, settings)
    hs = tuple(h for h in spec.bootstrap() if h.source_phase == 24)
    for at in (minute(1500), minute(1501), minute(1501) + pd.Timedelta(microseconds=1)):
        old, before = adapter(baseline(), store, at)
        new, after = adapter(data, store, at)
        a = old.observations(hs, minute(0), assets=["BTC", "ETH", "SOL"])
        b = new.observations(hs, minute(0), assets=["BTC", "ETH", "SOL"])
        assert canonical_json([asdict(o) for o in a]) == canonical_json([asdict(o) for o in b])
        assert sum("lab_prospective_results" in q for q in before.queries) == 24
        assert sum("lab_prospective_results" in q for q in after.queries) == 1


def test_positioning_equal_capture_future_missing_and_max_reuse(store):
    at = pd.Timestamp("2026-10-09T01:00:00Z")
    # Equal capture clocks across assets; the schema forbids same-asset capture ties.
    for coin, hour, capture, mark in [
        ("BTC", -1, at - pd.Timedelta(hours=1), 99),
        ("BTC", 0, at, 100),
        ("ETH", -1, at - pd.Timedelta(hours=1), 99),
        ("ETH", 0, at, 101),
        ("SOL", 0, at + pd.Timedelta(microseconds=1), 200),
    ]:
        store.con.execute(
            "INSERT INTO context_hl_oi_hourly (coin,grid_hour,captured_at,cadence_version,run_id,mark_px) "
            "VALUES (?,?,?,?,?,?)",
            [coin, at + pd.Timedelta(hours=hour), capture, "test", "test", mark],
        )
    oracle = baseline()
    legacy = reference("baseline_nimble_data", "market_signal.paper.nimble").Sources
    legacy.__bases__ = (oracle.Sources,)
    hs = tuple(h for h in spec.bootstrap() if h.source_phase == 23)
    a_counts, b_counts = Reads(store.con), Reads(store.con)
    a = legacy(SimpleNamespace(con=a_counts), at)
    b = nimble.Sources(SimpleNamespace(con=b_counts), at)
    a.run_id = b.run_id = "replay"
    a.context = b.context = lambda asset: {}
    before = a.observations(hs, at - pd.Timedelta(days=1), assets=["BTC", "ETH", "SOL"])
    after = b.observations(hs, at - pd.Timedelta(days=1), assets=["BTC", "ETH", "SOL"])
    assert canonical_json([asdict(o) for o in before]) == canonical_json([asdict(o) for o in after])
    assert sum("SELECT max(captured_at)" in q for q in a_counts.queries) == 18
    assert sum("SELECT max(captured_at)" in q for q in b_counts.queries) == 3
    assert all(o.available_at == at.isoformat() for o in after)


def test_context_raw_nullable_placeholders_are_preserved_and_isolated(store, monkeypatch):
    from copy import deepcopy

    from market_signal.context import ledger
    from market_signal.context.snapshot import ContextOperation, context_snapshot
    from tests.test_context import T0, ingest, obs

    ingest(store, obs(), at=T0)
    states = ledger.states_asof(store, T0)
    states[0]["legacy_ignored_nullable"] = float("nan")
    monkeypatch.setattr(ledger, "states_asof", lambda *a, **kw: deepcopy(states))
    old = context_snapshot(store, "AAVE", T0)
    operation = ContextOperation(store, T0)
    assert context_snapshot(store, "AAVE", T0, operation=operation) == old
    returned = operation.states()
    returned[0]["sources"].clear()
    assert operation.states()[0]["sources"] == states[0]["sources"]


def test_late_hourly_bar_and_revision_keep_exact_independent_cutoffs(store):
    from market_signal.intraday.bars import upsert_bars
    from market_signal.models.domain import Timeframe
    from tests.test_intraday import synth

    now = pd.Timestamp("2026-10-09T01:00:00Z")
    bars = synth(Timeframe.H1, now - pd.Timedelta(hours=1001), 1000)
    upsert_bars(
        store,
        "hyperliquid",
        "BTC",
        Timeframe.H1,
        bars,
        observed_at=now - pd.Timedelta(minutes=10),
        run_id="original",
    )
    hs = tuple(h for h in spec.bootstrap() if h.source_phase == 22)

    def replay(at):
        a, _ = adapter(baseline(), store, at)
        b, _ = adapter(data, store, at)
        before = a.observations(hs, now - pd.Timedelta(days=2), assets=["BTC"])
        after = b.observations(hs, now - pd.Timedelta(days=2), assets=["BTC"])
        assert canonical_json([asdict(o) for o in before]) == canonical_json(
            [asdict(o) for o in after]
        )
        # Raw breadth historically reads the stored revision directly; preserve that
        # existing behavior, while proving technical known-at reconstruction separately.
        return canonical_json(
            [{k: v for k, v in asdict(o).items() if k != "context"} for o in after]
        )

    original = replay(now)
    late = synth(Timeframe.H1, now - pd.Timedelta(hours=1), 1)
    upsert_bars(
        store,
        "hyperliquid",
        "BTC",
        Timeframe.H1,
        late,
        observed_at=now + pd.Timedelta(seconds=30),
        run_id="late",
    )
    assert replay(now) == original
    visible_late = replay(now + pd.Timedelta(seconds=30))
    assert visible_late != original
    revised = late.copy()
    revised.loc[0, "close"] *= 1.001
    revised.loc[0, "high"] = max(revised.loc[0, "high"], revised.loc[0, "close"])
    upsert_bars(
        store,
        "hyperliquid",
        "BTC",
        Timeframe.H1,
        revised,
        observed_at=now + pd.Timedelta(seconds=60),
        run_id="revision",
    )
    assert replay(now) == original
    assert replay(now + pd.Timedelta(seconds=30)) == visible_late
    replay(now + pd.Timedelta(seconds=60))


def test_production_minute_revision_ingest_boundary_and_restart_gap(store, tmp_path, monkeypatch):
    from market_signal.microstructure.ingest import ingest
    from market_signal.microstructure.spool import Spool
    from market_signal.ops.runtime import claim_authority
    from tests.test_microstructure import MIN, M, healthy, minute, trade

    claim_authority(store.path, "runtime-a-fixture", "test")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "runtime-a-fixture")
    late = ("T", M + MIN + 8_000, "BTC", M + 30_000, 100.0, 2.0, True, 21, None)
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1), late], end=M + 18 * MIN)
    spool = Spool(tmp_path / "revision-tape")
    spool.append(
        {
            "kind": "run_start",
            "run_id": "msrun_test",
            "at": M - 10_000,
            "role": "authoritative",
            "runtime_id": "runtime-a-fixture",
        },
        M,
    )
    r1 = minute(out, rev=1)
    for row in out:
        if row["kind"] == "minute" and row["revision"] == 0:
            spool.append(row, row["finalized_ms"])
    t = lambda milliseconds: pd.Timestamp(milliseconds, unit="ms", tz="UTC")  # noqa: E731
    first, revised_at = t(M + 19 * MIN), t(M + 19 * MIN + 30_000)
    ingest(store, spool, now=first.to_pydatetime())
    hs = tuple(h for h in spec.bootstrap() if h.source_phase == 24)

    def replay(at, revision):
        a, _ = adapter(baseline(), store, at)
        b, _ = adapter(data, store, at)
        pd.testing.assert_frame_equal(a.micro("BTC"), b.micro("BTC"))
        assert int(b.micro("BTC")["revision"].iloc[0]) == revision
        before = a.observations(hs, t(M - MIN), assets=["BTC"])
        after = b.observations(hs, t(M - MIN), assets=["BTC"])
        assert canonical_json([asdict(o) for o in before]) == canonical_json(
            [asdict(o) for o in after]
        )
        assert [asdict(q) for q in a.quotes(t(M))] == [asdict(q) for q in b.quotes(t(M))]

    replay(first, 0)
    spool.append(r1, r1["finalized_ms"])
    ingest(store, spool, now=revised_at.to_pydatetime())
    replay(revised_at - pd.Timedelta(microseconds=1), 0)
    replay(revised_at, 1)
    # A fresh adapter after collector/consumer restart reconstructs the same persisted revision;
    # later uncollected minutes remain gaps rather than forward-filled features.
    replay(t(M + 22 * MIN), 1)
