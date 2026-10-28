"""Independent AUDIT-006 probes. Run from repository root; disposable fixtures only."""
import hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path.cwd()
sys.path.insert(0,str(ROOT/'tests'))
from test_close_lifecycle import CloseLifecycleCase, PromoteChangeTest, promote_request
from test_lifecycle import task_request
OUT=[]
def cli(c,*args):
 p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(c.root),*args],capture_output=True,text=True)
 return {'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
def prepare(c):
 c.setUp()
 c.managed_task()
 c.verify(); assert c.handoff()[0]==0; assert c.accept("--result","accepted")[0]==0

def combined():
 c=CloseLifecycleCase(); c.setUp()
 try:
  c.managed_task(); c.add_plan('block',tasks=['DEV-1']); c.cli('index','--all')
  c.verify(); assert c.handoff()[0]==0; assert c.accept("--result","accepted")[0]==0
  request=task_request('DEV-1'); request['scope']['documents']=['plan:block']
  request['actions'].append({'action':'archive_document','document':'plan:block','reason':'accepted block'})
  rel=c.request(request)
  before=c.project_fingerprint(); result=cli(c,'lifecycle','plan',rel)
  OUT.append({'scenario':'combined_close_and_plan_archive','request':request,**result,'unchanged':before==c.project_fingerprint(),'expected_rc':0})
 finally:c.doCleanups()
def promotion():
 c=PromoteChangeTest();c.setUp()
 try:
  c.spec_with_change(); c.cli('index','--all')
  request=promote_request('spec:alpha','chg-1',payload='UNREVIEWED replacement\n')
  request['basis']='Executor finished implementation; independent audit has not happened'
  rel=c.request(request); result=cli(c,'lifecycle','plan',rel,'--save')
  if result['rc']==0:
   plan=next((c.root/'work/lifecycle/plans').glob('*.json'))
   result=cli(c,'lifecycle','apply',str(plan.relative_to(c.root)))
  OUT.append({'scenario':'promotion_without_acceptance','request':request,**result,'result_metadata':c.meta_of(c.specs/'alpha.md'),'expected_rc':1})
 finally:c.doCleanups()
def stale():
 c=CloseLifecycleCase();prepare(c)
 try:
  (c.root/'docs/new-input.md').write_text('changed after acceptance\n')
  before=c.project_fingerprint();result=cli(c,'close','DEV-1')
  OUT.append({'scenario':'stale_input_close',**result,'unchanged':before==c.project_fingerprint(),'expected_rc':1})
 finally:c.doCleanups()
def missing():
 c=CloseLifecycleCase();prepare(c)
 try:
  for p in (c.root/'work/verification').rglob('report.json'):p.unlink()
  before=c.project_fingerprint();result=cli(c,'close','DEV-1')
  OUT.append({'scenario':'missing_required_report',**result,'unchanged':before==c.project_fingerprint(),'expected_rc':1})
 finally:c.doCleanups()
for probe in [combined,promotion,stale,missing]:probe()
print(json.dumps(OUT,ensure_ascii=False,indent=2))
