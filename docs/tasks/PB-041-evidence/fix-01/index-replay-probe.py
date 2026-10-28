import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_close_lifecycle import CloseLifecycleCase
from test_playbook import playbook
case = CloseLifecycleCase()
case.setUp()
try:
    case.managed_task(status='in_review')
    path = case.tasks_dir() / 'DEV-1.md'
    path.write_text(path.read_text().replace('Status: in_review', 'Status: in_progress'))
    print('first transition:', case.cli('index'))
    path.write_text(path.read_text().replace('Status: in_progress', 'Status: in_review'))
    print('return transition:', case.cli('index'))
    _, paths = playbook.config(case.root)
    expected = playbook.render_index(playbook.read_tasks(case.root, paths), paths['task_index'].parent)
    print('index matches cards:', paths['task_index'].read_text() == expected)
finally:
    case.tearDown()
