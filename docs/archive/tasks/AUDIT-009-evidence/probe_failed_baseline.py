import sys,tempfile,json
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_contract,checks_runner,checks_gate
from test_checks_select import make_policy
with tempfile.TemporaryDirectory(prefix='audit009-failed-') as folder:
 root=Path(folder)
 (root/'src/payments').mkdir(parents=True)
 (root/'src/payments/pay.py').write_text('value = 1\n')
 policy=make_policy()
 policy['checks']=[{'id':'payment','title':'Payment','purpose':'Must pass','kind':'command','modules':['payments'],'argv':[sys.executable,'-c','raise SystemExit(1)'],'timeout_seconds':5}]
 checks_contract.validate_policy(policy,lambda c,m: None if c else (_ for _ in ()).throw(ValueError(m)))
 first=checks_runner.execute(root,policy,task_id='DEV-1',scope='task',mode='stale',checkpoint=None,task_content='',task_meta={},emit=lambda s:None)
 gate=checks_gate.evaluate(root,policy,task_id='DEV-1',task_content='',task_meta={})
 second=checks_runner.execute(root,policy,task_id='DEV-1',scope='task',mode='stale',checkpoint=None,task_content='',task_meta={},emit=lambda s:None)
 print(json.dumps({'first_ok':first['ok'],'first_summary':first['summary'],'gate_after_failure':gate,'second_ok':second['ok'],'second_summary':second['summary']},ensure_ascii=False,indent=2))
