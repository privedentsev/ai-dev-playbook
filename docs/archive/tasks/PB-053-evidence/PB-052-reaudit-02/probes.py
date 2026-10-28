"""Independent PB-052 audit probes; mutations are confined to temporary copies."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import document_links as links
import updating
import task_cards


def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


manifest = json.loads((ROOT / 'ai-dev-playbook/manifest.json').read_text())
for rel, expected in manifest['files'].items():
    assert digest(ROOT / rel) == expected, rel
assert updating.fingerprint(manifest['files']) == manifest['release_id']
print(f"PASS: self-manifest {len(manifest['files'])} file hashes and release_id", flush=True)

archive = ROOT / 'docs/archive/tasks'
meta = json.loads((archive / 'PB-052.meta.json').read_text())
report_path = archive / 'PB-052-evidence/work/verification/PB-052/c44cade656b64407b61f02a286ff2b26/report.json'
report = json.loads(report_path.read_text())
assert report['status'] == 'passed' and report['inputs_unchanged'] is True
assert digest(report_path) == meta['criterion_evidence']['CHECK-01']['report_sha256']
receipt = json.loads((archive / 'PB-052-evidence/close-lc-e5f9d931c8501e2a.json').read_text())
evidence_files = [item for item in receipt['files'] if '/PB-052-evidence/' in item['to']]
for item in evidence_files:
    assert digest(ROOT / item['to']) == item['sha256'], item['to']
parsed = task_cards.parse((archive / 'PB-052.md').read_text(), 'docs/archive/tasks/PB-052.md')
entries = {entry['id']: entry for entry in parsed['entries']}
assert entries['CHECK-01']['sha256'] == meta['criterion_evidence']['CHECK-01']['check_sha256']
assert entries['HANDOFF-01']['journal_sha256'] == meta['handoff']['journal_sha256']
assert entries['AUDIT-01']['sha256'] == meta['acceptance']['audit_sha256']
protected = {rel: value for rel, value in report['snapshot_before']['files'].items()
             if rel.startswith(('ai-dev-playbook/', 'tests/'))}
for rel, expected in protected.items():
    content = (ROOT / rel).read_bytes()
    if rel.endswith('.md') and task_cards.version(content.decode('utf-8')) == 2:
        content = task_cards.normalize_checkboxes(content.decode('utf-8')).encode('utf-8')
    assert hashlib.sha256(content).hexdigest() == expected, rel
print(f"PASS: report/CHECK/HANDOFF/AUDIT bindings, {len(evidence_files)} immutable evidence receipt hashes, {len(protected)} implementation/test snapshot files unchanged", flush=True)
print('NOTE: close receipt card/sidecar hashes describe pre-move bytes; final bindings checked separately.', flush=True)

with tempfile.TemporaryDirectory(prefix='pb052-links-') as tmp:
    root = Path(tmp).resolve()
    src = root / 'docs/specs/main.md'
    old = root / 'docs/tasks/T.md'
    new = root / 'docs/archive/tasks/T.md'
    tokens = ['../specs/other.md', './other.md', '../specs/%6Fther.md?x=1#p',
              '<../specs/other.md?x=1#p>', './dir/../other.md']
    for token in tokens:
        text = f'[plain]({token} "title")\n![image]({token})\n[ref]: {token} "title"\n[task](../tasks/T.md)\n'
        got = links.rewrite(text, src, src, {old: new})
        assert got['text'] == text.replace('../tasks/T.md', '../archive/tasks/T.md'), token
        assert len(got['changes']) == 1
    joint = links.rewrite('[x](./dir/../other.md?x=1#p)\n', src,
                          root / 'docs/archive/specs/main.md',
                          {root / 'docs/specs': root / 'docs/archive/specs'})
    assert joint['text'] == '[x](./dir/../other.md?x=1#p)\n' and not joint['changes']
    moved = links.rewrite('[x](./other.md?x=1#p)\n', src,
                          root / 'docs/archive/specs/main.md', {})
    assert moved['text'] == '[x](../../specs/other.md?x=1#p)\n'
print('PASS: 5 mixed inline/image/refdef preservation cases; joint move; source-only move', flush=True)

with tempfile.TemporaryDirectory(prefix='pb052-cli-adopt-') as tmp:
    project = Path(tmp)
    cli = [sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py')]
    result = subprocess.run([*cli, 'init', tmp, '--apply'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    (project / 'ai-dev-playbook/manifest.json').unlink()
    version_path = project / 'ai-dev-playbook/VERSION'
    version_path.write_text('9.8.7\n')
    config_path = project / 'ai-dev-playbook/project.json'
    config = json.loads(config_path.read_text())
    config['playbook_version'] = '9.8.7'
    config_path.write_text(json.dumps(config) + '\n')
    (project / 'custom.txt').write_bytes(b'preserve this\r\n')
    def tree():
        return {str(p.relative_to(project)): digest(p) for p in project.rglob('*')
                if p.is_file() and str(p.relative_to(project)) != 'work/gate.lock'}
    before = tree()
    result = subprocess.run([*cli, '--root', tmp, 'adopt', str(ROOT), '--apply'],
                            capture_output=True, text=True)
    assert result.returncode == 1 and 'VERSION' in result.stdout + result.stderr
    assert tree() == before, 'refused adopt changed files'
    result = subprocess.run([*cli, '--root', tmp, 'adopt', str(ROOT), '--apply',
                             '--resolve', 'ai-dev-playbook/VERSION=release'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert version_path.read_text().strip() == manifest['playbook_version']
    assert (project / 'custom.txt').read_bytes() == b'preserve this\r\n'
print('PASS: subprocess CLI adopt rejects conflict without file writes; explicit resolution restores current version and preserves custom bytes', flush=True)

with tempfile.TemporaryDirectory(prefix='pb052-next-version-') as tmp:
    clone = Path(tmp)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store')
    for folder in ('ai-dev-playbook', 'tests'):
        shutil.copytree(ROOT / folder, clone / folder, ignore=ignore)
    version = '0.1.1'
    (clone / 'ai-dev-playbook/VERSION').write_text(version + '\n')
    for rel in ('ai-dev-playbook/project.json', 'ai-dev-playbook/templates/project.json'):
        path = clone / rel
        data = json.loads(path.read_text())
        data['playbook_version'] = version
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    manifest_path = clone / 'ai-dev-playbook/manifest.json'
    data = json.loads(manifest_path.read_text())
    data['playbook_version'] = version
    data['files'] = {rel: digest(clone / rel) for rel in data['files']}
    data['release_id'] = updating.fingerprint(data['files'])
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tests = [
        'test_cli.AdoptTest.test_adopt_conflicting_version',
        'test_cli.AdoptTest.test_adopt_version_keep_forbidden',
        'test_pb028.Pb028CompatibilityAndUpdateTest.test_real_002_artifact_update_with_custom_paths_and_evidence',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A1_symlink_in_scope_and_inputs_rejected',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A2_null_plan_and_null_inputs_rejected',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A4_v2_card_rejected_without_task_workflow_opt_in',
    ]
    env = dict(os.environ, PYTHONPATH=str(clone / 'tests'))
    result = subprocess.run([sys.executable, '-m', 'unittest', '-v', *tests], cwd=clone,
                            env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=120)
    print(result.stdout, flush=True)
    assert result.returncode == 0, 'future-version scenario failed'
    print(f'PASS: hypothetical version {version}, six regression probes; historical 0.0.2 artifact unchanged', flush=True)

print('All independent probes passed.', flush=True)
