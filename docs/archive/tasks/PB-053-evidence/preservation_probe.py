"""PB-053: byte preservation, historical card and actual staged availability."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import file_operations

EVIDENCE = Path('docs/tasks/PB-053-evidence')
OLD = '../../../work/audits/PB-052-reaudit-02/report.md'
NEW = '../../tasks/PB-053-evidence/PB-052-reaudit-02/report.md'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def verify(root):
    evidence = root / EVIDENCE
    snapshot = json.loads((evidence / 'preservation.json').read_text())
    for name, expected in snapshot['files'].items():
        require(hashlib.sha256((evidence / 'PB-052-reaudit-02' / name).read_bytes()).hexdigest() == expected, name)
    card = root / 'docs/archive/tasks/PB-052.md'
    require(card.read_text() == snapshot['pb052_card_before'].replace(OLD, NEW), 'PB-052 changed beyond link')
    require(hashlib.sha256(card.with_suffix('.meta.json').read_bytes()).hexdigest() == snapshot['pb052_meta_sha256'], 'PB-052 metadata changed')
    report = card.parent / NEW
    require(report.is_file(), 'report unavailable')
    for name in ('tests.log', 'probes.log', 'probes.py', 'source-hashes.json', 'structure.log'):
        require((report.parent / name).is_file(), name)
    print(f'PASS: {root}: seven SHA-256 values, exact card link-only diff, unchanged metadata, report links')


verify(ROOT)
for name, expected in json.loads((ROOT / EVIDENCE / 'preservation.json').read_text())['files'].items():
    require(hashlib.sha256((ROOT / 'work/audits/PB-052-reaudit-02' / name).read_bytes()).hexdigest() == expected, 'original ' + name)
with tempfile.TemporaryDirectory(prefix='pb053-staged-') as tmp:
    op = Path(tmp)
    plan = dict(writes={}, removes=[], create_dirs=[], remove_dirs=[], expected={}, staged_checks=[verify])
    file_operations._prepare_staged(ROOT, op, plan, lambda root, rel: root / rel, require)
print('PASS: original evidence unchanged; real _prepare_staged executed')
