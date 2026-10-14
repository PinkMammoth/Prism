"""Build a SYNTHETIC schema-current Prism DuckDB for the vps_validate duckdb benchmark.

No production data: real migrations (Store.migrate) + generated microstructure_minutes rows.
Usage: PRISM_RUNTIME_ROLE=scratch python synth_db.py OUT.duckdb ROWS
"""

import os
import sys
import time
from pathlib import Path

os.environ["PRISM_RUNTIME_ROLE"] = "scratch"
from market_signal.data.store import Store  # noqa: E402

out, rows = Path(sys.argv[1]), int(sys.argv[2])
out.unlink(missing_ok=True)
t0 = time.monotonic()
st = Store(out, authority_check=False)
coins = "['BTC','ETH','SOL','HYPE','AAVE','LINK']"
st.con.execute(f"""
INSERT INTO microstructure_minutes (
  feature_version, coin, minute_open, revision, status, trade_cov, book_samples, depth20_samples,
  n_buy, n_sell, buy_vol, sell_vol, buy_ntl, sell_ntl, first_px, last_px, high_px, low_px,
  bid_end, ask_end, spread_bps_mean, bid5_mean, ask5_mean, bid20_mean, ask20_mean,
  oi_end, mark_end, oracle_end, funding_end, n_dup, n_late,
  finalized_at, ingested_at, session_id, content_sha)
SELECT 'synthetic_v1', ({coins})[1 + (i % 6)],
  TIMESTAMPTZ '2024-01-01 00:00:00+00' + to_minutes((i // 6)::BIGINT), 0, 'COMPLETE',
  random(), 60, 12, (random()*500)::INT, (random()*500)::INT,
  random()*1e3, random()*1e3, random()*1e7, random()*1e7,
  1e4+random(), 1e4+random(), 1e4+random(), 1e4+random(), 1e4+random(), 1e4+random(),
  random()*10, random()*1e6, random()*1e6, random()*1e7, random()*1e7,
  random()*1e9, 1e4+random(), 1e4+random(), random()*1e-4, 0, 0,
  TIMESTAMPTZ '2024-01-01 00:01:00+00' + to_minutes((i // 6)::BIGINT),
  TIMESTAMPTZ '2024-01-01 00:01:00+00' + to_minutes((i // 6)::BIGINT),
  'synthetic', md5(i::VARCHAR)
FROM range({rows}) t(i)""")
st.con.execute("CHECKPOINT")
st.close()
print(f"{out} rows={rows} bytes={out.stat().st_size} build_s={time.monotonic() - t0:.1f}")
