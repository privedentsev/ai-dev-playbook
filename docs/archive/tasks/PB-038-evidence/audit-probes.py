"""Independent PB-038 probes; run from repository root."""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_close_lifecycle import CloseLifecycleCase
from test_lifecycle import task_request


def case():
    c = CloseLifecycleCase()
    c.setUp()
    config = c.root / 'ai-dev-playbook/project.json'
    data = json.loads(config.read_text())
    for kind, folder in [('discussion', 'discussions'), ('design', 'designs'), ('adr', 'adr')]:
        data['documents']['collections'][kind] = {'active': f'docs/{folder}', 'archive': f'docs/archive/{folder}', 'index': f'docs/{folder}/index.md'}
        (c.root / f'docs/{folder}').mkdir(exist_ok=True)
    config.write_text(json.dumps(data))
    return c

c = case()
try:
    c.managed_task()
    c.add_plan('active', state='draft', tasks=['DEV-1'])
    assert c.cli('index', '--all')[0] == 0
    indices = sorted((c.root / 'docs').glob('*/index.md'))
    indices = [p for p in indices if p.parent.name != 'tasks']
    for p in indices:
        p.write_text('Manual [card](../tasks/DEV-1.md#goal)\n\n' + p.read_text())
    c.verify()
    assert c.handoff()[0] == 0
    assert c.accept('--result', 'accepted')[0] == 0
    reports = {p.relative_to(c.root): p.read_bytes() for p in (c.root / 'work/verification').rglob('*') if p.is_file()}
    code, out, err = c.cli('close', 'DEV-1')
    assert code == 0, out + err
    for p in indices:
        assert 'Manual [card](../archive/tasks/DEV-1.md#goal)' in p.read_text(), p
    assert c.cli('index', '--all')[0] == 0
    assert c.cli('check', '--ready')[0] == 0
    assert c.apply_request(task_request('DEV-1', action='reopen'))[0] == 0
    assert c.cli('index', '--all')[0] == 0
    assert c.cli('check', '--ready')[0] == 0
    for p in indices:
        assert 'Manual [card](../tasks/DEV-1.md#goal)' in p.read_text(), p
    # Original immutable report bytes survive relocation.
    all_bytes = [p.read_bytes() for p in c.root.rglob('*') if p.is_file()]
    assert all(data in all_bytes for data in reports.values())
    print('PASS: all collection indices, anchors, close/index/check/reopen/index/check, immutable reports')
finally:
    c.tearDown()

c = case()
try:
    c.managed_task()
    assert c.cli('index', '--all')[0] == 0
    p = c.root / 'docs/specs/index.md'
    p.write_text('# Manual ADR index\n\nNo task links.\n')
    assert c.cli('check', '--ready')[0] == 1
    c.verify()
    assert c.handoff()[0] == 0
    assert c.accept('--result', 'accepted')[0] == 0
    before = c.project_fingerprint()
    result = c.cli('close', 'DEV-1')
    assert result[0] == 1, result
    print('PASS: malformed unrelated index rejected without partial writes')
    assert c.project_fingerprint() == before
finally:
    c.tearDown()
