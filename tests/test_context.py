"""Phase 23 context intelligence: first-seen semantics, revisions, dedup, mapping, expiry,
macro state/surprise, positioning/crowding, snapshots, providers, import, thesis, boundaries."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from market_signal.context import ledger
from market_signal.context.entities import load_entities, parse
from market_signal.context.macro import macro_state, surprise, window_flags
from market_signal.context.model import Observation
from market_signal.context.positioning import (
    TERMINOLOGY,
    capture_hl_hourly,
    crowding,
    parse_ratio,
    positioning_context,
    vulnerability,
)
from market_signal.context.providers.base import FetchResult, provider_health, run_provider
from market_signal.context.providers.calendar import (
    CentralBankCalendarProvider,
    FredActualsProvider,
    scheduled_utc,
)
from market_signal.context.providers.hyperliquid import HyperliquidUniverseProvider
from market_signal.context.providers.importer import import_events
from market_signal.context.providers.rss import classify, parse_feed
from market_signal.context.snapshot import ContextIndex, context_snapshot
from market_signal.context.taxonomy import Confidence, materiality
from market_signal.context.thesis import (
    build_thesis,
    load_snapshot,
    proposal_from_ai,
    record_thesis,
)
from market_signal.data.http import HttpClient
from market_signal.data.store import MIGRATIONS, Store

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "market_signal"


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "ctx.duckdb", tmp_path / "raw")
    yield s
    s.close()


def obs(**kw) -> dict:
    base = {"source_id": "wire_a", "source_type": "news_wire", "subcategory": "exploit_suspected",
            "title": "Suspected exploit on Aave", "confidence": "REPORTED"}  # fmt: skip
    return base | kw


def ingest(st, *items, at):
    return ledger.ingest(st, list(items), now=at)


def only_event(st) -> str:
    ids = st.query("SELECT event_id FROM context_events")["event_id"].tolist()
    assert len(ids) == 1
    return ids[0]


# --------------------------------------------------------------------------- first-seen


def test_publication_before_prism_saw_it_is_not_known_until_first_seen(st):
    published = T0 - 6 * H  # the source published at 06:00; Prism first saw it at 12:00
    ingest(st, obs(published_at=published.isoformat()), at=T0)
    eid = only_event(st)
    assert ledger.state_asof(st, eid, T0 - timedelta(seconds=1)) is None
    assert ledger.states_asof(st, T0 - 3 * H) == []
    assert ledger.state_asof(st, eid, T0)["first_seen_at"] == T0.isoformat()
    snap_before = context_snapshot(st, "AAVE", T0 - H, include_positioning=False)
    assert snap_before["active_events"] == [] and snap_before["recent_events"] == []
    snap = context_snapshot(st, "AAVE", T0 + H, include_positioning=False)
    ev = snap["active_events"][0]
    assert ev["information_latency_sec"] == 6 * 3600  # measured, not used as availability
    # the relevance window never starts before Prism saw it
    assert pd.Timestamp(ev["relevance"]["start"]) == pd.Timestamp(T0)


def test_provider_cannot_supply_first_seen_or_observed_time(st):
    r = ingest(
        st, obs(first_seen_at=(T0 - 9 * H).isoformat()), obs(observed_at=T0.isoformat()), at=T0
    )
    assert len(r.rejected) == 2 and st.query("SELECT count(*) n FROM context_events")["n"][0] == 0


def test_historical_record_keeps_truthful_first_seen_and_is_flagged(st):
    ingest(st, obs(published_at="2024-05-01T10:00:00+00:00", observation_mode="historical"), at=T0)
    s = ledger.state_asof(st, only_event(st), T0)
    assert s["first_seen_at"] == T0.isoformat() and s["observation_mode"] == "historical"
    assert s["published_at"].startswith("2024-05-01")


# --------------------------------------------------------------------------- revisions


def test_suspected_then_confirmed_exploit_never_rewrites_history(st):
    ingest(st, obs(source_id="social_x", source_type="social", confidence="CONFIRMED"), at=T0)
    eid = only_event(st)
    early = context_snapshot(st, "AAVE", T0 + H, include_positioning=False, record=True)
    ingest(st, obs(source_id="aave_blog", source_type="protocol_announcement",
                   subcategory="exploit_confirmed", title="Aave confirms exploit", confidence="OFFICIAL",
                   attributes={"kind": "security", "status": "confirmed", "loss_usd_estimate": 2.5e7}),
           at=T0 + 2 * H)  # fmt: skip
    s1 = ledger.state_asof(st, eid, T0 + H)
    assert (s1["subcategory"], s1["confidence"]) == (
        "exploit_suspected",
        "REPORTED",
    )  # social capped
    s2 = ledger.state_asof(st, eid, T0 + 3 * H)
    assert (s2["subcategory"], s2["confidence"]) == ("exploit_confirmed", "OFFICIAL")
    assert s2["sources"] == ["social_x", "aave_blog"] and s2["first_seen_at"] == T0.isoformat()
    assert [h["kind"] for h in s2["history"]] == ["observed", "status_change"]
    # the earlier snapshot is reproducible after the update arrived
    again = context_snapshot(st, "AAVE", T0 + H, include_positioning=False)
    assert again["snapshot_id"] == early["snapshot_id"]
    stored = load_snapshot(st, early["snapshot_id"])
    assert stored["active_events"][0]["subcategory"] == "exploit_suspected"
    # the first observation row itself is untouched
    row = st.query("SELECT subcategory, confidence FROM context_events").iloc[0]
    assert (row["subcategory"], row["confidence"]) == ("exploit_suspected", "REPORTED")


def test_structured_update_upgrades_a_generic_first_report(st):
    ingest(st, obs(attributes={"kind": "generic", "facts": {"reported_source": "x"}}), at=T0)
    sec = {
        "kind": "security",
        "status": "confirmed",
        "loss_usd_estimate": 1.2e7,
        "withdrawals_paused": True,
    }
    ingest(st, obs(source_id="aave_blog", source_type="protocol_announcement", confidence="OFFICIAL",
                   subcategory="exploit_confirmed", attributes=sec), at=T0 + H)  # fmt: skip
    s = ledger.state_asof(st, only_event(st), T0 + 2 * H)
    a = s["attributes"]
    assert a["kind"] == "security" and a["loss_usd_estimate"] == 1.2e7 and a["withdrawals_paused"]
    assert a["facts"] == {"reported_source": "x"}
    assert ledger.state_asof(st, only_event(st), T0)["attributes"]["kind"] == "generic"


def test_update_times_are_monotonic_per_event_despite_clock_steps(st):
    ingest(st, obs(), at=T0)
    ingest(st, obs(source_id="b", confidence="CONFIRMED"), at=T0 - timedelta(seconds=0.6))
    s = ledger.state_asof(st, only_event(st), T0)
    assert s["history"][-1]["observed_at"] == T0.isoformat() and s["confidence"] == "CONFIRMED"


def test_false_rumour_later_denied(st):
    ingest(st, obs(source_id="social_x", source_type="social", confidence="UNCONFIRMED"), at=T0)
    ingest(st, obs(source_id="aave_blog", source_type="protocol_announcement", confidence="OFFICIAL",
                   title="Aave: no exploit, funds safe", update_kind="denial"), at=T0 + 2 * H)  # fmt: skip
    eid = only_event(st)
    assert ledger.is_active(ledger.state_asof(st, eid, T0 + H), T0 + H)
    later = ledger.state_asof(st, eid, T0 + 3 * H)
    assert later["confidence"] == "DENIED" and not ledger.is_active(later, T0 + 3 * H)
    assert ledger.state_asof(st, eid, T0 + H)["confidence"] == "UNCONFIRMED"


def test_macro_consensus_revised_before_release(st):
    when = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    key = "macro:us_cpi:2026-10-14"
    base = {"source_id": "fred_release_calendar", "source_type": "official_statistics",
            "subcategory": "us_cpi", "title": "CPI", "scheduled": True, "event_time": when.isoformat(),
            "confidence": "OFFICIAL", "market_wide": True, "dedup_key": key}  # fmt: skip
    attrs = {"kind": "macro", "series_key": "us_cpi_mom", "unit": "pct_mom"}
    ingest(st, base | {"attributes": attrs}, at=when - timedelta(days=10))
    imp = base | {"source_id": "import_manual", "source_type": "manual", "update_kind": "consensus"}
    ingest(st, imp | {"attributes": attrs | {"consensus": 0.3}}, at=when - timedelta(days=3))
    ingest(st, imp | {"attributes": attrs | {"consensus": 0.2}}, at=when - timedelta(days=1))
    ingest(st, base | {"source_id": "fred_alfred", "update_kind": "value_release",
                       "attributes": attrs | {"actual": 0.4, "previous": 0.25}}, at=when + timedelta(minutes=10))  # fmt: skip
    eid = only_event(st)
    pre = ledger.state_asof(st, eid, when - timedelta(days=2))
    assert pre["attributes"]["consensus"] == 0.3 and surprise(pre)["status"] == "pending"
    post = surprise(ledger.state_asof(st, eid, when + 12 * H))
    assert post["consensus"] == 0.2 and post["surprise"] == pytest.approx(0.2)
    assert post["direction_vs_consensus"] == "above"
    assert post["vs_previous"] == pytest.approx(0.15)
    # a consensus first seen AFTER the release is never used
    ingest(st, imp | {"attributes": attrs | {"consensus": 0.39}}, at=when + 13 * H)
    late = surprise(ledger.state_asof(st, eid, when + 14 * H))
    assert late["status"] == "consensus_after_release" and late["surprise"] is None


# --------------------------------------------------------------------------- dedup


def test_same_event_from_many_sources_is_one_event_with_confidence_evolution(st):
    ingest(st, obs(source_id="social_x", source_type="social", confidence="UNCONFIRMED"), at=T0)
    for i in range(10):  # ten articles about the same hack
        ingest(st, obs(source_id=f"site_{i % 3}", source_type="rss", title=f"Aave hack report {i}",
                       source_ref=f"https://news.example/{i}", confidence="REPORTED"), at=T0 + i * H)  # fmt: skip
    eid = only_event(st)
    s = ledger.state_asof(st, eid, T0 + 12 * H)
    assert s["sources"][0] == "social_x" and s["first_seen_at"] == T0.isoformat()
    assert len(s["sources"]) == 4
    # two distinct tier-1/2 sources at REPORTED make it CONFIRMED (corroboration_v1)
    assert s["confidence"] == "CONFIRMED"
    assert [h["confidence"] for h in s["history"]][:3] == ["UNCONFIRMED", "REPORTED", "CONFIRMED"]
    # re-polling the exact same report is a duplicate
    r = ingest(st, obs(source_id="site_0", source_type="rss", title="Aave hack report 0",
                       source_ref="https://news.example/0", confidence="REPORTED"), at=T0 + 20 * H)  # fmt: skip
    assert r.duplicates == 1


def test_different_protocols_and_outside_link_window_are_separate(st):
    ingest(st, obs(), at=T0)
    ingest(st, obs(title="Suspected exploit on Lido"), at=T0 + H)  # different entity
    ingest(st, obs(), at=T0 + timedelta(days=8))  # beyond the 7-day security link window
    assert st.query("SELECT count(*) n FROM context_events")["n"][0] == 3


def test_keyed_observations_never_merge_through_a_shared_url(st):
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    for d in ("2026-10-28", "2026-12-09"):
        ingest(st, {"source_id": "fomc_schedule", "source_type": "central_bank", "source_ref": url,
                    "subcategory": "fomc_decision", "title": "FOMC", "scheduled": True,
                    "event_time": f"{d}T18:00:00+00:00", "confidence": "OFFICIAL",
                    "dedup_key": f"macro:fomc_decision:{d}"}, at=T0)  # fmt: skip
    assert st.query("SELECT count(*) n FROM context_events")["n"][0] == 2


# --------------------------------------------------------------------------- mapping


def test_asset_mapping_is_explicit():
    em = load_entities()
    assert em.entities_in("Hyperliquid pauses HyperEVM deposits") == ["hyperliquid"]
    links = {(x.asset, x.link_type) for x in em.links(entities=["aave"])}
    assert links == {("AAVE", "direct"), ("ETH", "ecosystem")}
    assert em.entities_in("Banking Circle maintenance") == []  # no bare 'circle'
    assert em.entities_in("the eth bridge") == []  # tickers only match upper case
    assert em.entities_in("ETH withdrawals delayed") == ["ethereum"]
    assert em.entities_in("Monero (XMR) delisted") == ["monero"]
    assert {
        (x.asset, x.link_type) for x in em.links(entities=em.entities_in("privacy coins ban"))
    } == {("XMR", "ecosystem"), ("ZEC", "ecosystem")}
    assert em.links(entities=["not_an_entity"]) == []
    mw = {x.asset for x in em.links(market_wide=True)}
    assert {"BTC", "HYPE", "XMR"} <= mw
    assert em.version.startswith("entities_v1:")
    em2 = parse({"version": "entities_v1", "entities": {"x": {"kind": "asset", "assets": ["X"]}}})
    assert em2.version != em.version


def test_listing_on_a_venue_does_not_link_the_venue_token(st):
    ingest(st, {"source_id": "hyperliquid_meta", "source_type": "exchange_api", "subcategory": "listing",
                "title": "Hyperliquid perp listing: FOO", "confidence": "OFFICIAL", "assets": ["FOO"],
                "attributes": {"kind": "listing", "exchange": "hyperliquid", "action": "listing"}}, at=T0)  # fmt: skip
    assert st.query("SELECT asset FROM context_asset_links")["asset"].tolist() == ["FOO"]
    ingest(st, {"source_id": "rss_hl", "source_type": "exchange_notice", "subcategory": "exchange_outage",
                "title": "Hyperliquid API outage", "confidence": "OFFICIAL", "scope": "exchange"}, at=T0)  # fmt: skip
    assert "HYPE" in set(st.query("SELECT asset FROM context_asset_links")["asset"])


# --------------------------------------------------------------------------- expiry


def test_old_event_leaves_active_context(st):
    ingest(st, obs(subcategory="exploit_confirmed", confidence="CONFIRMED"), at=T0)
    s = ledger.state_asof(st, only_event(st), T0 + H)
    assert ledger.is_active(s, T0 + timedelta(days=6))
    assert not ledger.is_active(s, T0 + timedelta(days=7, seconds=1))
    snap = context_snapshot(st, "AAVE", T0 + timedelta(days=8), include_positioning=False)
    assert snap["active_events"] == []
    # provider/manual override of the default window
    ingest(st, obs(title="Lido exploit", relevance_end=(T0 + 5 * H).isoformat()), at=T0)
    lido = next(x for x in ledger.states_asof(st, T0 + H) if "Lido" in x["title"])
    assert ledger.is_active(lido, T0 + 4 * H) and not ledger.is_active(lido, T0 + 6 * H)


# --------------------------------------------------------------------------- scheduled macro


def _cpi(st, when, seen):
    ingest(st, {"source_id": "fred_release_calendar", "source_type": "official_statistics",
                "subcategory": "us_cpi", "title": "CPI", "scheduled": True, "event_time": when.isoformat(),
                "confidence": "OFFICIAL", "market_wide": True, "dedup_key": f"macro:us_cpi:{when.date()}",
                "attributes": {"kind": "macro", "series_key": "us_cpi_mom", "importance": 1}}, at=seen)  # fmt: skip


def test_scheduled_macro_pre_and_post_state(st):
    when = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    _cpi(st, when, seen=when - timedelta(days=20))
    states = lambda t: ledger.states_asof(st, t)  # noqa: E731
    before_known = macro_state(states(when - timedelta(days=21)), when - timedelta(days=21))
    assert before_known["next_tier1"] is None  # Prism did not know the schedule yet
    m = macro_state(states(when - timedelta(minutes=20)), when - timedelta(minutes=20))
    assert m["minutes_to_next_tier1"] == 20 and m["next_tier1"]["subcategory"] == "us_cpi"
    assert m["tier1_within"] == {"5m": False, "15m": False, "30m": True, "60m": True}
    assert m["sensitive_next_24h"]["usd"] and m["sensitive_next_24h"]["global_rates"]
    assert not m["sensitive_next_24h"]["jpy"]
    post = macro_state(states(when + H), when + H)
    assert post["in_post_event_window"] and post["minutes_since_last_tier1"] == 60
    assert not macro_state(states(when + 5 * H), when + 5 * H)["in_post_event_window"]
    wf = window_flags(states(when)[0], when - timedelta(minutes=10))
    assert wf["within_15m"] and not wf["within_5m"] and wf["pre"]


def test_surprise_hand_calculated():
    def st_(i, actual, cons, t):
        return {"event_id": f"e{i}", "event_time": t.isoformat(),
                "attributes": {"kind": "macro", "series_key": "s", "actual": actual, "consensus": cons,
                               "previous": 0.1, "revision_of_previous": 0.15},
                "attr_seen": {"consensus": (t - H).isoformat(), "actual": t.isoformat()}}  # fmt: skip

    hist = [st_(i, 0.2 + d, 0.2, T0 - timedelta(days=30 * (i + 1)))
            for i, d in enumerate([0.1, -0.1, 0.2, -0.2, 0.1, -0.1, 0.0, 0.0])]  # fmt: skip
    cur = st_(99, 0.5, 0.3, T0)
    r = surprise(cur, hist)
    assert r["surprise"] == pytest.approx(0.2) and r["abs_surprise"] == pytest.approx(0.2)
    sd = float(np.std([0.1, -0.1, 0.2, -0.2, 0.1, -0.1, 0.0, 0.0]))
    assert r["standardised"] == pytest.approx(0.2 / sd) and r["n_history"] == 8
    assert r["revision_impact"] == pytest.approx(0.05)
    assert surprise(cur, hist[:7])["standardised"] is None  # too little history
    assert "bull" not in json.dumps(r) and "bear" not in json.dumps(r)


def test_materiality_is_versioned_and_not_directional():
    m = materiality("exploit_confirmed", Confidence.CONFIRMED, link="ecosystem", loss_usd=2e8)
    assert m["version"] == "materiality_v1" and m["score"] == round(100 * 0.9 * 0.6)
    assert materiality("exploit_suspected", Confidence.DENIED)["score"] == 0
    assert "not a direction" in m["meaning"]


# --------------------------------------------------------------------------- positioning


def _hl(st, coin, at, oi, mark=100.0, oracle=100.0, funding=1e-5):
    st.con.execute("INSERT INTO perp_snapshots (coin, source, snapshot_at, mark_px, oracle_px, funding_rate, "
                   "open_interest, oi_notional, ingest_run_id) VALUES (?, 'hyperliquid', ?, ?, ?, ?, ?, ?, 'r')",
                   [coin, at, mark, oracle, funding, oi, oi * mark])  # fmt: skip


def _bn(st, coin, obs_at, oi, ingested):
    st.con.execute("INSERT INTO perp_oi_history VALUES ('binance', ?, ?, 'usdm_perpetual', '1h', ?, ?, ?, "
                   "'provider:sumOpenInterestValue', ?, ?, 'r')",
                   [coin, f"{coin}USDT", obs_at, oi, oi * 100, ingested, ingested])  # fmt: skip


def test_oi_context_keeps_venues_separate_and_strict_availability(st):
    for k in range(60):
        t = T0 - timedelta(hours=59 - k)
        _hl(st, "HYPE", t, 1000 + k)
        _bn(st, "HYPE", t - H, 5_000_000 + k, ingested=T0 + 30 * 60 * H / 3600)  # one late backfill
    p = positioning_context(st, "HYPE", T0)
    hl = p["venues"]["hyperliquid"]
    assert hl["open_interest"] == 1059 and "binance" not in p["venues"]  # not ingested yet
    assert hl["oi_change_24h_pct"] == pytest.approx((1059 / 1035 - 1) * 100)
    later = positioning_context(st, "HYPE", T0 + H)
    bn = later["venues"]["binance"]
    # once ingested, the whole backfilled history is usable (indexed by market time)
    assert bn["open_interest"] == 5_000_059
    assert bn["oi_change_24h_pct"] == pytest.approx(
        (5_000_059 / 5_000_036 - 1) * 100
    )  # t-24h = T0-23h
    assert later["venues"]["hyperliquid"]["open_interest"] == 1059  # venues never mix
    stale = positioning_context(st, "HYPE", T0 + 4 * H)
    assert stale["venues"]["hyperliquid"]["open_interest"] is None  # older than 3h: withheld
    assumed = positioning_context(st, "HYPE", T0, strict=False)
    assert assumed["venues"]["binance"]["availability"] == "assumed_latency"


def test_crowding_never_claims_skew_without_a_skew_measure():
    c = crowding({"oi_change_24h_z": 3.0, "funding_pct_90d": None, "ls_account_pct_30d": None})
    assert c["leverage"] == "expanding" and c["skew"] == "unknown"
    assert (
        crowding({"oi_change_24h_z": 3.0, "funding_pct_90d": 0.95})["skew"] == "crowded_long_like"
    )
    assert crowding({"oi_change_24h_z": 0.0, "funding_pct_90d": 0.95})["skew"] == "neutral"
    assert crowding({"oi_change_24h_z": 2.0, "funding_pct_90d": 0.05,
                     "ls_account_pct_30d": 0.02})["skew"] == "crowded_short_like"  # fmt: skip
    assert crowding({})["leverage"] == "unknown"
    assert "equal long and short" in TERMINOLOGY
    txt = json.dumps([crowding({"oi_change_24h_z": z, "funding_pct_90d": f})
                      for z in (-3, 0, 3) for f in (None, 0.05, 0.5, 0.95)]).lower()  # fmt: skip
    assert "mostly long" not in txt and "manipulat" not in txt and "liquidation level" not in txt


def test_vulnerability_counts_observable_ingredients():
    v = vulnerability({"funding_pct_90d": 0.95, "oi_change_24h_z": 2.0, "price_change_24h_pct": 0.1,
                       "ls_account_pct_30d": None}, catalyst_24h=True)  # fmt: skip
    assert v["long_side"]["present"] == 4 and v["long_side"]["level"] == "high"
    assert v["short_side"]["components"]["funding_extreme"] is False
    assert vulnerability({}, None)["long_side"]["level"] == "unknown"


def test_binance_ratio_parsing():
    df = parse_ratio("global_account", [{"symbol": "BTCUSDT", "longAccount": "0.49", "shortAccount": "0.51",
                                         "longShortRatio": "0.96", "timestamp": 1791295200000}])  # fmt: skip
    assert df["ratio"].iloc[0] == pytest.approx(0.96) and df["long_share"].iloc[0] == pytest.approx(
        0.49
    )
    tk = parse_ratio("taker_volume", [{"buySellRatio": "1.2", "buyVol": "12", "sellVol": "10",
                                       "timestamp": 1791295200000}])  # fmt: skip
    assert tk["buy_volume"].iloc[0] == 12


class _Ctx:
    def __init__(self):
        self.calls = 0

    def perp_contexts(self):
        self.calls += 1
        return pd.DataFrame([{"coin": c, "open_interest": 10.0, "oi_notional": 1000.0, "mark_px": 100.0,
                              "oracle_px": 100.1, "mid_px": 100.0, "funding_rate": 1e-5, "premium": 0.0,
                              "impact_bid_px": 99.9, "impact_ask_px": 100.1, "day_ntl_vlm": 1.0}
                             for c in ("BTC", "HYPE")])  # fmt: skip

    def drain_raw(self):
        return []


def test_hl_fixed_hour_capture_is_idempotent_and_on_grid(st):
    p = _Ctx()
    r = capture_hl_hourly(st, ["BTC", "HYPE", "XYZ"], p, now=T0 + timedelta(minutes=4))
    assert r["status"] == "ok" and r["written"] == 2 and r["not_listed"] == ["XYZ"]
    again = capture_hl_hourly(st, ["BTC", "HYPE"], p, now=T0 + timedelta(minutes=40))
    assert again["status"] == "skipped" and p.calls == 1
    assert capture_hl_hourly(st, ["BTC", "HYPE"], p, now=T0 + timedelta(minutes=64))["written"] == 2
    rows = st.query("SELECT grid_hour, captured_at FROM context_hl_oi_hourly ORDER BY grid_hour")
    assert len(rows) == 4
    with pytest.raises(Exception, match=r"(?i)constraint"):  # a capture can never sit off its hour
        st.con.execute("INSERT INTO context_hl_oi_hourly (coin, grid_hour, captured_at, cadence_version, run_id) "
                       "VALUES ('ETH', ?, ?, 'v', 'r')", [T0, T0 + 2 * H])  # fmt: skip
    # the snapshot positioning only sees captures made by t
    assert (
        positioning_context(st, "HYPE", T0 + timedelta(minutes=3))["venues"]["hyperliquid"][
            "open_interest"
        ]
        is None
    )
    assert (
        positioning_context(st, "HYPE", T0 + timedelta(minutes=5))["venues"]["hyperliquid"][
            "open_interest"
        ]
        == 10.0
    )


# --------------------------------------------------------------------------- providers


class _Fixed:
    name, stale_hours = "fixed", 2.0

    def __init__(self, items, fail=False):
        self.items, self.fail = items, fail

    def fetch(self, now, last_state):
        if self.fail:
            from market_signal.data.http import ProviderError

            raise ProviderError("boom")
        return FetchResult(observations=list(self.items), received=len(self.items))


def test_provider_retry_is_idempotent_and_failure_writes_nothing(st):
    p = _Fixed([Observation.model_validate(obs())])
    a = run_provider(st, p, now=T0)
    b = run_provider(st, p, now=T0 + H)  # restart / retry: same item again
    assert (a["new_events"], b["new_events"], b["duplicates"]) == (1, 0, 1)
    assert ledger.state_asof(st, only_event(st), None)["first_seen_at"] == T0.isoformat()
    f = run_provider(st, _Fixed([], fail=True), now=T0 + 2 * H)
    assert f["status"] == "failed"
    assert st.query("SELECT count(*) n FROM context_events")["n"][0] == 1


def test_stale_and_failing_provider_health_flags_snapshot(st):
    run_provider(st, _Fixed([]), now=T0)
    h = {x["provider"]: x for x in provider_health(st, {"fixed": 2.0, "never": 1.0}, now=T0 + H)}
    assert h["fixed"]["state"] == "OK" and h["never"]["state"] == "NEVER_RUN"
    assert provider_health(st, {"fixed": 2.0}, now=T0 + 3 * H)[0]["state"] == "STALE"
    for k in range(3):
        run_provider(st, _Fixed([], fail=True), now=T0 + (4 + k) * H)
    hf = provider_health(st, {"fixed": 2.0}, now=T0 + 8 * H)[0]
    assert (
        hf["state"] == "FAILING" and hf["consecutive_failures"] == 3 and hf["last_error"] == "boom"
    )
    snap = context_snapshot(
        st, "BTC", T0 + 3 * H, stale_hours={"fixed": 2.0}, include_positioning=False
    )
    assert "provider fixed STALE" in snap["freshness"]["warnings"]
    # health is reproducible as of an earlier time (runs finished later are ignored)
    assert provider_health(st, {"fixed": 2.0}, now=T0 + H)[0]["state"] == "OK"


def test_central_bank_calendar_times_and_rerun(st):
    cal = {"central_banks": {"fomc": {"subcategory": "fomc_decision", "series_key": "ffr", "time": "14:00",
                                      "tz": "America/New_York", "source": "https://fed.example", "country": "US",
                                      "decision_dates": ["2026-10-28", "2026-12-09"]},
                             "boj": {"subcategory": "boj_decision", "series_key": "boj", "time": "12:00",
                                     "tz": "Asia/Tokyo", "time_precision": "approximate",
                                     "source": "https://boj.example", "decision_dates": ["2026-10-30"]}}}  # fmt: skip
    p = CentralBankCalendarProvider(cal)
    assert run_provider(st, p, now=T0)["new_events"] == 3
    assert run_provider(st, p, now=T0 + H)["duplicates"] == 3
    times = dict(
        st.query("SELECT dedup_key, event_time FROM context_events").itertuples(index=False)
    )
    assert pd.Timestamp(times["macro:fomc_decision:2026-10-28"]) == pd.Timestamp(
        "2026-10-28T18:00Z"
    )  # EDT
    assert pd.Timestamp(times["macro:fomc_decision:2026-12-09"]) == pd.Timestamp(
        "2026-12-09T19:00Z"
    )  # EST
    assert pd.Timestamp(times["macro:boj_decision:2026-10-30"]) == pd.Timestamp("2026-10-30T03:00Z")
    assert scheduled_utc(datetime(2026, 3, 9).date(), "08:30", "America/New_York").hour == 12


def test_fred_actuals_first_print_previous_and_revision(st):
    when = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
    ingest(st, {"source_id": "fred_release_calendar", "source_type": "official_statistics",
                "subcategory": "us_nfp", "title": "Employment Situation", "scheduled": True,
                "event_time": when.isoformat(), "confidence": "OFFICIAL", "market_wide": True,
                "dedup_key": "macro:us_nfp:2026-10-02",
                "attributes": {"kind": "macro", "series_key": "us_nfp_change"}}, at=when - timedelta(days=5))  # fmt: skip
    vint = {
        "2026-10-01": {"2026-07-01": 1000, "2026-08-01": 1162},
        "2026-10-02": {"2026-07-01": 1000, "2026-08-01": 1133, "2026-09-01": 1162},
    }

    def handler(req: httpx.Request):
        rt = req.url.params["realtime_start"]
        obs_ = [{"date": d, "value": str(v)} for d, v in vint[rt].items()]
        return httpx.Response(200, json={"observations": obs_})

    http = HttpClient("fred", "https://api.stlouisfed.org", requests_per_second=0,
                      transport=httpx.MockTransport(handler))  # fmt: skip
    cal = {"fred_releases": [{"release_id": 50, "name": "Employment Situation", "time": "08:30",
                              "tz": "America/New_York",
                              "events": [{"subcategory": "us_nfp", "series_key": "us_nfp_change",
                                          "fred_series": "PAYEMS", "transform": "diff", "unit": "k_jobs"}]}]}  # fmt: skip
    r = run_provider(st, FredActualsProvider(http, "k", cal, st), now=when + H)
    assert r["new_updates"] == 1
    s = ledger.state_asof(st, only_event(st), when + 2 * H)
    a = s["attributes"]
    assert (a["actual"], a["previous"], a["revision_of_previous"]) == (29.0, 162.0, 133.0)
    assert s["attr_seen"]["actual"] == (when + H).isoformat()
    assert (
        ledger.state_asof(st, only_event(st), when + timedelta(minutes=30))["attributes"].get(
            "actual"
        )
        is None
    )
    assert (
        run_provider(st, FredActualsProvider(http, "k", cal, st), now=when + 2 * H)["received"] == 0
    )


STATUS_RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Degraded Performance - Trading API</title><link>https://status.x/incidents/abc1</link>
<description>&lt;strong&gt;Investigating&lt;/strong&gt; - errors placing orders</description>
<pubDate>Tue, 06 Oct 2026 11:30:00 -0000</pubDate></item>
<item><title>Krak Card degraded performance.</title><link>https://status.x/incidents/abc2</link>
<description>&lt;strong&gt;Investigating&lt;/strong&gt;</description><pubDate>Tue, 06 Oct 2026 11:00:00 -0000</pubDate></item>
<item><title>Delayed Sends - ALEO</title><link>https://status.x/incidents/abc3</link>
<description>x</description><pubDate>Tue, 06 Oct 2026 10:00:00 -0000</pubDate></item>
<item><title>WMTX-USD to Limit Only</title><link>https://status.x/incidents/abc4</link>
<description>x</description><pubDate>Tue, 06 Oct 2026 10:00:00 -0000</pubDate></item>
<item><title>ETH and ERC20 tokens delayed withdrawals</title><link>https://status.x/incidents/abc5</link>
<description>x</description><pubDate>Tue, 06 Oct 2026 09:00:00 -0000</pubDate></item>
</channel></rss>"""


