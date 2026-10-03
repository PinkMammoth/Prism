"""Perps phase 3: strategies are causal, the research pipeline can earn a passing verdict
for a REAL (planted) edge and rejects noise, and runs are saved like spot research."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_signal.config import get_settings
from market_signal.perps.backtest import PerpCosts, PerpInput
from market_signal.perps.research import run_perp_research, save_perp_report
from market_signal.perps.strategies import STRATEGIES, funding_percentile, get_strategy

COSTS = PerpCosts(4.5, 3.0, 10.0)


def frame(c, fund, start="2023-06-01"):
    c = np.asarray(c, float)
    o = np.concatenate([[c[0]], c[:-1]])
    w = np.abs(np.random.default_rng(1).normal(0, 0.01, len(c)))
    ts = pd.date_range(start, periods=len(c), freq="1D", tz="UTC")
    return pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o,
                         "high": np.maximum(o, c) * (1 + w), "low": np.minimum(o, c) * (1 - w), "close": c,
                         "volume": 1.0, "funding_day": fund})  # fmt: skip


def market(seed, n=1100, trend=True):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.025, n)
    if trend:  # persistent multi-week trends of random direction: what trend-following needs
        i = 0
        while i < n:
            length = int(rng.integers(40, 120))
            r[i : i + length] += rng.choice([-1, 1]) * 0.006
            i += length
    return frame(100 * np.exp(np.cumsum(r)), rng.normal(0.0003, 0.0004, n))


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_strategy_signals_are_causal(name):
    f = market(7)
    full = get_strategy(name).signals(f)
    k = 700
    cut = get_strategy(name).signals(f.iloc[:k])
    for attr in ("long", "short"):
        pd.testing.assert_series_equal(getattr(full, attr).iloc[:k].reset_index(drop=True),
                                       getattr(cut, attr).reset_index(drop=True), check_names=False)  # fmt: skip
    np.testing.assert_allclose(full.long_stop.iloc[:k].to_numpy(), cut.long_stop.to_numpy())


def test_funding_fade_fires_on_crowded_funding_after_a_run_up():
    n = 400
    c = np.concatenate([np.full(380, 100.0), np.linspace(100, 130, 20)])  # run-up at the end
    fund = np.full(n, 0.0002)
    fund[-10:] = 0.003  # funding spikes to the top of its year
    s = get_strategy("funding_fade").signals(frame(c, fund))
    assert s.short.iloc[-10:].any()  # crowded long after a run-up → fade it (short)
    assert not s.long.any()
    pct = funding_percentile(frame(c, fund), 7, 365, 120)
    assert (
        pct.iloc[:119].isna().all() and pct.iloc[-1] > 0.99
    )  # ties at the top share an averaged rank


def test_research_passes_a_planted_edge_and_rejects_noise():
    s = get_settings()
    trend = [PerpInput(c, market(k), COSTS, 20.0) for k, c in enumerate("ABCDEF")]
    noise = [
        PerpInput(c, market(100 + k, trend=False), COSTS, 20.0) for k, c in enumerate("ABCDEF")
    ]
    good = run_perp_research(None, s, "trend_ls", inputs=trend)
    bad = run_perp_research(None, s, "trend_ls", inputs=noise)
    assert good.verdict["verdict"] in ("PROMISING", "WEAK_POSITIVE"), good.verdict
    assert good.wf_summary["folds_positive"] >= good.wf_summary["folds_with_events"] / 2
    assert bad.verdict["verdict"] in ("REJECT", "INCONCLUSIVE", "INSUFFICIENT_DATA"), bad.verdict
    # both sides are measured against their own same-side baseline
    assert set(good.study.by_class["asset_class"]) == {"perp_long", "perp_short"}
    assert good.sim.metrics["trades"] > 20 and (good.sim.trades["leverage"] <= 3 + 1e-9).all()


def test_runs_are_saved_and_readable_like_spot_research(settings, store):
    from market_signal.presenter import load_evidence

    s, st = settings, store  # isolated project root: reports land in its own results/
    inputs = [PerpInput(c, market(k), COSTS, 20.0) for k, c in enumerate("ABC")]
    rep = run_perp_research(
        None, s, "breakout_ls", inputs=inputs, walk_forward_on=False, sensitivity_on=False
    )
    out = save_perp_report(st, s, rep)
    assert (out / "report.md").read_text().startswith("# Perp research: Range breakout")
    ev = load_evidence(st)["perp_breakout_ls"]
    assert ev.verdict == rep.verdict["verdict"] and ev.horizon == "1m"
    assert ev.n_independent == int(
        rep.study.summary.set_index("horizon").loc["1m", "n_independent"]
    )
    assert set(ev.class_share) <= {"perp_long", "perp_short"}
    assert str(out).startswith(str(s.paths.root))


def test_research_on_demo_db_cli(tmp_path, monkeypatch):
    """The CLI path end to end on the synthetic demo DB: random walks must not pass."""
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db

    s = get_settings()
    st = Store(tmp_path / "d.duckdb")
    try:
        build_demo_db(s, st, years=2, seed=8)
        rep = run_perp_research(st, s, "funding_fade", walk_forward_on=False, sensitivity_on=False)
        assert rep.coins == [str(c) for c in s.yaml("perps.yaml")["coins"]]
        assert rep.verdict["verdict"] not in ("PROMISING",)
    finally:
        st.close()
