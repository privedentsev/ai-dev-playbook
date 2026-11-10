"""Independent minimal reproduction of missing optional Finder material."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

actual_meta = json.loads((ROOT / 'docs/tasks/PB-051.meta.json').read_text())
actual = next(item for item in actual_meta['materials'] if item['id'] == 'finder-metadata')
assert actual['source'] == 'work/verification/PB-051/.DS_Store'
assert actual['required'] is False
assert (ROOT / actual['source']).is_file()
with tempfile.TemporaryDirectory(prefix='pb051-finder-probe-') as directory:
    root = Path(directory)
    code = subprocess.run([sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'), 'init', str(root), '--apply'], capture_output=True, text=True)
    assert code.returncode == 0, code.stderr
    code = subprocess.run([sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'), '--root', str(root), 'card', 'create', 'PROBE-51', '--title', 'Finder material reproduction', '--apply'], capture_output=True, text=True)
    assert code.returncode == 0, code.stderr
    meta = json.loads((root / 'docs/tasks/PROBE-51.meta.json').read_text())
    item = dict(actual, source='work/verification/PROBE-51/.DS_Store', used_by=['PROBE-51'])
    meta['materials'] = [item]
    source = root / item['source']
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'Finder metadata fixture')
    playbook.lifecycle._task_material_catalog(root, 'PROBE-51', meta, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require)
    source.unlink()
    try:
        playbook.lifecycle._task_material_catalog(root, 'PROBE-51', meta, playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require)
    except ValueError as error:
        assert 'материал отсутствует' in str(error) and '.DS_Store' in str(error)
        print('REPRODUCED:', str(error))
    else:
        raise AssertionError('Missing optional Finder material was not rejected')
    print('CONFIRMED: required=false does not bypass material-catalog existence validation')
print('Current PB-051 still references the same Finder material; original files/metadata unchanged')
