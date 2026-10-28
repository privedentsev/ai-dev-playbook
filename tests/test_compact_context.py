"""External storage and context selection, exercised on temporary projects."""
import copy
import json
from pathlib import Path

from test_task_cards import BODY, record, finding, text, cards
from test_close_lifecycle import CloseLifecycleCase, TASK_META, lifecycle
from test_lifecycle import task_request
from test_documents import tree_fingerprint
from test_playbook import playbook

docs = playbook.documents


class CompactContextTest(CloseLifecycleCase):
    def setup_external(self, journal=True):
        body = BODY.replace('- AC-01:', '- [ ] AC-01:')
        body = body.replace('Локальный проект.', '[Config](../../ai-dev-playbook/project.json).')
        if journal:
            body += record('EXEC') + record('CHECK', **{'Доказательства': '[Config](../../ai-dev-playbook/project.json)'}) + record('HANDOFF')
        self.managed_task(status='in_progress', meta={**copy.deepcopy(TASK_META), 'card_format_version': 2}, body=body)
        self.path = self.tasks_dir() / 'DEV-1.md'
        self.externalize(self.path)
        self.assertEqual(self.cli('index')[0], 0)

    def externalize(self, path):
        original = path.read_text()
        hydrated = original.replace(docs.META_START, docs.EXTERNAL_META + '\n' + docs.META_START, 1)
        writes = docs.split_card_writes({str(path.relative_to(self.root)): hydrated.encode()})
        for rel, content in writes.items():
            (self.root / rel).write_bytes(content)

    def append(self, value):
        self.path.write_text(self.path.read_text() + value)

    def test_create_preview_pair_and_no_overwrite(self):
        before = tree_fingerprint(self.root)
        result = self.cli('card', 'create', 'DEV-2', '--title', 'Пример')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(before, tree_fingerprint(self.root))
        result = self.cli('card', 'create', 'DEV-2', '--title', 'Пример', '--apply')
        self.assertEqual(result[0], 0, result)
        path = self.tasks_dir() / 'DEV-2.md'
        self.assertIn(docs.EXTERNAL_META, path.read_text())
        self.assertNotIn(docs.META_START, path.read_text())
        self.assertTrue(docs.meta_path(path).is_file())
        self.assertEqual(cards.parse(docs.read_text(path))['header']['status'], 'draft')
        before = tree_fingerprint(self.root)
        self.assertEqual(self.cli('card', 'create', 'DEV-2', '--title', 'Again', '--apply')[0], 1)
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_context_selection_is_read_only_and_exposes_unhanded_work(self):
        self.setup_external()
        self.append(record('AUDIT', **{'Результат': 'changes_requested'}) + finding(1))
        self.append(record('EXEC', 2, title='Исправление', **{'Замечания': 'A-01'}))
        before = tree_fingerprint(self.root)
        show = self.cli('card', 'show', 'DEV-1')
        self.assertEqual(show[0], 0, show)
        self.assertIn('Работа без актуальной передачи: EXEC-02', show[1])
        self.assertIn('A-01', show[1])
        self.assertNotIn('schema_version', show[1])
        result = self.cli('card', 'show', 'DEV-1', '--entry', 'A-01', '--json')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])['audit_id'], 'AUDIT-01')
        result = self.cli('card', 'show', 'DEV-1', '--entry', 'EXEC-02')
        self.assertIn('Замечания: A-01', result[1])
        self.assertEqual(self.cli('card', 'show', 'DEV-1', '--entry', 'EXEC-99')[0], 1)
        full = self.cli('card', 'show', 'DEV-1', '--full')
        self.assertIn('### EXEC-01', full[1])
        self.assertNotIn(docs.META_START, full[1])
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_lost_corrupt_conflicting_symlink_and_orphan_metadata(self):
        self.setup_external(False)
        sidecar = docs.meta_path(self.path)
        original = sidecar.read_bytes()
        for invalid in (b'broken', b'[]'):
            sidecar.write_bytes(invalid)
            self.assertEqual(self.cli('card', 'show', 'DEV-1')[0], 1)
        sidecar.unlink()
        self.assertEqual(self.cli('card', 'show', 'DEV-1')[0], 1)
        other = self.root / 'other.json'; other.write_bytes(original)
        sidecar.symlink_to(other)
        self.assertEqual(self.cli('card', 'show', 'DEV-1')[0], 1)
        sidecar.unlink(); sidecar.write_bytes(original)
        original_card = self.path.read_text()
        self.path.write_text(docs.read_text(self.path))
        self.assertEqual(self.cli('card', 'show', 'DEV-1')[0], 1)
        self.path.write_text(original_card)
        orphan = self.tasks_dir() / 'DEV-99.meta.json'; orphan.write_bytes(original)
        self.assertEqual(self.cli('check')[0], 1)

    def test_full_two_close_cycles_and_immutable_proof(self):
        self.setup_external()
        proof = self.tasks_dir() / 'DEV-1-evidence' / 'proof.txt'
        proof.parent.mkdir(); proof.write_bytes(b'original proof\n')
        result = self.cli('lifecycle', 'register-material', 'DEV-1', str(proof.parent.relative_to(self.root)),
            '--id', 'proof', '--role', 'report', '--immutable', '--required', '--apply')
        self.assertEqual(result[0], 0, result)
        for cycle in (1, 2):
            self.path.write_text(self.path.read_text().replace("Status: in_progress", "Status: in_review"))
            self.assertEqual(self.cli("index")[0], 0)
            self.verify()
            result = self.handoff()
            self.assertEqual(result[0], 0, result)
            self.append(record('AUDIT', cycle, actor='Auditor', **{'Передача': f'HANDOFF-{cycle:02d}',
                'Доказательства': '[Config](../../ai-dev-playbook/project.json)'}))
            self.assertEqual(self.accept('--result', 'accepted')[0], 0)
            result = self.cli('close', 'DEV-1')
            self.assertEqual(result[0], 0, result)
            archive = self.archive_dir() / 'DEV-1.md'
            self.assertTrue(docs.meta_path(archive).is_file())
            self.assertFalse(docs.meta_path(self.path).exists())
            self.assertEqual(self.cli('check', '--ready')[0], 0)
            view = self.cli('card', 'show', 'DEV-1', '--json')
            self.assertEqual(json.loads(view[1])['acceptance_state'], 'historical')
            self.assertEqual((self.archive_dir() / 'DEV-1-evidence/proof.txt').read_bytes(), b'original proof\n')
            if cycle == 1:
                result = self.apply_request(task_request('DEV-1', 'reopen', status='in_progress', reason='Second cycle'))
                self.assertEqual(result[0], 0, result)
                self.assertTrue(docs.meta_path(self.path).is_file())
                self.append(record('EXEC', 2) + record('CHECK', 2) + record('HANDOFF', 2,
                    **{'Выполнение': 'EXEC-02', 'Проверки': 'CHECK-02'}))

    def test_metadata_write_failure_restores_pair(self):
        self.setup_external(False)
        sidecar = docs.meta_path(self.path)
        before = (self.path.read_bytes(), sidecar.read_bytes())
        rel = str(self.path.relative_to(self.root))
        hydrated = docs.read_text(self.path)
        meta = cards.metadata(hydrated)
        ctx = playbook._lifecycle_ctx(None)
        # Engine callbacks can also reject the final state; rollback must restore both files.
        original_run = ctx['run_operation']
        def reject_final(root, spec):
            spec = dict(spec)
            spec['final_checks'] = [lambda _: (_ for _ in ()).throw(ValueError('injected final failure'))]
            return original_run(root, spec)
        ctx['run_operation'] = reject_final
        with self.assertRaises(ValueError):
            lifecycle._write_card_meta(self.root, rel, hydrated.replace('Python.', 'Python 3.'),
                meta, 'probe', 'tester', 'rollback probe', ctx, playbook.safe_path, playbook.require)
        self.assertEqual((self.path.read_bytes(), sidecar.read_bytes()), before)

    def test_explicit_card_snapshot_includes_sidecar_and_old_report_goes_stale(self):
        self.setup_external()
        rel = str(self.path.relative_to(self.root))
        snapshot = playbook.verification.snapshot(self.root, [rel], playbook.safe_path, playbook.require)
        self.assertIn(str(docs.meta_path(rel)), snapshot['files'])
        self.verify()
        self.path.write_text(self.path.read_text().replace('Добавлен apply.', 'Changed implementation.'))
        self.assertEqual(self.handoff()[0], 1)

    def test_many_iterations_have_small_context_and_full_json_remains_available(self):
        self.setup_external(False)
        paragraph = 'Подробное описание выполненной работы. ' * 35
        for number in range(1, 9):
            self.append(record('EXEC', number, **{'Изменения': paragraph}) + record('CHECK', number,
                **{'Факт': paragraph}) + record('HANDOFF', number,
                **{'Выполнение': f'EXEC-{number:02d}', 'Проверки': f'CHECK-{number:02d}'}) + record('AUDIT', number,
                **{'Передача': f'HANDOFF-{number:02d}', 'Независимо проверено': paragraph}))
        compact = self.cli('card', 'show', 'DEV-1')
        full = self.cli('card', 'show', 'DEV-1', '--full')
        structured = self.cli('card', 'show', 'DEV-1', '--json')
        self.assertEqual(compact[0], 0, compact)
        self.assertEqual(len(json.loads(structured[1])['entries']), 32)
        self.assertLess(len(compact[1]), len(full[1]) / 4)
        print('Context sample: full_chars=%d compact_chars=%d' % (len(full[1]), len(compact[1])))

    def test_sidecar_is_not_a_material_and_saved_plan_detects_sidecar_change(self):
        self.setup_external()
        sidecar = docs.meta_path(self.path)
        result = self.cli('lifecycle', 'register-material', 'DEV-1', str(sidecar.relative_to(self.root)),
            '--id', 'bad-proof', '--role', 'report', '--immutable', '--required', '--apply')
        self.assertEqual(result[0], 1, result)
        self.path.write_text(self.path.read_text().replace('Status: in_progress', 'Status: in_review'))
        self.assertEqual(self.cli('index')[0], 0)
        self.verify(); self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', actor='Auditor'))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        _, plan_rel = self.save_plan(task_request('DEV-1', 'close_task', outcome='done'))
        sidecar.write_text(sidecar.read_text() + '\n')
        before = tree_fingerprint(self.root)
        result = self.cli('lifecycle', 'apply', plan_rel)
        self.assertEqual(result[0], 1, result)
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_installed_cli_creates_pair_and_sigkill_restores_partial_creation(self):
        import subprocess
        import sys
        installed = self.root / 'ai-dev-playbook/scripts/playbook.py'
        result = subprocess.run([sys.executable, str(installed), '--root', str(self.root),
            'card', 'create', 'DEV-2', '--title', 'Installed', '--apply'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tasks_dir() / 'DEV-2.meta.json').is_file())
        original_index = (self.tasks_dir() / 'index.md').read_bytes()
        script = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, SCRIPTS)
