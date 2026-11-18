"""Final real staged check after CLI archival and changelog bookkeeping."""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'ai-dev-playbook/scripts/playbook.py').is_file())
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

card = ROOT / 'docs/archive/tasks/PB-051.md'
assert card.is_file() and card.with_suffix('.meta.json').is_file()
assert not (ROOT / 'docs/tasks/PB-051.md').exists()
assert not (ROOT / 'docs/tasks/PB-051-evidence').exists()
text = card.read_text()
assert 'Status: done' in text and '### AUDIT-02 — Аудит' in text
assert 'Закрытые замечания: A-04' in text
changelog = (ROOT / 'CHANGELOG.md').read_text()
assert changelog == (OUT / 'changelog-after-close.txt').read_text()
assert changelog.count('## [Unreleased]') == 1
assert 'PB-041' in changelog and 'PB-044' in changelog

hashes = json.loads((OUT / 'material-hashes-before.json').read_text())
for original, digest in hashes.items():
    if original.startswith('docs/tasks/PB-051-evidence/'):
        moved = original.replace('docs/tasks/PB-051-evidence/', 'docs/archive/tasks/PB-051-evidence/', 1)
    else:
        assert original.startswith('work/verification/PB-051/')
        moved = 'docs/archive/tasks/PB-051-evidence/' + original
    assert playbook.updating.sha256((ROOT / moved).read_bytes()) == digest, original
print(f'PASS: archived pair/status; original active paths absent; {len(hashes)} historical file hashes preserved')

meta = json.loads(card.with_suffix('.meta.json').read_text())
assert meta['acceptance']['result'] == 'accepted'
assert meta['acceptance']['audit_id'] == 'AUDIT-02'
assert meta['handoff']['entry_id'] == 'HANDOFF-02'
assert not any(item['id'] == 'finder-metadata' for item in meta['materials'])

def check(staged):
    output, error = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        code = playbook.main(['--root', str(staged), 'check', '--ready'])
    assert code == 0, output.getvalue() + error.getvalue()
    assert (staged / 'docs/archive/tasks/PB-051.md').is_file()
    print('PASS real staged check:', output.getvalue().strip())

with tempfile.TemporaryDirectory(prefix='pb051-post-close-staged-') as directory:
    plan = dict(writes={}, removes=[], create_dirs=[], remove_dirs=[], expected={}, staged_checks=[check])
    playbook.file_operations._prepare_staged(ROOT, Path(directory), plan, playbook.safe_path, playbook.require)
print('PASS: final archive, links, materials and changelog remain valid after real _prepare_staged')