def test_rss_statuspage_classifier_keeps_only_material_mapped_items(st):
    em = load_entities()
    feed = {"kind": "statuspage", "exchange": "kraken", "source_type": "exchange_notice"}
    out = [classify(i, "kraken_status", feed, em) for i in parse_feed(STATUS_RSS)]
    kept = [(o.subcategory, o.title) for o in out if o is not None]
    assert kept == [
        ("exchange_outage", "Degraded Performance - Trading API"),
        ("exchange_deposit_withdrawal_disruption", "ETH and ERC20 tokens delayed withdrawals"),
    ]
    first = out[0]
    ingest(st, first, at=T0)
    resolved = parse_feed(STATUS_RSS.replace(b"Investigating", b"Resolved"))[0]
    ingest(st, classify(resolved, "kraken_status", feed, em), at=T0 + H)
    s = ledger.state_asof(st, only_event(st), T0 + 2 * H)
    assert (
        s["history"][-1]["kind"] == "resolution"
        and s["attributes"]["facts"]["status"] == "resolved"
    )


def test_fomc_statement_attaches_to_the_scheduled_decision(st):
    when = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    ingest(st, {"source_id": "fomc_schedule", "source_type": "central_bank", "subcategory": "fomc_decision",
                "title": "FOMC policy decision", "scheduled": True, "event_time": when.isoformat(),
                "confidence": "OFFICIAL", "dedup_key": "macro:fomc_decision:2026-10-28"}, at=T0)  # fmt: skip
    item = {"title": "Federal Reserve issues FOMC statement", "link": "https://fed.example/a.htm", "guid": "",
            "description": "", "published": "Wed, 28 Oct 2026 18:00:00 GMT", "category": ""}  # fmt: skip
    o = classify(
        item, "fed_press", {"kind": "central_bank", "source_type": "central_bank"}, load_entities()
    )
    ingest(st, o, at=when + timedelta(minutes=3))
    s = ledger.state_asof(st, only_event(st), when + H)
    assert s["history"][-1]["kind"] == "value_release" and len(s["history"]) == 2


