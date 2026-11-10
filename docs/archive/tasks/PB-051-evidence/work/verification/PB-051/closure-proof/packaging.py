"""Byte-preserving repair of an auditor-created raw snapshot's media type."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

OLD = 'work/verification/PB-051/audit-20261027/card-original.md'
NEW = 'work/verification/PB-051/audit-20261027/card-original.txt'
META = 'docs/tasks/PB-051.meta.json'

def operation(root):
    root = root.resolve()
    content = (root / OLD).read_bytes()
    digest = playbook.updating.sha256(content)
    meta = json.loads((root / META).read_text())
    assert not (root / NEW).exists()
    meta['moves'] = list(meta.get('moves', [])) + [{'from': OLD, 'to': NEW}]
    updated = (json.dumps(meta, ensure_ascii=False, indent=2) + '\n').encode()
    def check(project):
        assert not (project / OLD).exists()
        assert playbook.updating.sha256((project / NEW).read_bytes()) == digest
        stored = json.loads((project / META).read_text())
        assert stored == meta
        playbook.check(project, True)
    spec = {'operation': 'audit evidence packaging',
            'plan_id': 'pb051-snapshot-txt-' + digest[7:23],
            'actor': 'Codex (auditor)',
            'basis': 'Engineer authorized PB-051 audit and closure; repair own raw snapshot packaging while preserving every evidence byte',
            'writes': {NEW: content, META: updated}, 'removes': [OLD],
            'inputs': playbook.file_operations.snapshot_paths(root, [OLD, NEW, META, 'docs/tasks/PB-051.md'], playbook.safe_path, playbook.require),
            'moves': {OLD: NEW},
            'receipt': {'sha256': digest, 'purpose': 'raw snapshot is data, not live Markdown documentation'},
            'staged_checks': [check], 'final_checks': [check]}
    result = playbook.file_operations.run_operation(root, spec, playbook.safe_path, playbook.require)
    print('PACKAGING:', json.dumps({'old': OLD, 'new': NEW, 'unchanged_sha256': digest, 'result': str(result)}, ensure_ascii=False))
    return digest

def close_copy(root):
    root = root.resolve()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert playbook.main(['--root', str(root), 'lifecycle', 'register-material', 'PB-051', 'work/verification/PB-051/closure-proof', '--id', 'closure-proof', '--role', 'report', '--immutable', '--required', '--actor', 'Codex', '--apply']) == 0
        digest = operation(root)
        assert playbook.main(['--root', str(root), 'close', 'PB-051', '--dry-run']) == 0
        assert playbook.main(['--root', str(root), 'close', 'PB-051']) == 0
        playbook.check(root, True)
    archived = root / 'docs/archive/tasks/PB-051-evidence' / NEW
    assert playbook.updating.sha256(archived.read_bytes()) == digest
    assert 'Status: done' in (root / 'docs/archive/tasks/PB-051.md').read_text()
    print('PASS: actual recoverable packaging -> dry-run -> actual close -> full structural/link check on temporary copy')
    print('PASS: immutable snapshot bytes preserved in final archive')
    return output.getvalue()

if sys.argv[1:] == ['--probe']:
    config = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())
    with tempfile.TemporaryDirectory(prefix='pb051-close-packaging-') as directory:
        root = (Path(directory) / 'project').resolve()
        root.mkdir()
        for entry in config['verification']['inputs'] + ['work/verification']:
            source, target = ROOT / entry, root / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns('.DS_Store', '__pycache__', '*.pyc', 'pilot-workspace'))
            else:
                shutil.copyfile(source, target)
        transcript = close_copy(root)
        (OUT / 'temporary-close-details.log').write_text(transcript)
elif sys.argv[1:] == ['--apply']:
    operation(ROOT)
else:
    raise SystemExit('Use --probe or --apply')
