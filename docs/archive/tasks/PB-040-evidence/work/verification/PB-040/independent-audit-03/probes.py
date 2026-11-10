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
  if earlier:assert state['checked'] and h==a==close==0
  else:assert not state['checked'] and h==a==close==1
 finally:c.tearDown()