def test_hyperliquid_universe_baseline_then_listing():
    calls = iter([{"universe": [{"name": "BTC"}, {"name": "HYPE"}]},
                  {"universe": [{"name": "BTC"}, {"name": "HYPE", "isDelisted": True}, {"name": "NEWC"}]}])  # fmt: skip
    http = HttpClient("hyperliquid", "https://api.hyperliquid.xyz", requests_per_second=0,
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(calls))))  # fmt: skip
    p = HyperliquidUniverseProvider(http)
    a = p.fetch(T0, {})
    assert a.observations == [] and a.state["universe"] == {"BTC": False, "HYPE": False}
    b = p.fetch(T0 + H, a.state)
    got = {(o.subcategory, o.assets) for o in b.observations}
    assert got == {("listing", ("NEWC",)), ("delisting", ("HYPE",))}


# --------------------------------------------------------------------------- external import


def _batch(**item) -> str:
    base = {"event_time": "2026-10-01T09:00:00Z", "first_seen_at": "2026-10-01T10:00:00Z",
            "source": "Protocol status page", "url": "https://status.aave.example/1", "assets": ["AAVE"],
            "entities": ["aave"], "category": "security", "subcategory": "exploit_suspected",
            "title": "Aave reports suspicious withdrawals", "summary": "Investigating.",
            "confidence": "CONFIRMED"}  # fmt: skip
    return json.dumps(
        {"schema": "context_import_v1", "producer": "chatgpt_work", "items": [base | item]}
    )


