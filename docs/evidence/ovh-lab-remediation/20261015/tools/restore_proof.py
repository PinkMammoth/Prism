from contextlib import closing
from pathlib import Path
import gzip,shutil,tempfile,json,os,sys
from datetime import datetime,UTC
from market_signal.ops import backups as b, profiles as pf
from market_signal.data.store import Store,NotAuthoritative,authority_claim
root=Path(__file__).resolve().parent.parent
receipt_path=Path(sys.argv[1]) if len(sys.argv)>1 else root/'real-backup-offhost-pull.json'
receipt=json.loads(receipt_path.read_text())
archive=Path(receipt['archive']);manifest=json.loads(b.sidecar(archive).read_text())
scratch=Path(tempfile.mkdtemp(prefix='ovh-lab-remediation-restore-'))/'restored.duckdb'
with gzip.open(archive,'rb') as fi,scratch.open('wb') as fo:shutil.copyfileobj(fi,fo)
original=b.inspect_backup(scratch)
assert original['sha256']==manifest['sha256']
os.environ.update(PRISM_PROFILE='autotrader_lab',PRISM_AUTHORITY='none',PRISM_DEPLOYMENT_TARGET='local',PRISM_RUNTIME_ROLE='scratch',PRISM_RUNTIME_ID='lab-remediation-restore')
with closing(Store(scratch)) as s:
 claim=authority_claim(s.con)
 claims=s.con.execute("SELECT count(*) FROM runtime_events WHERE event_type='authority_claimed'").fetchone()[0]
 active=s.con.execute("SELECT count(*) FROM runtime_events WHERE event_type='lab_experiment_activated'").fetchone()[0]
 assert s.con.execute('SELECT count(*) FROM paper_nimble_runs').fetchone()[0]==0
restored=b.inspect_backup(scratch)
assert original['table_fingerprints']==restored['table_fingerprints']==manifest['table_fingerprints']
assert original['source_authority']==restored['source_authority']
os.environ.pop('PRISM_RUNTIME_ROLE');guard=False
try:
 with closing(Store(scratch)):pass
except NotAuthoritative:guard=True
assert guard
os.environ['PRISM_RUNTIME_ROLE']='scratch'
resolution=pf.resolve().describe();assert not resolution['execution_allowed'] and not resolution['external_actions_allowed']
out={'at':datetime.now(UTC).isoformat(),'source':receipt['source'],'archive':str(archive),'scratch':str(scratch),'source_sha256':manifest['sha256'],'source_frontier':manifest['source_frontier'],'all_table_fingerprints_match':True,'tables_verified':len(original['table_fingerprints']),'schema_version':original['schema_version'],'scratch_profile':resolution,'historical_authority_metadata':claim,'authority_events':claims,'new_authority_events_created':0,'lab_activation_events':active,'role_unset_writable_open_refused':guard,'restored_ok':True}
print(json.dumps(out,indent=2,default=str))
