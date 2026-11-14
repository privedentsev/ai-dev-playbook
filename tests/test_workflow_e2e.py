"""PB-037: composed CLI journeys; all engineer statements are synthetic fixtures."""
import hashlib
import json
import sys

from test_close_lifecycle import CloseLifecycleCase
import test_task_workflow_lifecycle as lifecycle_fixture
from test_playbook import playbook


class WorkflowE2ETest(CloseLifecycleCase):
    """Сквозные маршруты workflow: итерации, независимые счётчики,
    маршруты, checkpoint."""
    save_project = lifecycle_fixture.TaskWorkflowLifecycleTest.save_project
    create_v2_task = lifecycle_fixture.TaskWorkflowLifecycleTest.create_v2_task
    card = lifecycle_fixture.TaskWorkflowLifecycleTest.card
    meta = lifecycle_fixture.TaskWorkflowLifecycleTest.meta
    trans = lifecycle_fixture.TaskWorkflowLifecycleTest.trans

    def setUp(self):
        super().setUp()
        self.project_file = self.root / 'ai-dev-playbook/project.json'
        self.settings = json.loads(self.project_file.read_text())
        self.settings['task_workflow'] = {'schema_version': 2, 'registry': []}
        (self.root / 'src').mkdir()
        (self.root / 'src/a.txt').write_text('a')
        (self.root / 'src/b.txt').write_text('b')

    def setup_task(self, registry, checks, route='quick'):
        self.settings['task_workflow']['registry'] = registry
        self.save_project()
        self.create_v2_task(status='draft', scope=['src'], checks=checks)
        meta = self.meta()
        meta['workflow']['route'] = route
        self.card.write_text(playbook.lifecycle._splice_meta(self.card.read_text(), meta)
            + '\n## Цель и границы\nИзолированный пилот.\n\n## Приёмка\n- [ ] Проверяемый результат.\n')
        self.assertEqual(self.cli('index')[0], 0)
        self.trans('start')

    def command(self, ident, inputs):
        return {'id': ident, 'kind': 'command', 'purpose': 'Count actual executions',
            'argv': [sys.executable, '-c',
                "from pathlib import Path; p=Path('work/count-" + ident + "'); "
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'timeout_seconds': 30, 'default_inputs': inputs, 'applicability': 'always'}

    def check(self, entry, inputs):
        return {'id': entry['id'], 'kind': entry['kind'], 'purpose': entry['purpose'],
                'registry_id': entry['id'], 'inputs': inputs,
                'applicability': entry.get('applicability', 'always')}

    def accept_as(self, role):
        result = self.accept('--role', role, '--result', 'accepted',
            '--note', 'Synthetic fixture: explicitly accepts the latest candidate')
        self.assertEqual(result[0], 0, result)

    def latest(self):
        return max((self.root / 'work/verification/DEV-1').glob('*/report.json'),
                   key=lambda p: p.stat().st_mtime_ns)

    def immutable_bytes(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.root / 'work/verification/DEV-1').rglob('*') if p.is_file()}

    def assert_archived_bytes(self, saved):
        meta = playbook.task_cards.metadata(
            (self.root / 'docs/archive/tasks/DEV-1.md').read_text())
        self.assertTrue(meta['moves'])
        for source, digest in saved.items():
            # Archive nests the original work path under the task evidence directory.
            matches = list((self.root / 'docs/archive/tasks/DEV-1-evidence').rglob(source.split('DEV-1/', 1)[1]))
            self.assertEqual(len(matches), 1, source)
            self.assertEqual(hashlib.sha256(matches[0].read_bytes()).hexdigest(), digest, source)

    def test_three_manual_iterations_stale_candidate_then_archive(self):
        """Три manual-итерации, stale-кандидат и архив"""
        visual = {'id': 'visual', 'kind': 'manual', 'purpose': 'Inspect local spacing',
            'scenario': 'Inspect displayed spacing, focus, readability at narrow and wide sizes.',
            'default_inputs': ['src'], 'applicability': 'always'}
        self.setup_task([visual], [self.check(visual, ['src', {'type': 'task_projection'}])])
        previous = None
        for iteration in range(1, 4):
            (self.root / 'src/a.txt').write_text('spacing ' + str(iteration))
            result = self.cli('verify', 'DEV-1')
            self.assertEqual(result[0], 1, result)  # No observation: not_run, not passed.
            self.assertEqual(self.handoff()[0], 1)
            report = json.loads(self.latest().read_text())
            candidate = report['results'][0]['candidate_snapshot']
            if previous:
                (self.root / 'work/manual.json').write_text(json.dumps(previous))
                self.assertEqual(self.cli('verify', 'DEV-1', '--record-manual', 'work/manual.json')[0], 1)
            statement = {'check_id': 'visual', 'status': 'passed',
                'observation': f'Synthetic observation, iteration {iteration}; no actual browser used.',
                'environment': {'browser': 'Synthetic fixture, not a real browser'},
                'candidate_snapshot': candidate,
                'engineer_confirmation': {'who': 'Fixture engineer',
                    'at': '2026-11-06T09:00:00+03:00', 'statement': 'Synthetic displayed candidate confirmed'}}
            (self.root / 'work/manual.json').write_text(json.dumps(statement))
            result = self.cli('verify', 'DEV-1', '--record-manual', 'work/manual.json')
            self.assertEqual(result[0], 0, result)
            previous = statement
            self.assertIn('Status: in_progress', self.card.read_text())
            self.assertFalse((self.root / 'docs/archive/tasks/DEV-1.md').exists())
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.accept_as('engineer')
        saved = self.immutable_bytes()
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        self.assert_archived_bytes(saved)
        self.assertFalse(list((self.root / 'work').glob('count-*')))

    def test_independent_counters_add_remove_and_service_cycle(self):
        """Независимые счётчики: add/remove и сервисный цикл"""
        a, b = self.command('a', ['src/a.txt']), self.command('b', ['src/b.txt'])
        self.setup_task([a, b], [self.check(a, ['src/a.txt']), self.check(b, ['src/b.txt'])])
        self.verify()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.handoff()[0], 0)
        self.accept_as('engineer')
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual([(self.root / ('work/count-' + k)).read_text() for k in ('a', 'b')], ['1', '1'])
        self.trans('revise')
        (self.root / 'src/a.txt').write_text('changed logic')
        self.verify()
        self.assertEqual([(self.root / ('work/count-' + k)).read_text() for k in ('a', 'b')], ['2', '1'])
        (self.root / 'src/a.txt').unlink()
        self.verify()
        self.assertEqual((self.root / 'work/count-a').read_text(), '3')
        (self.root / 'src/a.txt').write_text('readded')
        self.verify()
        self.assertEqual((self.root / 'work/count-a').read_text(), '4')
        self.assertEqual((self.root / 'work/count-b').read_text(), '1')
        self.assertEqual(self.handoff()[0], 0)
        self.accept_as('engineer')
        saved = self.immutable_bytes()
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)
        self.assert_archived_bytes(saved)
        self.assertEqual((self.root / 'work/count-a').read_text(), '4')

    def test_nonquick_routes_with_auditor_and_archive(self):
        """Не-quick маршруты с аудитором и архивом"""
        entry = self.command('a', ['src'])
        self.setup_task([entry], [self.check(entry, ['src'])], route='small')
        for route in ('small', 'medium', 'large'):
            if route != 'small':
                self.trans('revise')
                self.trans('route', '--to', route)
            self.verify()
            self.assertEqual(self.handoff()[0], 0)
            self.accept_as('auditor')
            self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual(self.meta()['workflow']['route'], 'large')
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)

    def test_checkpoint_runs_regression_separately_from_local_check(self):
        """Чекпоинт запускает regression отдельно от local check"""
        local = self.command('local', ['src/a.txt'])
        regression = self.command('regression', ['src'])
        regression['applicability'] = {'kind': 'checkpoint', 'checkpoint': 'release'}
        self.settings['task_workflow']['checkpoints'] = {'release': ['regression']}
        self.setup_task([local, regression], [self.check(local, ['src/a.txt']), self.check(regression, ['src'])])
        self.verify()
        self.assertEqual((self.root / 'work/count-local').read_text(), '1')
        self.assertFalse((self.root / 'work/count-regression').exists())
        result = self.cli('verify', 'DEV-1', '--checkpoint', 'release')
        self.assertEqual(result[0], 0, result)
        self.assertIn('REUSE: local', result[1])
        self.assertIn('RUN: regression', result[1])
        self.assertEqual((self.root / 'work/count-local').read_text(), '1')
        self.assertEqual((self.root / 'work/count-regression').read_text(), '1')
        report = json.loads(self.latest().read_text())
        self.assertTrue(all(item['status'] == 'passed' for item in report['results']))
