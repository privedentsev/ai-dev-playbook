"""Compare the approved venv interpreter with the actual unittest adapter process."""
import json, os, subprocess, sys, venv
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_run import run_policy
import checks_gate, checks_results
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root; card=case.create_card('DEV-01')
 env_dir=case.base/'approved-venv'
 venv.EnvBuilder(with_pip=False,symlinks=True).create(env_dir)
 interpreter=env_dir/'bin/python3'
 observed=case.base/'observed.json'
 case.write('tests/test_payments.py',f"""import unittest, sys, json
from pathlib import Path
class TestPay(unittest.TestCase):
 def test_ok(self):
  Path({str(observed)!r}).write_text(json.dumps({{'executable':sys.executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix}}))
  self.assertEqual(sys.prefix, sys.base_prefix)
""")
 policy=run_policy();policy['checks']=policy['checks'][:1]
 policy['checks'][0]['argv'][0]=str(interpreter)
 case.write('checks-policy.json',json.dumps(policy))
 events=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],text=True,capture_output=True)
  events.append({'argv':list(args),'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
  return p
 assert cli('setup','apply').returncode==0,events[-1]
 direct=subprocess.run(policy['checks'][0]['argv'],cwd=root,text=True,capture_output=True)
 direct_observed=json.loads(observed.read_text())
 observed.unlink()
 run=cli('checks','run','DEV-01','--scope','project','--mode','force')
 adapter_observed=json.loads(observed.read_text()) if observed.exists() else None
 records=checks_results.load_results(root)[0]
 meta=json.loads(card.with_suffix('.meta.json').read_text())
 gate=checks_gate.evaluate(root,policy,task_id='DEV-01',scope='project',task_content=card.read_text(),task_meta=meta)
 control=subprocess.run([str(interpreter),str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),'checks','run','DEV-01','--scope','project','--mode','force'],text=True,capture_output=True)
 control_observed=json.loads(observed.read_text()) if observed.exists() else None
 control_gate=checks_gate.evaluate(root,policy,task_id='DEV-01',scope='project',task_content=card.read_text(),task_meta=meta)
 print(json.dumps({'fixture':'Temporary no-Git project and stdlib venv without pip; real setup/apply, actual approved interpreter command and CLI. No mocks, history/outcome edits or missing executable. Test records Python runtime and compares sys.prefix with base_prefix.','approved_argv':policy['checks'][0]['argv'],'direct_rc':direct.returncode,'direct_stdout':direct.stdout,'direct_stderr':direct.stderr,'direct_observed':direct_observed,'adapter_rc':run.returncode,'adapter_observed':adapter_observed,'results':records,'gate':gate,'commands':events,'venv_parent_control':{'rc':control.returncode,'stdout':control.stdout,'stderr':control.stderr,'observed':control_observed,'gate':control_gate}},indent=2,ensure_ascii=False))
finally:case.doCleanups()
