import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from test_workflow_install import WorkflowInstallTest
from test_cli import CliCase, REPO_VERSION, PASS_CHECK
from test_playbook import playbook

class IndependentProbes(CliCase):
    def test_multiple_legacy_commands_preserved_and_required(self):
        checks = [dict(PASS_CHECK), {'id': 'second', 'argv': [sys.executable, '-c', 'print(" spaced argument ")'], 'timeout_seconds': 47.5}]
        self.install(version=REPO_VERSION, verification={'inputs': ['AGENTS.md', 'docs'], 'checks': checks})
        project = self.root / 'ai-dev-playbook/project.json'
        before = json.loads(project.read_text())
        before['user_setting'] = {'nested': ['untouched', 7]}
        project.write_text(json.dumps(before))
        initial = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(self.cli('workflow', 'enable-v2')[0], 0)
        self.assertEqual(initial, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        code, out, err = self.cli('workflow', 'enable-v2', '--apply')
        self.assertEqual(code, 0, out + err)
        after = json.loads(project.read_text())
        self.assertEqual(after['verification'], before['verification'])
        self.assertEqual(after['user_setting'], before['user_setting'])
        registry = after['task_workflow']['registry']
        self.assertEqual(len(registry), 2)
        for entry, old in zip(registry, checks):
            self.assertEqual({k: entry[k] for k in old}, old)
            self.assertEqual(entry['default_inputs'], before['verification']['inputs'])
            self.assertEqual(entry['applicability'], 'always')
        self.assertEqual(after['task_workflow']['checkpoints'], {'merge': ['unit', 'second'], 'release': ['unit', 'second']})
        self.assertEqual(self.cli('card', 'create', 'DEV-1', '--title', 'Audit probe', '--apply')[0], 0)
        (self.root / 'plan.json').write_text(json.dumps({'revision': 1, 'rationale': 'Incomplete plan', 'scope': ['docs'], 'checks': [{'id': 'unit', 'kind': 'command', 'purpose': 'First only', 'registry_id': 'unit', 'inputs': ['docs']}]}))
        card = self.root / 'docs/tasks/DEV-1.md'
        preserved = card.read_bytes(), card.with_suffix('.meta.json').read_bytes()
        code, out, err = self.cli('workflow', 'migrate-card', 'DEV-1', '--route', 'small', '--reason', 'Probe', '--actor', 'Codex', '--model', 'GPT-6', '--verification-plan', 'plan.json', '--apply')
        self.assertEqual(code, 1)
        self.assertIn('Missing required registry check: second', err)
        self.assertEqual(preserved, (card.read_bytes(), card.with_suffix('.meta.json').read_bytes()))

    def test_migrate_interruption_and_standalone_restore(self):
        self.install(version=REPO_VERSION, verification={'inputs': ['docs'], 'checks': [PASS_CHECK]})
        self.assertEqual(self.cli('workflow', 'enable-v2', '--apply')[0], 0)
        self.assertEqual(self.cli('card', 'create', 'DEV-1', '--title', 'Restore probe', '--apply')[0], 0)
        (self.root / 'plan.json').write_text(json.dumps({'revision': 1, 'rationale': 'Restore', 'scope': ['docs'], 'checks': [{'id': 'unit', 'kind': 'command', 'purpose': 'Required', 'registry_id': 'unit', 'inputs': ['docs']}]}))
        paths = [self.root / 'docs/tasks/DEV-1.md', self.root / 'docs/tasks/DEV-1.meta.json', self.root / 'docs/tasks/index.md', self.root / 'ai-dev-playbook/manifest.json']
        before = {p: p.read_bytes() for p in paths}
        original = playbook.file_operations._atomic_write
        def crash(target, content, op_id):
            original(target, content, op_id)
            if target == paths[1]:
                raise KeyboardInterrupt()
        with patch.object(playbook.file_operations, '_atomic_write', crash):
            with self.assertRaises(KeyboardInterrupt):
                self.cli('workflow', 'migrate-card', 'DEV-1', '--route', 'small', '--reason', 'Probe', '--actor', 'Codex', '--model', 'GPT-6', '--verification-plan', 'plan.json', '--apply')
        pending = playbook.file_operations.pending_info(self.root)
        self.assertIsNotNone(pending)
        self.assertEqual(self.cli('lifecycle', 'start', 'DEV-1', '--actor', 'Codex', '--model', 'GPT-6', '--reason', 'Probe')[0], 1)
        proc = subprocess.run([sys.executable, str(self.root / pending['op_dir'] / 'restore.py')], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        self.assertFalse((self.root / 'docs/tasks/DEV-1-evidence/workflow-v1.json').exists())
        self.assertIsNone(playbook.file_operations.pending_info(self.root))

unittest.main(verbosity=2)
