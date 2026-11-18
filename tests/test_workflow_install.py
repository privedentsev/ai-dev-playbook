"""PB-036: local installation, explicit opt-in and version compatibility."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_cli import CliCase, REPO, REPO_VERSION, PASS_CHECK
from test_close_lifecycle import managed_card
from test_playbook import playbook


class WorkflowInstallTest(CliCase):
    """Установка/обновление workflow v2: init opt-in, legacy-
    совместимость, отказы, переходы."""
    def project(self):
        return self.root / 'ai-dev-playbook/project.json'

    def settings(self):
        return json.loads(self.project().read_text())

    def configure(self):
        self.install(version=REPO_VERSION, verification={
            'inputs': ['AGENTS.md', 'docs'], 'checks': [PASS_CHECK]})

    def enable(self, apply=True):
        return self.cli('workflow', 'enable-v2', *(['--apply'] if apply else []))

    def migrate(self, apply=True):
        return self.cli('workflow', 'migrate-card', 'DEV-1', '--route', 'small',
            '--verification-plan', 'plan.json', '--actor', 'Executor',
            '--model', 'Test model', '--reason', 'Engineer authorized explicit opt-in',
            *(['--apply'] if apply else []))

    def test_new_init_opt_in_preview_and_validation(self):
        """Новый init --workflow-v2 preview и валидация"""
        self.assertEqual(self.init('--workflow-v2')[0], 0)
        self.assertEqual(list(self.root.iterdir()), [])
        code, out, err = self.init('--workflow-v2', '--apply')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.cli('check')[0], 0)
        data = self.settings()
        self.assertEqual(data['task_workflow']['schema_version'], 2)
        self.assertEqual(data['task_workflow']['registry'][0]['applicability'], 'always')
        self.assertEqual(data['task_workflow']['registry'][0]['argv'],
                         data['verification']['checks'][0]['argv'])
        self.assertEqual(self.enable()[0], 1)

    def test_init_from_artifact_without_project_docs(self):
        """init из артефакта без documents проекта"""
        artifact = self.fixture_release(version=REPO_VERSION)
        proc = subprocess.run([sys.executable, str(artifact / 'ai-dev-playbook/scripts/playbook.py'),
            'init', str(self.root), '--workflow-v2', '--apply'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.cli('check')[0], 0)

    def test_existing_opt_in_preserves_legacy_and_records_operation(self):
        """Существующий opt-in сохраняет legacy и пишет операцию"""
        self.configure()
        data = self.settings()
        data['custom'] = {'keep': ['value']}
        self.project().write_text(json.dumps(data))
        before = self.project().read_bytes()
        self.assertEqual(self.enable(apply=False)[0], 0)
        self.assertEqual(self.project().read_bytes(), before)
        self.assertFalse((self.root / 'work').exists())
        code, out, err = self.enable()
        self.assertEqual(code, 0, out + err)
        after = self.settings()
        expected = dict(data, schema_version=2)
        self.assertEqual({k: v for k, v in after.items() if k != 'task_workflow'}, expected)
        item = after['task_workflow']['registry'][0]
        self.assertEqual(item['argv'], PASS_CHECK['argv'])
        self.assertEqual(item['timeout_seconds'], PASS_CHECK['timeout_seconds'])
        self.assertEqual(item['default_inputs'], data['verification']['inputs'])
        self.assertEqual(item['applicability'], 'always')
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        self.assertTrue(list((self.root / 'work').rglob('backup_index.json')))
        saved = self.project().read_bytes()
        self.assertEqual(self.enable()[0], 1)
        self.assertEqual(self.project().read_bytes(), saved)

    def test_missing_legacy_checks_and_unknown_schema_reject_without_writes(self):
        """Отсутствие legacy checks и неизвестная схема отклонены без записи"""
        self.install(version=REPO_VERSION)
        before = self.project().read_bytes()
        self.assertEqual(self.enable()[0], 1)
        self.assertEqual(self.project().read_bytes(), before)
        data = self.settings()
        data['task_workflow'] = {'schema_version': 99}
        self.project().write_text(json.dumps(data))
        before = self.project().read_bytes()
        self.assertEqual(self.enable()[0], 1)
        self.assertEqual(self.project().read_bytes(), before)

    def test_enable_refuses_old_install_and_concurrent_gate(self):
        """enable отклоняет старую установку и конкурентный gate"""
        self.install(verification={'inputs': ['docs'], 'checks': [PASS_CHECK]})
        self.assertEqual(self.enable()[0], 1)
        gate = playbook.file_operations.acquire_gate(self.root, playbook.safe_path,
                                                    playbook.require, owner='test')
        try:
            code, out, err = self.enable()
            self.assertEqual(code, 1, out + err)
            self.assertIn('блокировку', err)
        finally:
            gate.close()

    def test_actual_declared_transitions_preserve_custom_data_and_legacy(self):
        """Реальные переходы сохраняют custom data и legacy"""
        for version in ('0.0.2', '0.0.3', '0.1.0'):
            with self.subTest(version=version):
                # Each transition uses a separate installation and real 0.2.0 artifact.
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp)
                    original_root = self.root
                    self.root = target
                    try:
                        self.install(version=version, verification={'inputs': ['docs'], 'checks': [PASS_CHECK]})
                        data = self.settings()
                        data['custom'] = {'keep': version}
                        self.project().write_text(json.dumps(data))
                        (target / 'docs/user.txt').write_text('user data')
                        code, out, err = self.cli('update', 'plan', str(REPO), '--save')
                        self.assertEqual(code, 0, out + err)
                        code, out, err = self.cli('update', 'apply')
                        self.assertEqual(code, 0, out + err)
                        after = self.settings()
                        self.assertEqual(after['playbook_version'], '0.2.0')
                        self.assertNotIn('task_workflow', after)
                        self.assertEqual(after['verification'], data['verification'])
                        self.assertEqual(after['custom'], data['custom'])
                        self.assertEqual((target / 'docs/user.txt').read_text(), 'user data')
                        self.assertEqual(self.enable()[0], 0)
                    finally:
                        self.root = original_root

    def test_old_cli_rejects_new_artifact_before_writes(self):
        """Старый CLI отклоняет новый артефакт до записи"""
        self.install()
        before = self.project().read_bytes()
        old = REPO / 'tests/fixtures/release-0.0.2/ai-dev-playbook/scripts/playbook.py'
        proc = subprocess.run([sys.executable, str(old), '--root', str(self.root),
                               'update', 'plan', str(REPO)], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('неподдерживаемая схема', proc.stderr)
        self.assertEqual(self.project().read_bytes(), before)
        self.assertFalse((self.root / 'work/update/plan.json').exists())

    def test_old_cli_rejects_enabled_project_before_verify(self):
        """Старый CLI отклоняет enabled-проект до verify"""
        self.configure()
        self.assertEqual(self.enable()[0], 0)
        managed_card(self.root / 'docs/tasks', 'DEV-1', 'in_progress')
        before = self.project().read_bytes()
        old = REPO / 'tests/fixtures/release-0.0.2/ai-dev-playbook/scripts/playbook.py'
        for command in (['check'], ['verify', 'DEV-1'], ['close', 'DEV-1']):
            proc = subprocess.run([sys.executable, str(old), '--root', str(self.root), *command],
                                   capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Неподдерживаемая схема project.json', proc.stderr)
        self.assertEqual(self.project().read_bytes(), before)
        self.assertFalse((self.root / 'work/verification').exists())

    def test_update_preserves_enabled_project_schema(self):
        """Обновление сохраняет схему enabled-проекта"""
        self.configure()
        self.assertEqual(self.enable()[0], 0)
        data = self.settings()
        def mutate(kit):
            updates = json.loads((REPO / 'ai-dev-playbook/updates.json').read_text())
            updates['updates_from'] = {'0.2.0': {'steps': []}}
            (kit / 'updates.json').write_text(json.dumps(updates))
        artifact = self.fixture_release(version='0.2.1', mutate=mutate, with_release_json=False)
        self.assertEqual(self.cli('update', 'plan', str(artifact), '--save')[0], 0)
        code, out, err = self.cli('update', 'apply')
        self.assertEqual(code, 0, out + err)
        after = self.settings()
        self.assertEqual(after['schema_version'], 2)
        self.assertEqual(after['task_workflow'], data['task_workflow'])

    def test_minimum_cli_and_unknown_updates_schema_reject(self):
        """Минимальный CLI и неизвестная updates-схема отклонены"""
        self.install()
        for schema, minimum in ((2, '99.0.0'), (99, '0.2.0')):
            artifact = self.fixture_release(version='0.2.0', with_release_json=False)
            (artifact / 'ai-dev-playbook/updates.json').write_text(json.dumps({
                'schema_version': schema, 'minimum_cli_version': minimum, 'updates_from': {}}))
            self.assertEqual(self.cli('update', 'plan', str(artifact), '--save')[0], 1)
        self.assertFalse((self.root / 'work/update/plan.json').exists())

    def test_card_migration_preserves_history_and_requires_new_coverage(self):
        """Миграция карточки сохраняет историю и требует нового покрытия"""
        self.configure()
        self.assertEqual(self.enable()[0], 0)
        managed_card(self.root / 'docs/tasks', 'DEV-1', 'in_progress',
                     body='\n## Цель и границы\nTest scope.\n\n## Приёмка\n- [ ] Observable result.\n')
        self.assertEqual(self.cli('index')[0], 0)
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 0)
        code, out, err = self.cli('lifecycle', 'handoff', 'DEV-1', '--summary', 'Legacy result',
                                  '--actor', 'Executor', '--model', 'Test model')
        self.assertEqual(code, 0, out + err)
        reports = {p: p.read_bytes() for p in (self.root / 'work/verification').rglob('report.json')}
        card = self.root / 'docs/tasks/DEV-1.md'
        before = card.read_text()
        plan = {'revision': 1, 'rationale': 'Preserve mandatory check', 'scope': ['docs'],
                'checks': [{'id': 'unit', 'kind': 'command', 'purpose': 'Legacy check',
                            'registry_id': 'unit', 'inputs': ['docs']}]}
        (self.root / 'plan.json').write_text(json.dumps(plan))
        self.assertEqual(self.migrate(apply=False)[0], 0)
        self.assertEqual(card.read_text(), before)
        code, out, err = self.migrate()
        self.assertEqual(code, 0, out + err)
        meta = playbook.documents.parse_meta(card.read_text(), str(card), [])
        self.assertEqual(meta['schema_version'], 2)
        self.assertEqual(meta['verification_plan'], plan)
        self.assertEqual(meta['workflow']['history'][0]['to_route'], 'small')
        history = json.loads((self.root / 'docs/tasks/DEV-1-evidence/workflow-v1.json').read_text())
        self.assertEqual(history['markdown'], before)
        self.assertNotIn('handoff', meta)
        self.assertIn('handoff', meta['workflow']['transitions'][0])
        self.assertIn('Status: ready', card.read_text())
        self.assertEqual(self.migrate()[0], 1)
        self.assertEqual(self.cli('lifecycle', 'handoff', 'DEV-1', '--summary', 'old report')[0], 1)
        for path, content in reports.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.cli('lifecycle', 'start', 'DEV-1', '--actor', 'Executor',
            '--model', 'Test model', '--reason', 'Start with v2 baseline')[0], 0)
        self.assertEqual(self.cli('lifecycle', 'handoff', 'DEV-1', '--summary', 'v1 is insufficient')[0], 1)
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 0)

    def test_external_card_migration_requires_complete_plan_and_keeps_journal(self):
        """Миграция внешней карточки требует полного плана и журнала"""
        self.configure()
        self.assertEqual(self.enable()[0], 0)
        self.assertEqual(self.cli('card', 'create', 'DEV-1', '--title', 'Migration', '--apply')[0], 0)
        card = self.root / 'docs/tasks/DEV-1.md'
        before = card.read_text()
        sidecar = card.with_suffix('.meta.json')
        plan = {'revision': 1, 'rationale': 'Migration', 'scope': ['docs'],
                'checks': [{'id': 'local', 'kind': 'manual', 'purpose': 'View',
                            'scenario': 'Check view', 'inputs': ['docs']}]}
        (self.root / 'plan.json').write_text(json.dumps(plan))
        self.assertEqual(self.migrate()[0], 1)
        self.assertEqual(card.read_text(), before)
        plan['checks'] = [{'id': 'unit', 'kind': 'command', 'purpose': 'Mandatory',
                           'registry_id': 'unit', 'inputs': ['docs']}]
        (self.root / 'plan.json').write_text(json.dumps(plan))
        code, out, err = self.migrate()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(sidecar.read_text())['schema_version'], 2)
        self.assertIn('<!-- playbook-meta:external -->', card.read_text())
        self.assertEqual(card.read_text().split('## Задача')[1], before.split('## Задача')[1])


    def test_enable_interruption_blocks_work_and_standalone_restore_recovers(self):
        """Прерывание enable блокирует работу, restore восстанавливает"""
        self.configure()
        before = self.project().read_bytes()
        manifest = (self.root / 'ai-dev-playbook/manifest.json').read_bytes()
        original = playbook.file_operations._atomic_write

        def crash(target, content, op_id):
            original(target, content, op_id)
            if target == self.project():
                raise KeyboardInterrupt()

        with patch.object(playbook.file_operations, '_atomic_write', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.enable()
        self.assertEqual(self.enable()[0], 1)
        pending = playbook.file_operations.pending_info(self.root)
        op = self.root / pending['op_dir']
        proc = subprocess.run([sys.executable, str(op / 'restore.py')], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.project().read_bytes(), before)
        self.assertEqual((self.root / 'ai-dev-playbook/manifest.json').read_bytes(), manifest)
        self.assertEqual(self.enable()[0], 0)

    def test_real_update_interruption_corrupt_backup_then_autonomous_restore(self):
        """Реальное прерывание update + повреждённый backup → автономный restore"""
        self.install(version='0.1.0', verification={'inputs': ['docs'], 'checks': [PASS_CHECK]})
        before = {p: p.read_bytes() for p in (self.root / 'ai-dev-playbook').rglob('*') if p.is_file()}
        self.assertEqual(self.cli('update', 'plan', str(REPO), '--save')[0], 0)
        original = playbook.updating._write_file

        def crash(source, target):
            original(source, target)
            if target == self.root / 'ai-dev-playbook/VERSION':
                raise KeyboardInterrupt()

        with patch.object(playbook.updating, '_write_file', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.cli('update', 'apply')
        pending = playbook.updating.pending_info(self.root)
        op = self.root / pending['op_dir']
        self.assertEqual(self.enable()[0], 1)
        backup = op / 'backup/ai-dev-playbook/project.json'
        saved = backup.read_bytes()
        backup.write_bytes(b'corrupt')
        proc = subprocess.run([sys.executable, str(op / 'restore.py')], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNotNone(playbook.updating.pending_info(self.root))
        backup.write_bytes(saved)
        proc = subprocess.run([sys.executable, str(op / 'restore.py')], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertIsNone(playbook.updating.pending_info(self.root))

    def test_custom_paths_and_update_conflict_are_preserved(self):
        """Custom paths и update-конфликт сохраняются"""
        self.install(version='0.1.0', verification={'inputs': ['records'], 'checks': [PASS_CHECK]})
        # Configure custom paths before checking/migrating; installed documents
        # remain available and user data stays outside the managed kit.
        import shutil
        shutil.move(str(self.root / 'docs'), str(self.root / 'records'))
        data = self.settings()
        data['paths'] = {key: value.replace('docs', 'records', 1) for key, value in data['paths'].items()}
        self.project().write_text(json.dumps(data))
        local = self.root / 'ai-dev-playbook/rules/tasks.md'
        local.write_text(local.read_text() + '\nLocal user rule.\n')
        self.assertEqual(self.cli('update', 'plan', str(REPO))[0], 0)
        # A local modification matching the release base is preserved as kept;
        # an actual release/local conflict requires an explicit decision.
        artifact = self.fixture_release(version='0.2.0', updates_from={'0.1.0': {'steps': []}},
            mutate=lambda kit: (kit / 'rules/tasks.md').write_text('# Replacement\n'))
        before = self.project().read_bytes()
        self.assertEqual(self.cli('update', 'plan', str(artifact), '--save')[0], 0)
        self.assertEqual(self.cli('update', 'apply')[0], 1)
        self.assertEqual(self.project().read_bytes(), before)
        self.assertTrue(local.read_text().endswith('Local user rule.\n'))
        self.assertEqual(self.cli('update', 'plan', str(REPO), '--save')[0], 0)
        self.assertEqual(self.cli('update', 'apply')[0], 0)
        self.assertEqual(self.settings()['paths'], data['paths'])
        self.assertEqual(self.enable()[0], 0)
        self.assertEqual(self.settings()['task_workflow']['registry'][0]['default_inputs'], ['records'])