def test_external_import_is_validated_capped_and_deduplicated(st):
    r = import_events(st, _batch(), now=T0)
    assert (r["new_events"], r["invalid"]) == (1, [])
    s = ledger.state_asof(st, only_event(st), T0)
    assert s["confidence"] == "REPORTED"  # tier-3 AI monitor capped
    assert s["first_seen_at"] == T0.isoformat()  # Prism's receipt, not the monitor's sighting
    assert s["reported_first_seen_at"].startswith("2026-10-01T10:00")
    assert import_events(st, _batch(), now=T0 + H)["duplicates"] == 1
    bad = [import_events(st, _batch(direction="short"), now=T0)["invalid"],
           import_events(st, _batch(attributes={"kind": "generic", "facts": {"size": 1}}), now=T0)["invalid"],
           import_events(st, _batch(first_seen_at="2026-10-02T00:00:00Z"), now=T0)["invalid"],
           import_events(st, _batch(entities=["madeup"]), now=T0)["invalid"],
           import_events(st, _batch(subcategory="us_cpi"), now=T0)["invalid"],
           import_events(st, _batch(url="ftp://x"), now=T0)["invalid"]]  # fmt: skip
    assert all(len(b) == 1 for b in bad)
    assert "AI boundary" in bad[0][0]["error"] and "AI boundary" in bad[1][0]["error"]
    with pytest.raises(ValueError):
        import_events(st, '{"schema": "context_import_v1", "producer": "x", "items": []}', now=T0)
    with pytest.raises(ValueError):  # duplicate keys
        import_events(st, '{"schema": "a", "schema": "b"}', now=T0)


