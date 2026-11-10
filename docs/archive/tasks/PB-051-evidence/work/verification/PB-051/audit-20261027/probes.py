import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

OUT = Path(__file__).parent
INPUTS = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())['verification']['inputs']
START = playbook.verification.snapshot(ROOT, INPUTS + ['CHANGELOG.md'], playbook.safe_path, playbook.require)
(OUT / 'snapshot-before.json').write_text(json.dumps(START, indent=2))

def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()

def cli(root, *args):
    command = [sys.executable, str(root / 'ai-dev-playbook/scripts/playbook.py'), '--root', str(root), *args]
    result = subprocess.run(command, capture_output=True, text=True)
    print(json.dumps({'argv': command, 'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}, ensure_ascii=False))
    assert result.returncode == 0
    return result

manifest = json.loads((ROOT / 'ai-dev-playbook/manifest.json').read_text())
version = (ROOT / 'ai-dev-playbook/VERSION').read_text().strip()
assert version == '0.1.0' == manifest['playbook_version']
assert all(digest(ROOT / name) == value for name, value in manifest['files'].items())
artifact = playbook.updating.load_artifact(ROOT, playbook.safe_path, playbook.require)
assert artifact['release_id'] == manifest['release_id']
assert set(artifact['updates_from']) == {'0.0.2', '0.0.3'}
print('PASS: VERSION/project/manifest 0.1.0; all managed hashes; release_id; declared transitions')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    result = subprocess.run([sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'), 'init', str(root), '--apply'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for file in ('VERSION', 'project.json', 'manifest.json'):
        path = root / 'ai-dev-playbook' / file
        actual = path.read_text().strip() if file == 'VERSION' else json.loads(path.read_text())['playbook_version']
        assert actual == '0.1.0'
    print('PASS: fresh CLI init installs coherent 0.1.0')

    # Isolate the declared 0.0.3 transition; this is a version-adjusted fixture,
    # not a claim to execute an authentic historical 0.0.3 CLI.
    config_path = root / 'ai-dev-playbook/project.json'
    config = json.loads(config_path.read_text())
    config['playbook_version'] = '0.0.3'
    config['custom_audit_setting'] = {'keep': 51}
    config_path.write_text(json.dumps(config))
    version_path = root / 'ai-dev-playbook/VERSION'
    version_path.write_text('0.0.3\n')
    manifest_path = root / 'ai-dev-playbook/manifest.json'
    old_manifest = json.loads(manifest_path.read_text())
    old_manifest['playbook_version'] = '0.0.3'
    old_manifest['files']['ai-dev-playbook/VERSION'] = digest(version_path)
    old_manifest['release_id'] = playbook.updating.fingerprint(old_manifest['files'])
    manifest_path.write_text(json.dumps(old_manifest))
    protected = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_relative_to(root / 'ai-dev-playbook') and '__pycache__' not in p.parts}
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    preview = cli(root, 'update', 'plan', str(ROOT))
    assert '0.0.3 → 0.1.0' in preview.stdout
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    assert before == after
    cli(root, 'update', 'plan', str(ROOT), '--save')
    cli(root, 'update', 'apply')
    assert json.loads(config_path.read_text())['custom_audit_setting'] == {'keep': 51}
    assert json.loads(config_path.read_text())['playbook_version'] == '0.1.0'
    assert version_path.read_text().strip() == '0.1.0'
    assert all((root / rel).read_bytes() == content for rel, content in protected.items())
    print('PASS: 0.0.3 transition preview read-only; apply; custom config and all project files preserved')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for entry in INPUTS:
        path = root / entry
        if Path(entry).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('audit fixture\n')
        else:
            path.mkdir(parents=True, exist_ok=True)
    changelog = root / 'CHANGELOG.md'
    changelog.write_text('Before audit\n')
    before = playbook.verification.snapshot(root, INPUTS, playbook.safe_path, playbook.require)
    changelog.write_text('Changed after verification\n')
    after = playbook.verification.snapshot(root, INPUTS, playbook.safe_path, playbook.require)
    assert before == after and 'CHANGELOG.md' not in before['files']
    print('FINDING A-02: real verification.snapshot with current configured inputs ignores CHANGELOG.md changes')

print('Probe run complete')
