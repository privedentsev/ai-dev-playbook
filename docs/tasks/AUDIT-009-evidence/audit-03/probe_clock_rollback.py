"""Controlled one-second wall-clock rollback; native history, real subprocess checks."""
import sys, json, tempfile, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
ROOT=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_runner, checks_gate, checks_results
from test_checks_select import make_policy
with tempfile.TemporaryDirectory(prefix='audit009-rollback-') as folder:
 base=Path(folder);root=base/'project';(root/'src/payments').mkdir(parents=True)
 (root/'src/payments/pay.py').write_text('value = 1\n')
 counter=base/'counter'
 cmd=f"from pathlib import Path; p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); raise SystemExit(int(n>1))"
 policy=make_policy();policy['checks']=[{'id':'payment','title':'Payment','purpose':'Later failure must block readiness despite wall-clock rollback','kind':'command','modules':['payments'],'applicability':'always','argv':[sys.executable,'-c',cmd],'timeout_seconds':5}]
 first=datetime.now(timezone.utc);times=[first.isoformat(),(first-timedelta(seconds=1)).isoformat()]
 runs=[]
 for stamp in times:
  o=checks_runner.execute(root,policy,task_id='DEV-1',scope='project',mode='force',checkpoint=None,task_content='',task_meta={},now=stamp,emit=lambda s:None)
  runs.append({'ok':o['ok'],'summary':o['summary'],'records':o['results']})
 results,notes,corrupt=checks_results.load_results(root)
 gates={scope:checks_gate.evaluate(root,policy,task_id='DEV-1',scope=scope,task_content='',task_meta={}) for scope in ['task','project']}
 stale=checks_runner.execute(root,policy,task_id='DEV-1',scope='task',mode='stale',checkpoint=None,task_content='',task_meta={},emit=lambda s:None)
 print(json.dumps({'controlled_clocks':times,'fixture':'Native records with run_seq; only wall clock is controlled. Commands execute real subprocesses. No history edits or UUID mocks.','no_git':not(root/'.git').exists(),'runs_in_execution_order':runs,'gates_after_failure':gates,'selected_after_failure':{k:{'status':v['status'],'run_seq':v['evidence']['run_seq']} for k,v in checks_results.latest(results).items()},'stale':{'ok':stale['ok'],'summary':stale['summary']},'actual_attempts':int(counter.read_text()),'notes':notes,'corrupt':sorted(corrupt)},ensure_ascii=False,indent=2))
