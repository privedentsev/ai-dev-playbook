import sys,subprocess
from pathlib import Path
sys.path.insert(0,'/Users/privedentsev1/Dev/ai-dev-playbook/tests')
from test_material_relocation import CollisionCase

def cli(c,*args):
 p=subprocess.run([sys.executable,str(c.root/'ai-dev-playbook/scripts/playbook.py'),'--root',str(c.root),*args],capture_output=True,text=True)
 return p.returncode,p.stdout,p.stderr
c=CollisionCase();c.setUp()
try:
 c.managed_task()
 d=c.tasks_dir()/'loose';d.mkdir();(d/'note.txt').write_text('material')
 c.archive_dir().mkdir(parents=True,exist_ok=True)
 (c.archive_dir()/'loose').write_text('existing archive file')
 for args in [
 ('lifecycle','register-material','DEV-1','docs/tasks/loose/note.txt','--id','note','--role','report','--immutable','--required','--apply'),
 ('verify','DEV-1','--preflight'),
 ('verify','DEV-1'),
 ('lifecycle','handoff','DEV-1','--summary','ready','--actor','Devin','--model','SWE-2 High'),
 ('lifecycle','accept','DEV-1','--result','accepted','--actor','Auditor','--model','GPT-6'),
 ('close','DEV-1','--dry-run')]:
  print('COMMAND',args,flush=True);print('RESULT',cli(c,*args),flush=True)
 print('EXISTING_ARCHIVE_BYTES',(c.archive_dir()/'loose').read_text(),flush=True)
finally:c.doCleanups()
