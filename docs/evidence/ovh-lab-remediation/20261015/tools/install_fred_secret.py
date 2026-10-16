"""Provision the existing operator FRED key to the Lab's designated secret file via stdin."""
import json,subprocess,shlex
from dotenv import dotenv_values
key=dotenv_values('/home/matth/prism/.env').get('FRED_API_KEY')
assert key and key.isalnum(), 'FRED source missing or unexpected format; value omitted'
remote='''import sys,pathlib,os,pwd,grp,tempfile,json
p=pathlib.Path('/etc/prism/secrets.env')
key=sys.stdin.read().strip()
assert key and key.isalnum(), 'invalid secret input; value omitted'
old=p.read_text()
assert not any(line.strip().removeprefix('export ').startswith('FRED_API_KEY=') for line in old.splitlines()), 'key already configured; refusing duplicate'
fd,tmp=tempfile.mkstemp(prefix='.secrets-',dir=p.parent)
with os.fdopen(fd,'w') as f:
 f.write(old+('' if not old or old.endswith('\\n') else '\\n')+'FRED_API_KEY='+key+'\\n');f.flush();os.fsync(f.fileno())
os.chown(tmp,0,grp.getgrnam('prism').gr_gid);os.chmod(tmp,0o640);os.replace(tmp,p)
print(json.dumps({'path':str(p),'FRED_API_KEY_present':True,'owner':'root','group':'prism','mode':'0640','other_entries_preserved':True}))
'''
p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','debian@178.33.95.139','sudo /usr/bin/python3 -c '+shlex.quote(remote)],input=key,text=True,capture_output=True)
assert p.returncode==0,'remote secret provisioning failed; output withheld'
print(p.stdout,end='')
