"""PB-080: real imports/CLI, naturally generated stale bytecode, no cache deletion."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_checks_e2e import NoGitChecksE2ECase, ROOT
from test_checks_run import run_policy, run_project, execute
import checks_contract
import checks_gate
import checks_select
import checks_results
import checks_unittest


class BytecodeContextTest(unittest.TestCase):
    def test_source_and_test_caches_matrix(self):
        # Both discovery and package imports; no timestamp wait or pyc editing.
        for package in (False, True):
            for target in ('source', 'test'):
                for environment in ('normal', 'dont-write', 'prefix'):
                    with self.subTest(package=package, target=target, env=environment):
                        proj = run_project(self)
                        root = proj.root
                        source = root / 'value.py'
                        source.write_text('value = 1\n')
                        if package:
                            (root / 'pkg').mkdir()
                            (root / 'pkg/__init__.py').write_text('')
                            source = source.rename(root / 'pkg/value.py')
                        observed = root / 'observed'
                        test = root / 'tests/test_payments.py'
                        test.write_text(
                            'import unittest\nfrom pathlib import Path\n'
                            f'from {"pkg.value" if package else "value"} import value\n'
                            'class TestPay(unittest.TestCase):\n'
                            ' def test_ok(self):\n'
                            f'  Path({str(observed)!r}).write_text(str(value))\n'
                            '  self.assertEqual(value, 1)\n')
                        check = run_policy()['checks'][0]
                        seed_env = dict(os.environ)
                        seed_env.pop('PYTHONDONTWRITEBYTECODE', None)
                        seed_env.pop('PYTHONPYCACHEPREFIX', None)
                        seed_env['PYTHONPATH'] = str(root)
                        seed = subprocess.run(check['argv'], cwd=root, env=seed_env,
                                              capture_output=True)
                        self.assertEqual(seed.returncode, 0, seed.stderr)
                        caches = {p: p.read_bytes() for p in root.rglob('*.pyc')}
                        self.assertTrue(caches)
                        edited = source if target == 'source' else test
                        old = edited.stat()
                        text = edited.read_text()
                        edited.write_text(text.replace('value = 1', 'value = 2')
                                          if target == 'source' else
                                          text.replace('value, 1', 'value, 2'))
                        os.utime(edited, ns=(old.st_atime_ns, old.st_mtime_ns))
                        self.assertEqual(edited.stat().st_size, old.st_size)
                        self.assertEqual(edited.stat().st_mtime_ns, old.st_mtime_ns)
                        env = dict(seed_env)
                        if environment == 'dont-write':
                            env['PYTHONDONTWRITEBYTECODE'] = '1'
                        if environment == 'prefix':
                            env['PYTHONPYCACHEPREFIX'] = str(root / 'user-cache')
                        for attempt in range(2):
                            run_dir = root / f'work/checks/runs/bytecode-{attempt}'
                            run_dir.mkdir(parents=True)
                            result = checks_unittest.collect(root, check, run_dir,
                                                            timeout=20, env=env)
                            self.assertIsNone(result['status'], result)
                            self.assertEqual(result['report']['expected'],
                                             ['test_payments.TestPay.test_ok'])
                            self.assertEqual(result['report']['actual'][0]['outcome'],
                                             'failed')
                            self.assertEqual(observed.read_text(),
                                             '2' if target == 'source' else '1')
                        self.assertEqual({p: p.read_bytes() for p in root.rglob('*.pyc')},
                                         caches)
                        # Ordinary size/mtime-changing edit also executes current bytes.
                        source.write_text('value = 12345\n')
                        test.write_text(test.read_text().replace(
                            'value, 1' if target == 'source' else 'value, 2',
                            'value, 12345'))
                        run_dir = root / 'work/checks/runs/ordinary'
                        run_dir.mkdir(parents=True)
                        result = checks_unittest.collect(root, check, run_dir,
                                                        timeout=20, env=env)
                        self.assertIsNone(result['status'], result)
                        self.assertEqual(result['report']['actual'][0]['outcome'], 'passed')
                        self.assertEqual(observed.read_text(), '12345')


    def test_old_adapter_identity_requires_execution_then_reuse(self):
        proj = run_project(self)
        policy = run_policy()
        policy["checks"] = policy["checks"][:1]
        # Real old-identity run; no synthesized result/IDs/outcomes.
        with mock.patch.object(checks_unittest, 'ADAPTER',
                               {'id': checks_contract.BUILTIN_ADAPTER, 'version': '3'}):
            first, _ = execute(proj, policy, scope='project', mode='force')
        self.assertTrue(first["ok"], first)
        gate = checks_gate.evaluate(
            proj.root, policy, task_id='DEV-1', scope='project',
            task_content=(proj.root / 'docs/tasks/DEV-1.md').read_text(), task_meta={})
        self.assertFalse(gate['ok'])
        second, output = execute(proj, policy, scope='project')
        self.assertTrue(second["ok"], output)
        records, _, _ = checks_results.load_results(proj.root)
        tests = [r for r in records if r['kind'] == 'test']
        latest = checks_select.evidence_latest(tests)['payments-tests']
        self.assertEqual(latest['binding']['adapter']['version'], '4')
        self.assertFalse(latest.get('reused'))
        count = len(records)
        old_ids = {r["result_id"] for r in records}
        third, output = execute(proj, policy, scope='project')
        self.assertTrue(third["ok"], output)
        records = checks_results.load_results(proj.root)[0]
        self.assertEqual(len(records), count + 1)
        self.assertTrue(next(r for r in records if r["result_id"] not in old_ids).get("reused"))


class BytecodeCliTest(NoGitChecksE2ECase):
    def test_stale_force_release_and_cross_task(self):
        card = self.create_card('DEV-01')
        self.create_card('DEV-02')
        observed = self.base / 'observed'
        source = self.write('src/payments/pay.py', 'value = 1\n')
        test = self.write('tests/test_payments.py',
                          'import unittest\nfrom pathlib import Path\n'
                          'from src.payments.pay import value\n'
                          'class TestPay(unittest.TestCase):\n'
                          ' def test_ok(self):\n'
                          f'  Path({str(observed)!r}).write_text(str(value))\n'
                          '  self.assertEqual(value, 1)\n')
        policy = run_policy()
        policy['checks'] = policy['checks'][:1]
        self.write('checks-policy.json', json.dumps(policy))
        env = dict(os.environ)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.pop('PYTHONPYCACHEPREFIX', None)
        env['PYTHONPATH'] = str(self.root)
        def cli(*args):
            return subprocess.run([sys.executable, '-B', str(ROOT / 'ai-dev-playbook/scripts/playbook.py'),
                                   '--root', str(self.root), *args], env=env,
                                  text=True, capture_output=True)
        self.assertEqual(cli('setup', 'apply').returncode, 0)
        seed = subprocess.run(policy['checks'][0]['argv'], cwd=self.root, env=env,
                              capture_output=True)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        caches = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*.pyc')}
        self.assertTrue(caches)
        self.assertEqual(cli('checks', 'run', 'DEV-01', '--mode', 'force').returncode, 0)
        old = source.stat()
        source.write_text('value = 2\n')
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        for mode in ('stale', 'force'):
            result = cli('checks', 'run', 'DEV-01', '--mode', mode)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(observed.read_text(), '2')
            meta = json.loads(card.with_suffix('.meta.json').read_text())
            gate = checks_gate.evaluate(self.root, policy, task_id='DEV-01', scope='project',
                                        task_content=card.read_text(), task_meta=meta)
            self.assertFalse(gate['ok'])
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in caches}, caches)
        # A new valid source run heals the gate; cross-task/release can reuse it.
        source.write_text('value = 1\n# corrected\n')
        result = cli('checks', 'run', 'DEV-01', '--scope', 'project', '--mode', 'stale')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        old_records = checks_results.load_results(self.root)[0]
        count = len(old_records)
        old_ids = {r["result_id"] for r in old_records}
        result = cli('checks', 'run', 'DEV-02', '--scope', 'project', '--checkpoint', 'release-ready')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        records = checks_results.load_results(self.root)[0]
        self.assertEqual(len(records), count + 1)
        self.assertTrue(next(r for r in records if r["result_id"] not in old_ids).get("reused"))