import playbook
root = Path(ROOT)
original = playbook.file_operations._atomic_write
def kill_after_card(target, content, op_id):
    original(target, content, op_id)
    if target == root / 'docs/tasks/DEV-3.md':
        os.kill(os.getpid(), signal.SIGKILL)
playbook.file_operations._atomic_write = kill_after_card
playbook.main(['--root', str(root), 'card', 'create', 'DEV-3', '--title', 'Interrupted', '--apply'])
'''.replace('SCRIPTS', repr(str(installed.parent))).replace('ROOT', repr(str(self.root)))
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, -9, result.stderr)
        self.assertEqual(self.cli('card', 'show', 'DEV-2')[0], 1)  # Pending operation blocks regular reads.
        result = self.cli('lifecycle', 'restore')
        self.assertEqual(result[0], 0, result)
        self.assertFalse((self.tasks_dir() / 'DEV-3.md').exists())
        self.assertFalse((self.tasks_dir() / 'DEV-3.meta.json').exists())
        self.assertEqual((self.tasks_dir() / 'index.md').read_bytes(), original_index)
        self.assertEqual(self.cli('card', 'show', 'DEV-2')[0], 0)

    def test_marker_examples_roundtrip_views_and_sync_preserve_card(self):
        self.setup_external()
        original = self.path.read_text()
        original_meta = docs.meta_path(self.path).read_bytes()
        examples = [
            '```markdown\n' + docs.EXTERNAL_META + '\n```\n',
            '~~~markdown\n## Example heading\n' + docs.EXTERNAL_META + '\n~~~\n',
            '````markdown\n```markdown\n' + docs.EXTERNAL_META + '\n```\n````\n',
        ]
        for index, example in enumerate(examples):
            with self.subTest(example=index):
                title = '# DEV-1: example ' + docs.EXTERNAL_META
                raw = original.replace(docs.EXTERNAL_META, example + docs.EXTERNAL_META, 1)
                raw = raw.replace(raw.splitlines()[0], title, 1)
                self.path.write_text(raw)
                docs.meta_path(self.path).write_bytes(original_meta)
                self.assertEqual(self.cli('index')[0], 0)
                hydrated = docs.read_text(self.path)
                self.assertEqual(cards.parse(hydrated)['header']['title'], title[2:])
                writes = docs.split_card_writes({'docs/tasks/DEV-1.md': hydrated.encode()})
                self.assertEqual(writes['docs/tasks/DEV-1.md'], raw.encode())
                self.assertEqual(json.loads(writes['docs/tasks/DEV-1.meta.json']), json.loads(original_meta))
                before = tree_fingerprint(self.root)
                for options in ([], ['--full'], ['--json'], ['--entry', 'CHECK-01']):
                    result = self.cli('card', 'show', 'DEV-1', *options)
                    self.assertEqual(result[0], 0, result)
                    if options == ['--full']:
                        self.assertIn(example, result[1])
                    if options == ['--json']:
                        self.assertEqual(json.loads(result[1])['header']['title'], title[2:])
                self.assertEqual(before, tree_fingerprint(self.root))
                self.verify()
                result = self.cli('card', 'sync', 'DEV-1', '--apply')
                self.assertEqual(result[0], 0, result)
                self.assertEqual(cards.normalize_checkboxes(self.path.read_text()), cards.normalize_checkboxes(raw))
                self.assertIn(example, self.path.read_text())
                self.assertIn('- [x] AC-01:', self.path.read_text())
                saved = json.loads(docs.meta_path(self.path).read_text())
                for key, value in json.loads(original_meta).items():
                    if key != 'materials':
                        self.assertEqual(saved[key], value)
                before = tree_fingerprint(self.root)
                self.assertEqual(self.cli('card', 'sync', 'DEV-1', '--apply')[0], 0)
                self.assertEqual(before, tree_fingerprint(self.root))

    def test_create_title_marker_and_duplicate_real_markers(self):
        title = 'Example ' + docs.EXTERNAL_META
        result = self.cli('card', 'create', 'DEV-2', '--title', title, '--apply')
        self.assertEqual(result[0], 0, result)
        result = self.cli('card', 'show', 'DEV-2', '--json')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])['header']['title'], 'DEV-2: ' + title)
        path = self.tasks_dir() / 'DEV-2.md'
        original = path.read_text()
        self.assertTrue(original.startswith('# DEV-2: ' + title + '\n'))
        self.assertNotIn(docs.META_START, original)
        position = docs.external_meta_position(original)
        lines = original.split('\n')
        lines.insert(position + 1, docs.EXTERNAL_META)
        path.write_text('\n'.join(lines))
        before = tree_fingerprint(self.root)
        self.assertEqual(self.cli('card', 'show', 'DEV-2')[0], 1)
        self.assertEqual(before, tree_fingerprint(self.root))
        # A fenced example cannot replace the missing actual marker.
        lines = original.split('\n')
        lines[position:position + 1] = ['~~~markdown', docs.EXTERNAL_META, '~~~']
        path.write_text('\n'.join(lines))
        self.assertEqual(self.cli('card', 'show', 'DEV-2')[0], 1)
