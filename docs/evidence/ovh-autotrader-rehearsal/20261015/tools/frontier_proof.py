import json
from pathlib import Path
from datetime import datetime,UTC
from market_signal.data.store import Store
s=Store('/data/prism.duckdb',read_only=True,lock_timeout=120);c=s.con
out={'at':datetime.now(UTC).isoformat()}
queries={
'bars_at_close':"SELECT source,coin,timeframe,count(*),max(open_time),max(close_time),max(first_observed_at) FROM perp_intraday_bars WHERE first_observed_at<'2026-10-15 17:45:00+00' AND timeframe IN ('15m','1h') GROUP BY 1,2,3 ORDER BY 1,2,3",
'funding_at_close':"SELECT source,coin,max(time),max(available_at) FROM perp_funding WHERE available_at<'2026-10-15 17:45:00+00' GROUP BY 1,2 ORDER BY 1,2",
'positioning_at_close':"SELECT coin,max(grid_hour),max(captured_at) FROM context_hl_oi_hourly WHERE captured_at<'2026-10-15 17:45:00+00' GROUP BY 1 ORDER BY 1",
'micro_at_close':"SELECT coin,max(minute_open),max(ingested_at) FROM microstructure_minutes WHERE ingested_at<'2026-10-15 17:45:00+00' GROUP BY 1 ORDER BY 1",
'oi_at_close':"SELECT source,coin,max(observed_at),max(ingested_at) FROM perp_oi_history WHERE ingested_at<'2026-10-15 17:45:00+00' GROUP BY 1,2 ORDER BY 1,2",
'context_providers':"SELECT source_id,count(*),max(first_seen_at) FROM context_events GROUP BY 1 ORDER BY 1",
}
for name,q in queries.items():out[name]=c.execute(q).fetchall()
s.close()
root=Path('/data/free_event_sources');out['free_source_files']={};bad=[]
for p in root.rglob('*'):
 if not p.is_file() or p.suffix not in ['.json','.jsonl']:continue
 data=p.read_text();n=0
 try:
  if p.suffix=='.json':json.loads(data);n=1
  else:
   for line in data.splitlines():json.loads(line);n+=1
 except ValueError:bad.append(str(p))
 out['free_source_files'][str(p.relative_to(root))]={'bytes':p.stat().st_size,'valid_json_records':n}
out['free_source_invalid_json']=bad
print(json.dumps(out,indent=2,default=str))
