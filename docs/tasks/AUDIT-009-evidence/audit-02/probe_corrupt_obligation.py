import sys,json,subprocess
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path.cwd()
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_select import make_policy
import checks_gate,checks_results
mode=sys.argv[2] if len(sys.argv)>2 else 'corrupt'
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root;card=case.create_card('DEV-01');case.write('src/payments/pay.py','value = 1\n')
 counter=case.base/'attempts.count'
 cmd=f"from pathlib import Path; p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(int(n>1))"
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'Corrupt history must not erase an unresolved obligation','kind':'command','modules':['payments'],'argv':[sys.executable,'-c',cmd],'timeout_seconds':5}]
 case.write('checks-policy.json',json.dumps(policy,ensure_ascii=False))
 events=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],text=True,capture_output=True)
  events.append({'argv':args,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr});return p
 def gate():
  return checks_gate.evaluate(root,policy,task_id='DEV-01',task_content=card.read_text(),task_meta=json.loads(card.with_suffix('.meta.json').read_text()))
 assert cli('setup','apply').returncode==0,events[-1]
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==0,events[-1]
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==1,events[-1]
 before=gate();assert not before['ok'],before
 results,_,_=checks_results.load_results(root);latest=checks_results.latest(results)['payment'];assert latest['status']=='failed'
 bad=root/'work/checks/results'/latest['evidence']['run_id']/'payment.json'
 if mode=='delete':bad.unlink()
 else:bad.write_text('{')
 after=gate();cli('checks','status','DEV-01')
 stale=cli('checks','run','DEV-01','--scope','task','--mode','stale')
 cli('checks','release-ready','DEV-01')
 print(json.dumps({'history_mutation':mode,'no_git':not(root/'.git').exists(),'last_real_result_before_corruption':'failed','gate_before_corruption':before,'gate_after_corruption':after,'stale_rc':stale.returncode,'attempts':int(counter.read_text()),'commands':events},ensure_ascii=False,indent=2))
finally:case.doCleanups()
