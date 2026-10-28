"""PB-051 A-04: normal copy, material integrity and explicit criterion sync."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'ai-dev-playbook/scripts/playbook.py').is_file())
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

expected = json.loads((ROOT / 'docs/tasks/PB-051-evidence/finder-fix/preserved.json').read_text())
for rel, digest in expected.items():
    assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest, rel
print(f'PASS: {len(expected)} historical evidence files unchanged')
with tempfile.TemporaryDirectory(prefix='pb051-fixed-copy-') as directory:
    copy = Path(directory) / 'repo'
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns('.git', '.DS_Store', '__pycache__', '.pytest_cache', '*.pyc', 'pilot-workspace'), symlinks=True)
    meta = json.loads((copy / 'docs/tasks/PB-051.meta.json').read_text())
    assert all(m['id'] != 'finder-metadata' for m in meta['materials'])
    assert not (copy / 'work/verification/PB-051/.DS_Store').exists()
    playbook.lifecycle._task_material_catalog(copy, 'PB-051', meta, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require)
    print('PASS: registered material catalog on normal copy without Finder metadata')
    playbook.lifecycle.sync_criteria_command(copy, 'PB-051', True, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require, bind=False)
    print('PASS: explicit PB-051 criterion sync on normal copy')
