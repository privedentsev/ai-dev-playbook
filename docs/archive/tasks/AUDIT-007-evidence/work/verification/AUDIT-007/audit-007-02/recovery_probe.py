"""Independent SIGKILL + foreign gate probe; only temporary projects mutated."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from probe import Project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    out = parser.parse_args().output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    p = Project(out, 'recovery', ('command',))
    try:
        p.cli('verify', 'DEV-1', expect=0)
        before = p.card.read_bytes()
        index = (p.root / 'docs/tasks/index.md').read_bytes()
        child = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, SCRIPTS)
import playbook
original = playbook.file_operations._atomic_write
def killed_write(target, content, op_id):
    original(target, content, op_id)
    if target == Path(ROOT) / 'docs/tasks/DEV-1.md':
        os.kill(os.getpid(), signal.SIGKILL)
playbook.file_operations._atomic_write = killed_write
playbook.main(['--root', ROOT, 'lifecycle', 'handoff', 'DEV-1', '--summary', 'synthetic',
               '--actor', 'Fixture executor', '--model', 'Synthetic model'])
'''
        code = 'ROOT = %r\nSCRIPTS = %r\n' % (str(p.root), str(p.root / 'ai-dev-playbook/scripts')) + child
        killed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=90, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        (p.output / 'kill.stdout.log').write_text(killed.stdout)
        (p.output / 'kill.stderr.log').write_text(killed.stderr)
        assert killed.returncode == -signal.SIGKILL, killed
        p.handoff(expect=1)
        p.accept(expect=1)
        p.cli('lifecycle', 'restore', expect=0)
        assert p.card.read_bytes() == before
        assert (p.root / 'docs/tasks/index.md').read_bytes() == index
        p.cli('lifecycle', 'restore', expect=1)
        # A genuine flock in another process blocks both mutation and preflight.
        lock_code = '''
import fcntl, sys
f = open(sys.argv[1], 'a+')
fcntl.flock(f, fcntl.LOCK_EX)
print('locked', flush=True)
sys.stdin.read()
'''
        holder = subprocess.Popen([sys.executable, '-c', lock_code, str(p.root / 'work/gate.lock')],
                                  text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            assert holder.stdout.readline().strip() == 'locked'
            p.handoff(expect=1)
            p.cli('verify', 'DEV-1', '--preflight', expect=1)
        finally:
            holder.communicate('', timeout=10)
        p.handoff()
        p.accept()
        assert p.counts() == {'a': 1}
        # Tampering with a handed-off log invalidates acceptance and closing.
        report = p.latest()
        log = p.root / report['results'][0]['stdout_log']
        log.write_text('changed after acceptance')
        p.accept(expect=1)
        p.cli('close', 'DEV-1', '--dry-run', expect=1)
        (out / 'summary.json').write_text(json.dumps({'sigkill': killed.returncode,
            'restore_card_and_index_bytes': 'identical', 'foreign_gate': 'blocked',
            'command_executions': p.counts(), 'tampered_log': 'accept/close refused'}, indent=2) + '\n')
        print((out / 'summary.json').read_text())
    finally:
        p.finish()


if __name__ == '__main__':
    main()
