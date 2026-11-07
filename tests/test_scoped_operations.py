"""PB-046: observable scoped writes, drift, bounded payloads and recovery."""
import contextlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_file_operations import EngineCase, fops, playbook

scoped = fops.scoped_operations


class ScopedEngineTest(EngineCase):
    def after_backup(self, action):
        original = fops._write_backup

        def hook(root, op_dir, plan, safe_path, require):
            result = original(root, op_dir, plan, safe_path, require)
            action(root, op_dir)
            return result
        return patch.object(fops, '_write_backup', hook)

    def test_only_planned_payloads_staged_and_no_tree_copy(self):
        for rel in ('src/app.py', 'media/movie', 'node_modules/package/code'):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'unrelated')

        def check(view):
            self.assertTrue((view / 'docs/new/x.md').is_file())
            self.assertFalse((view / 'docs/b.md').exists())
            marker = fops.pending_info(self.root)
            staged = self.root / marker['op_dir'] / 'staged'
            self.assertEqual({p.relative_to(staged).as_posix() for p in staged.rglob('*') if p.is_file()},
                             {'docs/a.md', 'docs/new/x.md'})
        with patch('shutil.copytree', side_effect=AssertionError('full tree copy')):
            result = self.apply(self.spec(staged_checks=[check]))
        self.assertFalse((self.root / result['op_dir'] / 'staged').exists())
        for rel in ('src/app.py', 'media/movie', 'node_modules/package/code'):
            self.assertEqual((self.root / rel).read_bytes(), b'unrelated')

    def test_unknown_callback_has_no_clone_fallback_or_backup(self):
        def unsupported(view):
            Path(view)
        with patch('shutil.copytree', side_effect=AssertionError('fallback')):
            with self.assertRaises(TypeError):
                self.apply(self.spec(staged_checks=[unsupported]))
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertEqual(self.op_dirs(), [])
        self.assertIsNone(fops.pending_info(self.root))

    def test_collection_addition_during_backup_is_detected(self):
        def check(view):
            list((view / 'docs').iterdir())
        with self.after_backup(lambda root, _: (root / 'docs/foreign.md').write_bytes(b'external')):
            with self.assertRaisesRegex(ValueError, 'stale collection'):
                self.apply(self.spec(staged_checks=[check]))
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertEqual((self.root / 'docs/foreign.md').read_bytes(), b'external')
        self.assertIsNone(fops.pending_info(self.root))

    def test_read_bytes_and_source_modes_rechecked_after_backup(self):
        dependency = self.root / 'dependency'
        dependency.write_bytes(b'before')
        def check(view):
            self.assertEqual((view / 'dependency').read_bytes(), b'before')
        with self.after_backup(lambda root, _: (root / 'dependency').write_bytes(b'after')):
            with self.assertRaisesRegex(ValueError, 'stale input bytes'):
                self.apply(self.spec(staged_checks=[check]))
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertEqual(dependency.read_bytes(), b'after')

    def test_original_drift_during_backup_preserves_external_change(self):
        with self.after_backup(lambda root, _: (root / 'docs/a.md').write_bytes(b'EXTERNAL')):
            with self.assertRaisesRegex(ValueError, 'Входы изменились'):
                self.apply(self.spec())
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'EXTERNAL')
        self.assertEqual((self.root / 'docs/b.md').read_bytes(), b'OLD-B\n')
        self.assertFalse((self.root / 'docs/new/x.md').exists())

    def test_staged_corruption_is_rejected_before_first_write(self):
        with self.after_backup(lambda _, op: (op / 'staged/docs/a.md').write_bytes(b'CORRUPT')):
            with self.assertRaisesRegex(ValueError, 'Итоговое состояние'):
                self.apply(self.spec())
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertFalse((self.root / 'docs/new/x.md').exists())

    def test_backup_corruption_is_rejected_before_first_write(self):
        with self.after_backup(lambda _, op: (op / 'backup/docs/a.md').write_bytes(b'CORRUPT')):
            with self.assertRaisesRegex(ValueError, 'повреждена'):
                self.apply(self.spec())
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertIsNone(fops.pending_info(self.root))

    def test_payload_source_bytes_are_part_of_read_set(self):
        source = self.root / 'source'
        source.write_bytes(b'old source')
        writes = {'docs/copied': scoped.Payload(source)}
        inputs = fops.snapshot_paths(self.root, list(writes), playbook.safe_path, playbook.require)
        with self.after_backup(lambda root, _: (root / 'source').write_bytes(b'new source')):
            with self.assertRaisesRegex(ValueError, 'stale input bytes'):
                self.apply(self.spec(writes=writes, removes=[], inputs=inputs))
        self.assertFalse((self.root / 'docs/copied').exists())
        self.assertEqual(source.read_bytes(), b'new source')

    def test_large_payload_never_uses_read_bytes(self):
        source = self.root / 'large'
        with source.open('wb') as outgoing:
            for _ in range(12):
                outgoing.write(b'x' * scoped.project_view.CHUNK_SIZE)
        old_read = Path.read_bytes
        def guarded(path):
            if path.name in {'large', 'copied'}:
                raise AssertionError('unbounded payload read')
            return old_read(path)
        with patch.object(Path, 'read_bytes', guarded):
            writes = {'docs/copied': scoped.Payload(source)}
            inputs = fops.snapshot_paths(self.root, list(writes), playbook.safe_path, playbook.require)
            result = self.apply(self.spec(writes=writes, removes=[], inputs=inputs))
        self.assertEqual(scoped.file_digest(self.root / 'docs/copied'), scoped.file_digest(source))
        self.assertFalse((self.root / result['op_dir'] / 'staged').exists())

    def test_cleanup_failure_does_not_rollback_completed_write(self):
        original = scoped.shutil.rmtree
        def fail_staged(path, *args, **kwargs):
            if Path(path).name == 'staged':
                raise OSError('injected cleanup failure')
            return original(path, *args, **kwargs)
        with patch.object(scoped.shutil, 'rmtree', fail_staged):
            result = self.apply(self.spec())
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['warnings'])
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'NEW-A\n')
        self.assertIsNone(fops.pending_info(self.root))
        receipt = json.loads((self.root / result['op_dir'] / 'receipt.json').read_text())
        self.assertEqual(receipt['result'], 'completed')

    def test_restore_rechecks_original_digest_during_atomic_copy(self):
        op = self._interrupted_op()
        original = fops._atomic_write
        def corrupt_after_validation(target, content, op_id):
            if isinstance(content, scoped.Payload) and 'backup' in content.source.parts:
                content.source.write_bytes(b'CORRUPT')
            return original(target, content, op_id)
        with patch.object(fops, '_atomic_write', corrupt_after_validation):
            with self.assertRaisesRegex(ValueError, 'payload changed'):
                fops.restore_operation(self.root, playbook.safe_path, playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'NEW-A\n')
        self.assertTrue((op / 'backup_index.json').exists())

    def test_repeated_restore_preserves_distinct_external_versions(self):
        self._interrupted_op()
        (self.root / 'docs/a.md').write_bytes(b'FOREIGN-ONE')
        with patch.object(fops, '_neutralize_receipt', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                fops.restore_operation(self.root, playbook.safe_path, playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))
        (self.root / 'docs/a.md').write_bytes(b'FOREIGN-TWO')
        result = fops.restore_operation(self.root, playbook.safe_path, playbook.require)
        conflicts = self.root / result['op_dir'] / 'conflicts'
        self.assertEqual({p.read_bytes() for p in conflicts.rglob('*') if p.is_file()},
                         {b'FOREIGN-ONE', b'FOREIGN-TWO'})
        self.assertEqual((self.root / 'docs/a.md').read_bytes(), b'OLD-A\n')
        self.assertFalse((self.root / result['op_dir'] / 'staged').exists())
