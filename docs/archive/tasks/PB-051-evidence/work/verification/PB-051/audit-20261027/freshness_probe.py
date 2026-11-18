import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

OUT = Path(__file__).parent
configured = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())['verification']
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for entry in configured['inputs']:
        path = root / entry
        if Path(entry).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('audit fixture\n')
        else:
            path.mkdir(parents=True, exist_ok=True)
    for name in ('project.json', 'resources.json'):
        (root / 'ai-dev-playbook' / name).write_text('{}\n')
    card = root / 'docs/tasks/PROBE-51.md'
    card.parent.mkdir(parents=True)
    card.write_text('# PROBE-51\n\nID: PROBE-51\nStatus: in_progress\nPriority: P2\nDepends-On: -\nCreated: 2026-10-27T09:00:00+03:00\nClosed: -\n')
    changelog = root / 'CHANGELOG.md'
    changelog.write_text('Original verified changelog\n')
    settings = {'inputs': configured['inputs'], 'checks': [{'id': 'controlled-pass', 'argv': [sys.executable, '-c', "print('controlled audit probe')"], 'timeout_seconds': 10}]}
    result = playbook.verification.run(root, {'id': 'PROBE-51', 'path': card}, settings, 2, playbook.safe_path, playbook.require)
    assert result == 0
    report_path = next((root / 'work/verification/PROBE-51').glob('*/report.json'))
    report = json.loads(report_path.read_text())
    assert report['status'] == 'passed' and report['inputs_unchanged']
    changelog.write_text('Changed AFTER successful verification\n')
    fresh, differences = playbook.verification.inputs_fresh(root, report, [], playbook.safe_path, playbook.require)
    assert fresh and not differences
    assert 'CHANGELOG.md' not in report['snapshot_after']['files']
    shutil.copyfile(report_path, OUT / 'freshness-fixture-report.json')
    print(json.dumps({'finding': 'A-02', 'report_status': report['status'], 'inputs_unchanged': report['inputs_unchanged'], 'changelog_changed_after_report': True, 'inputs_fresh': fresh, 'differences': differences, 'limitation': 'Controlled passing check on a temporary project; exact repository input list; not PB-051 final verify'}, ensure_ascii=False))
