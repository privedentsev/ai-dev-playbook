import sys,json,subprocess
sys.path.insert(0, '/Users/privedentsev1/Dev/ai-dev-playbook/tests')
from test_material_relocation import CollisionCase,lifecycle,playbook

def cli(c,*args):
 p=subprocess.run([sys.executable,str(c.root/'ai-dev-playbook/scripts/playbook.py'),'--root',str(c.root),*args],text=True,capture_output=True)
 return p.returncode,p.stdout,p.stderr

c=CollisionCase();c.setUp()
try:
 c.managed_task()
 ev=c.tasks_dir()/'DEV-1-evidence';ev.mkdir(exist_ok=True)
 (ev/'docs').write_text('file at future ancestor')
 (c.root/'docs/misc').mkdir(parents=True)
 (c.root/'docs/misc/note.txt').write_text('child')
 for src, ident in [('docs/tasks/DEV-1-evidence/docs','ancestor'),('docs/misc/note.txt','child')]:
  print('REGISTER',ident,cli(c,'lifecycle','register-material','DEV-1',src,'--id',ident,'--role','report','--immutable','--required','--apply'))
 print('PREFLIGHT',cli(c,'verify','DEV-1','--preflight'))
 print('VERIFY',cli(c,'verify','DEV-1'))
 print('HANDOFF',cli(c,'lifecycle','handoff','DEV-1','--summary','ready','--actor','Devin','--model','SWE-2 High'))
 print('ACCEPT',cli(c,'lifecycle','accept','DEV-1','--result','accepted','--actor','Auditor','--model','SWE-2 High'))
 # Bring state to in_review without changing requirements in legacy projection.
 # State already in_review
 print('INDEX',cli(c,'index'))
 print('CLOSE',cli(c,'close','DEV-1','--dry-run'))
finally:c.doCleanups()

c=CollisionCase();c.setUp()
try:
 c.collision_state()
 card=c.tasks_dir()/'DEV-1.md';meta=c.meta_of(card)
 meta['materials'].append({'id':'whole-evidence','role':'other','source':'docs/tasks/DEV-1-evidence','storage':'local','required':True,'immutable':True,'used_by':['DEV-1']})
 c.inject_materials(meta['materials'])
 print('RELOCATE_ROOT_DIRECTORY',cli(c,'lifecycle','relocate-material','DEV-1','--id','manual-copy','--to','docs/tasks/DEV-1-evidence/manual/copy.json','--reason','resolve','--actor','Auditor','--model','GPT-6'))
finally:c.doCleanups()