# --------------------------------------------------------------------------- thesis


def test_thesis_cites_all_evidence_and_rejects_unknown_facts(st):
    ingest(st, obs(confidence="REPORTED"), at=T0)
    eid = only_event(st)
    snap = context_snapshot(st, "AAVE", T0 + H, include_positioning=True, record=True)
    th = build_thesis(snap, direction="short", hypothesis_rule="manual",
                      invalidation={"kind": "event_denied", "description": "protocol denies exploit"},
                      expires_at=T0 + 25 * H, cited_event_ids=(eid,))  # fmt: skip
    tid = record_thesis(st, th)
    row = st.query("SELECT status, snapshot_id, payload FROM context_theses").iloc[0]
    assert row["status"] == "research_only" and row["snapshot_id"] == snap["snapshot_id"]
    p = json.loads(row["payload"])
    assert (
        p["evidence"]["event_ids"] == [eid] and p["evidence"]["snapshot_id"] == snap["snapshot_id"]
    )
    assert {"snapshot", "macro_state", "activity", "thesis", "crowding_hyperliquid"} <= set(
        p["evidence"]["rule_versions"]
    )
    assert tid.startswith("thesis_") and p["context"]["catalyst_subcategories"] == [
        "exploit_suspected"
    ]
    with pytest.raises(ValueError, match="not in the snapshot"):
        build_thesis(snap, direction="long", hypothesis_rule="m", expires_at=T0 + 5 * H,
                     invalidation={"kind": "time", "description": "x"}, cited_event_ids=("ctxev_fake",))  # fmt: skip
    with pytest.raises(ValueError):
        build_thesis(snap, direction="long", hypothesis_rule="m", expires_at=T0,
                     invalidation={"kind": "time", "description": "x"})  # fmt: skip
    with pytest.raises(Exception, match=r"(?i)constraint"):
        st.con.execute("INSERT INTO context_theses VALUES ('x', 'AAVE', ?, ?, 'live', ?, '{}')",
                       [T0, T0, snap["snapshot_id"]])  # fmt: skip
    ai = {"asset": "AAVE", "as_of": (T0 + H).isoformat(), "cited_event_ids": [eid],
          "direction_hypothesis": "short", "invalidation": {"kind": "time", "description": "24h"},
          "expires_at": (T0 + 20 * H).isoformat()}  # fmt: skip
    assert proposal_from_ai(st, json.dumps(ai)).origin == "ai_proposal"
    with pytest.raises(ValueError, match="AI boundary"):
        proposal_from_ai(st, json.dumps(ai | {"size": 10_000}))
    with pytest.raises(ValueError, match="not in the snapshot"):
        proposal_from_ai(st, json.dumps(ai | {"cited_event_ids": ["ctxev_invented"]}))


