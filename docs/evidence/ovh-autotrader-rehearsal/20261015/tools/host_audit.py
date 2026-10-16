import subprocess,json,os,pathlib,hashlib
from datetime import datetime,UTC

def cmd(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=30)
 return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
units=['prism-microstructure','prism-free-sources','prism-free-source-ingest','prism-scheduler','prism-soak','prism-nimble','prism-universe','prism-context-gateway','prism-context-ingest','prism-research-backup']
out={'at':datetime.now(UTC).isoformat(),'kernel':os.uname().release,'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'units':{},'processes':[]}
props='ActiveState,SubState,NRestarts,MainPID,ExecMainStartTimestamp,ActiveEnterTimestamp,MemoryCurrent,MemoryPeak,CPUUsageNSec,Result,UnitFileState'
for u in units:
 d=dict(x.split('=',1) for x in cmd(['systemctl','show',u,'-p',props])['stdout'].splitlines() if '=' in x)
 out['units'][u]=d
keys={'PRISM_PROFILE','PRISM_AUTHORITY','PRISM_DEPLOYMENT_TARGET','PRISM_RUNTIME_ID','PRISM_RUNTIME_ROLE','PRISM_DB_PATH','PRISM_MICRO_DIR','PRISM_UNIVERSE_STAGE','PRISM_NIMBLE_RUNTIME'}
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=p.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode()
  if ('market_signal' not in args and '/opt/prism' not in args and 'supercronic' not in args) or 'sudo ' in args or 'ssh ' in args or 'host_audit' in args: continue
  env={k:v for kv in p.joinpath('environ').read_bytes().split(b'\0') if b'=' in kv for k,v in [kv.decode(errors='replace').split('=',1)] if k in keys}
  status=p.joinpath('status').read_text()
  out['processes'].append({'pid':p.name,'cmdline':args,'env_allowlist':env,'status':{x.split(':',1)[0]:x.split(':',1)[1].strip() for x in status.splitlines() if x.startswith(('PPid:','VmRSS:','VmHWM:','Threads:'))},'cgroup':p.joinpath('cgroup').read_text()})
 except (FileNotFoundError,PermissionError): pass
for key,args in {
 'clock':['chronyc','tracking'],'time':['timedatectl','show'],'nft':['nft','list','ruleset'],
 'sshd_effective':['sshd','-T'],'sshd_check':['sshd','-t'],'listeners':['ss','-lntup'],
 'network_address':['ip','-j','address'],'network_routes':['ip','-j','route','show','table','all'],
 'enabled':['systemctl','list-unit-files','prism*','--no-pager'],'failed':['systemctl','--failed','--no-pager'],
 'kernel_errors':['journalctl','-k','-p','err','--since','2026-10-14 15:44:00 UTC','--no-pager'],
 'system_errors':['journalctl','-p','err','--since','2026-10-14 15:44:00 UTC','--no-pager'],
 'locks':['lslocks','--json'],'disk':['df','-B1','/data'],'memory':['free','-b'],
 'system_state':['systemctl','is-system-running'],'boot':['journalctl','--list-boots','--no-pager'],
 'kernel_targets':['ls','-l','/vmlinuz','/boot/grub/grub.cfg'],
 'data_usage':['du','-B1','--max-depth=2','/data'],
}.items(): out[key]=cmd(args)
files=[pathlib.Path('/etc/nftables.conf'),pathlib.Path('/etc/ssh/sshd_config'),*pathlib.Path('/etc/ssh/sshd_config.d').glob('*.conf')]
out['security_config_sha256']={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()}
out['loadavg']=pathlib.Path('/proc/loadavg').read_text()
out['profile']=json.loads(pathlib.Path('/data/state/profile.json').read_text())
for path in ['/data/free_event_sources/collector.json','/data/free_event_sources/worker.json','/data/microstructure/status.json']:
 out[path]=json.loads(pathlib.Path(path).read_text())
print(json.dumps(out,indent=2,default=str))
