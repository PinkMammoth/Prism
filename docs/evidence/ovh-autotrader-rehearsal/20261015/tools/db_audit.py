import json, sys
from datetime import datetime, UTC
from market_signal.data.store import Store, authority_claim
p=sys.argv[1] if len(sys.argv)>1 else '/data/prism.duckdb'
s=Store(p,read_only=True,lock_timeout=120)
c=s.con
out={'at':datetime.now(UTC).isoformat(),'db':p,'authority':authority_claim(c),'tables':{}}
for (t,) in c.execute('SHOW TABLES').fetchall():
    cols=c.execute(f'DESCRIBE "{t}"').fetchall()
    d={'rows':c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]}
    if t.startswith(('runtime_','microstructure_','universe_','perp_','context_','paper_')):
        d['columns']=[x[0] for x in cols]
        times=[x[0] for x in cols if 'TIMESTAMP' in x[1]]
        d['frontiers']={col:str(c.execute(f'SELECT max("{col}") FROM "{t}"').fetchone()[0]) for col in times}
    out['tables'][t]=d
for t in ('runtime_cycles','runtime_steps'):
    if t not in out['tables']: continue
    cols=out['tables'][t]['columns']
    where=" WHERE started_at >= '2026-10-14 15:44:00+00'" if 'started_at' in cols else ''
    rows=c.execute(f'SELECT * FROM "{t}"'+where).fetchall()
    out[t]=[dict(zip(cols,r)) for r in rows]
out['authority_events']=[list(r) for r in c.execute("SELECT event_type,runtime_id,recorded_at FROM runtime_events WHERE event_type IN ('authority_claimed','lab_experiment_activated','lab_seeded') ORDER BY recorded_at").fetchall()]
if 'microstructure_minutes' in out['tables']:
    out['micro_by_coin']=c.execute('SELECT coin,status,count(*),min(minute_open),max(minute_open) FROM microstructure_minutes GROUP BY 1,2 ORDER BY 1,2').fetchall()
    out['micro_recent']=c.execute("SELECT coin,minute_open,status,revision,flags FROM microstructure_minutes WHERE minute_open >= current_timestamp-INTERVAL 30 MINUTE ORDER BY minute_open,coin").fetchall()
s.close()
print(json.dumps(out,indent=2,default=str))
