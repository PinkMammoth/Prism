"""Daily brief, alert delivery, data freshness and the Telegram client (no network)."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from market_signal.brief import build_brief, mark_delivered, pending_alerts
from market_signal.data.freshness import check_freshness, latest_nyse_close
from market_signal.portfolio.telegram import TelegramClient, TelegramError, _chunks

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db

    db = tmp_path_factory.mktemp("brief") / "demo.duckdb"
    old = {k: os.environ.get(k) for k in ("PRISM_DB_PATH", "PRISM_SOURCE_OVERRIDE", "PRISM_HOME")}
    os.environ.update(
        PRISM_DB_PATH=str(db), PRISM_SOURCE_OVERRIDE="synthetic", PRISM_HOME=str(REPO)
    )
    s = get_settings(REPO)
    store = Store(db)
    build_demo_db(s, store, years=3, seed=7)
    yield s, store
    store.close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _newest(store, crypto: bool) -> pd.Timestamp:
    inv = store.series_inventory()
    inv = inv[inv["timeframe"] == "1d"]
    sym = "BTC" if crypto else "SPY"
    return pd.Timestamp(inv[inv["symbol"] == sym]["last_close"].max()).tz_convert("UTC")


# ----------------------------------------------------------------- freshness


def test_latest_nyse_close_skips_weekends_and_waits_for_grace():
    g = timedelta(hours=8)
    # Monday 2026-10-05 06:00 UTC → Friday 2026-10-02's close (20:00 UTC, EDT)
    assert latest_nyse_close(pd.Timestamp("2026-10-05T06:00Z"), g) == pd.Timestamp(
        "2026-10-02T20:00Z"
    )
    # Tuesday 03:00 UTC: Monday's close + 8h grace is not reached yet → still Friday
    assert latest_nyse_close(pd.Timestamp("2026-10-06T03:00Z"), g) == pd.Timestamp(
        "2026-10-02T20:00Z"
    )
    assert latest_nyse_close(pd.Timestamp("2026-10-06T05:00Z"), g) == pd.Timestamp(
        "2026-10-05T20:00Z"
    )


def test_freshness_fresh_then_stale(env):
    s, store = env
    just_after = max(_newest(store, True), _newest(store, False)) + pd.Timedelta(hours=9)
    f = check_freshness(store, s, now=just_after)
    assert not f.data_stale, f.problems()
    later = just_after + pd.Timedelta(days=4)
    f = check_freshness(store, s, now=later)
    assert f.data_stale
    assert any("Crypto prices are out of date" in p for p in f.problems())


# ----------------------------------------------------------------- brief + alerts


def test_brief_content_and_alert_delivery(env):
    from market_signal.portfolio.alerts import evaluate_alerts
    from market_signal.scoring.engine import run_scan

    s, store = env
    res = run_scan(store, s, persist=True)
    evaluate_alerts(store, s, res, notifiers=[])  # e.g. the dashboard: recorded, not delivered
    now = pd.Timestamp.now(tz="UTC")
    n_pending = len(pending_alerts(store, now))
    assert n_pending > 0

    b = build_brief(store, s, update_exit=1, now=now)
    assert "SYNTHETIC DEMO DATA" in b.text
    assert "last data update had failures (exit 1)" in b.text
    assert ("ACTIONABLE" in b.text) or ("NO ACTIONABLE OPPORTUNITIES TODAY" in b.text)
    assert "<b>Market</b>" in b.text and "Prism never places trades" in b.text
    assert len(b.alert_ids) == n_pending and f"({n_pending} new)" in b.text
    # rejected setups are never presented as ACTIONABLE/WAIT in the brief either
    assert "REJECTED" not in b.text.split("<b>Best opportunities</b>")[-1].split("<b>Market</b>")[0]

    mark_delivered(store, b.alert_ids)
    assert pending_alerts(store, now).empty
    assert "<b>Alerts</b>" not in build_brief(store, s, now=now).text


def test_brief_without_scans(tmp_path, env):
    from market_signal.data.store import Store

    s, _ = env
    empty = Store(tmp_path / "e.duckdb")
    try:
        b = build_brief(empty, s)
        assert "No scan stored yet. Run <code>market scan</code>." in b.text
        assert "⚠️ No scan stored yet." in b.text
    finally:
        empty.close()


# ----------------------------------------------------------------- telegram


def _client(handler, chat="42"):
    return TelegramClient("123:SECRET", chat, transport=httpx.MockTransport(handler))


def test_telegram_send_posts_html_to_chat():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    _client(handler).send("<b>hi</b> &amp; bye")
    path, body = seen[0]
    assert path == "/bot123:SECRET/sendMessage"
    assert body == {"chat_id": "42", "text": "<b>hi</b> &amp; bye", "parse_mode": "HTML",
                    "disable_web_page_preview": True}  # fmt: skip


def test_telegram_errors_never_leak_the_token():
    def bad(req):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def down(req):
        raise httpx.ConnectError("boom", request=req)

    for h in (bad, down):
        with pytest.raises(TelegramError) as exc:
            _client(h).send("x")
        assert "SECRET" not in str(exc.value)
    with pytest.raises(TelegramError, match="Unauthorized"):
        _client(bad).send("x")


def test_telegram_recent_chats_and_chunking():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "result": [
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Matt"}}},
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Matt"}}}]})  # fmt: skip

    assert _client(handler, chat=None).recent_chats() == [
        {"id": "42", "name": "Matt", "type": "private"}
    ]
    text = "\n".join(f"<b>line {i}</b> " + "x" * 90 for i in range(200))
    parts = _chunks(text)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == text


def test_token_format_is_checked_before_calling_telegram():
    from market_signal.portfolio.telegram import check_token

    check_token("123456789:AAH" + "x" * 32)
    for bad, hint in (("bot123456789:AAH" + "x" * 32, "starts with 'bot'"), ("@MyPrismBot", "username"),
                      ("<123456789:AAH" + "x" * 32 + ">", "brackets"), ("123456789:AAHshort", "shape")):  # fmt: skip
        with pytest.raises(TelegramError, match=hint) as exc:
            check_token(bad)
        assert bad not in str(exc.value)  # never echo the token


def test_telegram_http_errors_are_explained():
    def status(code, desc):
        return lambda req: httpx.Response(code, json={"ok": False, "description": desc})

    with pytest.raises(TelegramError, match=r"doesn.t exist .HTTP 404"):
        _client(status(404, "Not Found")).get_me()
    with pytest.raises(TelegramError, match="revoked"):
        _client(status(401, "Unauthorized")).get_me()
    with pytest.raises(TelegramError, match="chat not found"):
        _client(status(400, "Bad Request: chat not found")).send("x")
    ok = _client(
        lambda req: httpx.Response(200, json={"ok": True, "result": {"username": "PrismBot"}})
    )
    assert ok.get_me()["username"] == "PrismBot"
