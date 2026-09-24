"""Crypto fundamental data: DefiLlama (fees/revenue) and Hyperliquid (supply, USDC, AF).

Storage policy (``crypto_metrics``):
  - DefiLlama daily history → pit_method='reconstructed' (display only; the research
    loader refuses it).
  - Every fetch also stores today's values as pit_method='snapshot' with the fetch time,
    so genuinely point-in-time history accumulates from first use onwards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from market_signal.config import Settings
from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.data.providers.base import HttpProvider
from market_signal.data.providers.crypto import HyperliquidProvider
from market_signal.data.store import Store
from market_signal.models.domain import PitMethod, utcnow

AF_ADDRESS = "0xfefefefefefefefefefefefefefefefefefefefe"


class DefiLlamaProvider(HttpProvider):
    name = "defillama"

    def __init__(self, http: HttpClient):
        super().__init__(http)

    @staticmethod
    def parse_chart(payload: Any, slug: str) -> pd.DataFrame:
        try:
            chart = payload["totalDataChart"]
        except (KeyError, TypeError) as exc:
            raise SchemaError(f"defillama {slug}: missing totalDataChart") from exc
        if chart is None:
            raise SchemaError(
                f"defillama {slug}: totalDataChart is null (endpoint semantics changed?)"
            )
        rows = []
        for item in chart:
            if not (isinstance(item, list) and len(item) == 2):
                raise SchemaError(f"defillama {slug}: unexpected chart row {item!r}")
            rows.append((datetime.fromtimestamp(int(item[0]), tz=UTC).date(), float(item[1])))
        return pd.DataFrame(rows, columns=["obs_date", "value"])

    def fees_history(self, slug: str, data_type: str = "dailyRevenue") -> pd.DataFrame:
        payload = self.http.get_json(
            f"/summary/fees/{slug}",
            params={"dataType": data_type, "excludeTotalDataChartBreakdown": "true"},
        )
        return self.parse_chart(payload, slug)


def hyperliquid_token_details(hl: HyperliquidProvider, token: str) -> dict[str, Any]:
    meta = hl.info({"type": "spotMeta"})
    try:
        token_id = next(t["tokenId"] for t in meta["tokens"] if t["name"] == token)
    except (StopIteration, KeyError, TypeError) as exc:
        raise SchemaError(f"hyperliquid spotMeta: token {token} not found") from exc
    details = hl.info({"type": "tokenDetails", "tokenId": token_id})
    if not isinstance(details, dict) or "circulatingSupply" not in details:
        raise SchemaError(f"hyperliquid tokenDetails schema changed for {token}")
    return details


def af_balance(hl: HyperliquidProvider, coin: str = "HYPE") -> float:
    state = hl.info({"type": "spotClearinghouseState", "user": AF_ADDRESS})
    try:
        for b in state["balances"]:
            if b["coin"] == coin:
                return float(b["total"])
    except (KeyError, TypeError) as exc:
        raise SchemaError("hyperliquid spotClearinghouseState schema changed") from exc
    return 0.0


def upsert_metrics(store: Store, df: pd.DataFrame, run_id: str) -> int:
    if df.empty:
        return 0
    stage = df.copy()
    stage["run_id"] = run_id
    store.con.register("_cm", stage)
    try:
        store.con.execute(
            """INSERT OR REPLACE INTO crypto_metrics
               SELECT symbol, source, metric, obs_date, value, available_at, pit_method, fetched_at, run_id FROM _cm"""
        )
    finally:
        store.con.unregister("_cm")
    return len(stage)


def _rows(
    symbol: str, source: str, metric: str, hist: pd.DataFrame, fetched: datetime
) -> pd.DataFrame:
    h = hist.copy()
    h["symbol"], h["source"], h["metric"] = symbol, source, metric
    h["available_at"] = pd.to_datetime(h["obs_date"]).dt.tz_localize("UTC") + pd.Timedelta(days=1)
    h["pit_method"] = PitMethod.RECONSTRUCTED.value
    h["fetched_at"] = fetched
    return h


def _snapshot(
    symbol: str, source: str, metric: str, value: float, fetched: datetime
) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": symbol, "source": source, "metric": metric, "obs_date": fetched.date(),
                          "value": value, "available_at": fetched, "pit_method": PitMethod.SNAPSHOT.value,
                          "fetched_at": fetched}])  # fmt: skip


def update_crypto_fundamentals(settings: Settings, store: Store) -> pd.DataFrame:
    from market_signal.data.registry import ProviderRegistry

    reg = ProviderRegistry(settings)
    cfg = settings.provider_cfg("defillama")
    dl = DefiLlamaProvider(reg.http("defillama"))
    dl.http.base_url = cfg.get("base_url", "https://api.llama.fi")
    results = []
    fetched = utcnow()
    # protocol / chain revenue for assets with a DefiLlama mapping
    for a in settings.active_assets():
        slug = (
            a.provider_ids.get("defillama_protocol")
            or (a.provider_ids.get("defillama_chain") or "").lower()
            or None
        )
        if not slug or a.symbol == "BTC":  # Bitcoin has no protocol revenue concept
            continue
        run_id = store.start_run("defillama", "fees_revenue", a.symbol, {"slug": slug})
        try:
            hist = dl.fees_history(slug, "dailyRevenue")
            archived = store.archive_raw(dl.drain_raw(), run_id)
            frames = [_rows(a.symbol, "defillama", "daily_revenue", hist, fetched)]
            last30 = hist[hist["obs_date"] > (fetched.date() - pd.Timedelta(days=31))]["value"]
            if len(last30) >= 28:
                frames.append(
                    _snapshot(
                        a.symbol, "defillama", "revenue_30d_sum", float(last30.sum()), fetched
                    )
                )
            n = upsert_metrics(store, pd.concat(frames, ignore_index=True), run_id)
            store.finish_run(
                run_id, status="ok", rows_received=len(hist), rows_written=n, archived=archived
            )
            results.append(
                (
                    a.symbol,
                    "ok",
                    len(hist),
                    n,
                    f"defillama:{slug} (reconstructed history + snapshot)",
                )
            )
        except ProviderError as exc:
            store.finish_run(run_id, status="failed", error=str(exc)[:500])
            results.append((a.symbol, "failed", 0, 0, str(exc)[:80]))
    # Hyperliquid: HYPE supply, USDC on Hyperliquid, Assistance Fund balance
    hl = reg.market("hyperliquid")
    run_id = store.start_run("hyperliquid", "hype_fundamentals", "HYPE", {})
    try:
        hype = hyperliquid_token_details(hl, "HYPE")
        usdc = hyperliquid_token_details(hl, "USDC")
        bal = af_balance(hl, "HYPE")
        archived = store.archive_raw(hl.drain_raw(), run_id)
        snaps = [
            _snapshot(
                "HYPE",
                "hyperliquid",
                "circulating_supply",
                float(hype["circulatingSupply"]),
                fetched,
            ),
            _snapshot(
                "HYPE",
                "hyperliquid",
                "total_supply",
                float(hype.get("totalSupply", "nan")),
                fetched,
            ),
            _snapshot(
                "HYPE",
                "hyperliquid",
                "future_emissions",
                float(hype.get("futureEmissions", "nan")),
                fetched,
            ),
            _snapshot(
                "HYPE",
                "hyperliquid",
                "usdc_on_hyperliquid",
                float(usdc["circulatingSupply"]),
                fetched,
            ),
            _snapshot("HYPE", "hyperliquid", "af_hype_balance", bal, fetched),
        ]
        n = upsert_metrics(store, pd.concat(snaps, ignore_index=True), run_id)
        store.finish_run(run_id, status="ok", rows_received=5, rows_written=n, archived=archived)
        results.append(("HYPE", "ok", 5, n, "hyperliquid snapshot (supply, USDC, AF)"))
    except ProviderError as exc:
        store.finish_run(run_id, status="failed", error=str(exc)[:500])
        results.append(("HYPE", "failed", 0, 0, str(exc)[:80]))
    return pd.DataFrame(results, columns=["entity", "status", "received", "new_rows", "note"])
