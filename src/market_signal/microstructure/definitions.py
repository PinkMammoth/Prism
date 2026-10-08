"""Frozen definitions of ``microstructure_1m_v1``.

Changing ANY constant below that affects a stored value (sampling cadence, staleness limits,
completeness rules, histogram edges, print grouping, large-print rule) requires a new
``FEATURE_VERSION`` (and ``LP_VERSION`` for the large-print rule). Historical rows are never
reinterpreted: every row carries the version it was computed under.
"""

from __future__ import annotations

import hashlib
import json
import math

FEATURE_VERSION = "microstructure_1m_v1"
LP_VERSION = "lp_p99_prior7d_v1"

COINS = ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")  # Prism's six-asset perp universe
WS_URL = "wss://api.hyperliquid.xyz/ws"

# Feeds, one subscription each per coin (24 subscriptions for six coins).
#   trades : every fill, with the taker ("aggressor") side B/A, tid and tx hash
#   book5  : l2Book fast=true -> 5 levels/side, pushed every ~0.54 s (measured 2026-10-07)
#   book20 : l2Book (default) -> 20 levels/side, pushed every ~5.4 s (measured 2026-10-07)
#   ctx    : activeAssetCtx  -> OI, mark, oracle, funding, impact prices (~1/s, no timestamp)
FEEDS = ("trades", "book5", "book20", "ctx")

MINUTE_MS = 60_000
GRACE_MS = 5_000  # a minute [open, close) is finalized at close + GRACE (receipt clock)
REVISION_WINDOW_MS = 60_000  # late trades up to close + GRACE + this -> one bounded revision
SAMPLE_STEP_MS = 1_000  # book sampled on a fixed 1 s exchange-time grid: open + 0..59 s
SAMPLES_PER_MINUTE = MINUTE_MS // SAMPLE_STEP_MS
BOOK5_MAX_AGE_MS = 3_000  # a 1 s sample is valid only if consecutive book5 snapshots around it
# are at most this far apart (the feed provably continued through the sample instant)
BOOK20_MAX_AGE_MS = 15_000  # ... and for the slower 20-level book
CTX_MAX_AGE_MS = 10_000  # end-of-minute asset context must be received within this of close
DYNAMICS_MAX_GAP_MS = BOOK5_MAX_AGE_MS  # consecutive book5 snapshots further apart: no pair

# Completeness (per coin, per minute)
TRADE_COV_MIN = 0.99  # fraction of the minute the trades subscription was live
BOOK_SAMPLES_MIN = 57  # of 60 valid 1 s samples (book5 and book20 each)
STATUSES = ("COMPLETE", "PARTIAL", "TRADE_ONLY", "BOOK_ONLY", "GAP")
GAP_FILL_MAX_MINUTES = 7 * 1440  # explicit GAP rows written for a collector outage up to this

# Trade-size distribution: half-decade notional buckets $10 .. $10M (14 buckets, edges below)
SIZE_EDGES = tuple(10 ** (1 + 0.5 * i) for i in range(13))
# Fine log histogram (spool only, for the large-print calibration): bin b covers
# [FINE_RATIO**b, FINE_RATIO**(b+1)) USD; notional < $1 goes to bin 0.
FINE_RATIO = 1.05
_LOG_R = math.log(FINE_RATIO)

# Large prints (LP_VERSION): a taker print whose notional >= the coin's threshold for the UTC
# day, where threshold = the 99th percentile of print notional over the PRIOR 7 complete UTC
# days (fine-histogram upper bin edge). Warmup: at least 3 of those days with >= 720 observed
# minutes and >= 1000 prints in total; until then large-print fields are NULL (unavailable).
LP_QUANTILE = 0.99
LP_DAYS = 7
LP_MIN_DAYS = 3
LP_MIN_DAY_MINUTES = 720
LP_MIN_PRINTS = 1000

ZERO_HASH = "0x" + "0" * 64

# Stored columns of microstructure_minutes, in DDL order (tests pin this to the migration).
MINUTE_COLUMNS = (
    "feature_version", "coin", "minute_open", "revision", "status", "flags",
    "trade_cov", "book_samples", "depth20_samples",
    "n_buy", "n_sell", "n_buy_prints", "n_sell_prints",
    "buy_vol", "sell_vol", "buy_ntl", "sell_ntl",
    "first_px", "last_px", "high_px", "low_px",
    "max_print_ntl", "med_print_ntl", "size_hist",
    "lp_threshold", "lp_n", "lp_buy_n", "lp_ntl", "lp_buy_ntl",
    "bid_end", "ask_end",
    "spread_mean", "spread_bps_mean", "spread_bps_med", "spread_bps_min", "spread_bps_max",
    "bid5_mean", "ask5_mean", "bid5_end", "ask5_end",
    "bid20_mean", "ask20_mean", "bid20_end", "ask20_end",
    "book_updates", "bid_changes", "ask_changes", "mid_changes", "bid_replenish", "ask_replenish",
    "oi_end", "mark_end", "oracle_end", "funding_end", "impact_bid_end", "impact_ask_end",
    "lat_p50_ms", "lat_max_ms", "n_dup", "n_late",
    "first_recv_at", "last_recv_at", "finalized_at", "ingested_at", "session_id", "content_sha",
)  # fmt: skip

# Fields covered by content_sha: the aggregate itself, never when/where it was produced.
CONTENT_FIELDS = tuple(c for c in MINUTE_COLUMNS if c not in {
    "first_recv_at", "last_recv_at", "finalized_at", "ingested_at", "session_id", "content_sha",
    "n_dup",
})  # fmt: skip

# The definition, as data: its digest is recorded with every collector run so a future study
# can cite exactly what was computed.
DEFINITION = {
    "feature_version": FEATURE_VERSION, "lp_version": LP_VERSION, "coins": COINS,
    "feeds": FEEDS, "grace_ms": GRACE_MS, "revision_window_ms": REVISION_WINDOW_MS,
    "sample_step_ms": SAMPLE_STEP_MS, "book5_max_age_ms": BOOK5_MAX_AGE_MS,
    "book20_max_age_ms": BOOK20_MAX_AGE_MS, "ctx_max_age_ms": CTX_MAX_AGE_MS,
    "trade_cov_min": TRADE_COV_MIN, "book_samples_min": BOOK_SAMPLES_MIN,
    "size_edges": SIZE_EDGES, "fine_ratio": FINE_RATIO, "lp_quantile": LP_QUANTILE,
    "lp_days": LP_DAYS, "lp_min_days": LP_MIN_DAYS, "lp_min_day_minutes": LP_MIN_DAY_MINUTES,
    "lp_min_prints": LP_MIN_PRINTS, "columns": MINUTE_COLUMNS,
}  # fmt: skip


def definition_digest() -> str:
    blob = json.dumps(DEFINITION, sort_keys=True, default=list).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def fine_bin(ntl: float) -> int:
    return 0 if ntl < 1.0 else math.floor(math.log(ntl) / _LOG_R + 1e-12)


def fine_upper_edge(b: int) -> float:
    return FINE_RATIO ** (b + 1)


def content_sha(rec: dict) -> str:
    blob = json.dumps({k: rec.get(k) for k in CONTENT_FIELDS}, sort_keys=True,
                      separators=(",", ":"), default=str).encode()  # fmt: skip
    return hashlib.sha256(blob).hexdigest()[:16]
