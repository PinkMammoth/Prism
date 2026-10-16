import json,sys,hashlib
from pathlib import Path
from datetime import datetime,UTC
from collections import Counter
from market_signal.data.store import Store
s=Store('/data/prism.duckdb',read_only=True,lock_timeout=120)
c=s.con
out={'at':datetime.now(UTC).isoformat(),'offsets':[],'spool_events':[],'malformed_lines':[]}
for path,offset,updated in c.execute('SELECT path,bytes,updated_at FROM microstructure_ingest_offsets ORDER BY path').fetchall():
 p=Path(path); raw=p.read_bytes() if p.exists() else b''
 out['offsets'].append({'path':path,'offset':offset,'size':len(raw),'within_file':p.exists() and 0<=offset<=len(raw),'newline_boundary':offset==0 or raw[offset-1:offset]==b'\n','updated_at':updated})
records={};duplicates=[];statuses=Counter()
cutoff=int(datetime(2026,10,15,17,40,tzinfo=UTC).timestamp()*1000)
for p in sorted(Path('/data/microstructure/spool').glob('*/*.jsonl')):
 for lineno,line in enumerate(p.read_bytes().splitlines(),1):
  try:r=json.loads(line)
  except ValueError:out['malformed_lines'].append([str(p),lineno]);continue
  if r.get('kind')=='minute' and r.get('minute_open',0)>=cutoff:
   key=(r['coin'],r['minute_open']); rev=r.get('revision',0)
   if key not in records or rev>records[key]['revision']:records[key]=r
   elif rev==records[key]['revision']:duplicates.append([*key,rev])
  elif r.get('kind') in ['run_start','run_end','conn_open','conn_close'] and r.get('at',0)>=cutoff:out['spool_events'].append(r)
rows=c.execute("SELECT coin,epoch_ms(minute_open),revision,status,flags FROM microstructure_minutes WHERE minute_open >= '2026-10-15 17:40:00+00' ORDER BY minute_open,coin").fetchall()
latest=max((r[1] for r in rows),default=cutoff)
missing=[];diff=[]
for coin,minute,rev,status,flags in rows:
 r=records.get((coin,minute))
 if r is None:missing.append([coin,minute])
 elif (rev,status)!=(r.get('revision',0),r.get('status')):diff.append([coin,minute,rev,status,r.get('revision'),r.get('status')])
expected={(coin,minute) for coin,minute in records if minute<=latest}
stored={(r[0],r[1]) for r in rows}
out.update({'db_rows':rows,'spool_latest_revision_keys':len(records),'db_latest_minute':latest,'spool_keys_through_db_frontier':len(expected),'spool_keys_missing_in_db_through_frontier':sorted(expected-stored),'db_rows_missing_from_spool':missing,'revision_status_mismatches':diff,'duplicate_same_revision_records':duplicates,'db_duplicate_coin_minute':c.execute('SELECT coin,minute_open,count(*) FROM microstructure_minutes GROUP BY 1,2 HAVING count(*)>1').fetchall()})
out['free_ingest_receipts']=c.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE '%receipt%' OR table_name LIKE '%gateway%'").fetchall()
s.close()
print(json.dumps(out,indent=2,default=str))
