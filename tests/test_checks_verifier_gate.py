"""PB-079: real signed outcomes and CLI consumption of trusted history."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_checks_trust import (ROOT, command_policy, init_home, verifier_run)
import checks_gate
import checks_trust
import checks_verifier
import verification_policy


class VerifierHistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.flag = Path(tmp.name) / 'flag'
        self.flag.write_text('0')
        code = ("import sys,time;from pathlib import Path;"
                "n=int(Path(sys.argv[1]).read_text());"
                "time.sleep(3) if n==2 else None;"
                "Path('src/cart/basket.py').write_text('changed') if n==3 else None;"
                "sys.exit(1 if n==1 else 0)")
        self.policy = command_policy([sys.executable, '-c', code, str(self.flag)])
        self.policy['checks'][0]['timeout_seconds'] = 1
        self.proj, self.home = init_home(self, self.policy)

    def run_outcome(self, state, stamp):
        self.flag.write_text(str(state))
        out = verifier_run(self.proj, self.home, now=stamp)
        self.assertTrue(checks_verifier.verify(self.home, out['receipt'])['ok'])
        expected = {0: 'passed', 1: 'failed', 2: 'timeout', 3: 'blocked'}[state]
        self.assertEqual(out['receipt']['subject']['results'][0]['status'], expected)
        return out

    def gate(self, **kw):
        return checks_verifier.gate(self.proj.root, self.home,
                                    emit=lambda s: None, **kw)

    def cli(self, covered):
        out = subprocess.run(
            [sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'),
             '--root', str(self.proj.root), 'verifier', 'gate',
             '--home', str(self.home)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0 if covered else 1,
                         out.stdout + out.stderr)

    def assert_coverage(self, covered):
        reference = self.gate()
        self.assertEqual(reference['ok'], covered, reference)
        self.assertEqual(reference['covered'], int(covered))
        self.assertFalse(reference['protected_confirmed'])
        for scope in ('task', 'project'):
            main = checks_gate.evaluate(
                self.proj.root, self.policy, task_id='DEV-1',
                scope=scope, verifier_home=self.home)
            self.assertEqual(main['covered'], ['probe'] if covered else [])
        self.cli(covered)

    def test_later_nonpass_and_recovery(self):
        for state in (1, 2, 3):
            with self.subTest(state=state):
                self.run_outcome(0, f'2026-11-17T{state+10}:00:00+00:00')
                self.assert_coverage(True)
                self.run_outcome(state, f'2026-11-17T{state+10}:01:00+00:00')
                self.assert_coverage(False)
                copies = self.proj.root / checks_trust.PROJECT_RECEIPTS_DIR
                for path in copies.glob('*.json'):
                    path.rename(path.with_suffix('.renamed'))
                self.assert_coverage(False)
                self.run_outcome(0, f'2026-11-17T{state+10}:02:00+00:00')
                for path in copies.iterdir():
                    path.unlink()
                self.assert_coverage(True)

    def test_conflicting_completion_and_commit_order(self):
        # Signed executions reproduce the order constraints of overlapping runs.
        self.run_outcome(1, '2026-11-17T10:02:00+00:00')
        self.run_outcome(0, '2026-11-17T10:01:00+00:00')
        self.assert_coverage(False)
        self.run_outcome(0, '2026-11-17T10:03:00+00:00')
        self.assert_coverage(True)

    def test_missing_trusted_failure_cannot_resurrect_pass(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        failed = self.run_outcome(1, '2026-11-17T10:01:00+00:00')
        path = self.home / checks_trust.RECEIPTS_DIR / (failed['receipt']['receipt_id'] + '.json')
        path.unlink()
        verdict = self.gate()
        self.assertFalse(verdict['ok'])
        self.assertTrue(verdict['rejections'])
        self.cli(False)

    def test_corrupt_trusted_failure_cannot_resurrect_pass(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        failed = self.run_outcome(1, '2026-11-17T10:01:00+00:00')
        path = self.home / checks_trust.RECEIPTS_DIR / (failed['receipt']['receipt_id'] + '.json')
        path.write_text('{broken')
        self.assertFalse(self.gate()['ok'])
        self.cli(False)

    def test_tampered_epoch_cannot_hide_failure(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        failed = self.run_outcome(1, '2026-11-17T10:01:00+00:00')
        path = self.home / checks_trust.RECEIPTS_DIR / (failed['receipt']['receipt_id'] + '.json')
        receipt = json.loads(path.read_text())
        receipt['candidate_digest'] = 'sha256:' + '0' * 64
        path.write_text(json.dumps(receipt))
        self.assertFalse(self.gate()['ok'])
        self.cli(False)

    def test_corrupt_journal_fails_closed(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        with (self.home / checks_trust.JOURNAL_FILE).open('a') as stream:
            stream.write('{broken\n')
        self.assertFalse(self.gate()['ok'])
        self.cli(False)

    def test_candidate_change_invalidates_receipt(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        self.proj.write('src/cart/basket.py', 'new candidate')
        self.assert_coverage(False)

    def test_declared_policy_must_match_approved(self):
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        changed = dict(self.policy, revision=self.policy['revision'] + 1)
        self.proj.write('checks-policy.json', json.dumps(changed))
        verdict = self.gate()
        self.assertFalse(verdict['ok'])
        self.assertTrue(verdict['rejections'])
        self.cli(False)

    def test_always_run_prevents_coverage(self):
        self.policy['environment']['external'] = [
            {'id': 'api', 'freshness': {'kind': 'always_run'}}]
        self.policy['checks'][0]['required_environment'] = ['api']
        checks_trust.approve_policy(checks_trust.load_home(self.home), self.policy,
                                    actor='Engineer', reapprove=True)
        self.proj.write('checks-policy.json', json.dumps(self.policy))
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        self.assert_coverage(False)

    def test_manual_stays_outside_reference_consumer(self):
        self.policy['checks'].append({
            'id': 'review', 'title': 'Review', 'purpose': 'Review',
            'kind': 'manual', 'modules': [], 'scenario': 'Inspect',
            'applicability': 'always'})
        checks_trust.approve_policy(checks_trust.load_home(self.home), self.policy,
                                    actor='Engineer', reapprove=True)
        self.proj.write('checks-policy.json', json.dumps(self.policy))
        self.run_outcome(0, '2026-11-17T10:00:00+00:00')
        verdict = self.gate()
        self.assertTrue(verdict['ok'])
        self.assertEqual(verdict['manual'], ['review'])
        self.assertEqual(verdict['obligated'], 1)
        self.assertFalse(verdict['protected_confirmed'])

    def test_task_binding_requires_matching_projection(self):
        config = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())
        config.pop('verification', None)
        config.pop('documents', None)
        config['components'] = []
        self.proj.write('ai-dev-playbook/project.json', json.dumps(config))
        for name in ('VERSION', 'manifest.json'):
            self.proj.write('ai-dev-playbook/' + name,
                            (ROOT / 'ai-dev-playbook' / name).read_text())
        self.policy['checks'][0]['inputs'].append({'type': 'task_projection'})
        checks_trust.approve_policy(checks_trust.load_home(self.home), self.policy,
                                    actor='Engineer', reapprove=True)
        self.proj.write('checks-policy.json', json.dumps(self.policy))
        content = (self.proj.root / 'docs/tasks/DEV-1.md').read_text()
        out = verifier_run(self.proj, self.home, task_id='DEV-1',
                           task_content=content, task_meta={})
        self.assertEqual(out['receipt']['subject']['results'][0]['status'], 'passed')
        self.assertFalse(self.gate()['ok'])
        projection = verification_policy.task_projection(content, {})
        self.assertTrue(self.gate(task_projection=projection)['ok'])
        self.assertFalse(self.gate(task_projection='other task')['ok'])
        argv = [sys.executable, str(ROOT / 'ai-dev-playbook/scripts/playbook.py'),
                '--root', str(self.proj.root), 'verifier', 'gate',
                '--home', str(self.home), '--task', 'DEV-1']
        cli = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)



if __name__ == '__main__':
    unittest.main()
