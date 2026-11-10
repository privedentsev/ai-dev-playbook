import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

config = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())
inputs = config['verification']['inputs']
assert 'CHANGELOG.md' in inputs
manifest = json.loads((ROOT / 'ai-dev-playbook/manifest.json').read_text())
artifact = playbook.updating.load_artifact(ROOT, playbook.safe_path, playbook.require)
assert (ROOT / 'ai-dev-playbook/VERSION').read_text().strip() == config['playbook_version'] == manifest['playbook_version'] == '0.1.0'
assert artifact['hashes'] == manifest['files']
assert artifact['release_id'] == manifest['release_id']
assert set(artifact['updates_from']) == {'0.0.2', '0.0.3'}
print(f'PASS versions, {len(manifest["files"])} manifest hashes, release_id, declared transitions')

changelog = (ROOT / 'CHANGELOG.md').read_text()
assert changelog.count('## [Unreleased]') == 1
assert not re.search(r'^## \[0\.1\.0\].*\d{4}-\d{2}-\d{2}', changelog, re.M)
for link in re.findall(r'\]\(([^)]+)\)', changelog):
    assert (ROOT / link).is_file(), link
assert all(token in changelog for token in ['0.0.3', 'legacy', 'v2', 'PB-041', 'PB-044', 'PB-038', 'PB-030'])
print('PASS changelog version, no release date, content and local links')

commands = []
def cli(root, *args):
    result = subprocess.run([sys.executable, str(root / 'ai-dev-playbook/scripts/playbook.py'), '--root', str(root), *args], capture_output=True, text=True)
    commands.append({'args': list(args), 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    assert result.returncode == 0, result.stderr
    return result

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    install = subprocess.run([sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'), 'init', str(root), '--apply'], capture_output=True, text=True)
    assert install.returncode == 0, install.stderr
    for name in ['project.json', 'manifest.json']:
        assert json.loads((root / 'ai-dev-playbook' / name).read_text())['playbook_version'] == '0.1.0'
    assert (root / 'ai-dev-playbook/VERSION').read_text().strip() == '0.1.0'
    print('PASS fresh CLI init 0.1.0')

    project_path = root / 'ai-dev-playbook/project.json'
    project = json.loads(project_path.read_text())
    project['playbook_version'] = '0.0.3'
    project['custom_setting'] = {'audit': 51}
    project_path.write_text(json.dumps(project))
    version_path = root / 'ai-dev-playbook/VERSION'
    version_path.write_text('0.0.3\n')
    manifest_path = root / 'ai-dev-playbook/manifest.json'
    old_manifest = json.loads(manifest_path.read_text())
    old_manifest['playbook_version'] = '0.0.3'
    old_manifest['files']['ai-dev-playbook/VERSION'] = playbook.updating.sha256(version_path.read_bytes())
    old_manifest['release_id'] = playbook.updating.fingerprint(old_manifest['files'])
    manifest_path.write_text(json.dumps(old_manifest))
    def files():
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and not p.is_relative_to(root / 'work')}
    before = files()
    preview = cli(root, 'update', 'plan', str(ROOT))
    assert '0.0.3 → 0.1.0' in preview.stdout
    assert before == files()
    protected = {rel: content for rel, content in before.items() if not rel.startswith('ai-dev-playbook/')}
    cli(root, 'update', 'plan', str(ROOT), '--save')
    cli(root, 'update', 'apply')
    assert all((root / rel).read_bytes() == content for rel, content in protected.items())
    assert json.loads(project_path.read_text())['custom_setting'] == {'audit': 51}
    assert json.loads(project_path.read_text())['playbook_version'] == '0.1.0'
    assert version_path.read_text().strip() == '0.1.0'
    print('PASS 0.0.3 fixture preview writes nothing; apply preserves project files/custom config')
(OUT / 'cli-results.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2) + '\n')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for entry in inputs:
        path = root / entry
        if Path(entry).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('audit fixture\n')
        else:
            path.mkdir(parents=True, exist_ok=True)
    for name in ['project.json', 'resources.json']:
        (root / 'ai-dev-playbook' / name).write_text('{}\n')
    card = root / 'docs/tasks/PROBE-51.md'
    card.parent.mkdir(parents=True)
    card.write_text('# PROBE-51\n\nID: PROBE-51\nStatus: in_progress\nPriority: P2\nDepends-On: -\nCreated: 2026-10-27T09:00:00+03:00\nClosed: -\n')
    target = root / 'CHANGELOG.md'
    original = target.read_bytes()
    settings = {'inputs': inputs, 'checks': [{'id': 'controlled-pass', 'argv': [sys.executable, '-c', "print('independent freshness probe')"], 'timeout_seconds': 10}]}
    assert playbook.verification.run(root, {'id': 'PROBE-51', 'path': card}, settings, 2, playbook.safe_path, playbook.require) == 0
    report = json.loads(next((root / 'work/verification/PROBE-51').glob('*/report.json')).read_text())
    assert report['status'] == 'passed' and report['inputs_unchanged']
    assert 'CHANGELOG.md' in report['snapshot_after']['files']
    assert playbook.verification.inputs_fresh(root, report, [], playbook.safe_path, playbook.require) == (True, [])
    target.write_text('changed after successful verification\n')
    assert playbook.verification.inputs_fresh(root, report, [], playbook.safe_path, playbook.require) == (False, ['CHANGELOG.md'])
    target.write_bytes(original)
    assert playbook.verification.inputs_fresh(root, report, [], playbook.safe_path, playbook.require) == (True, [])
    (OUT / 'freshness-fixture-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('PASS A-02: runner passed; unchanged fresh; edited CHANGELOG stale with sole diff; restored fresh')

line = next(line for line in changelog.splitlines() if line.startswith('- Исходные ошибки проверки PB-051'))
planned = changelog.replace(line + '\n', '')
assert all(token in planned for token in ['[Unreleased]', '0.1.0', '0.0.3', 'PB-041', 'PB-044'])
(OUT / 'changelog-after-close.txt').write_text(planned)
print('PASS planned post-close changelog edit removes only obsolete PB-051 release limitation')
print('All independent probes passed')
