import sys, json, unittest, contextlib, io
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path.cwd()/'tests'), str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_run import run_project, run_policy, execute
import checks_runner, checks_results

class Probes(unittest.TestCase):
    def setUp(self):
        self.proj=run_project(self)
        self.policy=run_policy()
        self.policy['checks']=[c for c in self.policy['checks'] if c['id']=='payments-tests']
    def run_at(self, sec, **kw):
        return execute(self.proj,self.policy,now=f'2026-11-09T10:{sec//60:02d}:{sec%60:02d}+03:00',**kw)[0]
    def test_ttl(self):
        self.policy['environment']['external']=[{'id':'api','freshness':{'kind':'ttl','ttl_seconds':60}}]
        self.policy['checks'][0]['required_environment']=['api']
        outcomes=[self.run_at(s)['outcomes'] for s in [0,50,100,150]]
        print('TTL 60s at t=0,50,100,150:',json.dumps(outcomes))
    def test_corrupt_latest_failed(self):
        first=self.run_at(0)
        self.proj.write('tests/test_payments.py','import unittest\nclass TestPay(unittest.TestCase):\n def test_ok(self): self.fail()\n')
        bad=self.run_at(10)
        self.proj.write('tests/test_payments.py','import unittest\n\nclass TestPay(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n')
        path=self.proj.root/'work/checks/results'/bad['run_id']/'payments-tests.json'
        path.write_text('{corrupt')
        last=self.run_at(20)
        print('CORRUPT failed after passed:',first['outcomes'],bad['outcomes'],last['outcomes'])
    def test_reuse_rebinding(self):
        self.run_at(0)
        original=checks_runner._snapshot_inputs
        def mutate(*a,**kw):
            self.proj.write('tests/test_payments.py','raise RuntimeError("new broken input")\n')
            return original(*a,**kw)
        with mock.patch.object(checks_runner,'_snapshot_inputs',mutate):
            second=self.run_at(10)
        third=self.run_at(20)
        print('REUSE input changes after plan:',second['outcomes'],third['outcomes'])
    def test_status_stale(self):
        self.run_at(0)
        self.proj.write('tests/test_payments.py','raise RuntimeError("new broken input")\n')
        capture=io.StringIO()
        result=checks_runner.status(self.proj.root,self.policy,task_id='DEV-1',task_content=(self.proj.root/'docs/tasks/DEV-1.md').read_text(),task_meta={},emit=lambda s: capture.write(s+'\n'))
        print('STALE STATUS:',result,capture.getvalue())
    def test_project_import(self):
        import subprocess
        self.proj.write('tests/test_payments.py','import unittest\nimport src.payments.pay\nclass TestPay(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        direct=subprocess.run(self.policy['checks'][0]['argv'],cwd=self.proj.root,capture_output=True,text=True)
        outcome, output=execute(self.proj,self.policy)
        print('PROJECT IMPORT original argv exit:',direct.returncode,'adapter:',outcome['outcomes'],output)

unittest.main(verbosity=2)
