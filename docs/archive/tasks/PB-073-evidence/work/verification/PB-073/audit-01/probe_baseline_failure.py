import sys,json,subprocess
from pathlib import Path
ROOT=Path.cwd()
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_select import make_policy
import checks_gate
case=NoGitChecksE2ECase();case.setUp()
try:
 root=case.root; card=case.create_card('DEV-01');case.write('src/payments/pay.py','value=1\n')
 counter=case.base/'count'
 command=f"from pathlib import Path; p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(0 if n==1 else 1)"
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'Must pass','kind':'command','modules':['payments'],'argv':[sys.executable,'-c',command],'timeout_seconds':5}]
 case.write('checks-policy.json',json.dumps(policy));events=[]
 def cli(*args):
  p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],capture_output=True,text=True)
  events.append({'argv':args,'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr});return p
 assert cli('setup','apply').returncode==0,events
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==0,events
 assert cli('checks','run','DEV-01','--scope','project','--mode','force').returncode==1,events
 p=cli('checks','run','DEV-01','--scope','task','--mode','stale')
 meta=json.loads(card.with_suffix('.meta.json').read_text())
 gate=checks_gate.evaluate(root,policy,task_id='DEV-01',task_content=card.read_text(),task_meta=meta)
 print(json.dumps({'no_git':not(root/'.git').exists(),'attempts':int(counter.read_text()),'stale_rc':p.returncode,'gate':gate,'commands':events},ensure_ascii=False,indent=2))
finally:case.doCleanups()
