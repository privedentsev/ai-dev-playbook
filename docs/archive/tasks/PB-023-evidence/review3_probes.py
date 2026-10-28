"""PB-023 review 3: fixed scenarios and deterministic restore/retry interleaving."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

capture = io.StringIO()
with contextlib.redirect_stdout(capture):
    ns = runpy.run_path(str(Path(__file__).with_name('reaudit_probes.py')))
results = {'previous_scenarios': json.loads(capture.getvalue())}
fo = ns['fo']
with tempfile.TemporaryDirectory(prefix='pb023-review3-') as folder:
    root = Path(folder).resolve()
    (root / 'docs').mkdir()
    (root / 'docs/a.md').write_bytes(b'OLD')
    op = ns['child'](root, 'receipt')
    assert fo.pending_info(root) is not None
    original = fo.find_completed
    calls = []
    # Решение о завершённости принимается только под gate, после проверки
    # pending: квитанция читается уже при удерживаемой блокировке, поэтому
    # конкурирующая playbook-команда в этом окне обязана отказать.
    def interleave(project, plan_id):
        assert fo.pending_info(root) is None
        competing = subprocess.run(
            [sys.executable, str(Path(fo.__file__).resolve().parent
                                 / 'playbook.py'),
             '--root', str(root), 'index'], capture_output=True, text=True)
        assert competing.returncode != 0, competing.stdout + competing.stderr
        calls.append('gate-held')
        return original(project, plan_id)
    fo.find_completed = interleave
    try:
        try:
            retry = ns['apply'](root)['status']
        except ValueError:
            retry = 'refused'
    finally:
        fo.find_completed = original
    # Живой pending исключает ответ о применении: повтор отклонён до чтения
    # квитанции (find_completed не вызывался).
    assert retry == 'refused' and calls == []
    assert fo.pending_info(root) is not None
    # Завершение restore: маркер снят, квитанция нейтрализована.
    assert ns['restore'](root, op, False) == 'restored'
    assert (root / 'docs/a.md').read_bytes() == b'OLD'
    # Повтор реально применяет план; чтение квитанций происходит под gate.
    fo.find_completed = interleave
    try:
        retry = ns['apply'](root)
    finally:
        fo.find_completed = original
    assert retry['status'] == 'completed' and calls == ['gate-held']
    assert (root / 'docs/a.md').read_text() == 'NEW'
    results['remaining_A2'] = {'interleaving': 'pending refuse -> restore -> retry applies under gate',
                               'retry': retry['status'], 'actual': 'NEW', 'planned': 'NEW',
                               'pending': fo.pending_info(root) is not None,
                               'defect_reproduced': False}
print(json.dumps(results, ensure_ascii=False, indent=2))
