"""Positive venv execution, legal reuse, and pyvenv/PYTHONPATH invalidation."""
import json, os, subprocess, sys, venv
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_run import run_policy
import checks_results
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root;case.create_card('DEV-01');case.create_card('DEV-02')
 vp=case.base/'runtime-venv';venv.EnvBuilder(with_pip=False,symlinks=True).create(vp)
 exe=vp/'bin/python3';observed=case.base/'observed.json'
 case.write('tests/test_payments.py',f'''import unittest,sys,json
from pathlib import Path
class TestPay(unittest.TestCase):
 def test_ok(self):
  p=Path({str(observed)!r}); data=json.loads(p.read_text()) if p.exists() else []
  data.append({{'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix}})
  p.write_text(json.dumps(data))
  self.assertEqual(sys.prefix,{str(vp)!r})
''')
 policy=run_policy();policy['checks']=policy['checks'][:1];policy['checks'][0]['argv'][0]=str(exe)
 case.write('checks-policy.json',json.dumps(policy))
 env=dict(os.environ); events=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],env=env,text=True,capture_output=True)
  events.append({'argv':list(args),'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
  assert p.returncode==0,events[-1]
  return p
 def run(task='DEV-01',mode='stale',release=False):
  extra=['--checkpoint','release-ready'] if release else []
  p=cli('checks','run',task,'--scope','project','--mode',mode,*extra)
  records=checks_results.load_results(root)[0]
  latest=max(records,key=lambda r:r['evidence']['run_seq'])
  return {'rc':p.returncode,'reused':bool(latest.get('reused')),'status':latest['status'],'executions':len(json.loads(observed.read_text())),'binding':latest['binding']}
 cli('setup','apply');steps=[]
 steps.append(run(mode='force'));steps.append(run());assert [x['executions'] for x in steps]==[1,1] and steps[-1]['reused']
 cfg=vp/'pyvenv.cfg';stat=cfg.stat();before=cfg.read_bytes();after=before.replace(b'include-system-site-packages = false',b'include-system-site-packages = true ')
 assert len(after)==len(before) and after!=before
 cfg.write_bytes(after);os.utime(cfg,ns=(stat.st_atime_ns,stat.st_mtime_ns))
 steps.append(run());assert steps[-1]['executions']==2 and not steps[-1]['reused']
 env['PYTHONPATH']=str(case.base/'new-empty-path')
 steps.append(run());assert steps[-1]['executions']==3 and not steps[-1]['reused']
 steps.append(run(task='DEV-02',release=True));assert steps[-1]['executions']==3 and steps[-1]['reused']
 print(json.dumps({'fixture':'No-Git project, stdlib venv, real CLI. Test only records process observations. No history/outcome edits or mocks. pyvenv.cfg changes preserve size/mtime_ns.','steps':steps,'observations':json.loads(observed.read_text()),'commands':events},ensure_ascii=False,indent=2))
finally:case.doCleanups()
