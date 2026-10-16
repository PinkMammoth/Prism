import json
from pathlib import Path
from datetime import datetime,UTC
p=Path('/data/validation/lab-remediation');d=json.loads((p/'latest.json').read_text());w=json.loads((p/'window.json').read_text());now=datetime.now(UTC)
scheduled=[x for x in d.get('cycles',[]) if x[2]=='schedule']
result={'now':now.isoformat(),'sample_at':d['at'],'elapsed_hours':round((now-datetime.fromisoformat(w['start'])).total_seconds()/3600,3),'planned_end':w['planned_end'],'concerns':d['concerns'],'cycles':len(d.get('cycles',[])),'successful_cycles':sum(x[5]=='ok' for x in d.get('cycles',[])),'natural_macro_successes':sum(x[1]=='context_macro' and x[5]=='ok' for x in scheduled),'natural_backup_successes':sum(x[1]=='backup' and x[5]=='ok' for x in scheduled),'ram_available_gb':round(d['host']['memory']['MemAvailable']/1e9,3),'disk_free_gb':round(d['host']['disk_free']/1e9,3),'load':d['host']['load'],'collector_connected':d['micro'].get('connected'),'collector_last_message_age_s':d['micro'].get('last_message_age_s'),'final_exists':(p/'final.json').exists()}
print(json.dumps(result,indent=2))
