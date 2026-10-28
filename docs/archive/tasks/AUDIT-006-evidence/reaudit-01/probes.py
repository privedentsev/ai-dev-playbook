"""Independent subprocess re-audit; only disposable projects are mutated."""
import sys,json,subprocess,hashlib
from pathlib import Path
ROOT=Path.cwd();sys.path.insert(0,str(ROOT/'tests'))
from test_close_lifecycle import CloseLifecycleCase,PromoteChangeTest,promote_request
from test_lifecycle import task_request
out=[]
def cli(c,*args):
 p=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(c.root),*args],capture_output=True,text=True)
 return {'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
def saved(c,req):
 rel=c.request(req);result=cli(c,'lifecycle','plan',rel,'--save');assert result['rc']==0,result
 line=next(l for l in result['stdout'].splitlines() if l.startswith('План записан:'))
 return line.split(':',1)[1].strip()
for reverse in [False,True]:
 c=CloseLifecycleCase();c.setUp()
 try:
  binary=bytes(range(256));ep=c.root/'docs/tasks/DEV-1-evidence/raw.bin';ep.parent.mkdir();ep.write_bytes(binary)
  meta={'schema_version':1,'relations':[],'moves':[],'materials':[{'id':'raw','role':'report','source':'docs/tasks/DEV-1-evidence/raw.bin','storage':'local','required':True,'immutable':True,'used_by':['DEV-1']}]}
  c.managed_task(meta=meta,body='\n[plan](../plans/block.md)\n');c.add_plan('block',tasks=['DEV-1']);c.add_spec('active',state='accepted');c.cli('index','--all')
  spec=(c.specs/'active.md').read_bytes();c.verify();assert c.handoff()[0]==0;assert c.accept('--result','accepted')[0]==0
  req=task_request('DEV-1');req['scope']['documents']=['plan:block'];req['actions'].append({'action':'archive_document','document':'plan:block','reason':'accepted block'})
  if reverse:req['actions'].reverse()
  result=cli(c,'lifecycle','apply',saved(c,req))
  assert result['rc']==0,result
  assert (c.root/'docs/archive/tasks/DEV-1-evidence/raw.bin').read_bytes()==binary
  assert (c.specs/'active.md').read_bytes()==spec
  assert (c.root/'docs/archive/plans/block.md').is_file()
  assert not (c.root/'docs/tasks/DEV-1.md').exists()
  check=cli(c,'check','--ready');assert check['rc']==0,check
  out.append({'scenario':'combined_close_reverse='+str(reverse),**result,'check':check,'immutable_equal':True,'active_spec_equal':True})
 finally:c.doCleanups()
for variant in ['missing','diagnostic','rejected','wrong_document','wrong_change','wrong_revision','wrong_payload','wrong_source','executor','bad_time','receipt_changed','valid']:
 c=PromoteChangeTest();c.setUp()
 try:
  c.spec_with_change();c.cli('index','--all');doc=c.specs/'alpha.md';req=promote_request('spec:alpha','chg-1',payload='Reviewed exact content\n');action=req['actions'][0]
  digest=lambda data:'sha256:'+hashlib.sha256(data).hexdigest()
  receipt={'schema_version':1,'document':'spec:alpha','change':'chg-1','revision':1,'result':'accepted','kind':'acceptance','actor':{'name':'Synthetic independent auditor','role':'auditor','model':'probe'},'at':'2026-10-22T13:30:00+03:00','document_sha256':digest(doc.read_bytes()),'payload_sha256':digest(action['payload'].encode())}
  fields={'diagnostic':('kind','diagnostic'),'rejected':('result','rejected'),'wrong_document':('document','spec:other'),'wrong_change':('change','chg-2'),'wrong_revision':('revision',2),'wrong_payload':('payload_sha256',digest(b'other')),'wrong_source':('document_sha256',digest(b'other')),'executor':('actor',{'name':'Executor','role':'executor'}),'bad_time':('at','yesterday')}
  if variant in fields:k,v=fields[variant];receipt[k]=v
  receipt_path=c.root/'work/receipt.json';receipt_path.parent.mkdir(exist_ok=True);receipt_path.write_text(json.dumps(receipt))
  if variant!='missing':action['acceptance']='work/receipt.json'
  rel=c.request(req);before=c.project_fingerprint()
  if variant in ['receipt_changed','valid']:
   plan=saved(c,req)
   if variant=='receipt_changed':receipt_path.write_text(receipt_path.read_text()+' ')
   result=cli(c,'lifecycle','apply',plan)
  else:result=cli(c,'lifecycle','plan',rel)
  expected=0 if variant=='valid' else 1;assert result['rc']==expected,result
  if expected==1:assert c.project_fingerprint()==before
  else:
   meta=c.meta_of(doc);assert meta['current_revision']==2 and meta['changes'][0]['state']=='accepted'
   companion=c.root/meta['changes'][0]['acceptance'];assert companion.is_file();assert json.loads(companion.read_text())['acceptance_record']==receipt
  out.append({'scenario':'promotion_'+variant,**result,'expected_rc':expected,'unchanged_on_refusal':expected==1})
 finally:c.doCleanups()
print(json.dumps(out,ensure_ascii=False,indent=2))
