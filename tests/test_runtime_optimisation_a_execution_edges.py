"""Independent same-asset run envelopes and delayed accounting differential fixture."""

from dataclasses import replace

from market_signal.paper.nimble import data, engine
from market_signal.paper.v2.data import Funding
from tests.test_nimble_paper import T0, Source, h, observation, quote, time


def test_same_asset_opposite_runs_independent_cutoffs_and_unfinalised_closes(store):
    engine.register(store, now=T0)
    engine.register(store, now=T0, events=True)
    runs = [engine.create(store, now=T0)["run_id"]]
    runs.extend(
        engine.create(store, now=T0, events=True, variant=variant)["run_id"]
        for variant in ("immediate", "confirmed")
    )
    snapshots = []
    for index, (rid, side) in enumerate(zip(runs, ("long", "long", "short"), strict=True)):
        at = time(59, index)
        source = data.Sources(store, at)
        snapshot = source.context("BTC")
        snapshots.append(snapshot)
        obs = replace(observation(h("crowding_vulnerability", side), minute=59), context=snapshot)
        with store.transaction():
            assert engine.admit(
                store,
                rid,
                [obs],
                [quote(59, index)],
                Source(),
                {"BTC": {"funding_rate": 0.0001}},
                now=at,
            ) == {"ADMITTED": 1}
            engine.monitor(
                store,
                rid,
                [quote(59, index + 4)],
                [],
                [],
                {"BTC": {"funding_rate": 0.0001}},
                now=time(59, index + 4),
            )
        position = engine.positions(store, rid)[0]
        assert position["asset"] == "BTC" and position["side"] == side
        assert position["state"] == "OPEN"
    assert len({s["snapshot_id"] for s in snapshots}) == 3
    assert len({s["as_of"] for s in snapshots}) == 3
    for index, (rid, side) in enumerate(zip(runs, ("long", "long", "short"), strict=True)):
        price = 102 if side == "long" else 98
        with store.transaction():
            for second in (index + 8, index + 12):
                engine.monitor(
                    store,
                    rid,
                    [quote(60, second, price)],
                    [],
                    [],
                    {"BTC": {"funding_rate": 0.0001}},
                    now=time(60, second),
                )
        closed = engine.state(store, rid)["closed"][0]
        assert not closed["accounting_final"]
        assert not engine.positions(store, rid)
        rate = Funding("BTC", time(60).isoformat(), time(61).isoformat(), -0.0002)
        with store.transaction():
            engine.reconcile(store, rid, [rate], time(61))
        closed = engine.state(store, rid)["closed"][0]
        assert closed["accounting_final"]
        assert (closed["funding"] < 0) == (side == "long")
