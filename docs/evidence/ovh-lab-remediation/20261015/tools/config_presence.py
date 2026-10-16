import json,stat,pwd,grp,pathlib,subprocess
paths=[pathlib.Path('/etc/prism/prism.env'),pathlib.Path('/etc/prism/secrets.env'),pathlib.Path('/opt/prism/current/.env'),pathlib.Path('/home/debian/.env'),pathlib.Path('/home/debian/.ovh.conf'),pathlib.Path('/etc/ovh.conf')]
out={'files':[],'service_visibility':[]}
for p in paths:
 d={'path':str(p),'exists':p.exists()}
 if p.exists():
  s=p.stat();d.update(owner=pwd.getpwuid(s.st_uid).pw_name,group=grp.getgrgid(s.st_gid).gr_name,mode=oct(stat.S_IMODE(s.st_mode)))
  if p.name.endswith('.env') or p.name=='.env':
   data=p.read_text();names=[x.split('=',1)[0].strip().removeprefix('export ').strip() for x in data.splitlines() if '=' in x and not x.lstrip().startswith('#')]
   d['FRED_API_KEY_defined']='FRED_API_KEY' in names;d['fred_key_names']=[n for n in names if 'FRED' in n.upper()]
 out['files'].append(d)
for unit in ['prism-scheduler','prism-free-source-ingest','prism-microstructure']:
 pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip();e=pathlib.Path('/proc',pid,'environ').read_bytes().split(b'\0') if pid!='0' else []
 out['service_visibility'].append({'unit':unit,'pid':pid,'FRED_API_KEY_present':any(x.startswith(b'FRED_API_KEY=') and x.split(b'=',1)[1].strip() for x in e)})
print(json.dumps(out,indent=2))
