import sys,subprocess,json,hashlib
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_select import make_policy
import checks_gate
case=NoGitChecksE2ECase(); case.setUp()
try:
 root=case.root
 card=case.create_card('DEV-01')
 case.write('src/payments/pay.py','value = 1\n')
 counter=case.base/'attempts.count'
 command=f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); raise SystemExit(1)"
 policy=make_policy()
 policy['checks']=[{'id':'payment','title':'Payment','purpose':'Must pass','kind':'command','modules':['payments'],'argv':[sys.executable,'-c',command],'timeout_seconds':5}]
 case.write('checks-policy.json',json.dumps(policy,ensure_ascii=False))
 events=[]
 def cli(*args):
  r=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),*args],capture_output=True,text=True)
  events.append({'argv':args,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
  return r
 assert cli('setup','apply').returncode==0,events[-1]
 assert cli('checks','run','DEV-01','--scope','task','--mode','stale').returncode==1,events[-1]
 cli('checks','status','DEV-01')
 assert cli('checks','run','DEV-01','--scope','task','--mode','stale').returncode==0,events[-1]
 gate=checks_gate.evaluate(root,policy,task_id='DEV-01',task_content=card.read_text(),task_meta=json.loads(card.with_suffix('.meta.json').read_text()))
 cli('checks','release-ready','DEV-01')
 print(json.dumps({'no_git':not (root/'.git').exists(),'attempts':int(counter.read_text()),'gate_after_failed_run':gate,'commands':events},ensure_ascii=False,indent=2))
finally:
 case.doCleanups()
