"""Read-only bounded remediation observer. Never schedules or runs application jobs."""
import argparse,json,os,shutil,subprocess,time
from datetime import datetime,UTC
from pathlib import Path
from market_signal.data.store import Store,authority_claim

UNITS=['prism-microstructure','prism-free-sources','prism-free-source-ingest','prism-scheduler']

def unit(name):
 p=subprocess.check_output(['systemctl','show',name,'-p','ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp'],text=True)
 return dict(x.split('=',1) for x in p.splitlines() if '=' in x)

def collect(start):
 now=datetime.now(UTC);mem={x.split(':',1)[0]:int(x.split()[1])*1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith(('MemTotal:','MemAvailable:','SwapTotal:','SwapFree:'))};disk=shutil.disk_usage('/data')
 out={'at':now.isoformat(),'units':{u:unit(u) for u in UNITS},'host':{'memory':mem,'load':os.getloadavg(),'cpu_jiffies':[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]],'disk_free':disk.free,'disk_total':disk.total},'profile':json.loads(Path('/data/state/profile.json').read_text()),'micro':json.loads(Path('/data/microstructure/status.json').read_text()),'free_collector':json.loads(Path('/data/free_event_sources/collector.json').read_text()),'free_ingest':json.loads(Path('/data/free_event_sources/worker.json').read_text()),'db_bytes':Path('/data/prism.duckdb').stat().st_size}
 try:
  s=Store('/data/prism.duckdb',read_only=True,lock_timeout=15);c=s.con
  out['authority']=authority_claim(c)
  out['cycles']=c.execute('SELECT cycle_id,job,trigger,started_at,finished_at,status FROM runtime_cycles WHERE started_at>=? ORDER BY started_at',[start]).fetchall()
  out['providers']=c.execute("SELECT provider,status,count(*),max(finished_at),sum(new_updates) FROM context_provider_runs WHERE started_at>=? AND provider IN ('fred_calendar','fred_actuals') GROUP BY 1,2 ORDER BY 1,2",[start]).fetchall()
  out['paper_rows']={t:c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for (t,) in c.execute('SHOW TABLES').fetchall() if t.startswith('paper_') and t not in ['paper_nimble_policies','paper_nimble_triggers']}
  out['universe_evaluations']=c.execute('SELECT count(*) FROM universe_evaluations').fetchone()[0]
  out['frontiers']={'bars':c.execute("SELECT coin,timeframe,max(close_time) FROM perp_intraday_bars WHERE source='hyperliquid' AND timeframe IN ('15m','1h') GROUP BY 1,2 ORDER BY 1,2").fetchall(),'micro':c.execute('SELECT coin,max(minute_open) FROM microstructure_minutes GROUP BY 1 ORDER BY 1').fetchall(),'funding':c.execute("SELECT coin,max(time) FROM perp_funding WHERE source='hyperliquid' GROUP BY 1 ORDER BY 1").fetchall()}
  s.close()
 except Exception as exc:out['db_observer_error_type']=type(exc).__name__
 return json.loads(json.dumps(out,default=str))

def concerns(sample,baseline):
 errors=[]
 for u,v in sample['units'].items():
  if v['ActiveState']!='active' or v['MainPID']!=baseline['units'][u]['MainPID'] or v['NRestarts']!=baseline['units'][u]['NRestarts']:errors.append('service continuity: '+u)
 p=sample['profile']
 if p['identity']!='prism_autotrader_lab_v1' or p['authority']!='rehearsal' or p['execution_allowed'] or p['external_actions_allowed']:errors.append('profile/authority drift')
 if sample.get('authority',baseline.get('authority'))!=baseline.get('authority'):errors.append('DB authority changed')
 for c in sample.get('cycles',[]):
  if c[5] not in ['ok','running']:errors.append('failed cycle: '+c[0]+'/'+c[1])
 if any(sample.get('paper_rows',{}).values()) or sample.get('universe_evaluations',0):errors.append('unexpected execution or DU data')
 mem=sample['host']['memory']
 if mem['MemAvailable']<.4*mem['MemTotal'] or mem['SwapTotal']-mem['SwapFree']>0:errors.append('RAM/swap stop threshold')
 if sample['host']['disk_free']<.7*sample['host']['disk_total']:errors.append('disk stop threshold')
 if sample['micro'].get('spool_errors',0) or sample['micro'].get('dropped_records',0):errors.append('collector spool error/drop')
 return errors

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--end',required=True);args=p.parse_args()
 start=datetime.now(UTC);end=datetime.fromisoformat(args.end)
 assert 6*3600<= (end-start).total_seconds()<=12*3600
 args.root.mkdir(parents=True,exist_ok=True)
 metadata={'start':start.isoformat(),'planned_end':end.isoformat(),'base_evidence':'26-hour rehearsal 2026-10-14T15:44:38Z to 2026-10-15T17:45:00Z','purpose':'targeted remediation extension; no reset of base evidence','minimum_seconds':21600}
 (args.root/'window.json').write_text(json.dumps(metadata,indent=2)+'\n')
 baseline=collect(start);seen=set();samples=0
 while True:
  current=baseline if samples==0 else collect(start);samples+=1
  for error in concerns(current,baseline):seen.add(error)
  current['concerns']=sorted(seen)
  with (args.root/'samples.jsonl').open('a') as f:f.write(json.dumps(current)+'\n');f.flush()
  (args.root/'latest.json').write_text(json.dumps(current,indent=2)+'\n')
  if datetime.now(UTC)>=end:
   scheduled=[x for x in current.get('cycles',[]) if x[2]=='schedule']
   macro=[x for x in scheduled if x[1]=='context_macro' and x[5]=='ok'];backup=[x for x in scheduled if x[1]=='backup' and x[5]=='ok']
   if len(macro)<2:seen.add('fewer than two successful natural macro cycles')
   if not backup:seen.add('normal nightly backup not successful')
   if current.get('db_observer_error_type'):seen.add('final DB observer unavailable')
   result={**metadata,'end':datetime.now(UTC).isoformat(),'duration_seconds':(datetime.now(UTC)-start).total_seconds(),'samples':samples,'scheduled_macro_successes':len(macro),'scheduled_backup_successes':len(backup),'concerns':sorted(seen),'clean':not seen}
   (args.root/'final.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True);return
  time.sleep(min(300,max(1,(end-datetime.now(UTC)).total_seconds())))
if __name__=='__main__':main()
