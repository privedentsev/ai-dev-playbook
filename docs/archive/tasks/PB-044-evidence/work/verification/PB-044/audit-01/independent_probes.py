"""Independent PB-044 CLI acceptance probes; fixture writes stay in tempfile roots."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

REPO = Path('/Users/privedentsev1/Dev/ai-dev-playbook')
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / 'tests'))
from test_close_lifecycle import CloseLifecycleCase
from test_lifecycle import task_request
from test_documents import tree_fingerprint
from test_playbook import playbook, write_card

class IndependentRepair(CloseLifecycleCase):
    def fixture(self, ident='AUD-1', status='done'):
        write_card(self.archive_dir(), ident, status, executor='Historical actor',
                   model='Historical model', created='2024-03-01T10:00:00+03:00', closed='2024-04-01T12:00:00+03:00')
        code, out, err = self.cli('index')
        self.assertEqual(code, 0, err + out)
        src = self.tasks_dir() / (ident + '-evidence')
        (src / 'space directory').mkdir(parents=True)
        (src / 'empty directory').mkdir()
        (src / 'space directory' / 'данные.bin').write_bytes(bytes(range(256)))
        (src / 'report.md').write_text('[card](../../archive/tasks/' + ident + '.md)\n', encoding='utf-8')
        return src

    def payload(self, *ids):
        payload = task_request(ids[0], action='repair_task_materials', reason='Independent historical repair probe')
        payload['scope']['tasks'] = list(ids)
        payload['actions'] = [dict(payload['actions'][0], task=ident) for ident in ids]
        return payload

    def test_batch_real_cli_roundtrip_bytes_and_scoped_work(self):
        sources = [self.fixture(), self.fixture('AUD-2', 'cancelled')]
        historical = {}
        for src in sources:
            historical.update({str(p.relative_to(self.root)): p.read_bytes() for p in src.rglob('*') if p.is_file()})
        run = self.root / 'work' / 'verification' / 'AUD-1' / 'old'
        run.mkdir(parents=True)
        (run / 'log.txt').write_bytes(b'historical runner evidence\r\n')
        historical_run = tree_fingerprint(run)
        card = self.archive_dir() / 'AUD-1.md'
        with card.open('a') as stream:
            stream.write('\n[report](../../tasks/AUD-1-evidence/report.md)\n')
        request_path = self.request(self.payload('AUD-1', 'AUD-2'))
        before = tree_fingerprint(self.root)
        code, out, err = self.cli('lifecycle', 'plan', request_path)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(tree_fingerprint(self.root), before)
        _, plan = self.save_plan(self.payload('AUD-1', 'AUD-2'))
        self.assertEqual(self.cli('lifecycle', 'apply', plan)[0], 0)
        for rel, raw in historical.items():
            dst = self.root / rel.replace('docs/tasks/', 'docs/archive/tasks/', 1)
            self.assertEqual(dst.read_bytes(), raw)
            self.assertFalse((self.root / rel).exists())
        self.assertEqual(tree_fingerprint(run), historical_run)
        self.assertIn('Status: done', card.read_text())
        self.assertIn('Closed: 2024-04-01T12:00:00+03:00', card.read_text())
        self.assertIn('Executor: Historical actor', card.read_text())
        self.assertIn('[report](AUD-1-evidence/report.md)', card.read_text())
        self.assertIn('Status: cancelled', (self.archive_dir() / 'AUD-2.md').read_text())
        self.assertTrue((self.archive_dir() / 'AUD-1-evidence' / 'empty directory').is_dir())
        meta = self.meta_of(card)
        self.assertNotIn('handoff', meta)
        self.assertNotIn('acceptance', meta)
        self.assertEqual(meta['materials'][0]['source'], 'docs/archive/tasks/AUD-1-evidence')
        self.assertTrue(meta['materials'][0]['immutable'])
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        after = tree_fingerprint(self.root)
        self.assertEqual(self.cli('lifecycle', 'apply', plan)[0], 0)
        self.assertEqual(tree_fingerprint(self.root), after)
        # Repair deliberately leaves work/verification unregistered; remove that
        # independently created probe artifact before testing normal reopen.
        import shutil
        shutil.rmtree(run.parent)
        code, out, err = self.apply_request(task_request('AUD-1', action='reopen'))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        for rel, raw in historical.items():
            if 'AUD-1-evidence/' in rel:
                self.assertEqual((self.root / rel).read_bytes(), raw)

    def test_unselected_violation_is_not_exempted(self):
        self.fixture(); self.fixture('AUD-2')
        req = self.request(self.payload('AUD-1'))
        before = tree_fingerprint(self.root)
        code, _, err = self.cli('lifecycle', 'plan', req)
        self.assertEqual(code, 1)
        self.assertIn('AUD-2', err)
        self.assertIn('docs/tasks/AUD-2-evidence', err)
        self.assertEqual(tree_fingerprint(self.root), before)

    def test_stale_source_content_keeps_entire_project(self):
        src = self.fixture()
        _, plan = self.save_plan(self.payload('AUD-1'))
        (src / 'report.md').write_text('late content modification\n')
        before = tree_fingerprint(self.root)
        code, _, err = self.cli('lifecycle', 'apply', plan)
        self.assertEqual(code, 1)
        self.assertIn('устарел', err)
        self.assertEqual(tree_fingerprint(self.root), before)
        self.assertIsNone(playbook.file_operations.pending_info(self.root))

    def test_root_source_symlink_is_refused_without_mutation(self):
        import shutil
        src = self.fixture()
        target = self.root / 'external-evidence'
        shutil.move(src, target)
        src.symlink_to(target, target_is_directory=True)
        req = self.request(self.payload('AUD-1'))
        before = {p.name: p.read_bytes() for p in target.rglob('*') if p.is_file()}
        code, _, err = self.cli('lifecycle', 'plan', req)
        self.assertEqual(code, 1)
        self.assertIn('ссыл', err)
        self.assertTrue(src.is_symlink())
        self.assertEqual({p.name: p.read_bytes() for p in target.rglob('*') if p.is_file()}, before)
        self.assertFalse((self.archive_dir() / src.name).exists())

    def test_occupied_file_destination_is_refused(self):
        self.fixture()
        dst = self.archive_dir() / 'AUD-1-evidence'
        dst.write_bytes(b'occupied destination')
        req = self.request(self.payload('AUD-1'))
        before = tree_fingerprint(self.root)
        self.assertEqual(self.cli('lifecycle', 'plan', req)[0], 1)
        self.assertEqual(tree_fingerprint(self.root), before)

if __name__ == '__main__':
    unittest.main(verbosity=2)
