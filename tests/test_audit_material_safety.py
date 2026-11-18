"""PB-054: early material failures, recoverable audit packaging and complete-run reuse."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_close_lifecycle import CloseLifecycleCase, fops, playbook
from test_documents import tree_fingerprint
from test_cli import REPO, REPO_VERSION
from test_task_cards import BODY, record


class AuditMaterialSafetyTest(CloseLifecycleCase):
    """Безопасность материалов аудита: ранние отказы коллизий архива,
    read-only preflight/preview, сохранение raw snapshot, повторные операции."""
    def setUp(self):
        super().setUp()
        self.managed_task()

    def prepare(self, *flags, ident='audit-01'):
        return self.cli('lifecycle', 'prepare-audit', 'DEV-1', '--id', ident, *flags)

    def counter(self):
        project = self.root / 'ai-dev-playbook/project.json'
        data = json.loads(project.read_text())
        data['verification']['checks'] = [{'id': 'count', 'argv': [sys.executable, '-c',
            "from pathlib import Path; p=Path('work/count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'timeout_seconds': 30}]
        project.write_text(json.dumps(data))

    def count(self):
        path = self.root / 'work/count'
        return int(path.read_text()) if path.exists() else 0

    def test_unchanged_root_target_is_available_without_physical_staging(self):
        """Неизменённая цель в корне проекта доступна без физического staging каталога"""
        self.counter()
        audit = self.root / 'work/audits/report.txt'
        audit.parent.mkdir(parents=True)
        audit.write_text('proof')
        card = self.tasks_dir() / 'DEV-1.md'
        card.write_text(card.read_text() + '\n[proof](../../work/audits/report.txt)\n')
        before = tree_fingerprint(self.root)
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 0)
        self.assertEqual(before, tree_fingerprint(self.root))
        result = self.cli('verify', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 1)
        self.assertEqual(audit.read_text(), 'proof')


    def test_registration_preview_and_apply_both_reject_finder_file(self):
        """Регистрация материала отклоняет finder-файл и в preview, и в apply"""
        path = self.tasks_dir() / 'DEV-1-evidence'
        path.mkdir()
        (path / '.DS_Store').write_bytes(b'finder')
        for flags in ([], ['--apply']):
            result = self.cli('lifecycle', 'register-material', 'DEV-1',
                'docs/tasks/DEV-1-evidence/.DS_Store', '--id', 'finder', '--role', 'report',
                '--immutable', '--required', *flags)
            self.assertEqual(result[0], 1, result)
            self.assertIn('исключён из staged', result[2])
        self.assertEqual((path / '.DS_Store').read_bytes(), b'finder')

    def test_preflight_writes_nothing_and_executes_nothing(self):
        """Preflight не пишет файлы и не запускает команд при неизменных входах"""
        self.counter()
        before = tree_fingerprint(self.root)
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(self.count(), 0)

    def test_audit_preview_apply_repeat_and_archive_preserve_raw_snapshot(self):
        """Audit preview/apply/повтор и архивация сохраняют raw snapshot побайтно"""
        card = self.tasks_dir() / 'DEV-1.md'
        original = card.read_bytes()
        before = tree_fingerprint(self.root)
        result = self.prepare()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(before, tree_fingerprint(self.root))
        result = self.prepare('--apply')
        self.assertEqual(result[0], 0, result)
        folder = self.root / 'work/verification/DEV-1/audit-01'
        self.assertEqual((folder / 'snapshot.txt').read_bytes(), original)
        provenance = json.loads((folder / 'provenance.json').read_text())
        self.assertEqual(provenance['files']['snapshot.txt']['sha256'],
                         'sha256:' + hashlib.sha256(original).hexdigest())
        saved = tree_fingerprint(self.root)
        self.assertEqual(self.prepare('--apply')[0], 0)
        self.assertEqual(saved, tree_fingerprint(self.root))
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        archived = self.archive_dir() / 'DEV-1-evidence/work/verification/DEV-1/audit-01/snapshot.txt'
        self.assertEqual(archived.read_bytes(), original)
        self.assertEqual(self.prepare('--apply')[0], 1)

    def test_prepare_refuses_changed_or_foreign_snapshot_and_uses_shared_gate(self):
        """Prepare audit отклоняет изменённый или чужой snapshot и использует общий gate"""
        self.assertEqual(self.prepare('--apply')[0], 0)
        folder = self.root / 'work/verification/DEV-1/audit-01'
        (folder / 'snapshot.txt').write_text('tampered')
        self.assertEqual(self.prepare('--apply')[0], 1)
        gate = fops.acquire_gate(self.root, playbook.safe_path, playbook.require, owner='foreign')
        try:
            self.assertEqual(self.prepare('--apply', ident='audit-02')[0], 1)
        finally:
            gate.close()
        self.assertFalse((self.root / 'work/verification/DEV-1/audit-02').exists())

    def test_prepare_repeat_rejects_modified_provenance_without_rewriting_it(self):
        """Повторный prepare отклоняет изменённый provenance, не переписывая его"""
        self.assertEqual(self.prepare('--apply')[0], 0)
        path = self.root / 'work/verification/DEV-1/audit-01/provenance.json'
        original = path.read_bytes()
        for key, value in [('actor', 'other'), ('task_id', 'DEV-2'), ('at', '2020-01-01T00:00:00Z')]:
            data = json.loads(original)
            data[key] = value
            path.write_text(json.dumps(data))
            before = path.read_bytes()
            result = self.prepare('--apply')
            self.assertEqual(result[0], 1, result)
            self.assertIn('provenance', result[2])
            self.assertEqual(path.read_bytes(), before)
        path.write_bytes(original)
        self.assertEqual(self.prepare('--apply')[0], 0)
        data = json.loads(original)
        data['at'] = '2020-01-01T00:00:00Z'
        path.write_text(json.dumps(data))
        (path.parent / 'provenance.sha256').write_text(fops.sha256(path.read_bytes()))
        # A checksum stored beside the evidence is not the trusted binding:
        # the task metadata holds the original digest independently.
        self.assertEqual(self.prepare('--apply')[0], 1)
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 1)

    def test_prepare_audit_failure_rolls_back_all_files(self):
        """Сбой prepare audit откатывает все созданные файлы к пред-образу"""
        before = tree_fingerprint(self.root)
        original = fops._write_backup
        def fail(*args):
            raise ValueError('injected before write')
        with patch.object(fops, '_write_backup', fail):
            result = self.prepare('--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIsNone(fops.pending_info(self.root))
        self.assertFalse((self.root / 'work/verification/DEV-1/audit-01').exists())
        card = 'docs/tasks/DEV-1.md'
        self.assertEqual(tree_fingerprint(self.root)[card], before[card])

    def test_explicit_reuse_skips_process_and_changed_input_runs_it(self):
        """Явное переиспользование пропускает процесс, изменённый вход запускает его заново"""
        self.counter()
        self.verify()
        self.assertEqual(self.count(), 1)
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertIn('reused:', result[1])
        self.assertEqual(self.count(), 1)
        (self.root / 'docs/new.txt').write_text('new requirement')
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertIn('изменились входы', result[1])
        self.assertEqual(self.count(), 2)

    def test_audit_added_outside_declared_inputs_does_not_force_rerun(self):
        """Добавленный аудит-файл вне объявленных входов не заставляет повторять прогон"""
        self.counter()
        self.verify()
        self.assertEqual(self.prepare('--apply')[0], 0)
        # A JSON audit report is material, not a runner report even when it
        # uses the conventional name report.json.
        (self.root / 'work/verification/DEV-1/audit-01/report.json').write_text('{"result":"accepted"}')
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 1)
        self.assertIn('reused:', result[1])
        self.assertEqual(self.handoff()[0], 0)

    def test_changed_environment_or_missing_runtime_requires_real_run(self):
        """Изменённая среда или отсутствующий runtime требуют реального прогона"""
        self.counter()
        self.verify()
        with patch.dict(os.environ, {'PB054_TEST_ENV': 'changed'}):
            result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 2)
        reports = list((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        latest = max(reports, key=lambda p: p.stat().st_mtime_ns)
        data = json.loads(latest.read_text()); data.pop('runtime')
        latest.write_text(json.dumps(data))
        self.assertEqual(self.cli('verify', 'DEV-1', '--reuse')[0], 0)
        self.assertEqual(self.count(), 3)

    def test_later_failed_report_cannot_reuse_older_pass(self):
        """Более поздний failed-отчёт не может переиспользовать старый passed"""
        self.counter()
        self.verify()
        report = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        data = json.loads(report.read_text())
        other = report.parent.parent / 'newer'
        other.mkdir()
        data.update(run_id='newer', status='failed')
        (other / 'report.json').write_text(json.dumps(data))
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 2)
        self.assertNotIn('reused:', result[1])

    def test_corrupt_nested_checks_cause_real_run_not_exception(self):
        """Повреждённые вложенные checks вызывают реальный прогон, а не исключение"""
        self.counter()
        self.verify()
        for corrupt in ('corrupt', None):
            latest = max((self.root / 'work/verification/DEV-1').glob('*/report.json'),
                         key=lambda p: p.stat().st_mtime_ns)
            data = json.loads(latest.read_text())
            data['checks'] = [corrupt]
            latest.write_text(json.dumps(data))
            before = self.count()
            result = self.cli('verify', 'DEV-1', '--reuse')
            self.assertEqual(result[0], 0, result)
            self.assertEqual(self.count(), before + 1)
            self.assertNotIn('reused:', result[1])

    def test_touching_older_pass_cannot_hide_later_failed_run(self):
        """Касание старого passed не может скрыть более поздний failed прогон"""
        self.counter()
        self.verify()
        path = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        passed = json.loads(path.read_text())
        passed['started_at'] = '2020-01-01T00:00:00Z'
        failed = dict(passed, run_id='later-failure', status='failed', started_at='2021-01-01T00:00:00Z')
        folder = path.parent.parent / 'later-failure'
        folder.mkdir()
        (folder / 'report.json').write_text(json.dumps(failed))
        path.write_text(json.dumps(passed))  # New mtime, older actual run.
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 2)
        self.assertNotIn('reused:', result[1])

    def test_malformed_snapshot_and_inputs_cause_real_run_not_exception(self):
        """Некорректные snapshot и inputs вызывают реальный прогон, а не исключение"""
        self.counter()
        self.verify()
        corruptions = [
            {'snapshot_after': ['corrupt']},
            {'snapshot_after': True},
            {'snapshot_after': {'files': ['corrupt']}},
            {'inputs': [None]},
        ]
        for change in corruptions:
            latest = max((self.root / 'work/verification/DEV-1').glob('*/report.json'),
                         key=lambda p: p.stat().st_mtime_ns)
            data = json.loads(latest.read_text())
            data.update(change)
            latest.write_text(json.dumps(data))
            before = self.count()
            result = self.cli('verify', 'DEV-1', '--reuse')
            self.assertEqual(result[0], 0, result)
            self.assertEqual(self.count(), before + 1)
            self.assertNotIn('reused:', result[1])

    def test_corrupt_runner_identity_is_preserved_and_replaced_not_blocked_by_preflight(self):
        """Повреждённая identity runner сохраняется и заменяется, preflight не блокируется"""
        self.counter()
        self.verify()
        latest = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        data = json.loads(latest.read_text())
        data['run_id'] = 'wrong-id'
        latest.write_text(json.dumps(data))
        preserved = latest.read_bytes()
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 2)
        self.assertEqual(latest.read_bytes(), preserved)
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.cli('verify', 'DEV-1', '--reuse')[0], 0)
        self.assertEqual(self.count(), 2)

    def test_invalid_json_history_is_diagnostic_material_and_does_not_force_endless_reruns(self):
        """Невалидная JSON-история — диагностический материал, не вызывает бесконечные прогоны"""
        self.counter()
        self.verify()
        report = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        report.write_bytes(b'{broken')
        result = self.cli('verify', 'DEV-1', '--reuse')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(report.read_bytes(), b'{broken')
        self.assertEqual(self.cli('verify', 'DEV-1', '--reuse')[0], 0)
        self.assertEqual(self.count(), 2)

    def test_v2_criterion_binding_never_treats_copied_audit_json_as_runner(self):
        """V2 criterion binding не принимает скопированный audit JSON за runner"""
        self.managed_task(meta={'schema_version': 1, 'card_format_version': 2,
                               'relations': [], 'materials': []},
                          body=BODY.replace('- AC-01:', '- [ ] AC-01:') + record('EXEC'))
        self.verify()
        original = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        self.assertEqual(self.prepare('--apply')[0], 0)
        copied = json.loads(original.read_text())
        copied['started_at'] = '2099-01-01T00:00:00Z'
        (self.root / 'work/verification/DEV-1/audit-01/report.json').write_text(json.dumps(copied))
        card = self.tasks_dir() / 'DEV-1.md'
        card.write_text(card.read_text() + record('CHECK') + record('HANDOFF'))
        result = self.handoff()
        self.assertEqual(result[0], 0, result)
        meta = self.meta_of(card)
        self.assertEqual(meta['criterion_evidence']['CHECK-01']['report_id'], 'verify-' + original.parent.name)
        self.assertNotIn('verify-audit-01', {m['id'] for m in meta['materials']})
        card.write_text(card.read_text() + record('AUDIT', actor='Auditor'))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)

    def test_incidental_cache_inside_material_directory_is_not_required_evidence(self):
        """Случайный __pycache__ внутри каталога материала не считается обязательным доказательством"""
        self.assertEqual(self.prepare('--apply')[0], 0)
        folder = self.root / 'work/verification/DEV-1/audit-01/__pycache__'
        folder.mkdir()
        (folder / 'probe.pyc').write_bytes(b'cache')
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)

    def test_bad_raw_markdown_rejected_in_future_close_tree(self):
        # A snapshot lives outside docs before close; moving it turns it into
        # a checked Markdown document with an incorrect relative base.
        """Некорректный raw markdown отклоняется в будущем дереве закрытия"""
        folder = self.root / 'work/verification/DEV-1/raw'
        folder.mkdir(parents=True)
        raw = folder / 'card-original.md'
        raw.write_text('[doc](../architecture.md)\n')
        meta = self.meta_of(self.tasks_dir() / 'DEV-1.md')
        meta['materials'] = [{'id': 'raw', 'role': 'report', 'source': 'work/verification/DEV-1/raw',
                             'storage': 'local', 'immutable': True, 'required': True, 'used_by': ['DEV-1']}]
        # Legacy material injection reproduces the old failure, bypassing the
        # newly added registration check solely in this fixture.
        path = self.tasks_dir() / 'DEV-1.md'
        text = path.read_text()
        path.write_text(playbook.lifecycle._splice_meta(text, meta))
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 1, result)
        self.assertIn('битая', result[2])

    def test_close_preview_and_apply_reject_same_future_link_without_touching_evidence(self):
        """Close preview и apply отклоняют одну будущую ссылку, не трогая доказательства"""
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        folder = self.root / 'work/verification/DEV-1/raw'
        folder.mkdir()
        raw = folder / 'card-original.md'
        raw.write_bytes(b'[broken](../missing-original.md)\n')
        card = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(card)
        meta['materials'].append({'id': 'raw', 'role': 'report', 'source': 'work/verification/DEV-1/raw',
            'storage': 'local', 'immutable': True, 'required': True, 'used_by': ['DEV-1']})
        card.write_text(playbook.lifecycle._splice_meta(card.read_text(), meta))
        before = tree_fingerprint(self.root)
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 1, result)
        self.assertIn('missing-original.md', result[2])
        self.assertEqual(before, tree_fingerprint(self.root))
        original_card = card.read_bytes()
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 1, result)
        self.assertIn('missing-original.md', result[2])
        self.assertEqual(original_card, card.read_bytes())
        self.assertEqual(raw.read_bytes(), b'[broken](../missing-original.md)\n')
        self.assertIsNone(fops.pending_info(self.root))

    def test_preview_refuses_temporary_directory_inside_project(self):
        """Preview отклоняет временный каталог внутри проекта"""
        before = tree_fingerprint(self.root)
        with patch.object(fops.tempfile, 'gettempdir', return_value=str(self.root)):
            result = self.prepare()
        self.assertEqual(result[0], 1, result)
        self.assertIn('TMPDIR', result[2])
        self.assertEqual(before, tree_fingerprint(self.root))


class VersionChangeRegressionTest(unittest.TestCase):
    """Регрессия смены версии комплекта: следующая версия сохраняет
    текущие ожидания и исторические фикстуры."""
    def test_next_version_keeps_current_expectations_and_historical_fixtures(self):
        """Run the former PB-052 failures against a next-version local artifact."""
        parts = REPO_VERSION.split('.')
        parts[-1] = str(int(parts[-1]) + 1)
        next_version = '.'.join(parts)
        with tempfile.TemporaryDirectory(prefix='pb054-next-version-') as directory:
            root = Path(directory).resolve()
            ignored = shutil.ignore_patterns('__pycache__', '.DS_Store', '*.pyc')
            shutil.copytree(REPO / 'ai-dev-playbook', root / 'ai-dev-playbook', ignore=ignored)
            shutil.copytree(REPO / 'tests', root / 'tests', ignore=ignored)
            kit = root / 'ai-dev-playbook'
            (kit / 'VERSION').write_text(next_version + '\n')
            project = json.loads((kit / 'project.json').read_text())
            project['playbook_version'] = next_version
            (kit / 'project.json').write_text(json.dumps(project))
            manifest = json.loads((kit / 'manifest.json').read_text())
            manifest['playbook_version'] = next_version
            manifest['files'] = {rel: playbook.updating.sha256(data) for rel, data in
                playbook.updating.managed_files(root, playbook.safe_path, playbook.require).items()}
            manifest['release_id'] = playbook.updating.fingerprint(manifest['files'])
            (kit / 'manifest.json').write_text(json.dumps(manifest))
            tests = [
                'test_cli.AdoptTest.test_adopt_conflicting_version',
                'test_pb028.Pb028CompatibilityAndUpdateTest.test_real_002_artifact_update_with_custom_paths_and_evidence',
                'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A1_symlink_in_scope_and_inputs_rejected',
                'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A2_null_plan_and_null_inputs_rejected',
                'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A4_v2_card_rejected_without_task_workflow_opt_in',
                'test_document_links.RewriteTest.test_unrelated_move_preserves_authored_destinations',
            ]
            env = dict(os.environ, PYTHONPATH=str(root / 'tests'))
            result = subprocess.run([sys.executable, '-m', 'unittest', *tests, '-q'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Ran 6 tests', result.stderr)
            self.assertEqual((root / 'tests/fixtures/release-0.0.2/ai-dev-playbook/VERSION').read_text().strip(), '0.0.2')
        self.assertEqual((REPO / 'ai-dev-playbook/VERSION').read_text().strip(), REPO_VERSION)


if __name__ == '__main__':
    unittest.main()
