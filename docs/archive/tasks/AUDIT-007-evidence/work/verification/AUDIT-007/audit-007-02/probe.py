"""AUDIT-007 repeated independent subprocess probes after A1/A2/A3; statements are synthetic fixtures.

Reproduce: python3 <path-to-this-probe.py> --output /tmp/audit007-repeat
Only project scaffolding is reused from tests; assertions and CLI journeys below
are auditor authored. Every mutation targets a temporary project.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = next(p for p in Path(__file__).resolve().parents if (p / 'tests/test_close_lifecycle.py').is_file())
sys.path.insert(0, str(REPO / 'tests'))
from test_close_lifecycle import CloseLifecycleCase, managed_card


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Project:
    def __init__(self, output, name, kinds=('command', 'command')):
        self.output = output / name
        self.output.mkdir(parents=True)
        self.fixture = CloseLifecycleCase()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.card = self.root / 'docs/tasks/DEV-1.md'
        self.config = self.root / 'ai-dev-playbook/project.json'
        self.settings = json.loads(self.config.read_text())
        (self.root / 'src').mkdir()
        for ident in ('a', 'b'):
            (self.root / f'src/{ident}.txt').write_text('original ' + ident)
        registry, checks = [], []
        for ident, kind in zip(('a', 'b'), kinds):
            item = {'id': ident, 'kind': kind, 'purpose': 'Auditor probe ' + ident,
                    'default_inputs': [f'src/{ident}.txt'], 'applicability': 'always'}
            if kind == 'command':
                item.update(argv=[sys.executable, '-c',
                    "from pathlib import Path; p=Path('work/count-" + ident + "'); "
                    "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
                    timeout_seconds=10)
            else:
                item['scenario'] = 'Read the local text file and verify its wording in a terminal.'
            registry.append(item)
            checks.append({'id': ident, 'kind': kind, 'purpose': item['purpose'],
                           'registry_id': ident, 'inputs': [f'src/{ident}.txt']})
        self.settings['task_workflow'] = {'schema_version': 2, 'registry': registry}
        self.save_settings()
        self.meta = {'schema_version': 2,
                     'workflow': {'route': 'quick', 'reason': 'Local low risk fixture', 'history': []},
                     'verification_plan': {'revision': 1, 'rationale': 'Independent explicit files',
                                           'scope': ['src'], 'checks': checks, 'decisions': []},
                     'relations': [], 'materials': [], 'moves': [], 'handoff': None, 'acceptance': None}
        managed_card(self.root / 'docs/tasks', 'DEV-1', 'draft', meta=self.meta,
                     body='\n## Цель и границы\nПроверить изолированный пример.\n\n'
                          '## Приёмка\n- [ ] Актуальный результат принят.\n')
        self.steps = []
        self.cli('index', expect=0)
        self.cli('lifecycle', 'start', 'DEV-1', '--actor', 'Fixture executor',
                 '--model', 'Synthetic model', '--reason', 'Synthetic engineer instruction', expect=0)

    def save_settings(self):
        self.config.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2) + '\n')

    def cli(self, *args, expect=None):
        command = [sys.executable, str(self.root / 'ai-dev-playbook/scripts/playbook.py'),
                   '--root', str(self.root), *args]
        begin = time.monotonic()
        proc = subprocess.run(command, text=True, capture_output=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=90)
        n = len(self.steps)
        (self.output / f'{n:02}.stdout.log').write_text(proc.stdout)
        (self.output / f'{n:02}.stderr.log').write_text(proc.stderr)
        self.steps.append({'argv': list(args), 'returncode': proc.returncode,
                           'duration_seconds': round(time.monotonic() - begin, 3)})
        (self.output / 'commands.json').write_text(json.dumps(self.steps, indent=2) + '\n')
        if expect is not None:
            assert proc.returncode == expect, (args, proc.returncode, proc.stdout, proc.stderr)
        return proc

    def counts(self):
        return {k: int((self.root / f'work/count-{k}').read_text())
                for k in ('a', 'b') if (self.root / f'work/count-{k}').exists()}

    def latest(self):
        p = max((self.root / 'work/verification/DEV-1').glob('*/report.json'),
                key=lambda p: p.stat().st_mtime_ns)
        return json.loads(p.read_text())

    def handoff(self, expect=0):
        return self.cli('lifecycle', 'handoff', 'DEV-1', '--summary', 'Auditor synthetic fixture',
                        '--actor', 'Fixture executor', '--model', 'Synthetic model', expect=expect)

    def accept(self, role='engineer', expect=0):
        return self.cli('lifecycle', 'accept', 'DEV-1', '--result', 'accepted', '--role', role,
                        '--actor', 'Fixture engineer', '--model', 'Synthetic model',
                        '--note', 'Synthetic explicit final version acceptance', expect=expect)

    def manual(self, candidate, browser=False, expect=None):
        value = {'check_id': 'a', 'status': 'passed',
                 'observation': 'Synthetic terminal review: expected wording verified.',
                 'environment': {'terminal': 'Synthetic terminal fixture'},
                 'candidate_snapshot': candidate,
                 'engineer_confirmation': {'who': 'Fixture engineer',
                    'at': '2026-11-06T15:00:00+03:00',
                    'statement': 'Synthetic explicit confirmation of current text'}}
        if browser:
            value['environment']['browser'] = 'Synthetic browser, no real browser used'
        (self.root / 'work/manual.json').write_text(json.dumps(value))
        return self.cli('verify', 'DEV-1', '--record-manual', 'work/manual.json', expect=expect)

    def finish(self):
        # Preserve safe snapshots and all immutable reports; no live Markdown copy.
        for source, target in ((self.card, 'card.txt'), (self.config, 'project.json')):
            if source.exists():
                (self.output / target).write_bytes(source.read_bytes())
        import shutil
        for rel, dest in (('work/verification/DEV-1', 'reports'),
                          ('docs/archive/tasks/DEV-1-evidence', 'archived-evidence')):
            p = self.root / rel
            if p.exists():
                shutil.copytree(p, self.output / dest)
        self.fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    out = parser.parse_args().output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {}

    p = Project(out, 'independent-policy')
    try:
        p.cli('verify', 'DEV-1', expect=0)
        p.cli('verify', 'DEV-1', expect=0)
        assert p.counts() == {'a': 1, 'b': 1}
        (p.root / 'src/a.txt').write_text('changed a')
        p.cli('verify', 'DEV-1', expect=0)
        assert p.counts() == {'a': 2, 'b': 1}
        # A registry definition change leaves b's definition, inputs and
        # selected policy projection unchanged. b should still be reusable.
        p.settings['task_workflow']['registry'][0]['argv'][-1] += '; print("a version 2")'
        p.save_settings()
        p.cli('verify', 'DEV-1', expect=0)
        assert p.counts() == {'a': 3, 'b': 1}
        summary['independent_policy'] = {'before': {'a': 2, 'b': 1}, 'after': p.counts(),
                                         'expected': {'a': 3, 'b': 1}}
        p.handoff()
        p.handoff()
        p.cli('close', 'DEV-1', '--dry-run', expect=1)
        p.accept('auditor', expect=1)
        p.accept()
        before = {f.relative_to(p.root).as_posix(): digest(f)
                  for f in (p.root / 'work/verification/DEV-1').rglob('*') if f.is_file()}
        counts = p.counts()
        p.cli('close', 'DEV-1', expect=0)
        archived = p.root / 'docs/archive/tasks/DEV-1-evidence'
        for source, expected in before.items():
            matches = list(archived.rglob(source.split('DEV-1/', 1)[1]))
            assert len(matches) == 1 and digest(matches[0]) == expected, source
        assert p.counts() == counts
        summary['service_and_archive'] = {'unchanged_counts': counts,
                                          'immutable_files_verified': len(before)}
    finally:
        p.finish()

    p = Project(out, 'manual-terminal', ('manual',))
    try:
        p.settings['task_workflow']['registry'][0]['required_environment'] = ['terminal']
        p.save_settings()
        p.cli('verify', 'DEV-1', expect=1)
        candidate = p.latest()['results'][0]['candidate_snapshot']
        p.handoff(expect=1)
        no_browser = p.manual(candidate, expect=0)
        assert p.latest()['results'][0]['status'] == 'passed'
        summary['manual_terminal'] = {'actual_status': p.latest()['results'][0]['status'],
            'returncode': no_browser.returncode, 'expected': 'passed: terminal review with engineer confirmation'}
        # Compatibility control: extra browser information is accepted,
        # but terminal review has already passed without it.
        p.manual(candidate, browser=True, expect=0)
        for i in (2, 3):
            (p.root / 'src/a.txt').write_text('wording ' + str(i))
            p.manual(candidate, browser=True, expect=1)
            p.cli('verify', 'DEV-1', expect=1)
            candidate = p.latest()['results'][0]['candidate_snapshot']
            p.manual(candidate, browser=True, expect=0)
            assert 'Status: in_progress' in p.card.read_text()
        p.settings['task_workflow']['registry'][0]['scenario'] += ' New scenario revision.'
        p.save_settings()
        p.manual(candidate, browser=True, expect=1)
        p.cli('verify', 'DEV-1', expect=1)
        p.manual(candidate, browser=True, expect=1)
        candidate = p.latest()['results'][0]['candidate_snapshot']
        p.manual(candidate, browser=True, expect=0)
        p.handoff()
        p.accept()
        p.cli('close', 'DEV-1', '--dry-run', expect=0)
        assert p.counts() == {}
        summary['manual_stale_and_iterations'] = 'PASS; three versions, stale input/scenario rejected, zero commands'
    finally:
        p.finish()

    p = Project(out, 'failed-and-preflight', ('command',))
    try:
        # Corrupt index must reject before the command and preserve counters.
        original_index = (p.root / 'docs/tasks/index.md').read_bytes()
        (p.root / 'docs/tasks/index.md').write_text('stale index\n')
        p.cli('verify', 'DEV-1', '--preflight', expect=1)
        assert p.counts() == {}
        (p.root / 'docs/tasks/index.md').write_bytes(original_index)
        p.cli('index', expect=0)
        p.settings['task_workflow']['registry'][0]['argv'][-1] += '; raise SystemExit(7)'
        p.save_settings()
        p.cli('verify', 'DEV-1', expect=1)
        p.handoff(expect=1)
        p.cli('close', 'DEV-1', '--dry-run', expect=1)
        assert p.latest()['results'][0]['status'] == 'failed'
        p.settings['task_workflow']['registry'][0]['argv'] = [sys.executable, '-c', 'import time; time.sleep(2)']
        p.settings['task_workflow']['registry'][0]['timeout_seconds'] = 0.1
        p.save_settings()
        p.cli('verify', 'DEV-1', expect=1)
        assert p.latest()['results'][0]['status'] == 'timeout'
        p.handoff(expect=1)
        summary['failed_timeout_preflight'] = 'PASS; stale index zero executions, failed/timeout block handoff/close'
    finally:
        p.finish()

    p = Project(out, 'environment-reuse', ('command',))
    original_mode = os.environ.get('AUDIT007_MODE')
    try:
        p.settings['task_workflow']['registry'][0]['argv'][-1] += (
            '; import os; raise SystemExit(0 if os.environ.get("AUDIT007_MODE") == "pass" else 9)')
        p.save_settings()
        os.environ['AUDIT007_MODE'] = 'pass'
        p.cli('verify', 'DEV-1', expect=0)
        p.handoff()
        p.accept()
        os.environ['AUDIT007_MODE'] = 'fail'
        p.handoff(expect=1)
        p.accept(expect=1)
        p.cli('close', 'DEV-1', '--dry-run', expect=1)
        p.cli('verify', 'DEV-1', expect=1)
        assert p.latest()['results'][0]['exit_code'] == 9
        assert p.counts() == {'a': 2}
        summary['environment_reuse'] = {'environment_change': 'rejected handoff/accept/close; fresh command failed exit9', 'counts': p.counts()}
    finally:
        if original_mode is None:
            os.environ.pop('AUDIT007_MODE', None)
        else:
            os.environ['AUDIT007_MODE'] = original_mode
        p.finish()

    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
