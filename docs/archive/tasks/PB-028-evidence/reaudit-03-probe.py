"""Independent fresh install, rollback and update using the frozen 0.0.2 CLI."""
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'ai-dev-playbook/scripts'))
import playbook
import updating


def cli(script, *args):
    r = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and 'work' not in p.relative_to(root).parts
            and '__pycache__' not in p.parts}

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    current = REPO / 'ai-dev-playbook/scripts/playbook.py'
    for rel in ('single', 'nested/parents/project'):
        target = base / rel
        cli(current, 'init', target, '--apply')
        cli(target / 'ai-dev-playbook/scripts/playbook.py', '--root', target, 'check')
    print('PASS init absent and nested target; installed CLI check')

    target = base / 'rollback-parent/child'
    original = Path.open
    writes = []
    def fail_on_write(path, mode='r', *args, **kwargs):
        if mode == 'xb':
            writes.append(str(path))
            if path.name == 'CHANGELOG.md':
                raise OSError('independent failure after first written file')
        return original(path, mode, *args, **kwargs)
    with patch.object(Path, 'open', fail_on_write), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = playbook.main(['init', str(target), '--apply'])
    assert code == 1 and len(writes) >= 2 and not target.parent.exists(), (code, writes)
    print('PASS rollback after actual first file write; new parent removed')

    artifact = REPO / 'tests/fixtures/release-0.0.2'
    release = json.loads((artifact / 'release.json').read_text())
    content = updating.managed_files(artifact, playbook.safe_path, playbook.require)
    hashes = {k: updating.sha256(v) for k,v in content.items()}
    assert hashes == release['files']
    try:
        updating.load_artifact(artifact, playbook.safe_path, playbook.require)
    except ValueError as error:
        print('FAIL frozen artifact integrity:', str(error), 'computed:', updating.fingerprint(hashes))
    else:
        print('PASS frozen artifact integrity')
    print('Frozen fixture:', release['release_id'], 'files:', len(hashes))
    root = base / 'upgrade'
    old = artifact / 'ai-dev-playbook/scripts/playbook.py'
    cli(old, 'init', root, '--apply')
    installed = root / 'ai-dev-playbook/scripts/playbook.py'
    config_path = root / 'ai-dev-playbook/project.json'
    config = json.loads(config_path.read_text())
    config['name'] = 'independent-upgrade'
    config['setup_status'] = 'configured'
    config['paths'] = {'docs':'custom', 'tasks':'custom/tasks', 'archive':'custom/history', 'task_index':'custom/tasks/index.md'}
    config['custom'] = {'keep': 'value'}
    config_path.write_text(json.dumps(config)+'\n')
    (root / 'docs').rename(root / 'custom')
    for p in (root / 'custom').glob('*.md'):
        p.write_text(p.read_text().replace('TODO:', 'Filled:'))
    agents = root / 'AGENTS.md'
    agents.write_text(agents.read_text().replace('docs/', 'custom/'))
    history = root / 'custom/history'; history.mkdir()
    (history / 'HIST-1.md').write_text('# HIST-1: Historical\n\nID: HIST-1\nStatus: done\nPriority: P2\nDepends-On: -\nCreated: unknown\nClosed: unknown\n')
    evidence = root / 'custom/tasks/HIST-1-evidence'; evidence.mkdir()
    (evidence / 'raw.bin').write_bytes(bytes(range(256)))
    (root / 'foreign.txt').write_text('foreign\n')
    cli(installed, '--root', root, 'index')
    cli(installed, '--root', root, 'check')
    before = files(root)
    cli(installed, '--root', root, 'update', 'plan', REPO, '--save')
    cli(installed, '--root', root, 'update', 'apply')
    after = files(root)
    protected = [p for p in before if not p.startswith('ai-dev-playbook/')]
    assert all(before[p] == after.get(p) for p in protected)
    result = json.loads(config_path.read_text())
    assert result['paths'] == config['paths'] and result['custom'] == config['custom'] and result['playbook_version'] == '0.0.3'
    cli(installed, '--root', root, 'check', '--ready')
    print('PASS update via installed 0.0.2 CLI; custom config and all', len(protected), 'project files preserved byte-for-byte; target CLI ready')
