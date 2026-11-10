import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import record
c=CriterionCliTest();c.setUp()
try:
 c.setup_card();c.path.write_text(c.path.read_text().replace('Доказательства: Локальная CLI-проба.','Доказательства: [Config](../../ai-dev-playbook/project.json)'))
 c.verify();c.append(record('HANDOFF'));assert c.handoff()[0]==0
 c.append(record('AUDIT',actor='Auditor'));assert c.accept('--result','accepted')[0]==0
 assert c.cli('close','DEV-1','--dry-run')[0]==0
 assert c.cli('close','DEV-1')[0]==0
 result=c.cli('check','--ready');print(result)
 state=c.view()['criterion_states']['AC-01'];print(state)
 assert result[0]==1 and state['reason']=='unbound_or_changed_check'
 print('REPRODUCED: relative CHECK link relocated; historical binding hash no longer matches')
finally:c.tearDown()
