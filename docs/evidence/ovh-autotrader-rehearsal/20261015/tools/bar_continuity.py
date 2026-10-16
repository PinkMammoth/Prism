import json
from datetime import datetime,UTC,timedelta
from market_signal.data.store import Store
s=Store('/data/prism.duckdb',read_only=True,lock_timeout=120);out={}
for tf,begin,end,step in [('15m','2026-10-14T15:45:00+00:00','2026-10-15T17:15:00+00:00',900),('1h','2026-10-14T15:00:00+00:00','2026-10-15T16:00:00+00:00',3600)]:
 expected=set();x=datetime.fromisoformat(begin);last=datetime.fromisoformat(end)
 while x<=last:expected.add(x);x+=timedelta(seconds=step)
 out[tf]={}
 for coin in ['AAVE','BTC','ETH','HYPE','LINK','SOL']:
  rows=s.con.execute("SELECT open_time FROM perp_intraday_bars WHERE source='hyperliquid' AND coin=? AND timeframe=? AND open_time BETWEEN ? AND ? AND first_observed_at < '2026-10-15 17:45:00+00'",[coin,tf,begin,end]).fetchall()
  seen={r[0] for r in rows};out[tf][coin]={'expected':len(expected),'present':len(rows),'missing':sorted(expected-seen),'duplicates':len(rows)-len(seen)}
s.close();print(json.dumps(out,indent=2,default=str))
