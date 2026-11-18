import sys,json,subprocess
from pathlib import Path
ROOT=Path.cwd()
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_select import make_policy
import checks_gate, checks_results,checks_select
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root;card=case.create_card('DEV-01');case.write('src/payments/pay.py','value=1\n')
 exe=case.base/'tool';exe.write_text('#!/bin/sh\nexit 0\n');exe.chmod(0o755)
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'Must pass','kind':'command','modules':['payments'],'argv':[str(exe)],'timeout_seconds':5}]
 case.write('checks-policy.json',json.dumps(policy));events=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],capture_output=True,text=True)
  events.append({'argv':args,'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr});return p
 assert cli('setup','apply').returncode==0,events
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==0,events
 exe.unlink()
 failed=cli('checks','run','DEV-01','--scope','project','--mode','force')
 stale=cli('checks','run','DEV-01','--scope','task','--mode','stale')
 meta=json.loads(card.with_suffix('.meta.json').read_text())
 gate=checks_gate.evaluate(root,policy,task_id='DEV-01',task_content=card.read_text(),task_meta=meta)
 results,notes,corrupt=checks_results.load_results(root)
 print(json.dumps({'no_git':not(root/'.git').exists(),'failed_rc':failed.returncode,'stale_rc':stale.returncode,'gate':gate,'latest':checks_select.evidence_latest(results),'commands':events},ensure_ascii=False,indent=2))
finally:case.doCleanups()
