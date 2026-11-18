import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import record
for earlier in (False,True):
 c=CriterionCliTest();c.setUp()
 try:
  c.setup_card()
  if earlier:
   c.append(record('EXEC',2,**{'Изменения':'Вторая часть реализации.'}));c.append(record('CHECK',2))
  c.verify();assert c.sync()[0]==0
  c.append(record('HANDOFF',**({'Выполнение':'EXEC-01, EXEC-02','Проверки':'CHECK-02'} if earlier else {})))
  assert c.handoff()[0]==0
  c.path.write_text(c.path.read_text().replace('Добавлен apply.','Изменено проверенное выполнение.'))
  state=c.view()['criterion_states']['AC-01']
  c.append(record('HANDOFF',2,**({'Выполнение':'EXEC-01, EXEC-02','Проверки':'CHECK-02'} if earlier else {})))
  h=c.handoff()[0]
  c.append(record('AUDIT',actor='Auditor',**{'Передача':'HANDOFF-02'}))
  a=c.accept('--result','accepted')[0];close=c.cli('close','DEV-1','--dry-run')[0]
  print({'changed':'earlier EXEC-01 with EXEC-02 present' if earlier else 'latest EXEC-01','checked':state['checked'],'handoff':h,'accept':a,'close_dry_run':close},flush=True)
  assert not state['checked'] and h==a==close==1
  c.verify(); c.append(record('CHECK',3)); assert c.sync()[0]==0
  assert c.view()['criterion_states']['AC-01']['checked']
  c.append(record('HANDOFF',3,**{'Выполнение':'EXEC-01, EXEC-02' if earlier else 'EXEC-01','Проверки':'CHECK-03'}))
  assert c.handoff()[0]==0
  c.append(record('AUDIT',2,actor='Auditor',**{'Передача':'HANDOFF-03'}))
  assert c.accept('--result','accepted')[0]==0
  assert c.cli('close','DEV-1','--dry-run')[0]==0
  assert c.cli('close','DEV-1')[0]==0
  assert c.cli('check','--ready')[0]==0
  print('PASS: new verify/CHECK restores and real close succeeds',flush=True)
 finally:c.tearDown()
