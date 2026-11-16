import sys,tempfile,json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path.cwd()
sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_contract,checks_runner,checks_gate,checks_results
from test_checks_select import make_policy
with tempfile.TemporaryDirectory(prefix='audit009-clock-') as folder:
 base=Path(folder);root=base/'project';(root/'src/payments').mkdir(parents=True)
 (root/'src/payments/pay.py').write_text('value = 1\n')
 counter=base/'counter'
 allow=base/'allow'
 cmd=f"from pathlib import Path; p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(int(n>1 and not Path({str(allow)!r}).exists()))"
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'No old pass after failure','kind':'command','modules':['payments'],'argv':[sys.executable,'-c',cmd],'timeout_seconds':5}]
 checks_contract.validate_policy(policy,lambda c,m: None if c else (_ for _ in ()).throw(ValueError(m)))
 now='2026-11-16T03:00:00.000000+00:00'
 outcomes=[]
 order=['0'*32,'f'*32] if len(sys.argv)>2 and sys.argv[2]=='forward' else ['f'*32,'0'*32]
 for ident in order:
  with patch.object(checks_runner.uuid,'uuid4',return_value=SimpleNamespace(hex=ident)):
   outcome=checks_runner.execute(root,policy,task_id='DEV-1',scope='project',mode='force',checkpoint=None,task_content='',task_meta={},now=now,emit=lambda s:None)
  outcomes.append({'ok':outcome['ok'],'summary':outcome['summary']})
 before=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='project',task_content='',task_meta={},now=now)
 # Legacy records from before PB-073 have no evidence.run_seq.
 for path in (root/'work/checks/results').rglob('*.json'):
  legacy=json.loads(path.read_text());legacy['evidence'].pop('run_seq',None);path.write_text(json.dumps(legacy))
 (root/'work/checks/results-index.json').unlink()
 # Reproduce a valid old derived index via the unchanged writer.
 docs=[json.loads(p.read_text()) for p in (root/'work/checks/results').rglob('*.json')]
 if len(sys.argv)>2 and sys.argv[2]=='clean':
  checks_results.rebuild_index(root,policy)
 else:
  for doc in sorted(docs,key=lambda d:d['status']!='passed'):
   checks_results.update_index(root,doc['evidence']['run_id'],doc)
 index_before=json.loads((root/'work/checks/results-index.json').read_text())
 before_loss=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='project',task_content='',task_meta={},now=now)
 checks_results.rebuild_index(root,policy)
 index_after_rebuild=json.loads((root/'work/checks/results-index.json').read_text())
 for path in (root/'work/checks/results').rglob('*.json'):
  if json.loads(path.read_text())['status']=='failed':path.unlink()
 after_loss=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='project',task_content='',task_meta={},now=now)
 task_after_loss=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='task',task_content='',task_meta={},now=now)
 stale=checks_runner.execute(root,policy,task_id='DEV-1',scope='task',mode='stale',checkpoint=None,task_content='',task_meta={},now=now,emit=lambda s:None)
 attempts_after_stale=int(counter.read_text())
 allow.write_text('yes')
 recovery=checks_runner.execute(root,policy,task_id='DEV-1',scope='project',mode='force',checkpoint=None,task_content='',task_meta={},now=now,emit=lambda s:None)
 recovered_gate=checks_gate.evaluate(root,policy,task_id='DEV-1',scope='project',task_content='',task_meta={},now=now)
 print(json.dumps({'uuid_order':order,'attempts_after_stale':attempts_after_stale,'recovery_ok':recovery['ok'],'recovered_gate':recovered_gate,'task_after_loss':task_after_loss,'stale':stale,'index_before':index_before,'index_after_rebuild':index_after_rebuild,'gate_before_loss':before_loss,'gate_after_loss':after_loss,'attempts':int(counter.read_text())},ensure_ascii=False,indent=2))
