"""Repeat original probes and independently probe fix boundaries; temp roots only."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

capture = io.StringIO()
with contextlib.redirect_stdout(capture):
    ns = runpy.run_path(str(Path(__file__).with_name('audit_repro.py')))
results = {'original_fix_scenarios': json.loads(capture.getvalue()), 'boundary_probes': []}
fo, pb, child, restore, apply = [ns[n] for n in ('fo', 'pb', 'child', 'restore', 'apply')]
for standalone in (False, True):
    with tempfile.TemporaryDirectory(prefix='pb023-review2-') as folder:
        root = Path(folder).resolve()
        (root / 'docs').mkdir()
        (root / 'docs/a.md').write_bytes(b'OLD')
        op = child(root, 'temp')
        foreign = root / 'docs' / ('.notes.fop-' + op.name + '-manual.tmp')
        foreign.write_bytes(b'EXTERNAL CONTENT, NOT AN ENGINE TEMP')
        restore(root, op, standalone)
        assert foreign.read_bytes() == b'EXTERNAL CONTENT, NOT AN ENGINE TEMP'
        results['boundary_probes'].append({'case': 'foreign_temp_name', 'restore': 'standalone' if standalone else 'engine', 'foreign_preserved': foreign.exists(), 'pending': fo.pending_info(root) is not None})

with tempfile.TemporaryDirectory(prefix='pb023-review2-') as folder:
    root = Path(folder).resolve()
    (root / 'docs').mkdir()
    (root / 'docs/a.md').write_bytes(b'OLD')
    op = child(root, 'receipt')
    code = '''
import sys, os, signal
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import file_operations as f
import playbook as p
def stop(*args): os.kill(os.getpid(), signal.SIGKILL)
f._neutralize_receipt = stop
f.restore_operation(Path(sys.argv[2]), p.safe_path, p.require)
'''
    result = subprocess.run([sys.executable, '-c', code, str(ns['SCRIPTS']), str(root)], capture_output=True, text=True)
    assert result.returncode == -9, result.stderr
    assert (root / 'docs/a.md').read_bytes() == b'OLD'
    assert fo.pending_info(root) is not None
    # Живой pending исключает already_completed: повтор отклоняется до restore.
    try:
        retry = apply(root)['status']
    except ValueError as error:
        retry = 'refused: ' + str(error)
    assert retry != 'already_completed'
    # Завершённый restore снимает маркер, повтор реально применяет план.
    assert restore(root, op, False) == 'restored'
    retry = apply(root)
    assert retry['status'] == 'completed'
    assert (root / 'docs/a.md').read_text() == 'NEW'
    results['boundary_probes'].append({'case': 'kill_restore_before_receipt_neutralization', 'retry': retry['status'], 'actual': (root / 'docs/a.md').read_text(), 'planned': 'NEW', 'pending': fo.pending_info(root) is not None})

for mask in (0o022, 0o077):
    with tempfile.TemporaryDirectory(prefix='pb023-review2-') as folder:
        root = Path(folder).resolve()
        (root / 'docs').mkdir()
        (root / 'docs/a.md').write_bytes(b'OLD')
        (root / 'docs/private').mkdir(mode=0o700)
        code = '''
import sys, os, signal
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import file_operations as f
import playbook as p
root = Path(sys.argv[2])
def stop(root): os.kill(os.getpid(), signal.SIGKILL)
s = dict(operation='audit', plan_id='dirs', removes=[], remove_dirs=['docs/private'], inputs={'docs/private':'dir'}, final_checks=[stop])
f.run_operation(root, s, p.safe_path, p.require)
'''
        r = subprocess.run([sys.executable, '-c', code, str(ns['SCRIPTS']), str(root)], capture_output=True, text=True)
        assert r.returncode == -9, r.stderr
        op = root / fo.pending_info(root)['op_dir']
        code = 'import os,runpy,sys; os.umask(int(sys.argv[2])); runpy.run_path(sys.argv[1],run_name="__main__")'
        r = subprocess.run([sys.executable, '-c', code, str(op / 'restore.py'), str(mask)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        actual = (root / 'docs/private').stat().st_mode & 0o777
        assert actual == 0o700 and fo.pending_info(root) is None
        results['boundary_probes'].append({'case': 'standalone_directory_modes', 'umask': oct(mask), 'restored_mode': oct(actual), 'passed': True})
print(json.dumps(results, ensure_ascii=False, indent=2))
