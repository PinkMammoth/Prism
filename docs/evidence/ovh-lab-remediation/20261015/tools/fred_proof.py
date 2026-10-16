import subprocess,pathlib,json
from datetime import datetime,UTC
from market_signal.data.store import Store
pid=subprocess.check_output(['systemctl','show','prism-scheduler','-p','MainPID','--value'],text=True).strip()
env=dict(e.decode().split('=',1) for e in pathlib.Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in e);key=env.get('FRED_API_KEY');assert key
s=Store('/data/prism.duckdb',read_only=True,lock_timeout=120);c=s.con
out={'at':datetime.now(UTC).isoformat(),'scheduler_pid':pid,'FRED_API_KEY_present':True}
out['macro_cycles']=c.execute("SELECT cycle_id,trigger,started_at,finished_at,status FROM runtime_cycles WHERE job='context_macro' AND started_at >= '2026-10-15 18:00:00+00' ORDER BY started_at").fetchall()
out['fred_provider_runs']=c.execute("SELECT provider,started_at,finished_at,status,received,new_events,new_updates,duplicates,rejected,filtered,coalesce(error,'')<>'' AS has_error FROM context_provider_runs WHERE provider IN ('fred_calendar','fred_actuals') AND started_at >= '2026-10-15 18:00:00+00' ORDER BY started_at").fetchall()
out['fred_update_rows']=c.execute("SELECT count(*),max(processed_at) FROM context_event_updates WHERE source_id='fred_alfred'").fetchall() if 'source_id' in [x[0] for x in c.execute('DESCRIBE context_event_updates').fetchall()] else None
out['context_tables']={t:[x[0] for x in c.execute(f'DESCRIBE {t}').fetchall()] for (t,) in c.execute('SHOW TABLES').fetchall() if t.startswith('context_') and any(x in t for x in ['provider','updates','runs'])}
s.close()
logs=subprocess.check_output(['journalctl','-u','prism-scheduler','--since','2026-10-15 18:00:00 UTC','--no-pager','-o','cat'])
out['secret_in_journal']=key.encode() in logs
out['secret_in_runtime_log']=key.encode() in pathlib.Path('/data/logs/runtime.log').read_bytes()
out['secret_in_fred_raw_files']=any(key.encode() in p.read_bytes() for p in pathlib.Path('/data/raw/fred').rglob('*') if p.is_file())
print(json.dumps(out,indent=2,default=str));assert not any(out[k] for k in ['secret_in_journal','secret_in_runtime_log','secret_in_fred_raw_files'])