# --------------------------------------------------------------------------- research adapters


def _bars(st, coin, start, n, f):
    rows = []
    for i in range(n):
        o = start + timedelta(minutes=15 * i)
        c = f(i)
        rows.append(
            (
                "hyperliquid",
                coin,
                "15m",
                o,
                o + timedelta(minutes=15),
                c,
                c * 1.001,
                c * 0.999,
                c,
                100.0,
                1,
                "native",
                o + timedelta(minutes=16),
                "r",
                True,
                0,
                o + timedelta(minutes=16),
                "r",
            )
        )
    st.con.executemany("INSERT INTO perp_intraday_bars VALUES (" + ",".join("?" * 18) + ")", rows)


def test_event_study_and_reaction_time_are_hand_checkable(st):
    start = T0 - timedelta(days=2)
    _bars(
        st, "HYPE", start, 400, lambda i: 100.0 if i < 192 else 102.0
    )  # +2% at T0 (bar 192 closes T0+15m)
    _bars(st, "BTC", start, 400, lambda i: 50.0)
    ingest(st, obs(title="Hyperliquid incident", source_id="wire_b"), at=T0)
    from market_signal.context.event_study import event_study

    ev = ledger.states_asof(st, T0 + H)
    df = event_study(st, ev, ["HYPE"], horizons_min=(15, 60))
    r = df.iloc[0]
    assert r["status"] == "ok" and r["basis"] == "prism_first_seen"
    assert r["ret_60m_pct"] == pytest.approx(2.0) and r["abn_60m_pct"] == pytest.approx(2.0)
    assert r["minutes_to_1.0pct"] == 15 and r["minutes_to_2.0pct"] == 15
    assert r["persistent"] and r["pre_return_pct"] == pytest.approx(0.0)
    pub = event_study(st, [ev[0] | {"published_at": None}], ["HYPE"], basis="published")
    assert pub.iloc[0]["status"] == "no_price"  # no publication time: never silently substituted


