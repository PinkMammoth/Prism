from contextlib import closing
import hashlib,json,os,shutil,tempfile
from pathlib import Path
from datetime import datetime,UTC
from market_signal.data.store import Store,authority_claim,NotAuthoritative
from market_signal.ops import profiles,runtime
source=Path('/tmp/ovh-lab-prism-20261015T013001Z.duckdb')
expected=Path('docs/evidence/ovh-autotrader-rehearsal/20261015/backup-source-sha256.txt').read_text().split()[0]
def sha(p): return hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
assert sha(source)==expected,'download checksum mismatch'
os.environ.update(PRISM_PROFILE='autotrader_lab',PRISM_AUTHORITY='none',PRISM_DEPLOYMENT_TARGET='local',PRISM_RUNTIME_ROLE='scratch',PRISM_RUNTIME_ID='ovh-lab-restore-proof')
def fp(p):
    with closing(Store(p,read_only=True)) as s:
        tables={}
        for (t,) in s.con.execute('SHOW TABLES').fetchall():
            n,h=s.con.execute(f'SELECT count(*),bit_xor(hash(x)) FROM "{t}" x').fetchone()
            tables[t]=[n,str(h)]
        return {'tables':tables,'authority':authority_claim(s.con)}
orig=fp(source)
scratch=Path(tempfile.mkdtemp(prefix='ovh-lab-restore-'))/'restored.duckdb'
shutil.copy2(source,scratch)
assert sha(scratch)==expected
with closing(Store(scratch)) as s:
    schema=s.con.execute('SELECT max(version) FROM schema_version').fetchone()[0]
    frontiers={}
    for t,col in [('perp_intraday_bars','open_time'),('perp_funding','time'),('context_hl_oi_hourly','captured_at'),('context_ls_ratios','ingested_at'),('microstructure_minutes','minute_open'),('context_events','first_seen_at'),('runtime_cycles','started_at')]:
        cols=[r[0] for r in s.con.execute(f'DESCRIBE {t}').fetchall()]
        if col in cols: frontiers[t]=str(s.con.execute(f'SELECT max({col}) FROM {t}').fetchone()[0])
after=fp(scratch)
assert orig==after,'restored table fingerprint mismatch'
os.environ.pop('PRISM_RUNTIME_ROLE')
blocked=False
try:
    with closing(Store(scratch)): pass
except NotAuthoritative: blocked=True
assert blocked,'scratch claim accepted without explicit role'
os.environ['PRISM_RUNTIME_ROLE']='scratch'
resolution=profiles.resolve().describe()
result={'at':datetime.now(UTC).isoformat(),'source_host':'178.33.95.139','source_path':'/data/backups/daily/prism-20261015T013001Z.duckdb','offhost_path':str(source),'scratch_path':str(scratch),'sha256':expected,'bytes':source.stat().st_size,'checksum_matches_source':True,'source_manifest_exists':False,'schema_version':schema,'frontiers':frontiers,'source_fingerprint':orig,'restored_fingerprint':after,'all_table_fingerprints_match':True,'authority_role_unset_write_refused':blocked,'scratch_profile':resolution,'scratch_claim_unchanged':orig['authority']==after['authority'],'new_authority_or_activation_events':0,'restored_ok':True,'verify_backup':runtime.verify_backup(source),'builtin_restore_test':runtime.restore_test(source)}
assert not resolution['execution_allowed'] and not resolution['external_actions_allowed']
print(json.dumps(result,indent=2,default=str))
