import json,collections,statistics
from pathlib import Path
from datetime import datetime,UTC
root=Path(__file__).resolve().parent.parent
start=datetime.fromisoformat('2026-10-14T15:44:38+00:00');end=datetime.fromisoformat('2026-10-15T17:45:00+00:00')
r=[json.loads(x) for x in (root/'soak-through-close.jsonl').read_text().splitlines()]
r=[x for x in r if start<=datetime.fromisoformat(x['at'])<end]
a,b=r[0],r[-1]
db=json.loads((root/'db-before.json').read_text())
cycles=[x for x in db['runtime_cycles'] if start<=datetime.fromisoformat(x['started_at'])<end]
out={'window':[start.isoformat(),end.isoformat()],'hours':(end-start).total_seconds()/3600,'samples':len(r),'sample_window':[a['at'],b['at']],'sample_interval_max_s':max((datetime.fromisoformat(y['at'])-datetime.fromisoformat(x['at'])).total_seconds() for x,y in zip(r,r[1:])),'sample_gaps_over_330s':[], 'units':{},'job_results':{},'busy_samples':[x['at'] for x in r if x['db'].get('busy')],'host':{},'growth':{},'failed_cycles':[x for x in cycles if x['status']!='ok']}
for u in a['units']:
 rows=[x['units'][u] for x in r];m=[x['MemoryCurrent'] for x in rows if isinstance(x['MemoryCurrent'],int)]
 out['units'][u]={'states':dict(collections.Counter(x['ActiveState'] for x in rows)),'start_times':sorted(set(x['ExecMainStartTimestamp'] for x in rows)),'nrestarts_max':max(x['NRestarts'] for x in rows),'memory_current_peak':max(m,default=0),'cpu_equivalents':((rows[-1]['CPUUsageNSec'] or 0)-(rows[0]['CPUUsageNSec'] or 0))/1e9/(datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds()}
for job in sorted(set(x['job'] for x in cycles)):
 j=[x for x in cycles if x['job']==job]
 out['job_results'][job]={'statuses':dict(collections.Counter(x['status'] for x in j)),'duration_max_s':max((datetime.fromisoformat(x['finished_at'])-datetime.fromisoformat(x['started_at'])).total_seconds() for x in j if x['finished_at'])}
cpu=[];steal=[]
for x,y in zip(r,r[1:]):
 d=[v-u for u,v in zip(x['host']['cpu_jiffies'],y['host']['cpu_jiffies'])];total=sum(d);cpu.append(100*(1-(d[3]+d[4])/total));steal.append(100*d[7]/total)
out['host']={'cpu_busy_interval_mean_pct':statistics.mean(cpu),'cpu_busy_interval_max_pct':max(cpu),'cpu_busy_interval_p95_pct':sorted(cpu)[int(.95*(len(cpu)-1))],'steal_interval_max_pct':max(steal),'mem_available_min':min(x['host']['mem_available'] for x in r),'mem_total':a['host']['mem_total'],'swap_max':max(x['host']['swap_used'] for x in r),'load_max':[max(x['host']['loadavg'][i] for x in r) for i in range(3)],'disk_free_start':a['disk']['free'],'disk_free_end':b['disk']['free'],'disk_free_min':min(x['disk']['free'] for x in r),'disk_total':a['disk']['total']}
for k in b['bytes']:
 out['growth'][k]={'start':a['bytes'].get(k,0),'end':b['bytes'][k],'delta':b['bytes'][k]-a['bytes'].get(k,0),'sample_max':max(x['bytes'].get(k,0) for x in r)}
out['heartbeat_max_age_s']={k:max((x['heartbeats'][k]['age_s'] for x in r if x['heartbeats'].get(k) and x['heartbeats'][k]['age_s'] is not None),default=None) for k in a['heartbeats']}
print(json.dumps(out,indent=2))
