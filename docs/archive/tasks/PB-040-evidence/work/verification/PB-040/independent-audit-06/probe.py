import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import record,cards
from test_lifecycle import task_request
c=CriterionCliTest();c.setUp()
try:
 c.setup_card();c.path.write_text(c.path.read_text().replace('Локальный проект.','Локальный проект. [Config](../../ai-dev-playbook/project.json)'))
 ev=c.tasks_dir()/'DEV-1-evidence';ev.mkdir();(ev/'proof.txt').write_bytes(b'immutable evidence\n')
 assert c.cli('lifecycle','register-material','DEV-1','docs/tasks/DEV-1-evidence','--id','proof','--role','report','--immutable','--required','--apply')[0]==0
 for i in (1,2):
  if i==2:
   c.path.write_text(c.path.read_text().replace('Status: in_progress','Status: in_review'))
   assert c.cli('index')[0]==0
   c.append(record('EXEC',2));c.append(record('CHECK',2))
  c.verify();c.append(record('HANDOFF',i,**{'Выполнение':f'EXEC-{i:02d}','Проверки':f'CHECK-{i:02d}'}));assert c.handoff()[0]==0
  c.append(record('AUDIT',i,actor='Auditor',**{'Передача':f'HANDOFF-{i:02d}'}));assert c.accept('--result','accepted')[0]==0
  meta=cards.metadata(c.path.read_text()); hashes={m['id']:hashlib.sha256((c.root/m['source']/'report.json').read_bytes()).hexdigest() for m in meta['materials'] if m['id'].startswith('verify-')}
  for args in [('close','DEV-1','--dry-run'),('close','DEV-1'),('check','--ready')]:
   result=c.cli(*args);assert result[0]==0,result
  v=c.view();assert v['acceptance_state']=='historical' and v['criterion_states']['AC-01']['checked']
  archive=c.archive_dir()/'DEV-1.md';meta=cards.metadata(archive.read_text())
  for m in meta['materials']:
   if m['id'] in hashes:assert hashlib.sha256((c.root/m['source']/'report.json').read_bytes()).hexdigest()==hashes[m['id']]
  print('PASS close cycle',i,'historical checkboxes and report bytes preserved',flush=True)
  if i==1:
   result=c.apply_request(task_request('DEV-1','reopen'));assert result[0]==0,result
   assert c.cli('check','--ready')[0]==0
   assert (ev/'proof.txt').read_bytes()==b'immutable evidence\n'
   assert not c.view()['criterion_states']['AC-01']['checked']
   print('PASS reopen: evidence preserved, markers reset',flush=True)
finally:c.tearDown()
