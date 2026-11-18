"""Read-only check of PB-054 final archive and all independently bound raw snapshots."""
import hashlib,json,sys
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'ai-dev-playbook/scripts/playbook.py').is_file())
sys.path.insert(0,str(ROOT/'ai-dev-playbook/scripts'))
import playbook
card=ROOT/'docs/archive/tasks/PB-054.md'
assert card.is_file(), 'PB-054 card not archived'
assert not (ROOT/'docs/tasks/PB-054.md').exists()
meta=json.loads(playbook.documents.meta_path(card).read_text())
by_id={m['id']:m for m in meta['materials']}
for ident,digest in meta.get('audit_packages',{}).items():
    folder=ROOT/by_id[ident]['source']
    provenance=(folder/'provenance.json').read_bytes()
    assert playbook.file_operations.sha256(provenance)==digest
    for name,info in json.loads(provenance)['files'].items():
        assert playbook.file_operations.sha256((folder/name).read_bytes())==info['sha256'],name
assert playbook.file_operations.pending_info(ROOT) is None
playbook.check(ROOT,True)
print('PASS: PB-054 archive, metadata, raw snapshot hashes, references and pending state')