def test_phase22_probe_adapter_attaches_point_in_time_context(st):
    from market_signal.context.probes import attach_context, interesting_variants

    when = T0 + 2 * H
    _cpi(st, when, seen=T0 - timedelta(days=3))
    ingest(st, obs(title="Hyperliquid exploit suspected"), at=T0)
    frame = pd.DataFrame(
        {
            "coin": ["HYPE", "HYPE"],
            "d": [1, 1],
            "signal_ns": [pd.Timestamp(T0 - H).value, pd.Timestamp(T0 + H).value],
        }
    )
    out = attach_context(st, frame)
    assert out["ctx_active_events"].tolist() == [
        0,
        1,
    ]  # the exploit was unknown at the first signal
    assert out["ctx_minutes_to_tier1"].tolist() == [180, 60]
    assert out.attrs["context_probe_version"] == "phase22_context_probe_v1"
    payload = {"strategies": [{"key": "a", "strategy_id": "i1", "side": "long", "timeframe": "1h",
                               "family": "f", "verdict": "INTERESTING"},
                              {"key": "b", "strategy_id": "i2", "side": "long", "timeframe": "1h",
                               "family": "f", "verdict": "REJECTED"}]}  # fmt: skip
    assert [v["key"] for v in interesting_variants(payload)] == ["a"]


