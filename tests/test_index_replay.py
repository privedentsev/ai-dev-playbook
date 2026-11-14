import unittest
from unittest.mock import patch

from test_document_cli import DocumentCliCase
from test_documents import doc_meta, write_doc, tree_fingerprint
from test_playbook import playbook, write_card


class IndexReplayTest(DocumentCliCase):
    """Повтор index: возврат к предыдущему состоянию и восстановление
    после прерывания."""
    def assert_noop(self, *args):
        before = tree_fingerprint(self.root)
        mtimes = {p: p.stat().st_mtime_ns for p in self.root.rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts
                  and p != self.root / playbook.file_operations.GATE_FILE}
        code, out, err = self.cli('index', *args)
        self.assertEqual(code, 0, err)
        self.assertIn('актуальны', out)
        self.assertEqual(tree_fingerprint(self.root), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in mtimes}, mtimes)

    def test_task_status_returns_to_previous_indexed_state(self):
        """Возврат статуса задачи к предыдущему индексированному состоянию"""
        for args in ((), ('--all',)):
            with self.subTest(args=args):
                for status in ('in_review', 'in_progress', 'in_review',
                               'in_progress', 'in_review'):
                    write_card(self.root / 'docs/tasks', 'DEV-1', status)
                    code, _, err = self.cli('index', *args)
                    self.assertEqual(code, 0, err)
                    _, paths = playbook.config(self.root)
                    expected = playbook.render_index(
                        playbook.read_tasks(self.root, paths),
                        paths['task_index'].parent)
                    self.assertEqual(paths['task_index'].read_text(), expected)
                    self.assert_noop(*args)

    def test_document_returns_to_previous_indexed_state(self):
        """Возврат документа к предыдущему индексированному состоянию"""
        for state in ('draft', 'accepted', 'draft', 'accepted', 'draft'):
            write_doc(self.specs, 'alpha.md', doc_meta(state=state))
            code, _, err = self.cli('index', '--documents')
            self.assertEqual(code, 0, err)
            playbook._catalog_consistency(self.root, 'test', ['spec', 'plan'])
            self.assertIn(f'| {state} |', (self.specs / 'index.md').read_text())
            self.assert_noop('--documents')

    def test_interrupted_new_request_restores_then_retries(self):
        """Прерванный новый запрос восстанавливается и повторяется"""
        write_card(self.root / 'docs/tasks', 'DEV-1', 'in_review')
        self.assertEqual(self.cli('index')[0], 0)
        write_card(self.root / 'docs/tasks', 'DEV-1', 'in_progress')
        self.assertEqual(self.cli('index')[0], 0)
        index = self.root / 'docs/tasks/index.md'
        before = index.read_bytes()
        write_card(self.root / 'docs/tasks', 'DEV-1', 'in_review')
        run = playbook._run_operation

        def interrupted(root, spec, gate):
            def crash(staged):
                raise KeyboardInterrupt()
            spec['final_checks'].append(crash)
            return run(root, spec, gate)

        with patch.object(playbook, '_run_operation', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.cli('index')
        self.assertIsNotNone(playbook.file_operations.pending_info(self.root))
        self.assertEqual(self.cli('index')[0], 1)
        code, _, err = self.cli('lifecycle', 'restore')
        self.assertEqual(code, 0, err)
        self.assertEqual(index.read_bytes(), before)
        self.assertIsNone(playbook.file_operations.pending_info(self.root))
        code, _, err = self.cli('index')
        self.assertEqual(code, 0, err)
        playbook._task_index_fresh(self.root, 'test')
        self.assert_noop()


if __name__ == '__main__':
    unittest.main()
