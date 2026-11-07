"""PB-056: preparation failures must not spend a configured test run."""
import json
import os
import sys
from unittest.mock import patch

import test_close_lifecycle as fixture
from test_documents import tree_fingerprint


class VerificationReadinessTest(fixture.CloseLifecycleCase):
    def setUp(self):
        super().setUp()
        self.managed_task()
        self.project = self.root / 'ai-dev-playbook/project.json'
        self.settings = json.loads(self.project.read_text())
        self.settings['verification']['checks'] = [{
            'id': 'count', 'argv': [sys.executable, '-c',
                "from pathlib import Path; p=Path('work/count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'timeout_seconds': 30}]
        self.save_settings()

    def save_settings(self):
        self.project.write_text(json.dumps(self.settings))

    def assert_early_failure(self, message):
        for mode in ('--preflight', '--audit-ready', None):
            before = tree_fingerprint(self.root)
            result = self.cli('verify', 'DEV-1', *([mode] if mode else []))
            self.assertEqual(result[0], 1, result)
            self.assertIn(message, result[2])
            if mode:
                self.assertEqual(before, tree_fingerprint(self.root))
            self.assertFalse((self.root / 'work/count').exists())
            self.assertFalse(list((self.root / 'work/verification').glob('*/*/report.json')))

    def test_missing_command_blocks_before_any_command(self):
        self.settings['verification']['checks'].append({
            'id': 'missing', 'argv': ['pb056-nonexistent-executable'], 'timeout_seconds': 30})
        self.save_settings()
        self.assert_early_failure('исполняемый файл недоступен')

    def test_stale_task_index_blocks_before_tests(self):
        index = self.root / 'docs/tasks/index.md'
        index.write_text(index.read_text().replace('in_review', 'in_progress'))
        self.assert_early_failure('индекс задач')

    def test_missing_component_blocks_before_tests(self):
        self.settings['components'][0]['tests'] = ['absent-tests']
        self.save_settings()
        self.assert_early_failure('Нет каталога компонента')

    def test_version_mismatch_blocks_before_tests(self):
        self.settings['playbook_version'] = '99.0.0'
        self.save_settings()
        self.assert_early_failure('Версия внедрения')

    def test_temporary_directory_inside_project_rejected_before_tests(self):
        with patch.object(fixture.playbook.verification.tempfile, 'gettempdir', return_value=str(self.root)):
            self.assert_early_failure('TMPDIR')

    def test_pending_markers_fail_read_only_even_without_lock(self):
        for rel in ('work/lifecycle/pending.json', 'work/update/pending.json'):
            with self.subTest(rel=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('broken interrupted state')
                self.assert_early_failure('Незавершён')
                path.unlink()

    def test_live_gate_is_probed_without_changing_holder(self):
        with patch.dict(os.environ):
            os.environ.pop(fixture.fops.GATE_ENV, None)
            with fixture.fops.acquire_gate(self.root, fixture.playbook.safe_path,
                    fixture.playbook.require, 'other operation'):
                self.assert_early_failure('блокировку записи')

    def history(self, status='passed', duration=40, argv=None):
        path = self.root / ('work/verification/DEV-1/' + 'a' * 32 + '/report.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'task_id': 'DEV-1',
            'cwd': str(self.root), 'status': status, 'inputs_unchanged': True,
            'checks': [{'status': status, 'duration_seconds': duration,
                        'argv': argv or self.settings['verification']['checks'][0]['argv']}]}))
        return path

    def test_measured_duration_rejects_insufficient_timeout_without_writing(self):
        self.history()
        before = tree_fingerprint(self.root)
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 1, result)
        self.assertIn('прежний успешный прогон 40s', result[2])
        self.assertEqual(before, tree_fingerprint(self.root))
        result = self.cli('verify', 'DEV-1')
        self.assertEqual(result[0], 1, result)
        self.assertFalse((self.root / 'work/count').exists())
        self.settings['verification']['checks'][0]['timeout_seconds'] = 60
        self.save_settings()
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)

    def test_failed_corrupt_foreign_and_other_commands_do_not_block_retry(self):
        for status, argv in (('failed', None), ('passed', ['other-command'])):
            self.history(status=status, argv=argv)
            self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)
        path = self.history()
        value = json.loads(path.read_text())
        value['cwd'] = '/another/project'
        path.write_text(json.dumps(value))
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)
        path.write_text('{broken')
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)

    def test_audit_ready_requires_handoff_and_detects_stale_inputs_without_tests(self):
        result = self.cli('verify', 'DEV-1', '--audit-ready')
        self.assertEqual(result[0], 1, result)
        self.assertIn('нет записи передачи', result[2])
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        before = tree_fingerprint(self.root)
        result = self.cli('verify', 'DEV-1', '--audit-ready')
        self.assertEqual(result[0], 0, result)
        self.assertIn('audit-ready:', result[1])
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual((self.root / 'work/count').read_text(), '1')
        path = self.root / 'docs/product.md'
        path.write_text(path.read_text() + '\nChanged requirement.\n')
        before = tree_fingerprint(self.root)
        result = self.cli('verify', 'DEV-1', '--audit-ready')
        self.assertEqual(result[0], 1, result)
        self.assertIn('нет свежего успешного прогона', result[2])
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_legacy_without_documents_also_checks_index(self):
        self.settings.pop('documents')
        self.save_settings()
        card = self.root / 'docs/tasks/DEV-1.md'
        card.write_text(card.read_text().split('<!-- playbook-meta:start -->')[0])
        index = self.root / 'docs/tasks/index.md'
        index.write_text('stale')
        self.assert_early_failure('Индекс устарел')
