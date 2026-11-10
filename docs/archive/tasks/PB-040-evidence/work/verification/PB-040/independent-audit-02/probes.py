import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import cards, record, text, BODY
for label,mutate in [('requirement',lambda s:s.replace('повтор безопасен.','новое требование.')),('new execution',lambda s:s+record('EXEC',2))]:
 c=CriterionCliTest(); c.setUp()
 try:
  c.setup_card(); c.verify(); assert c.sync()[0]==0
  c.path.write_text(mutate(c.path.read_text())); c.append(record('CHECK',2))
  assert c.sync()[0]==0
  assert not c.view()['criterion_states']['AC-01']['checked']
  print('PASS A-01 stale report rejected:',label)
  c.verify(); assert c.sync()[0]==0
  assert c.view()['criterion_states']['AC-01']['checked']
  print('PASS A-01 fresh report restores:',label)
 finally:c.tearDown()
for label,value in [('unknown',text().replace('Closed: -','Closed: -\nUnknown: ignored')),('duplicate',text().replace('Closed: -','Closed: -\nPriority: P1')),('heading',text(BODY.replace('Локальный проект.','Локальный проект.\n#### Лишний заголовок\nТекст.')))]:
 try:cards.parse(value)
 except ValueError:print('PASS A-02 rejected:',label)
 else:raise AssertionError(label)
c=CriterionCliTest();c.setUp()
try:
 c.setup_card(); c.verify(); assert c.sync()[0]==0
 c.append(record('HANDOFF')); assert c.handoff()[0]==0
 c.path.write_text(c.path.read_text().replace('Добавлен apply.','Изменена реализация после проверки.'))
 state=c.view()['criterion_states']['AC-01'];print('A-03 altered EXEC existing binding:',state)
 c.append(record('HANDOFF',2))
 handoff=c.handoff();print('A-03 new handoff exit:',handoff[0])
 c.append(record('AUDIT',actor='Auditor',**{'Передача':'HANDOFF-02'}))
 accept=c.accept('--result','accepted');print('A-03 accept exit:',accept[0])
 close=c.cli('close','DEV-1','--dry-run');print('A-03 close dry-run exit:',close[0])
 assert state['checked'] and handoff[0]==0 and accept[0]==0 and close[0]==0
 print('REPRODUCED: changed EXEC accepted with old verify; execution_sha256 not revalidated for existing bindings')
finally:c.tearDown()
