import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_criterion_checkboxes import CriterionCliTest
from test_task_cards import record, cards, text, BODY

case = CriterionCliTest()
case.setUp()
try:
    case.setup_card()
    case.verify()
    assert case.sync()[0] == 0
    old = cards.metadata(case.path.read_text())['criterion_evidence']['CHECK-01']['report_id']
    case.path.write_text(case.path.read_text().replace('повтор безопасен.', 'совершенно новое требование.'))
    case.append(record('EXEC', 2))
    case.append(record('CHECK', 2))
    result = case.sync()
    print('A-01 sync after changed requirement and new execution, WITHOUT verify:', result)
    state = case.view()['criterion_states']['AC-01']
    print('A-01 criterion state:', state)
    new = cards.metadata(case.path.read_text())['criterion_evidence']['CHECK-02']['report_id']
    print('A-01 reused old report:', old == new)
    assert state['checked'] and old == new
    case.append(record('HANDOFF', 2, **{'Выполнение': 'EXEC-02', 'Проверки': 'CHECK-02'}))
    print('A-01 handoff:', case.handoff())
    case.append(record('AUDIT', actor='Auditor', **{'Передача': 'HANDOFF-02'}))
    print('A-01 accept:', case.accept('--result', 'accepted'))
    close = case.cli('close', 'DEV-1', '--dry-run')
    print('A-01 close dry run:', close)
    assert close[0] == 0
finally:
    case.tearDown()
for name, value in [
    ('unknown header field', text().replace('Closed: -', 'Closed: -\nUnknown: ignored')),
    ('extra heading', text(BODY.replace('Локальный проект.', 'Локальный проект.\n#### Лишний заголовок\nТекст.'))),
]:
    result = cards.parse(value)
    print('A-02', name, result['status'])
    assert result['status'] == 'supported'
