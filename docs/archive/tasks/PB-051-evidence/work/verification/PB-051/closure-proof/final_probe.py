"""Verify final archived evidence through the recorded relocation map."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

card = ROOT / 'docs/archive/tasks/PB-051.md'
assert card.is_file() and card.with_suffix('.meta.json').is_file()
assert not (ROOT / 'docs/tasks/PB-051.md').exists()
assert not (ROOT / 'docs/tasks/PB-051-evidence').exists()
text = card.read_text()
assert 'Status: done' in text and '### AUDIT-03 — Аудит' in text
meta = json.loads(card.with_suffix('.meta.json').read_text())
assert meta['acceptance']['result'] == 'accepted' and meta['acceptance']['audit_id'] == 'AUDIT-03'
assert meta['handoff']['entry_id'] == 'HANDOFF-02'
assert not any(item['id'] == 'finder-metadata' for item in meta['materials'])
config, paths = playbook.config(ROOT)
records = playbook.read_tasks(ROOT, paths)
catalog = playbook.documents.load_catalog(ROOT, config, paths, records, playbook.safe_path, playbook.require)
def relocated(path):
    for _ in range(len(catalog['moves']) + 1):
        target = playbook.document_links.map_path(path, catalog['moves'])
        if target == path:
            return path
        path = target
    raise AssertionError('Relocation cycle')
audit = next(item for item in meta['materials'] if item['id'] == 'reaudit-03')
base = ROOT / audit['source']
hashes = json.loads((base / 'material-hashes-before.json').read_text())
for source, digest in hashes.items():
    actual = relocated(ROOT / source)
    assert playbook.updating.sha256(actual.read_bytes()) == digest, source
assert (ROOT / 'CHANGELOG.md').read_text() == (base / 'changelog-after-close.txt').read_text()
assert (ROOT / 'CHANGELOG.md').read_text().count('## [Unreleased]') == 1
print(f'PASS: archive/status/acceptance; {len(hashes)} historical hashes preserved through actual relocation map; changelog correct')
def check(project):
    playbook.check(project.resolve(), True)
with tempfile.TemporaryDirectory(prefix='pb051-final-staged-') as directory:
    plan = dict(writes={}, removes=[], create_dirs=[], remove_dirs=[], expected={}, staged_checks=[check])
    playbook.file_operations._prepare_staged(ROOT, Path(directory).resolve(), plan, playbook.safe_path, playbook.require)
print('PASS: actual final _prepare_staged checks archive, links and all registered evidence')
