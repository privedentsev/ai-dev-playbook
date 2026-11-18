"""Real CLI: a source hash changes, but a timestamp-valid normal .pyc executes old code."""
import sys,os,json,subprocess,hashlib
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve();sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_run import run_policy
import checks_gate,checks_results
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root;card=case.create_card('DEV-01');observed=case.base/'observed.json'
 source=case.write('src/payments/pay.py','value = 1\n');before=source.stat();old_sha=hashlib.sha256(source.read_bytes()).hexdigest()
 case.write('tests/test_payments.py',f'''import unittest,json
from pathlib import Path
from src.payments.pay import value
class TestPay(unittest.TestCase):
 def test_ok(self):
  p=Path({str(observed)!r})
  p.write_text(str(value))
  self.assertEqual(value, 1)
''')
 policy=run_policy();policy['checks']=[policy['checks'][0]];case.write('checks-policy.json',json.dumps(policy,ensure_ascii=False))
 env=dict(os.environ);env.pop('PYTHONDONTWRITEBYTECODE',None);env.pop('PYTHONPYCACHEPREFIX',None)
 events=[];values=[];runs=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],env=env,text=True,capture_output=True)
  events.append({'argv':list(args),'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
  if args[:2]==('checks','run') and observed.exists():
   values.append(int(observed.read_text()))
   records,_,_=checks_results.load_results(root)
   last=max(records,key=lambda r:r.get('evidence',{}).get('run_seq',-1))
   runs.append({'status':last['status'],'reused':last.get('reused'),'input_digest':last['binding']['input_digest']})
  return p
 assert cli('setup','apply').returncode==0,events[-1]
 seed=subprocess.run(policy['checks'][0]['argv'],cwd=root,env=dict(env,PYTHONPATH=str(root)),text=True,capture_output=True)
 assert seed.returncode==0,seed.stderr
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==0,events[-1]
 caches=list((source.parent/'__pycache__').glob('pay*.pyc'));assert caches,'real first run did not generate cache'
 source.write_text('value = 2\n');os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns));after=source.stat();new_sha=hashlib.sha256(source.read_bytes()).hexdigest()
 cli('checks','plan','DEV-01')
 if len(sys.argv)>2 and sys.argv[2]=='dont-write':
  env['PYTHONDONTWRITEBYTECODE']='1';os.environ['PYTHONDONTWRITEBYTECODE']='1'
 second=cli('checks','run','DEV-01','--scope','task','--mode','stale')
 cli('checks','status','DEV-01')
 meta=json.loads(card.with_suffix('.meta.json').read_text())
 gate=checks_gate.evaluate(root,policy,task_id='DEV-01',scope='project',task_content=card.read_text(),task_meta=meta)
 before_control=list(values)
 # Retain all natural user caches; repeat force on identical inputs.
 control=cli('checks','run','DEV-01','--scope','project','--mode','force')
 print(json.dumps({'fixture':'No-Git temporary project, real setup/apply and CLI subprocesses. No mock or bytecode editing. A normal unittest seed generates standard cache before the isolated adapter; source changes preserve size/mtime; control retains all temporary cache files. Baseline uses normal cache env; optional dont-write sets PYTHONDONTWRITEBYTECODE=1 for subsequent runs without clearing the existing cache.','variant':sys.argv[2] if len(sys.argv)>2 else 'normal','no_git':not(root/'.git').exists(),'cache_files':[str(p.relative_to(root)) for p in caches],'same_size':before.st_size==after.st_size,'same_mtime_ns':before.st_mtime_ns==after.st_mtime_ns,'old_sha256':old_sha,'new_sha256':new_sha,'source_after_edit':source.read_text(),'second_rc':second.returncode,'gate_before_control':gate,'observed_before_control':before_control,'control_rc':control.returncode,'observed_all':values,'results_in_order':runs,'commands':events},ensure_ascii=False,indent=2))
finally:case.doCleanups()
