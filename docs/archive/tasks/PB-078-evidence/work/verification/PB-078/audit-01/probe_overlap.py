import sys,tempfile,json,os,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(sys.argv[1]).resolve();sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
import checks_runner,checks_results,checks_gate
from test_checks_select import make_policy
with tempfile.TemporaryDirectory() as d:
 base=Path(d);root=base/'project';(root/'src/payments').mkdir(parents=True);(root/'src/payments/pay.py').write_text('value=1\n')
 ready=base/'ready';release=base/'release'
 code=f'''import os,time
from pathlib import Path
if os.environ.get('AUDIT_CASE')=='A':
 Path({str(ready)!r}).touch()
 while not Path({str(release)!r}).exists(): time.sleep(.02)
 raise SystemExit(1)
'''
 p=make_policy();common=dict(kind='command',modules=['payments'],applicability='always',timeout_seconds=15,title='probe',purpose='overlap probe')
 p['checks']=[dict(common,id='first',argv=[sys.executable,'-c','pass']),dict(common,id='payment',argv=[sys.executable,'-c',code])]
 def run(case,mode='force'):
  return checks_runner.execute(root,p,task_id='DEV-1',scope='project',mode=mode,checkpoint=None,task_content='',task_meta={},env=dict(os.environ,AUDIT_CASE=case),emit=lambda s:None)
 with ThreadPoolExecutor() as pool:
  a=pool.submit(run,'A')
  deadline=time.monotonic()+10
  while not ready.exists():
   assert time.monotonic()<deadline
   time.sleep(.02)
  b=run('B');release.touch();a=a.result()
 records,notes,corrupt=checks_results.load_results(root)
 gate=checks_gate.evaluate(root,p,task_id='DEV-1',scope='project',task_content='',task_meta={})
 stale=run('B','stale')
 def brief(o):return [{'id':r['check_id'],'status':r['status'],'seq':r['evidence'].get('run_seq'),'finished_at':r['finished_at']} for r in o['results']]
 print(json.dumps(dict(A=brief(a),B=brief(b),selected={k:{'status':v['status'],'seq':v['evidence'].get('run_seq')} for k,v in checks_results.latest(records).items()},gate=gate,stale=stale['summary'],corrupt=sorted(corrupt)),indent=2))
