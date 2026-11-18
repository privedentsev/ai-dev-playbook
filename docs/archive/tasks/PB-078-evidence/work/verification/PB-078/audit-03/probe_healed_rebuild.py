import sys,tempfile,json
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve();sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_runner,checks_results,checks_gate
from test_checks_select import make_policy
with tempfile.TemporaryDirectory() as d:
 base=Path(d);root=base/'project';(root/'src/payments').mkdir(parents=True);(root/'src/payments/pay.py').write_text('value=1\n'); counter=base/'counter'
 code=f"from pathlib import Path;p=Path({str(counter)!r});n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));raise SystemExit(int(n<3))"
 p=make_policy();p['checks']=[dict(id='payment',title='probe',purpose='loss of blocker',kind='command',modules=['payments'],applicability='always',argv=[sys.executable,'-c',code],timeout_seconds=5)]
 def run(stamp,mode='force'):return checks_runner.execute(root,p,task_id='DEV-1',scope='project',mode=mode,checkpoint=None,task_content='',task_meta={},now=stamp,emit=lambda s:None)
 runs=[run('2026-11-17T12:00:00+00:00'),run('2026-11-17T10:00:00+00:00'),run('2026-11-17T11:00:00+00:00')]
 def state():
  records,notes,corrupt=checks_results.load_results(root)
  return dict(selected={k:dict(status=v['status'],seq=v['evidence']['run_seq']) for k,v in checks_results.latest(records).items()},corrupt=sorted(corrupt),gate_ok=checks_gate.evaluate(root,p,task_id='DEV-1',scope='project',task_content='',task_meta={})['ok'],notes=notes)
 before=state();index=checks_results.index_entries(root)
 lost=checks_results.results_dir(root)/runs[0]['run_id']/'payment.json';lost.unlink()
 after=state();checks_results.rebuild_index(root,p);rebuilt=state();stale=run('2026-11-17T12:00:01+00:00','stale');healed=state();before_rebuild=checks_results.index_entries(root);checks_results.rebuild_index(root,p);after_index=checks_results.index_entries(root);rollback=run('2026-11-17T11:30:00+00:00');final=state()
 print(json.dumps(dict(runs=[dict(status=o['results'][0]['status'],seq=o['results'][0]['evidence']['run_seq'],finished_at=o['results'][0]['finished_at']) for o in runs],before=before,index=index,after_loss=after,after_rebuild=rebuilt,healed=healed,before_rebuild=before_rebuild,after_index=after_index,final=final,stale=stale['summary'],attempts=int(counter.read_text())),indent=2))
