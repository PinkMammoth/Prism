"""Phase 24B microstructure direction study (core tests; synthetic only)."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from market_signal.data.store import MIGRATIONS, Store
from market_signal.research.microdir import analysis as an
from market_signal.research.microdir import features as ft
from market_signal.research.microdir import governance as gv
from market_signal.research.microdir import spec as sp
from market_signal.research.microdir import synthetic as sy

ROOT = Path(__file__).resolve().parents[1]
COINS4 = ("BTC", "ETH", "SOL", "HYPE")


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config/perps.yaml").read_text())


@pytest.fixture(scope="module")
def defn():
    return sp.build_definition(_cfg())


def _run(defn, kind, **kw):
    sc = sy.Scenario(kind, kind=kind, coins=COINS4, days=35, **kw)
    return an.evaluate(sy.market(sc), defn)


def test_migration_24_is_appended_and_append_only():
    ddl = MIGRATIONS[23]
    assert "lab_prospective_studies" in ddl and "availability_mode = 'observed'" in ddl
    assert not any(x in ddl for x in ("paper_", "copilot_", "microstructure_minutes"))


def test_definition_is_content_addressed_and_frozen(defn):
    assert (
        defn.study_id.startswith("sstudy_")
        and defn.study_id == sp.build_definition(_cfg()).study_id
    )
    bad = dict(sp.frozen(), FLOW_STRONG=0.8)
    with pytest.raises(ValueError):
        sp.MicroStudyDefinition(definition=bad, costs=defn.costs)


def test_null_market_admits_nothing(defn):
    r = _run(defn, "null", seed=3)
    assert r["summary"]["candidates"] == []


def test_planted_absorption_detected_both_sides_and_oi_contrast(defn):
    r = _run(defn, "absorption", drift=0.008, per_coin_day=3, seed=41)
    for side in sp.SIDES:
        v = r["members"][f"absorption_oi_rising:{side}"]["verdict"]["verdict"]
        assert v in sp.CANDIDATE_VERDICTS
        assert r["contrasts"][f"oi_rising_vs_not:{side}"]["difference"]["t"] > 2


def test_candle_equivalent_effect_adds_no_microstructure_value(defn):
    r = _run(defn, "candle", drift=0.004, seed=51)
    assert r["summary"]["candidates"] == []
    flow = next(x for x in r["ladder"]["layers"] if x["layer"] == "flow")
    assert flow["added_test"]["p_value"] > 0.05


def _mirror(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    k = 1e4
    sw = (
        ("buy_ntl", "sell_ntl"),
        ("buy_vol", "sell_vol"),
        ("bid5_end", "ask5_end"),
        ("bid20_end", "ask20_end"),
        ("bid_replenish", "ask_replenish"),
    )
    for a, b in sw:
        m[a], m[b] = df[b], df[a]
    m["bid_end"], m["ask_end"] = k / df["ask_end"], k / df["bid_end"]
    m["first_px"], m["last_px"] = k / df["first_px"], k / df["last_px"]
    m["high_px"], m["low_px"] = k / df["low_px"], k / df["high_px"]
    m["funding_end"] = -df["funding_end"]
    m["lp_buy_ntl"] = df["lp_ntl"] - df["lp_buy_ntl"]
    return m


def test_long_short_symmetry_under_a_mirrored_market(defn):
    mk = sy.market(sy.Scenario("n", coins=("BTC", "ETH", "SOL"), days=8, seed=5))
    P = an.panel(mk)
    Q = an.panel({c: _mirror(f) for c, f in mk.items()})
    fp, fq = ft.flags(P), ft.flags(Q)
    for h in sp.HYPOTHESES:
        if "crowd_aligned" in h.requires:
            continue
        a = ft.member_mask(P, fp, h, "long")
        b = ft.member_mask(Q, fq, h, "short")
        assert a.sum() > 0 or h.family == "large_print"
        assert np.array_equal(a, b), h.key


def test_complete_only_and_causal_truncation():
    mk = sy.market(sy.Scenario("n", coins=("BTC",), days=6, seed=7))["BTC"]
    full, _ = ft.windows(mk, "BTC")
    cut = len(mk) // 2
    part, _ = ft.windows(mk.iloc[:cut], "BTC")
    cols = ["flow_pct", "resp_z", "sigma15", "oi_z", "vol_pct"]
    n = len(part) - 1
    pd.testing.assert_frame_equal(full[cols].iloc[:n], part[cols].iloc[:n])
    bad = mk.copy()
    bad.loc[5 * 1440 + 3, "status"] = "PARTIAL"
    f, _ = ft.windows(bad, "BTC")
    w = (5 * 1440 + 3) // sp.SIGNAL_MINUTES
    assert not f["complete"].iat[w] and not f["eligible"].iat[w]


def test_dedup_refractory_per_coin():
    t = pd.Series(
        pd.to_datetime(
            [
                "2026-01-01 00:00",
                "2026-01-01 00:15",
                "2026-01-01 00:45",
                "2026-01-01 01:00",
                "2026-01-01 00:15",
            ],
            utc=True,
        )
    )
    keep, ep = an.dedup(t, pd.Series(["BTC", "BTC", "BTC", "BTC", "ETH"]))
    assert keep.tolist() == [True, False, False, True, True]
    assert ep.tolist()[:3] == [0, 0, 0]


def test_maturity_levels():
    st = {"evaluable": 120, "by_asset": {c: {"n": 40} for c in COINS4}}
    assert an.maturity(5, st) == "WARMUP"
    assert an.maturity(10, st) == "EARLY"
    assert an.maturity(31, st) == "DEVELOPING"
    assert (
        an.maturity(95, {"evaluable": 400, "by_asset": {c: {"n": 100} for c in COINS4}})
        == "ADEQUATE"
    )
    assert an.verdict({"net": {}}, "WARMUP", None)["verdict"] == "IMMATURE"


def test_governance_grid_no_skipping_and_history(tmp_path, defn):
    from market_signal.cli.lab_cmds import _software

    st = Store(tmp_path / "g.duckdb", tmp_path / "raw")
    sw = _software()
    gv.register(st, defn, software=sw, origin="test", reason="t")
    with pytest.raises(gv.MicroStudyError):
        gv.register(st, defn, software=sw, origin="test", reason="t")
    reg = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=5)).to_pydatetime()
    st.con.execute(
        "UPDATE lab_prospective_studies SET registered_at=?", [reg]
    )  # test-only backdate
    first = gv.grid_after(reg, "daily")
    now = pd.Timestamp.now(tz="UTC")
    seq = iter(["INCUBATION_CANDIDATE", "NO_EVIDENCE"])

    def ev(_d, as_of):
        return {
            "study": sp.STUDY_NAME,
            "members": {
                "absorption_core:short": {
                    "maturity": "DEVELOPING",
                    "verdict": {"verdict": next(seq)},
                    "stats": {},
                }
            },
        }, {}

    with pytest.raises(gv.MicroStudyError):  # skipping ahead
        gv.checkpoint(
            st,
            defn.study_id,
            "daily",
            first + pd.Timedelta(days=1),
            software=sw,
            evaluate=ev,
            now=now,
        )
    with pytest.raises(gv.MicroStudyError):  # off grid
        gv.checkpoint(
            st,
            defn.study_id,
            "daily",
            first + pd.Timedelta(hours=3),
            software=sw,
            evaluate=ev,
            now=now,
        )
    for t in gv.due(st, defn.study_id, "daily", now)[:2]:
        gv.checkpoint(st, defn.study_id, "daily", t, software=sw, evaluate=ev, now=now)
    h = gv.history(st, defn.study_id)
    m = h["members"]["absorption_core:short"]
    assert [x["verdict"] for x in m["path"]] == ["INCUBATION_CANDIDATE", "NO_EVIDENCE"]
    assert m["fell_back"] and len(h["checkpoints"]) == 2


def test_no_execution_imports():
    banned = ("paper", "copilot", "ops", "forward", "telegram", "httpx", "execution")
    for p in (ROOT / "src/market_signal/research/microdir").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else ([a.name for a in node.names] if isinstance(node, ast.Import) else [])
            )
            for n in names:
                assert not any(f".{b}" in n or n.startswith(b) for b in banned), (p.name, n)
    for p in (ROOT / "src/market_signal").rglob("*.py"):
        if "microdir" in str(p) or p.name == "microdir_cmds.py" or p.name == "lab_cmds.py":
            continue
        assert "research.microdir" not in p.read_text(), p


# --------------------------------------------------------------------------- replenishment audit


def _b5(rows, open_ms=0, step=500):
    """book5 snapshots (t, recv, bid, ask, bid5, ask5) every ``step`` ms through the minute,
    with one carry-in before and one after (the 24A validity rule)."""
    out = [(open_ms - step, open_ms - step, *rows[0])]
    for i, r in enumerate(rows):
        out.append((open_ms + i * step, open_ms + i * step, *r))
    out.append((open_ms + 60_000, open_ms + 60_000, *rows[-1]))
    return out


def _b20(open_ms=0):
    return [(open_ms - 5000 + i * 5000, open_ms - 5000 + i * 5000, 1e6, 1e6) for i in range(15)]


def test_replenishment_proxy_behaves_on_synthetic_streams():
    from market_signal.microstructure.aggregate import book_stats

    n = 120
    static = [(99.0, 101.0, 5e5, 5e5)] * n
    # consumed then restored at the same ask price: the restore counts as replenishment
    refill = [(99.0, 101.0, 5e5, 5e5 if i % 2 == 0 else 3e5) for i in range(n)]
    # ask lifted and the price steps up: depth reappears at a NEW price -> not replenishment
    walk = [(99.0 + i * 0.01, 101.0 + i * 0.01, 5e5, 5e5 if i % 2 == 0 else 3e5) for i in range(n)]
    # cancel/re-add flicker at an unchanged price (no trades): ALSO counted (documented limit)
    s0 = book_stats(_b5(static), _b20(), 0)
    s1 = book_stats(_b5(refill), _b20(), 0)
    s2 = book_stats(_b5(walk), _b20(), 0)
    assert s0["ask_replenish"] == 0 and s0["bid_replenish"] == 0  # no book response
    assert s1["ask_replenish"] == pytest.approx(2e5 * (n // 2 - 1), rel=0.05)
    assert s2["ask_replenish"] == 0  # price walked: nothing replenished at a stable price
    # the study pairs the proxy with NET depth persistence (end/start), which flicker cannot move
    assert s1["ask5_end"] in (5e5, 3e5)


def test_book_state_flags_relative_to_flow_side():
    f = pd.DataFrame({
        "flow_pct": [0.95, 0.05, 0.95, 0.5], "ret_mid": [0.0, 0.0, 0.01, 0.0],
        "sigma15": [0.01] * 4, "flow_z": [2.0, -2.0, 2.0, 0.0],
        "rep_ask_pct": [0.8, 0.1, 0.2, 0.5], "rep_bid_pct": [0.1, 0.8, 0.9, 0.5],
        "ask_depth_ratio": [1.1, 0.8, 0.9, 1.0], "bid_depth_ratio": [0.8, 1.2, 1.2, 1.0],
        "oi_z": [1.0, 1.0, -1.0, np.nan], "funding_pct": [0.9, 0.1, 0.5, 0.5],
        "n_sub_same": [3, 3, 1, 0], "n_min_same": [12, 12, 4, 0], "burst_share": [0.1, 0.1, 0.6, 0],
        "lp_available": [False] * 4, "lp_net_ntl": [np.nan] * 4, "rv_pct": [0.5] * 4,
        "window_open": pd.to_datetime(["2026-01-01 14:00"] * 4, utc=True), "complete": [True] * 4,
        "c_ret_z": [0.0] * 4, "vol_pct": [0.5] * 4, "c_ret_pct": [0.5] * 4, "clv": [0.5] * 4,
    })  # fmt: skip
    c = ft.classify(f)
    assert c["flow_side"].tolist() == [1, -1, 1, 0]
    assert c["book_state"].tolist()[:3] == ["resilient", "resilient", "consumed"]
    assert c["response"].tolist()[:3] == ["weak", "weak", "efficient"]
    assert c["oi_state"].tolist() == ["rising", "rising", "falling", "unknown"]  # never assumed
    assert c["funding_aligned"].tolist() == [True, True, False, False]
    assert c["persistence"].tolist() == ["persistent", "persistent", "burst", "none"]


# --------------------------------------------------------------------------- data, causality


def _seed(st, frame, coin):
    from market_signal.microstructure import definitions as md

    df = frame.copy()
    df["feature_version"], df["revision"], df["flags"] = md.FEATURE_VERSION, 0, None
    df["trade_cov"], df["book_samples"], df["depth20_samples"] = 1.0, 60, 60
    df["n_dup"], df["n_late"], df["session_id"], df["content_sha"] = 0, 0, "s", "x"
    df["ingested_at"] = df["finalized_at"]
    for c in md.MINUTE_COLUMNS:
        if c not in df:
            df[c] = None
    st.con.register("seed", df[list(md.MINUTE_COLUMNS)])
    st.con.execute("INSERT INTO microstructure_minutes SELECT * FROM seed")
    st.con.unregister("seed")


def test_cutover_and_known_at_are_enforced(tmp_path):
    from market_signal.microstructure import definitions as md
    from market_signal.research.microdir import data

    st = Store(tmp_path / "m.duckdb", tmp_path / "raw")
    mk = sy.market(sy.Scenario("n", coins=("BTC",), days=3, seed=9))["BTC"]
    _seed(st, mk, "BTC")
    frames, meta = data.load_frames(st, sy.T0 + pd.Timedelta(days=3))
    assert frames == {} and meta["cutover"] is None  # scratch/dev: nothing confirmatory
    cut = sy.T0 + pd.Timedelta(days=1)
    st.con.execute("INSERT INTO microstructure_cutover VALUES (?,?,?,?,?,?,?)",
                   [md.FEATURE_VERSION, "rt", cut.to_pydatetime(), "BTC",
                    (cut + pd.Timedelta(minutes=1, seconds=5)).to_pydatetime(), "s",
                    cut.to_pydatetime()])  # fmt: skip
    as_of = sy.T0 + pd.Timedelta(days=2, hours=12, seconds=30)
    frames, _ = data.load_frames(st, as_of)
    f = frames["BTC"]
    assert f["minute_open"].min() == cut  # pre-cutover rows excluded
    seen = f[f["status"] != "MISSING"]
    assert (pd.to_datetime(seen["finalized_at"], utc=True) <= as_of).all()  # known_at


def test_context_category_uses_first_seen_only(tmp_path):
    from datetime import UTC, datetime, timedelta

    from market_signal.context import ledger
    from market_signal.research.microdir import data

    st = Store(tmp_path / "c.duckdb", tmp_path / "raw")
    when = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    seen = when - timedelta(minutes=40)  # Prism first saw the calendar entry 40 m before
    ledger.ingest(st, [{"source_id": "fred_release_calendar", "source_type": "official_statistics",
                        "subcategory": "us_cpi", "title": "CPI", "scheduled": True,
                        "event_time": when.isoformat(), "confidence": "OFFICIAL",
                        "market_wide": True, "dedup_key": f"macro:us_cpi:{when.date()}",
                        "attributes": {"kind": "macro", "series_key": "us_cpi_mom",
                                       "importance": 1}}], now=seen)  # fmt: skip
    ctx = data.context_provider(st)
    at = lambda m: pd.Timestamp(when + timedelta(minutes=m))  # noqa: E731
    assert ctx("BTC", at(-50)) == "none"  # 50 m before: inside 60 m but NOT yet first-seen
    assert ctx("BTC", at(-30)) == "pre_tier1_60m"
    assert ctx("BTC", at(5)) == "post_tier1_0_15m"
    assert ctx("BTC", at(30)) == "post_tier1_15_60m"
    assert ctx("BTC", at(90)) == "none"


# --------------------------------------------------------------------------- costs, BH


def test_cost_application_and_passive_sensitivity():
    from market_signal.research.microdir import outcomes as oc

    ev = pd.DataFrame({**{f"gross_long_{h}": [0.01] for h in sp.HORIZONS_MIN},
                       "funding_long": [0.0001], "path_max": [0.02], "path_min": [-0.005],
                       "t_max": [30], "t_min": [5], "first_touch": [1], "rv_path": [0.01]})  # fmt: skip
    lo = oc.side_view(ev, "long", 6.5, 4.5, 2.0)
    sh = oc.side_view(ev, "short", 6.5, 4.5, 2.0)
    assert lo["net"].iat[0] == pytest.approx(0.01 - 2 * 6.5e-4 - 0.0001)
    assert sh["net"].iat[0] == pytest.approx(-0.01 - 2 * 6.5e-4 + 0.0001)  # shorts receive
    assert lo["net_passive"].iat[0] == pytest.approx(0.01 - (1.5 + 4.5 + 2.0) / 1e4 - 0.0001)
    assert sh["mfe"].iat[0] == 0.005 and sh["mae"].iat[0] == -0.02


def test_bh_is_applied_within_each_family(defn):
    from market_signal.research.lab.batch import benjamini_hochberg

    r = _run(defn, "null", seed=4)
    for fam, b in r["bh"].items():
        keys = [k for k, v in r["members"].items() if v["family"] == fam]
        assert sorted(b["p"]) == sorted(keys)
        assert b["q"] == pytest.approx(benjamini_hochberg(b["p"]))


def test_frozen_study_identity_is_pinned(defn):
    # A deliberate definition change is a NEW study (new name + ID), never an edit of this one.
    assert defn.study_id == (
        "sstudy_16c5f72d6c4ec79e34d7d7bda50050323219d27df17fc2f76189afcc7684c8a9"
    )
    assert sp.definition_digest() == (
        "microdirdef_7304145184665359aed858721cf1c04f1e959185ce84bba0a22f442564dd2b6c"
    )
