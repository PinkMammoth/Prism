"""Root launcher: exact scheduler environment/user/cwd, values never serialized."""
import pathlib,subprocess,os,sys,json
pid=subprocess.check_output(['systemctl','show','prism-scheduler','-p','MainPID','--value'],text=True).strip()
env=dict(entry.decode().split('=',1) for entry in pathlib.Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in entry)
assert env.get('PRISM_AUTHORITY')=='rehearsal' and env.get('PRISM_RUNTIME_ID')=='vps-prism-autotrader-lab-rehearsal'
assert env.get('FRED_API_KEY'),'FRED not visible; value omitted'
command=['/opt/prism/current/.venv/bin/market','ops','cycle',sys.argv[1],'--trigger','remediation-manual','--wait','600']
print(json.dumps({'scheduler_pid':pid,'user':'prism','cwd':'/opt/prism/current','FRED_API_KEY_present':True,'command':command}),flush=True)
result=subprocess.run(command,cwd='/opt/prism/current',env=env,user='prism',group='prism',capture_output=True,text=True)
secret=env['FRED_API_KEY'];safe=result.stdout.replace(secret,'[REDACTED]')+result.stderr.replace(secret,'[REDACTED]')
print(safe,end='');print(json.dumps({'exit_code':result.returncode,'secret_in_child_output':secret in result.stdout or secret in result.stderr}),flush=True)
sys.exit(result.returncode)
