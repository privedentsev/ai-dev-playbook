"""Original handoff records and one-run final verification, on temporary projects."""
import json
import subprocess
import sys

from test_close_lifecycle import CloseLifecycleCase, TASK_META
from test_documents import tree_fingerprint
from test_task_cards import BODY, record, finding, cards
from test_playbook import playbook


class HandoffContextTest(CloseLifecycleCase):
    """Контекст handoff: записи, находки, unhanded work, view-флаги,
    archive и installed CLI."""
    def setup_card(self, body=None, external=False):
        self.managed_task(status='in_progress',
                          meta={**TASK_META, 'card_format_version': 2},
                          body=body if body is not None else BODY + record('EXEC'))
        self.path = self.tasks_dir() / 'DEV-1.md'
        if external:
            hydrated = self.path.read_text().replace(
                playbook.documents.META_START,
                playbook.documents.EXTERNAL_META + '\n' + playbook.documents.META_START, 1)
            for rel, content in playbook.documents.split_card_writes(
                    {str(self.path.relative_to(self.root)): hydrated.encode()}).items():
                (self.root / rel).write_bytes(content)
        self.assertEqual(self.cli('index')[0], 0)

    def append(self, value):
        self.path.write_text(self.path.read_text() + value)

    def show(self, *args):
        result = self.cli('card', 'show', 'DEV-1', '--handoff', *args)
        self.assertEqual(result[0], 0, result)
        return result[1]

    def test_linked_records_findings_and_unhanded_work_are_read_only(self):
        """Связанные записи, находки и unhanded work — read-only контекст"""
        body = (BODY + record('EXEC') + record('CHECK') + record('HANDOFF')
                + record('AUDIT', **{'Результат': 'changes_requested'}) + finding(1)
                + record('EXEC', 2, title='Исправление', **{'Замечания': 'A-01'})
                + record('CHECK', 2) + record('CHECK', 3)
                + record('HANDOFF', 2, **{'Выполнение': 'EXEC-01, EXEC-02',
                                         'Проверки': 'CHECK-01, CHECK-02'})
                + record('EXEC', 3, **{'Изменения': 'Новая работа после передачи.'}))
        self.setup_card(body)
        for external in (False, True):
            with self.subTest(external=external):
                if external:
                    hydrated = self.path.read_text().replace(
                        playbook.documents.META_START,
                        playbook.documents.EXTERNAL_META + '\n' + playbook.documents.META_START, 1)
                    for rel, content in playbook.documents.split_card_writes(
                            {str(self.path.relative_to(self.root)): hydrated.encode()}).items():
                        (self.root / rel).write_bytes(content)
                before = tree_fingerprint(self.root)
                view = json.loads(self.show('--json'))
                self.assertEqual([e['id'] for e in view['entries']],
                                 ['EXEC-01', 'CHECK-01', 'EXEC-02', 'CHECK-02', 'HANDOFF-02'])
                parsed = cards.parse(playbook.documents.read_text(self.path))
                for entry in view['entries']:
                    self.assertEqual(entry, cards.select_entry(parsed, entry['id']))
                self.assertEqual(view['context']['work_after_handoff'], ['EXEC-03'])
                self.assertEqual(view['context']['open_findings'][0]['id'], 'A-01')
                output = self.show()
                self.assertIn('Работа без актуальной передачи: EXEC-03', output)
                self.assertIn('Критерий исправления: повтор успешен.', output)
                self.assertNotIn('### CHECK-03', output)
                self.assertNotIn('### HANDOFF-01', output)
                self.assertNotIn('### EXEC-03', output)
                self.assertNotIn('playbook-meta:start', output)
                self.assertEqual(before, tree_fingerprint(self.root))

    def test_no_handoff_and_unsupported_fallback(self):
        """Без handoff — fallback, unsupported-флаги"""
        self.setup_card(external=True)
        before = tree_fingerprint(self.root)
        self.assertIn('Передача ещё не записана.', self.show())
        self.assertEqual(json.loads(self.show('--json'))['entries'], [])
        self.assertEqual(before, tree_fingerprint(self.root))
        sidecar = playbook.documents.meta_path(self.path)
        meta = json.loads(sidecar.read_text()); meta['card_format_version'] = 99
        sidecar.write_text(json.dumps(meta))
        before = tree_fingerprint(self.root)
        self.assertIn('docs/tasks/DEV-1.md', self.show())
        self.assertEqual(json.loads(self.show('--json'))['status'], 'unsupported')
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_view_flags_are_exclusive_and_old_json_is_unchanged(self):
        """View-флаги взаимоисключающие, старый JSON неизменен"""
        self.setup_card(BODY + record('EXEC') + record('CHECK') + record('HANDOFF'))
        for flag in ('--full', '--entry'):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as raised:
                self.cli('card', 'show', 'DEV-1', '--handoff', flag,
                         *(['EXEC-01'] if flag == '--entry' else []))
            self.assertEqual(raised.exception.code, 2)
        result = self.cli('card', 'show', 'DEV-1', '--json')
        original = json.loads(result[1])
        self.assertEqual(original['card_format_version'], 2)
        self.assertEqual(len(original['entries']), 3)
        self.assertIn('rendered_markdown', original)
        self.assertNotIn('context', original)
        compact = self.cli('card', 'show', 'DEV-1')[1]
        self.assertNotIn('\nПриёмка CLI:', compact)
        self.assertIn('\nПриёмка CLI:', self.cli('card', 'show', 'DEV-1', '--full')[1])

    def test_prepared_status_one_verify_and_stale_inputs(self):
        """Подготовленный статус, один verify и stale-входы"""
        self.setup_card(external=True)
        config = self.root / 'ai-dev-playbook/project.json'
        project = json.loads(config.read_text())
        counter = "from pathlib import Path; p=Path('work/check-count'); p.parent.mkdir(exist_ok=True); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        project['verification'] = {'inputs': ['docs', 'example.py', 'ai-dev-playbook/project.json'],
            'checks': [{'id': 'counted', 'argv': [sys.executable, '-c', counter], 'timeout_seconds': 30}]}
        config.write_text(json.dumps(project))
        (self.root / 'example.py').write_text('original implementation\n')
        self.path.write_text(self.path.read_text().replace('Status: in_progress', 'Status: in_review'))
        self.assertEqual(self.cli('index')[0], 0)
        self.assertIn('Передача не подтверждена', self.show())
        self.verify()
        self.append(record('CHECK') + record('HANDOFF', **{
            'Состояние': 'dev; следующий шаг — независимый аудит.',
            'Фокус аудита': 'Проверить повтор по сценарию CHECK-01.'}))
        self.assertEqual(self.handoff()[0], 0)
        view = json.loads(self.show('--json'))
        self.assertEqual(view['context']['handoff_state'], 'current')
        self.assertNotIn('Передача не подтверждена', self.show())
        self.assertEqual((self.root / 'work/check-count').read_text(), '1')
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
        (self.root / 'example.py').write_text('changed implementation\n')
        self.assertEqual(json.loads(self.show('--json'))['context']['handoff_state'], 'not_current')
        self.assertIn('Передача не подтверждена', self.show())

    def test_failed_verify_does_not_confirm_prepared_review(self):
        """Упавший verify не подтверждает prepared review"""
        self.setup_card(BODY + record('EXEC') + record('CHECK') + record('HANDOFF'))
        self.path.write_text(self.path.read_text().replace('Status: in_progress', 'Status: in_review'))
        config = self.root / 'ai-dev-playbook/project.json'
        project = json.loads(config.read_text())
        project['verification']['checks'][0]['argv'] = [sys.executable, '-c', 'raise SystemExit(3)']
        config.write_text(json.dumps(project))
        self.assertEqual(self.cli('index')[0], 0)
        self.assertNotEqual(self.cli('verify', 'DEV-1')[0], 0)
        self.assertEqual(self.handoff()[0], 1)
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.assertIn('Передача не подтверждена', self.show())
        self.assertEqual(json.loads(self.show('--json'))['context']['handoff_state'], 'not_current')

    def test_archive_and_installed_cli(self):
        """Контекст handoff в архиве и на установленном CLI"""
        self.setup_card(BODY + record('EXEC') + record('CHECK') + record('HANDOFF'))
        self.path.write_text(self.path.read_text().replace('Status: in_progress', 'Status: in_review'))
        self.assertEqual(self.cli('index')[0], 0)
        self.verify(); self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', actor='Auditor'))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)
        before = tree_fingerprint(self.root)
        result = subprocess.run([sys.executable, str(self.root / 'ai-dev-playbook/scripts/playbook.py'),
            '--root', str(self.root), 'card', 'show', 'DEV-1', '--handoff', '--json'],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(result.stdout)
        self.assertEqual(view['context']['handoff_state'], 'historical')
        self.assertEqual(view['context']['acceptance_state'], 'historical')
        self.assertEqual([e['id'] for e in view['entries']], ['EXEC-01', 'CHECK-01', 'HANDOFF-01'])
        self.assertEqual(before, tree_fingerprint(self.root))
