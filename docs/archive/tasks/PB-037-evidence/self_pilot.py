"""Run PB-037 on a disposable copy; never mutate the supplied source project.

Usage: python3 self_pilot.py SOURCE OUTPUT_DIRECTORY
The full configured regression runs once on the copy. No acceptance is invented.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def fingerprint(root):
    result = {}
    for name in ('AGENTS.md', 'README.md', 'CHANGELOG.md', 'ai-dev-playbook', 'docs', 'tests'):
        path = root / name
        for p in ([path] if path.is_file() else sorted(path.rglob('*'))):
            if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith('.pyc'):
                result[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def main():
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = fingerprint(source)
    source_config = (source / 'ai-dev-playbook/project.json').read_bytes()
    with tempfile.TemporaryDirectory(prefix='pb037-self-') as tmp:
        root = Path(tmp) / 'project'
        shutil.copytree(source, root, ignore=shutil.ignore_patterns('.git', '__pycache__', '.DS_Store'))
        commands = []

        def run(*args, expected=0):
            at = time.monotonic()
            proc = subprocess.run([sys.executable, 'ai-dev-playbook/scripts/playbook.py', *args],
                cwd=root, text=True, capture_output=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            number = len(commands) + 1
            (output / f'{number:02d}.stdout.log').write_text(proc.stdout)
            (output / f'{number:02d}.stderr.log').write_text(proc.stderr)
            commands.append({'argv': list(args), 'returncode': proc.returncode,
                             'seconds': round(time.monotonic() - at, 3)})
            (output / 'commands.json').write_text(json.dumps(commands, ensure_ascii=False, indent=2) + '\n')
            if proc.returncode != expected:
                failed = root / 'work/verification/PB-037'
                if failed.exists():
                    shutil.copytree(failed, output / 'failed-runs', dirs_exist_ok=True)
                raise RuntimeError(f'{args}: {proc.returncode}\n{proc.stdout}\n{proc.stderr}')
            print(f'{number}: {args}: {proc.returncode}', flush=True)
            return proc

        project = root / 'ai-dev-playbook/project.json'
        original = json.loads(project.read_text())
        run('workflow', 'enable-v2')
        assert json.loads(project.read_text()) == original
        run('workflow', 'enable-v2', '--apply')
        enabled = json.loads(project.read_text())
        for old, new in zip(original['verification']['checks'], enabled['task_workflow']['registry']):
            assert old['argv'] == new['argv']
            assert old['timeout_seconds'] == new['timeout_seconds']
            assert new['default_inputs'] == original['verification']['inputs']
            assert new['applicability'] == 'always'
        assert len(original['verification']['checks']) == len(enabled['task_workflow']['registry'])
        # Preserve registry defaults, but explicitly project task service fields in
        # the task's check inputs. Raw docs/ would also hash handoff itself.
        inputs = [p for p in original['verification']['inputs'] if p != 'docs']
        document_inputs = []
        excluded = {'docs/tasks/PB-037.md', original['paths']['task_index']}
        def include(directory):
            for path in sorted(directory.iterdir()):
                rel = path.relative_to(root).as_posix()
                if rel in excluded or path.name == '__pycache__':
                    continue
                if rel in {'docs/tasks', 'docs/tasks/PB-037-evidence'}:
                    include(path)
                else:
                    document_inputs.append(rel)
        include(root / 'docs')
        # Directory inputs retain membership and all existing file content.
        expected = {p.relative_to(root).as_posix() for p in (root / 'docs').rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts} - excluded
        covered = set()
        for rel in document_inputs:
            path = root / rel
            covered.update(p.relative_to(root).as_posix() for p in
                           ([path] if path.is_file() else path.rglob('*'))
                           if p.is_file() and '__pycache__' not in p.parts)
        assert expected == covered, 'Compact inputs must preserve every document file'
        inputs += document_inputs
        inputs += [{'type': 'task_projection'}, {'type': 'policy_projection'}]
        plan = {'revision': 1,
            'rationale': 'Preserve all mandatory commands and existing document files; '
                         'task requirements/policy are projected, generated task index is service data. '
                         'Directory membership is preserved; new task-root files require plan revision.',
            'scope': original['verification']['inputs'], 'checks': [
                {'id': c['id'], 'kind': 'command', 'purpose': 'Preserved mandatory regression',
                 'registry_id': c['id'], 'inputs': inputs}
                for c in original['verification']['checks']]}
        (root / 'work/pilot-plan.json').write_text(json.dumps(plan))
        common = ('PB-037', '--route', 'small', '--verification-plan', 'work/pilot-plan.json',
            '--actor', 'Codex', '--model', 'GPT-6', '--reason', 'Isolated PB-037 pilot')
        run('workflow', 'migrate-card', *common, '--apply')
        run('lifecycle', 'start', 'PB-037', '--actor', 'Codex', '--model', 'GPT-6', '--reason', 'Isolated pilot')
        (output / 'pilot-input-sha256.json').write_text(json.dumps(fingerprint(root), indent=2) + '\n')
        run('verify', 'PB-037', '--preflight')
        run('verify', 'PB-037')
        run('verify', 'PB-037')
        run('lifecycle', 'handoff', 'PB-037', '--summary', 'Self-copy pilot, no independent acceptance',
            '--actor', 'Codex', '--model', 'GPT-6')
        run('lifecycle', 'handoff', 'PB-037', '--summary', 'Self-copy pilot, no independent acceptance',
            '--actor', 'Codex', '--model', 'GPT-6')
        run('verify', 'PB-037', '--audit-ready')
        run('close', 'PB-037', '--dry-run', expected=1)
        state = output / 'state'
        state.mkdir(exist_ok=True)
        shutil.copy2(root / 'docs/tasks/PB-037.md', state / 'card.txt')
        shutil.copy2(project, state / 'project.json')
        for name in ('baseline.json', 'workflow-v1.json'):
            shutil.copy2(root / 'docs/tasks/PB-037-evidence' / name, state / name)
        runs = []
        for path in sorted((root / 'work/verification/PB-037').glob('*/report.json')):
            report = json.loads(path.read_text())
            runs.append(report)
            dest = output / 'runs' / path.parent.name
            shutil.copytree(path.parent, dest)
        (output / 'input-sha256.json').write_text(json.dumps(before, indent=2) + '\n')
        assert (source / 'ai-dev-playbook/project.json').read_bytes() == source_config, 'Source configuration changed'
        (output / 'summary.json').write_text(json.dumps({'source_configuration_unchanged': True,
            'legacy_checks_preserved': len(plan['checks']), 'report_count': len(runs),
            'acceptance': 'not performed; close refused as expected',
            'input_files': len(before)}, indent=2) + '\n')


if __name__ == '__main__':
    main()
