"""PB-039: CLI material registration and early handoff validation."""
import unittest

from test_close_lifecycle import CloseLifecycleCase, fops, lifecycle, playbook
from test_documents import tree_fingerprint


class MaterialRegistrationTest(CloseLifecycleCase):
    """Регистрация материалов: отказы handoff, preview/apply/repeat,
    общий гейт и rollback."""
    def setup_material(self):
        self.managed_task()
        path = self.tasks_dir() / 'DEV-1-evidence'
        path.mkdir()
        (path / 'proof.txt').write_bytes(b'original evidence\n')
        return path

    def register(self, source='docs/tasks/DEV-1-evidence', ident='evidence', *flags):
        return self.cli('lifecycle', 'register-material', 'DEV-1', source,
                        '--id', ident, '--role', 'report', '--immutable',
                        '--required', *flags)

    def test_unregistered_evidence_rejects_handoff_without_writes(self):
        """Незарегистрированный evidence отклоняет handoff без записи"""
        self.managed_task()
        self.verify()
        self.setup_material()
        before = self.project_fingerprint()
        code, _, err = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn('DEV-1-evidence', err)
        self.assertIn('register-material', err)
        self.assertEqual(before, self.project_fingerprint())
        self.assertNotIn('handoff', self.meta_of(self.tasks_dir() / 'DEV-1.md'))

    def test_preview_apply_repeat_and_full_close(self):
        """Регистрация preview/apply/repeat и полный close"""
        self.setup_material()
        before = tree_fingerprint(self.root)
        result = self.register()
        self.assertEqual(result[0], 0, result)
        self.assertIn('Предпросмотр', result[1])
        self.assertEqual(before, tree_fingerprint(self.root))
        result = self.register('docs/tasks/DEV-1-evidence', 'evidence', '--apply')
        self.assertEqual(result[0], 0, result)
        before = self.project_fingerprint()
        self.assertEqual(self.register('docs/tasks/DEV-1-evidence', 'evidence', '--apply')[0], 0)
        self.assertEqual(before, self.project_fingerprint())
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        before = self.project_fingerprint()
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(before, self.project_fingerprint())
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        self.assertEqual((self.archive_dir() / 'DEV-1-evidence/proof.txt').read_bytes(),
                         b'original evidence\n')
        self.assertEqual(self.cli('check', '--ready')[0], 0)

    def test_partial_registration_detects_new_untracked_file(self):
        """Частичная регистрация обнаруживает новый untracked-файл"""
        path = self.setup_material()
        result = self.register('docs/tasks/DEV-1-evidence/proof.txt', 'proof', '--apply')
        self.assertEqual(result[0], 0, result)
        (path / 'extra.txt').write_text('new')
        result = self.cli('verify', 'DEV-1')
        self.assertEqual(result[0], 1, result)
        self.assertIn('extra.txt', result[2])
        result = self.handoff()
        self.assertEqual(result[0], 1, result)
        self.assertIn('extra.txt', result[2])

    def test_registration_rejects_invalid_paths_and_collisions(self):
        """Регистрация отклоняет невалидные пути и коллизии"""
        path = self.setup_material()
        self.assertEqual(self.register('docs/tasks/DEV-1-evidence', 'evidence', '--apply')[0], 0)
        (self.root / 'work/audits').mkdir(parents=True, exist_ok=True)
        (self.root / 'work/audits/report.txt').write_text('x')
        (path / 'link').symlink_to(path / 'proof.txt')
        invalid = [('../outside', 'outside'), ('/tmp/outside', 'absolute'),
                   ('docs/missing', 'missing'), ('work/audits', 'work'),
                   ('work', 'work-root'),
                   ('docs/tasks/DEV-1.md', 'card'), ('docs/tasks', 'cards'),
                   ('ai-dev-playbook', 'code'),
                   ('docs/tasks/DEV-1-evidence/link', 'link')]
        # Remove nested link for cases where the catalog should otherwise be valid.
        (path / 'link').unlink()
        for source, ident in invalid[:-1]:
            with self.subTest(source=source):
                before = self.project_fingerprint()
                result = self.register(source, ident, '--apply')
                self.assertEqual(result[0], 1, result)
                self.assertEqual(before, self.project_fingerprint())
        for source, ident in [('docs/tasks/DEV-1-evidence/proof.txt', 'evidence'),
                              ('docs/tasks/DEV-1-evidence/proof.txt', 'overlap')]:
            result = self.register(source, ident, '--apply')
            self.assertEqual(result[0], 1, result)
        (path / 'link').symlink_to(path / 'proof.txt')
        result = self.register('docs/tasks/DEV-1-evidence/link', 'link', '--apply')
        self.assertEqual(result[0], 1, result)
        (path / 'link').unlink()

    def test_apply_uses_shared_gate(self):
        """Регистрация apply использует общий gate"""
        self.setup_material()
        before = self.project_fingerprint()
        gate = fops.acquire_gate(self.root, playbook.safe_path,
                                 playbook.require, owner='test holder')
        try:
            result = self.register('docs/tasks/DEV-1-evidence', 'proof', '--apply')
            self.assertEqual(result[0], 1, result)
            self.assertEqual(before, self.project_fingerprint())
        finally:
            gate.close()

    def test_failed_final_validation_rolls_back_registration(self):
        """Сбой финальной валидации откатывает регистрацию"""
        self.setup_material()
        before = self.project_fingerprint()
        ctx = dict(playbook._lifecycle_ctx(None))
        original = ctx['run_operation']

        def fail_final(root, spec):
            spec = dict(spec)
            def fail(_):
                raise ValueError('injected final failure')
            spec['final_checks'] = [fail]
            return original(root, spec)

        ctx['run_operation'] = fail_final
        with self.assertRaisesRegex(ValueError, 'injected final failure'):
            lifecycle.register_material_command(
                self.root, 'DEV-1', 'docs/tasks/DEV-1-evidence', 'evidence',
                'report', True, True, True, 'test', ctx,
                playbook.safe_path, playbook.require)
        self.assertEqual(before, self.project_fingerprint())
        self.assertIsNone(fops.pending_info(self.root))

    def test_mutable_optional_flags_are_explicit(self):
        """Флаги mutable/optional выбираются явно"""
        self.setup_material()
        result = self.cli('lifecycle', 'register-material', 'DEV-1',
                          'docs/tasks/DEV-1-evidence', '--id', 'notes',
                          '--role', 'other', '--mutable', '--optional', '--apply')
        self.assertEqual(result[0], 0, result)
        meta = self.meta_of(self.tasks_dir() / 'DEV-1.md')
        self.assertFalse(meta['materials'][0]['immutable'])
        self.assertFalse(meta['materials'][0]['required'])

    def test_registered_audit_logs_are_not_verify_reports(self):
        """Зарегистрированные audit-логи не считаются verify-отчётами"""
        self.managed_task()
        logs = self.root / 'work/verification/DEV-1/audit-logs'
        logs.mkdir(parents=True)
        (logs / 'audit.log').write_text('auditor log')
        result = self.register('work/verification/DEV-1/audit-logs', 'audit', '--apply')
        self.assertEqual(result[0], 0, result)
        self.verify()
        result = self.handoff()
        self.assertEqual(result[0], 0, result)
        # Explicitly selecting a non-runner material as --report still fails.
        result = self.handoff('--report', 'work/verification/DEV-1/audit-logs')
        self.assertEqual(result[0], 1, result)

    def test_other_owner_directory_overlap_is_rejected(self):
        """Пересечение с каталогом другого владельца отклоняется"""
        self.setup_material()
        self.managed_task('DEV-2')
        self.assertEqual(self.register('docs/tasks/DEV-1-evidence', 'evidence', '--apply')[0], 0)
        before = self.project_fingerprint()
        result = self.cli('lifecycle', 'register-material', 'DEV-2',
                          'docs/tasks/DEV-1-evidence/proof.txt', '--id', 'stolen',
                          '--role', 'log', '--mutable', '--optional', '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertEqual(before, self.project_fingerprint())


if __name__ == '__main__':
    unittest.main()
