import sys,tempfile,json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_contract,checks_runner,checks_gate,checks_results
from test_checks_select import make_policy
with tempfile.TemporaryDirectory(prefix='audit009-clock-') as folder:
 base=Path(folder);root=base/'project';(root/'src/payments').mkdir(parents=True)
 (root/'src/payments/pay.py').write_text('value = 1\n')
 counter=base/'counter'
 cmd=f"from pathlib import Path; p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(int(n>1))"
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'No old pass after failure','kind':'command','modules':['payments'],'applicability':'always','argv':[sys.executable,'-c',cmd],'timeout_seconds':5}]
 checks_contract.validate_policy(policy,lambda c,m: None if c else (_ for _ in ()).throw(ValueError(m)))
 now='2026-11-16T03:00:00.000000+00:00'
 outcomes=[]
 for ident in ['0'*32,'f'*32]:
  with patch.object(checks_runner.uuid,'uuid4',return_value=SimpleNamespace(hex=ident)):
   outcome=checks_runner.execute(root,policy,task_id='DEV-1',scope='project',mode='force',checkpoint=None,task_content='',task_meta={},now=now,emit=lambda s:None)
  outcomes.append({'ok':outcome['ok'],'summary':outcome['summary']})
 gate=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='project',task_content='',task_meta={},now=now)
 results,notes,corrupt=checks_results.load_results(root)
 print(json.dumps({'clock':now,'controlled_run_ids':['0'*32,'f'*32],'actual_attempts':int(counter.read_text()),'runs_in_execution_order':outcomes,'latest_selected':{k:{'status':v['status'],'result_id':v['result_id']} for k,v in checks_results.latest(results).items()},'gate_after_later_failure':gate,'notes':notes,'corrupt':sorted(corrupt)},ensure_ascii=False,indent=2))