def test_context_index_matches_database_folds(st):
    ingest(st, obs(), at=T0)
    ingest(
        st, obs(source_id="b", subcategory="exploit_confirmed", confidence="CONFIRMED"), at=T0 + H
    )
    idx = ContextIndex.load(st)
    for t in (T0 - H, T0, T0 + 2 * H):
        a = [(s["event_id"], s["subcategory"], s["confidence"]) for s in idx.states_at(t)]
        b = [(s["event_id"], s["subcategory"], s["confidence"]) for s in ledger.states_asof(st, t)]
        assert a == b
    s1 = context_snapshot(st, "AAVE", T0 + 2 * H, include_positioning=False)
    s2 = context_snapshot(st, "AAVE", T0 + 2 * H, include_positioning=False, index=idx)
    assert s1["snapshot_id"] == s2["snapshot_id"]


# --------------------------------------------------------------------------- boundaries


def test_no_live_execution_and_no_consumers():
    ctx_src = "\n".join(p.read_text() for p in (SRC / "context").rglob("*.py"))
    for forbidden in ("market_signal.paper", "market_signal.copilot", "lab.forward",
                      "research.incubation.prospective", "place_order", "submit_order"):  # fmt: skip
        assert forbidden not in ctx_src, forbidden
    assert not re.search(r"/exchange\"|\"type\":\s*\"order\"|action.*order", ctx_src)
    for consumer in ("paper", "copilot", "research/incubation", "perps"):
        for p in (SRC / consumer).rglob("*.py"):
            if consumer == "paper" and "v2" in p.parts:  # Phase 25A causal attribution consumer
                continue
            assert "market_signal.context" not in p.read_text(), p
    assert "market_signal.context" not in (SRC / "research" / "lab" / "forward.py").read_text()
    ddl = MIGRATIONS[21]
    assert "context_events" in ddl and not any(
        x in ddl for x in ("lab_", "copilot_", "paper_", "incubation_")
    )
    assert "CHECK (status = 'research_only')" in ddl


def test_context_ledger_is_append_only():
    for p in (SRC / "context").rglob("*.py"):
        text = p.read_text()
        assert not re.search(r"\b(UPDATE|DELETE FROM|INSERT OR REPLACE)\b", text), p


def test_runtime_schedules_context_jobs_safely():
    from market_signal.ops import runtime as rt

    assert rt.JOBS["positioning"] == [
        ("positioning", ["context", "refresh", "--group", "positioning"])
    ]
    assert rt.SCHEDULE["positioning"] == ["*:04"] and rt.JOB_WAIT["positioning"] < 55 * 60
    assert {"context_news", "positioning"} <= rt.QUIET_JOBS
    tab = rt.crontab()
    assert "4 * * * * market ops cycle positioning" in tab and "ops cycle context_macro" in tab


def test_snapshot_performance_is_cheap(st):
    rng = np.random.default_rng(0)
    items = []
    for i in range(400):
        items.append(obs(title=f"Event {i} on {'Aave' if i % 2 else 'Lido'}", source_id=f"s{i % 7}",
                         source_ref=f"https://x/{i}", subcategory=["exploit_suspected", "lawsuit",
                         "partnership"][i % 3], confidence="REPORTED"))  # fmt: skip
    import time

    t0 = time.perf_counter()
    for i, it in enumerate(items):
        ledger.ingest(st, [it], now=T0 - timedelta(days=int(rng.integers(0, 20)), minutes=i))
    ingest_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    context_snapshot(st, "AAVE", T0, include_positioning=False)
    snap_s = time.perf_counter() - t1
    assert snap_s < 5 and ingest_s / len(items) < 0.2
