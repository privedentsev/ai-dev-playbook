import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import record
c=CriterionCliTest();c.setUp()
try:
 c.setup_card();c.path.write_text(c.path.read_text().replace('Локальный проект.','Локальный проект. [Config](../../ai-dev-playbook/project.json)'))
 c.verify();c.append(record('HANDOFF'));assert c.handoff()[0]==0
 c.append(record('AUDIT',actor='Auditor'));assert c.accept('--result','accepted')[0]==0
 assert c.cli('close','DEV-1','--dry-run')[0]==0
 print('close:',c.cli('close','DEV-1'))
 result=c.cli('check','--ready');print('check after close:',result)
 assert result[0]==1
 print('REPRODUCED A-05: relative link relocation makes historical checkboxes stale')
finally:c.tearDown()
