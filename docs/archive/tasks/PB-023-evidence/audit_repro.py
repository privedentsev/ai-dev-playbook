"""Independent PB-023 audit probes. Run from any directory; only temp projects change."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[3] / 'ai-dev-playbook/scripts'
sys.path.insert(0, str(SCRIPTS))
import playbook as pb
import file_operations as fo

def spec(root):
    return dict(operation='audit', plan_id='audit-1', writes={'docs/a.md': b'NEW'},
                inputs=fo.snapshot_paths(root, ['docs/a.md'], pb.safe_path, pb.require))

def apply(root):
    return fo.run_operation(root, spec(root), pb.safe_path, pb.require)

def child(root, phase):
    code = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import file_operations as f
import playbook as p
root = Path(sys.argv[2]); phase = sys.argv[3]
if phase == 'receipt':
    def stop(*args): os.kill(os.getpid(), signal.SIGKILL)
    f._release_pending = stop
elif phase == 'write':
    original = f._journal
    def journal(op, event, **fields):
        if event == 'write': os.kill(os.getpid(), signal.SIGKILL)
        return original(op, event, **fields)
    f._journal = journal
elif phase == 'temp':
    original = Path.replace
    def replace(self, target):
        if '.fop-' in self.name and self.name.endswith('.tmp'):
            os.kill(os.getpid(), signal.SIGKILL)
        return original(self, target)
    Path.replace = replace
s = dict(operation='audit', plan_id='audit-1', writes={'docs/a.md':b'NEW'},
         inputs=f.snapshot_paths(root, ['docs/a.md'], p.safe_path, p.require))
f.run_operation(root, s, p.safe_path, p.require)
'''
    result = subprocess.run([sys.executable, '-c', code, str(SCRIPTS), str(root), phase], capture_output=True, text=True)
    assert result.returncode == -signal.SIGKILL, result.stderr
    marker = fo.pending_info(root)
    return root / marker['op_dir']

def restore(root, op, standalone):
    if standalone:
        result = subprocess.run([sys.executable, str(op / 'restore.py')], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    return fo.restore_operation(root, pb.safe_path, pb.require)['status']

results = []
for case in ('temp_collision', 'temp_kill', 'receipt_kill', 'lost_index', 'directory_mode'):
    for standalone in ([False, True] if case in ('temp_kill', 'receipt_kill', 'lost_index') else [False]):
        with tempfile.TemporaryDirectory(prefix='pb023-audit-') as folder:
            root = Path(folder).resolve()
            (root / 'docs').mkdir()
            (root / 'docs/a.md').write_bytes(b'OLD')
            if case == 'temp_collision':
                # A1: посторонний файл с именем, похожим на temp, переживает операцию
                temp = root / 'docs/a.md.fop-tmp'
                temp.write_bytes(b'UNRELATED USER FILE')
                result = apply(root)
                assert result['status'] == 'completed'
                assert temp.read_bytes() == b'UNRELATED USER FILE'
                observation = 'completed; unrelated docs/a.md.fop-tmp preserved'
            elif case == 'directory_mode':
                # A4: откат восстанавливает и каталог, и его режим
                directory = root / 'docs/private'
                directory.mkdir(mode=0o700)
                before = directory.stat().st_mode & 0o777
                s = spec(root)
                s['remove_dirs'] = ['docs/private']
                s['inputs']['docs/private'] = 'dir'
                def fail(_): raise ValueError('audit final check failure')
                s['final_checks'] = [fail]
                try: fo.run_operation(root, s, pb.safe_path, pb.require)
                except ValueError as error: message = str(error)
                after = directory.stat().st_mode & 0o777
                assert after == before and fo.pending_info(root) is None
                observation = f'rollback restored directory mode {oct(after)}'
            else:
                phase = {'temp_kill':'temp', 'receipt_kill':'receipt', 'lost_index':'write'}[case]
                op = child(root, phase)
                if case == 'lost_index': (op / fo.BACKUP_INDEX).unlink()
                if case == 'lost_index':
                    # A3: индекс утрачен после начала записи — отказ, маркер жив
                    if standalone:
                        r = subprocess.run([sys.executable, str(op / 'restore.py')],
                                           capture_output=True, text=True)
                        assert r.returncode == 1 and 'утрачен' in r.stderr, r.stderr
                        restore_result = 'standalone refused'
                    else:
                        try:
                            fo.restore_operation(root, pb.safe_path, pb.require)
                        except ValueError as error:
                            restore_result = f'engine refused: {error}'
                        else:
                            raise AssertionError('restore must refuse lost index')
                    assert fo.pending_info(root) is not None
                    assert (root / 'docs/a.md').read_text() == 'NEW'
                    observation = (f'{restore_result}; pending kept; '
                                   'a.md=NEW (preimage lost, nothing touched)')
                else:
                    restore_result = restore(root, op, standalone)
                    assert fo.pending_info(root) is None
                    current = (root / 'docs/a.md').read_text()
                    if case == 'temp_kill':
                        # A1: остаток temp этой операции убран, пред-образ возвращён
                        assert not list((root / 'docs').glob('*.fop-*.tmp'))
                        assert current == 'OLD'
                        observation = (f'{restore_result}; op temp cleaned; '
                                       f'a.md={current}')
                    else:
                        # A2: квитанция отменена откатом, повтор реально применяет
                        assert current == 'OLD'
                        rerun = apply(root)
                        assert rerun['status'] == 'completed'
                        assert (root / 'docs/a.md').read_text() == 'NEW'
                        observation = (f'{restore_result}; receipt neutralized; '
                                       'retry=completed, a.md=NEW')
            results.append(dict(case=case, restore='standalone' if standalone else 'engine', observation=observation))
print(json.dumps(results, ensure_ascii=False, indent=2))
