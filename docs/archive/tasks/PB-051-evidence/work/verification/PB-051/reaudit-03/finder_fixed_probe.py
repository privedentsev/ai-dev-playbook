"""Force the formerly failing criterion-sync write on a normal project copy."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

preserved = json.loads((ROOT / 'docs/tasks/PB-051-evidence/finder-fix/preserved.json').read_text())
assert len(preserved) == 50
assert all(playbook.updating.sha256((ROOT / rel).read_bytes()) == 'sha256:' + digest for rel, digest in preserved.items())
meta = json.loads((ROOT / 'docs/tasks/PB-051.meta.json').read_text())
assert not any(m['id'] == 'finder-metadata' or m['source'].endswith('/.DS_Store') for m in meta['materials'])
print('PASS A-04: Finder registration absent; 50 genuine historical evidence hashes preserved')
previous = json.loads((ROOT / 'work/verification/PB-051/reaudit-02/snapshot-before.json').read_text())['files']
checked = 0
for name, digest in previous.items():
    if name.startswith(('ai-dev-playbook/scripts/', 'tests/')):
        assert playbook.updating.sha256((ROOT / name).read_bytes()) == 'sha256:' + digest, name
        checked += 1
print(f'PASS: {checked} code/test/fixture files unchanged since preceding independent audit')

config = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())
with tempfile.TemporaryDirectory(prefix='pb051-a04-independent-') as directory:
    copy = Path(directory) / 'project'
    copy.mkdir()
    for entry in config['verification']['inputs'] + ['work/verification']:
        source, target = ROOT / entry, copy / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns('.DS_Store', '__pycache__', '*.pyc', 'pilot-workspace'))
        else:
            shutil.copyfile(source, target)
    assert not list(copy.rglob('.DS_Store'))
    playbook.lifecycle._task_material_catalog(copy, 'PB-051', meta, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require)
    print('PASS: catalog on copied verification inputs and materials without Finder metadata')
    card = copy / 'docs/tasks/PB-051.md'
    original = card.read_text()
    # Force the write path: A-04 is still open, so these synthetic checked
    # markers must be reset by sync (the original journal remains untouched).
    changed = original.replace('- [ ] AC-01:', '- [x] AC-01:', 1).replace('- [ ] AC-02:', '- [x] AC-02:', 1)
    assert changed != original
    card.write_text(changed)
    playbook.lifecycle.sync_criteria_command(copy, 'PB-051', True, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require, bind=False)
    assert card.read_text() == original
    print('PASS: forced criterion-sync write, staged/final checks; original text restored on copy')
print('All A-04 independent checks passed; original project unchanged')
