import sys,unittest,copy,json,io,contextlib
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'tests'), str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_run import run_project,run_policy,execute
import checks_runner, checks_results
class Probes(unittest.TestCase):
 def setUp(self):
  self.proj=run_project(self); self.policy=run_policy(); self.policy['checks']=[c for c in self.policy['checks'] if c['id']=='payments-tests']
 def run_at(self,s):
  return execute(self.proj,self.policy,now=f'2026-11-09T10:00:{s:02d}+03:00')[0]
 def test_deleted_latest_failed(self):
  first=self.run_at(0)
  original=(self.proj.root/'tests/test_payments.py').read_text()
  self.proj.write('tests/test_payments.py','import unittest\nclass TestPay(unittest.TestCase):\n def test_ok(self): self.fail()\n')
  bad=self.run_at(10)
  self.proj.write('tests/test_payments.py',original)
  path=self.proj.root/'work/checks/results'/bad['run_id']/'payments-tests.json'
  index=json.loads((self.proj.root/'work/checks/results-index.json').read_text())
  print('INDEX before loss:',index['entries']['payments-tests'])
  path.unlink()
  last=self.run_at(20)
  print('DELETED FAILED:',first['outcomes'],bad['outcomes'],last['outcomes'])
  assert last['outcomes']['payments-tests']=='reused'
unittest.main(verbosity=2)
