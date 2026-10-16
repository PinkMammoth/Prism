"""Register the already independently restored orphan without creating/replacing a snapshot."""
import json
from pathlib import Path
from market_signal.ops import backups as b
from market_signal.ops import runtime as rt
source=Path('/data/backups/daily/prism-20261015T013001Z.duckdb')
assert rt._sha256(source)=='28d26cdd48ff356eeba840c30f5a1a5eefc538ccaa20c969a316457061442d5a'
manifest=b.register(Path('/data/backups'),source,Path('/data/prism.duckdb'),origin='vps',reason='Targeted remediation: independently restored orphan from failed registration',source_revision='530ba0d18c3009352eee21bb7f1a1c01004ab73a')
print(json.dumps({'source':str(source),'sha256':manifest['sha256'],'snapshot_kind':manifest['snapshot_kind'],'manifest_created':b.sidecar(source).exists(),'tables_verified':len(manifest['table_fingerprints']),'source_frontier':manifest['source_frontier']},indent=2,default=str))
