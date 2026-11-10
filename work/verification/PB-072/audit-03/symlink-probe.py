import sys,subprocess
sys.path.insert(0,'/Users/privedentsev1/Dev/ai-dev-playbook/tests')
from test_material_relocation import CollisionCase
c=CollisionCase();c.setUp()
try:
 c.managed_task()
 (c.tasks_dir()/'loose').mkdir();(c.tasks_dir()/'loose/note.txt').write_text('n')
 target=c.archive_dir()/'real';target.mkdir(parents=True)
 (c.archive_dir()/'loose').symlink_to(target,target_is_directory=True)
 script=c.root/'ai-dev-playbook/scripts/playbook.py'
 for args in [('lifecycle','register-material','DEV-1','docs/tasks/loose/note.txt','--id','note','--role','report','--required','--immutable','--apply'),('verify','DEV-1','--preflight')]:
  p=subprocess.run([sys.executable,str(script),*args],cwd=c.root,text=True,capture_output=True)
  print(args, p.returncode,p.stdout,p.stderr,flush=True)
finally:c.doCleanups()
