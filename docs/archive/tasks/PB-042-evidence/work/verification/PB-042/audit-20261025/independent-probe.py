"""Independent PB-042 audit. Run from repository root with Python 3."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path.cwd()
sys.path.insert(0, str(REPO / 'tests'))
from test_document_cli import DocumentCliCase
from test_documents import doc_meta, write_doc
from test_playbook import playbook, write_card
from test_index_replay import IndexReplayTest

RESULTS = []
def done(name, **extra):
    row = {'scenario': name, 'result': 'PASS', **extra}
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

def state(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts
            and p != root / 'work/gate.lock'}

case = DocumentCliCase()
case.setUp()
root = case.root
cli = root / 'ai-dev-playbook/scripts/playbook.py'
def cmd(*args, expected=0):
    r = subprocess.run([sys.executable, str(cli), '--root', str(root), *args],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == expected, (args, r.returncode, r.stdout, r.stderr)
    return r

def fresh():
    playbook._task_index_fresh(root, 'independent audit')
    playbook._catalog_consistency(root, 'independent audit', ['spec', 'plan'])

def noop(*args):
    before = state(root)
    r = cmd(*args)
    assert 'актуальны' in r.stdout, r.stdout
    assert state(root) == before, 'no-op changed content, file composition or mtime'

try:
    for options in [(), ('--documents',), ('--all',)]:
        for n, (status, docstate) in enumerate([
            ('in_review', 'draft'), ('in_progress', 'accepted'),
            ('in_review', 'draft'), ('in_progress', 'accepted'), ('in_review', 'draft')]):
            if options != ('--documents',):
                write_card(root / 'docs/tasks', 'DEV-1', status)
            if options:
                write_doc(root / 'docs/specs', 'alpha.md', doc_meta(state=docstate))
            cmd('index', *options)
            fresh()
            noop('index', *options)
        done('subprocess cycles + full-tree no-op', options=list(options), transitions=5)
    for rel in ['docs/tasks/index.md', 'docs/specs/index.md']:
        p = root / rel
        p.unlink()
        cmd('index', '--all')
        fresh()
        noop('index', '--all')
        done('recreate deleted index with historical content', path=rel)
    # Shared gate is held by this process, the CLI runs as an independent child.
    with (root / 'work/gate.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for options in [(), ('--documents',), ('--all',)]:
            r = cmd('index', *options, expected=1)
            assert 'блокировк' in r.stderr, r.stderr
    done('shared gate refuses three CLI variants from foreign process')
    # Real SIGKILL after the first write of a multi-index operation.
    write_card(root / 'docs/tasks', 'DEV-1', 'in_progress')
    write_doc(root / 'docs/specs', 'alpha.md', doc_meta(state='accepted'))
    targets = ['docs/tasks/index.md', 'docs/specs/index.md']
    before = {rel: (root / rel).read_bytes() for rel in targets}
    launcher = '''import sys,signal
sys.path.insert(0, sys.argv[1])
import playbook
original=playbook.file_operations._atomic_write
def stop(target, content, op_id):
    original(target,content,op_id)
    print('AFTER_FIRST_WRITE',flush=True)
    signal.pause()
playbook.file_operations._atomic_write=stop
sys.exit(playbook.main(['--root',sys.argv[2],'index','--all']))
'''
    proc = subprocess.Popen([sys.executable, '-u', '-c', launcher, str(cli.parent), str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import select
    assert select.select([proc.stdout], [], [], 30)[0], 'child did not reach first write'
    assert proc.stdout.readline().strip() == 'AFTER_FIRST_WRITE'
    proc.kill()
    proc.communicate(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    marker = json.loads((root / 'work/lifecycle/pending.json').read_text())
    assert (root / targets[0]).read_bytes() != before[targets[0]]
    cmd('index', '--all', expected=1)
    restore = root / marker['op_dir'] / 'restore.py'
    r = subprocess.run([sys.executable, str(restore)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert {rel: (root / rel).read_bytes() for rel in targets} == before
    assert not (root / 'work/lifecycle/pending.json').exists()
    cmd('index', '--all')
    fresh()
    noop('index', '--all')
    done('SIGKILL after first write -> pending refusal -> standalone restore -> retry')
    # The same saved plan must retain its prior already_completed semantics.
    with tempfile.TemporaryDirectory() as td:
        eroot = Path(td)
        (eroot / 'a.txt').write_bytes(b'old')
        spec = {'operation': 'audit', 'plan_id': 'audit-saved-plan', 'actor': 'auditor',
                'writes': {'a.txt': b'new'},
                'inputs': playbook.file_operations.snapshot_paths(eroot, ['a.txt'], playbook.safe_path, playbook.require)}
        apply = lambda: playbook.file_operations.run_operation(eroot, spec, playbook.safe_path, playbook.require)
        assert apply()['status'] == 'completed'
        (eroot / 'a.txt').write_bytes(b'external-edit')
        assert apply()['status'] == 'already_completed'
        assert (eroot / 'a.txt').read_bytes() == b'external-edit'
    done('same saved plan remains already_completed and preserves later edits')
finally:
    case.doCleanups()

# Mutation reproduces the previous deterministic-index identity, in memory only.
run = playbook._run_operation
def old_identity(root, spec, gate):
    if spec['operation'] == 'index':
        deterministic = playbook._write_spec(root, 'index', 'index', spec['writes'], spec['staged_checks'])
        spec['plan_id'] = deterministic['plan_id']
    return run(root, spec, gate)
buffer = io.StringIO()
with patch.object(playbook, '_run_operation', old_identity):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IndexReplayTest)
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
(REPO / 'work/verification/PB-042/audit-20261025/old-identity-mutation.log').write_text(buffer.getvalue())
assert result.testsRun == 3 and not result.wasSuccessful(), buffer.getvalue()
done('permanent regression detects old identity mutation', tests=result.testsRun,
     failures=len(result.failures), errors=len(result.errors))
print(json.dumps({'result': 'PASS', 'scenarios': len(RESULTS)}, ensure_ascii=False))
