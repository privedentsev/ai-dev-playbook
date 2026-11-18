import sys,json,unittest,os
from pathlib import Path
from unittest import mock
sys.path[:0]=[str(Path.cwd()/'tests'),str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_run import run_project,run_policy,execute
import checks_runner,checks_results
class Probes(unittest.TestCase):
 def setUp(self):
  self.proj=run_project(self);self.policy=run_policy();self.policy['checks']=[c for c in self.policy['checks'] if c['id']=='payments-tests']
 def run_at(self,s,**kw): return execute(self.proj,self.policy,now=f'2026-11-09T10:00:{s:02d}+03:00',**kw)[0]
 def lose_failed(self):
  self.run_at(0)
  original=(self.proj.root/'tests/test_payments.py').read_text()
  self.proj.write('tests/test_payments.py','import unittest\nclass TestPay(unittest.TestCase):\n def test_ok(self): self.fail()\n')
  bad=self.run_at(10)
  self.proj.write('tests/test_payments.py',original)
  (self.proj.root/'work/checks/results'/bad['run_id']/'payments-tests.json').unlink()
 def test_direct_loss(self):
  self.lose_failed()
  result=self.run_at(20)
  again=self.run_at(30)
  print('DIRECT:',result['outcomes'],'NEXT:',again['outcomes'])
  self.assertEqual(result['outcomes']['payments-tests'],'passed')
  self.assertEqual(again['outcomes']['payments-tests'],'reused')
 def test_index_recovery(self):
  self.lose_failed()
  _,_,before=checks_results.load_results(self.proj.root)
  checks_results.rebuild_index(self.proj.root,self.policy)
  _,_,after=checks_results.load_results(self.proj.root)
  status=checks_runner.status(self.proj.root,self.policy,task_id='DEV-1',task_content=(self.proj.root/'docs/tasks/DEV-1.md').read_text(),task_meta={},emit=lambda text:None)
  result=self.run_at(20)
  print('STATUS AFTER REBUILD:',status)
  print('REBUILD: corrupt_ids before=',sorted(before),'after=',sorted(after),'outcomes=',result['outcomes'])
 def test_inapplicable_run_rebuild(self):
  self.policy['checks']=[{'id':'probe','title':'Probe','purpose':'Audit','kind':'command','modules':[],
   'argv':[sys.executable,'-c','import os,sys;sys.exit(int(os.environ.get("PB060_AUDIT_FAIL","0")))'],
   'timeout_seconds':5,'applicability':{'kind':'checkpoint','checkpoint':'release'}}]
  with mock.patch.dict(os.environ,{'PB060_AUDIT_FAIL':'0'}): first=self.run_at(0,checkpoint='release')
  with mock.patch.dict(os.environ,{'PB060_AUDIT_FAIL':'1'}): bad=self.run_at(10,checkpoint='release',mode='force')
  (self.proj.root/'work/checks/results'/bad['run_id']/'probe.json').unlink()
  _,_,before=checks_results.load_results(self.proj.root)
  skip=self.run_at(20)
  _,_,after=checks_results.load_results(self.proj.root)
  with mock.patch.dict(os.environ,{'PB060_AUDIT_FAIL':'1'}): last=self.run_at(30,checkpoint='release')
  self.assertEqual(bad['outcomes']['probe'],'failed')
  self.assertEqual(skip['outcomes']['probe'],'not_applicable')
  self.assertEqual(last['outcomes']['probe'],'reused')
  print('CHECKPOINT:',first['outcomes'],bad['outcomes'],'loss:',sorted(before),'no checkpoint:',skip['outcomes'],'after:',sorted(after),'next release:',last['outcomes'])
unittest.main(verbosity=2)
