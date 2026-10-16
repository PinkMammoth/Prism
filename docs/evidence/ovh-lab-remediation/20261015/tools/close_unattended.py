"""One-shot evidence closure. No deployment, scheduler, authority or trading mutations."""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from market_signal.data.store import Store, NotAuthoritative, authority_claim
from market_signal.ops import backups as b, profiles

ROOT=Path('/data/validation/lab-remediation')
def save(name,data):
    tmp=ROOT/(name+'.tmp')
    tmp.write_text(json.dumps(data,indent=2,default=str)+'\n')
    tmp.replace(ROOT/name)

def restore(source):
    manifest=json.loads(b.sidecar(source).read_text())
    expected=b.inspect_backup(source)
    assert expected['sha256']==manifest['sha256']
    assert expected['table_fingerprints']==manifest['table_fingerprints']
    assert manifest.get('source_frontier') and manifest.get('source_authority')
    with tempfile.TemporaryDirectory(prefix='closure-restore-',dir=ROOT) as d:
        target=Path(d)/'restored.duckdb'
        shutil.copyfile(source,target)
        os.environ.update(PRISM_PROFILE='autotrader_lab',PRISM_AUTHORITY='none',PRISM_RUNTIME_ROLE='scratch',PRISM_RUNTIME_ID='lab-remediation-closure-scratch',PRISM_DEPLOYMENT_TARGET='local')
        with closing(Store(target)) as s:
            claim=authority_claim(s.con)
            assert s.con.execute('SELECT count(*) FROM paper_nimble_runs').fetchone()[0]==0
        restored=b.inspect_backup(target)
        assert restored['table_fingerprints']==expected['table_fingerprints']
        assert restored['source_authority']==expected['source_authority']
        os.environ.pop('PRISM_RUNTIME_ROLE')
        refused=False
        try:
            with closing(Store(target)):pass
        except NotAuthoritative:refused=True
        assert refused
        os.environ['PRISM_RUNTIME_ROLE']='scratch'
        resolved=profiles.resolve().describe()
        assert not resolved['execution_allowed'] and not resolved['external_actions_allowed']
        return {'source':str(source),'sha256':expected['sha256'],'restored_ok':True,'scratch_fresh':True,'scratch_cleaned_after_verification':True,'all_table_fingerprints_match':True,'tables_verified':len(restored['table_fingerprints']),'schema_version':restored['schema_version'],'source_frontier':manifest['source_frontier'],'historical_authority_metadata':claim,'new_authority_events_created':0,'role_unset_writable_open_refused':refused,'scratch_execution_allowed':False,'scratch_authority':'none'}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--check-now',action='store_true');args=ap.parse_args()
    out={'started_at':datetime.now(UTC).isoformat(),'check_now_only':args.check_now,'activation_performed':False,'review_required':True}
    name='closure-check-now.json' if args.check_now else 'closure.json'
    try:
        window=json.loads((ROOT/'window.json').read_text())
        since=datetime.fromisoformat(window['start']).strftime('%Y-%m-%d %H:%M:%S UTC')
        if not args.check_now:
            gate=json.loads((ROOT/'final.json').read_text());out['targeted_gate']=gate
            assert gate['clean'] and gate['duration_seconds']>=21600
            assert gate['scheduled_macro_successes']>=2 and gate['scheduled_backup_successes']>=1
        pid=subprocess.check_output(['systemctl','show','prism-scheduler','-p','MainPID','--value'],text=True).strip()
        env=dict(x.decode().split('=',1) for x in Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in x)
        secret=env.get('FRED_API_KEY');assert secret
        def command(argv):
            r=subprocess.run(argv,capture_output=True,text=True,timeout=120)
            return {'returncode':r.returncode,'stdout':r.stdout.replace(secret,'[REDACTED]'),'stderr':r.stderr.replace(secret,'[REDACTED]')}
        s=Store('/data/prism.duckdb',read_only=True,lock_timeout=120)
        try:
            c=s.con
            out['macro_cycles']=c.execute("SELECT cycle_id,trigger,started_at,finished_at,status FROM runtime_cycles WHERE job='context_macro' AND started_at>=? ORDER BY started_at",[window['start']]).fetchall()
            out['fred_provider_runs']=c.execute("SELECT provider,started_at,finished_at,status,received,new_updates,filtered,coalesce(error,'')<>'' AS has_error FROM context_provider_runs WHERE provider IN ('fred_calendar','fred_actuals') AND started_at>=? ORDER BY started_at",[window['start']]).fetchall()
            assert out['fred_provider_runs'] and all(x[3]=='ok' and not x[7] for x in out['fred_provider_runs'])
            out['all_cycles']=c.execute('SELECT cycle_id,job,trigger,started_at,finished_at,status FROM runtime_cycles WHERE started_at>=? ORDER BY started_at',[window['start']]).fetchall()
            assert all(x[5] in ('ok','running') for x in out['all_cycles'])
            out['authority']=authority_claim(c);assert out['authority'][0]=='vps-prism-autotrader-lab-rehearsal'
            out['paper_rows']={t:c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for (t,) in c.execute('SHOW TABLES').fetchall() if t.startswith('paper_') and t not in ('paper_nimble_policies','paper_nimble_triggers')}
            assert not any(out['paper_rows'].values())
            assert c.execute('SELECT count(*) FROM universe_evaluations').fetchone()[0]==0
            out['backup_events']=c.execute("SELECT recorded_at,payload FROM runtime_events WHERE event_type='backup_verified' AND recorded_at>=? ORDER BY recorded_at",[window['start']]).fetchall()
        finally:s.close()
        profile=json.loads(Path('/data/state/profile.json').read_text());out['profile']=profile
        assert profile['identity']=='prism_autotrader_lab_v1' and profile['authority']=='rehearsal'
        assert not profile['execution_allowed'] and not profile['external_actions_allowed']
        candidates=sorted(Path('/data/backups/daily').glob('*.duckdb'),key=lambda p:p.stat().st_mtime)
        assert candidates
        source=candidates[-1]
        if not args.check_now:
            assert source.name.startswith('prism-20261016T0130')
        child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--restore-internal',str(source)],capture_output=True,text=True,timeout=600)
        assert child.returncode==0, 'scratch restore child failed'
        out['restore']=json.loads(child.stdout)
        out['host']={k:command(v) for k,v in {
            'clock':['chronyc','tracking'],'nft':['nft','list','ruleset'],'sshd':['sshd','-T'],'sshd_check':['sshd','-t'],'listeners':['ss','-lntup'],'network_address':['ip','-j','address'],'network_routes':['ip','-j','route','show','table','all'],'failed_units':['systemctl','--failed','--no-pager'],'service_state':['systemctl','show','prism-microstructure','prism-free-sources','prism-free-source-ingest','prism-scheduler','prism-nimble','prism-universe','-p','Id,ActiveState,SubState,MainPID,NRestarts,MemoryCurrent,MemoryPeak,CPUUsageNSec'],'kernel_errors':['journalctl','-k','-p','err','--since',since,'--no-pager'],'system_errors':['journalctl','-p','err','--since',since,'--no-pager'],'journal':['journalctl','-u','prism-scheduler','-u','prism-microstructure','-u','prism-free-sources','-u','prism-free-source-ingest','--since',since,'--no-pager','-o','short-iso'],'usage':['du','-B1','--max-depth=2','/data'],'memory':['free','-b'],'disk':['df','-B1','/data']}.items()}
        logs=subprocess.check_output(['journalctl','-u','prism-scheduler','--since',since,'--no-pager','-o','cat'])
        out['secret_in_journal']=secret.encode() in logs
        out['secret_in_runtime_log']=secret.encode() in Path('/data/logs/runtime.log').read_bytes()
        out['secret_in_fred_raw_files']=any(secret.encode() in p.read_bytes() for p in Path('/data/raw/fred').rglob('*') if p.is_file())
        assert not any(out[x] for x in ('secret_in_journal','secret_in_runtime_log','secret_in_fred_raw_files'))
        out['kernel']=os.uname().release
        out['security_config_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('/etc/nftables.conf'),Path('/etc/ssh/sshd_config'),*Path('/etc/ssh/sshd_config.d').glob('*.conf')] if p.is_file()}
        out['resources']={'disk':shutil.disk_usage('/data')._asdict(),'load':os.getloadavg(),'meminfo':Path('/proc/meminfo').read_text()}
        out['edge_control_plane']='Unavailable; user accepts limitation; manual OVH Manager verification if needed. No credentials or networking changes.'
        for tool in ('spool_compare.py','ingest_recovery_proof.py'):
            argv=[sys.executable,str(Path(__file__).resolve().parent/tool)]
            if tool=='spool_compare.py':argv+=['/data/microstructure/spool','2026-10-15T18:26:00+00:00','2026-10-16T01:40:00+00:00' if not args.check_now else datetime.now(UTC).replace(second=0,microsecond=0).isoformat()]
            out[tool]=command(argv)
            assert out[tool]['returncode']==0
        out['checks_completed']=True
    except Exception as exc:
        out['checks_completed']=False;out['error_type']=type(exc).__name__
    out['finished_at']=datetime.now(UTC).isoformat();save(name,out)
    print(json.dumps({'receipt':str(ROOT/name),'checks_completed':out['checks_completed'],'check_now_only':args.check_now,'activation_performed':False}))
    return 0 if out['checks_completed'] else 1

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--restore-internal':print(json.dumps(restore(Path(sys.argv[2])),default=str))
    else:sys.exit(main())
