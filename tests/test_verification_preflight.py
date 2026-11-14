"""Tests for read-only verification preflight (PB-033)."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-dev-playbook/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_close_lifecycle as fixture
from test_documents import tree_fingerprint
import verification


class VerificationPreflightTest(fixture.CloseLifecycleCase):
    """Preflight проверок: read-only, применимость и свежесть,
    stale index, материалы, exclusion отказов."""
    def setUp(self):
        super().setUp()
        self.project_file = self.root / "ai-dev-playbook/project.json"
        self.settings = json.loads(self.project_file.read_text(encoding="utf-8"))
        
        # Add task_workflow v2 configuration to project.json
        self.settings["task_workflow"] = {
            "schema_version": 2,
            "registry": [
                {
                    "id": "unit-tests",
                    "kind": "command",
                    "purpose": "Unit tests",
                    "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                    "timeout_seconds": 30,
                    "default_inputs": ["ai-dev-playbook"],
                    "applicability": {"kind": "always"},
                },
                {
                    "id": "doc-check",
                    "kind": "command",
                    "purpose": "Documentation check",
                    "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
                    "timeout_seconds": 30,
                    "default_inputs": ["docs"],
                    "applicability": {
                        "kind": "when_paths",
                        "paths": ["docs/**"],
                    },
                },
                {
                    "id": "visual-check",
                    "kind": "manual",
                    "purpose": "Manual visual check",
                    "scenario": "Verify button UI in browser",
                    "applicability": {"kind": "always"},
                },
            ],
            "checkpoints": {
                "release": ["unit-tests"],
            },
        }
        self.save_project()
        
    def save_project(self):
        self.project_file.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def create_v2_task(self, task_id="DEV-1", status="in_progress", checks=None, decisions=None, scope=None, materials=None):
        if checks is None:
            checks = [
                {
                    "id": "unit-tests",
                    "kind": "command",
                    "purpose": "Run unit tests",
                    "registry_id": "unit-tests",
                    "inputs": ["ai-dev-playbook/scripts/documents.py"],
                    "applicability": {"kind": "always"},
                },
                {
                    "id": "doc-check",
                    "kind": "command",
                    "purpose": "Check docs",
                    "registry_id": "doc-check",
                    "inputs": ["docs/tasks/DEV-1.md"],
                    "applicability": {
                        "kind": "when_paths",
                        "paths": ["docs/**"],
                    },
                },
                {
                    "id": "visual-check",
                    "kind": "manual",
                    "purpose": "Visual button check",
                    "registry_id": "visual-check",
                    "inputs": ["docs/tasks/DEV-1.md"],
                    "applicability": {"kind": "always"},
                },
            ]
        meta = {
            "schema_version": 2,
            "workflow": {
                "route": "quick",
                "reason": "Quick fix",
                "history": [],
            },
            "verification_plan": {
                "revision": 1,
                "rationale": "Testing scope",
                "scope": scope if scope is not None else ["ai-dev-playbook", "docs"],
                "checks": checks,
                "decisions": decisions or [],
            },
            "relations": [],
            "materials": materials or [],
            "moves": [],
            "handoff": None,
            "acceptance": None,
        }
        self.managed_task(ident=task_id, status=status, meta=meta)
        self.cli("index")

    def test_preflight_does_not_execute_commands_or_write_files(self):
        """Preflight не исполняет команды и не пишет"""
        self.create_v2_task("DEV-1")
        before_tree = tree_fingerprint(self.root)
        
        with patch("subprocess.Popen") as mock_popen:
            code, out, err = self.cli("verify", "DEV-1", "--preflight")
            self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
            mock_popen.assert_not_called()
            
        # Verify no files were created in work/verification
        self.assertFalse((self.root / "work/verification/DEV-1").exists())
        # Tree remains untouched
        self.assertEqual(before_tree, tree_fingerprint(self.root))
        # Preflight shows IDs, applicability, and summary
        self.assertIn("Preflight v2 для задачи DEV-1:", out)
        self.assertIn("unit-tests (command): обязательна", out)
        self.assertIn("visual-check (manual): обязательна", out)
        self.assertIn("preflight: структура, материалы, блокировки, команды и лимиты проверены", out)

    def start_task(self):
        card = self.root / 'docs/tasks/DEV-1.md'
        card.write_text(card.read_text() + '\n## Цель и границы\nПроверка fixture.\n\n## Приёмка\n- [ ] Проверяемый результат.\n')
        code, out, err = self.cli('lifecycle', 'start', 'DEV-1', '--actor', 'Test executor',
            '--model', 'Test model', '--reason', 'Test engineer authorized fixture')
        self.assertEqual(code, 0, out + err)

    def test_preflight_shows_applicability_and_freshness(self):
        # A real bound baseline proves unchanged docs; an unbound empty JSON
        # cannot establish independence from the current tree.
        """Preflight показывает применимость и свежесть"""
        self.create_v2_task('DEV-1', status='draft', scope=['ai-dev-playbook'])
        self.start_task()
        code, out, err = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(code, 0, out + err)
        self.assertIn('unit-tests (command): обязательна', out)
        self.assertIn('visual-check (manual): обязательна', out)
        self.assertIn('doc-check (command): неприменима', out)
        self.assertIn('not_run', out)

    def test_preflight_detects_stale_index_and_gives_remediation(self):
        """Preflight обнаруживает stale index с рекомендацией"""
        self.create_v2_task("DEV-1")
        # Tamper with index to make it stale
        index = self.root / "docs/tasks/index.md"
        before_content = index.read_text(encoding="utf-8")
        index.write_text(before_content + "\n<!-- stale marker -->\n", encoding="utf-8")
        before_tree = tree_fingerprint(self.root)
        
        code, out, err = self.cli("verify", "DEV-1", "--preflight")
        self.assertEqual(code, 1)
        self.assertIn("Индекс задач устарел", out + err)
        self.assertIn("Способ исправления: выполните", out + err)
        # Preflight does NOT write to disk
        self.assertEqual(before_tree, tree_fingerprint(self.root))

    def test_preflight_detects_unaccounted_materials(self):
        """Preflight обнаруживает неучтённые материалы"""
        self.create_v2_task("DEV-1")
        evidence_dir = self.root / "docs/tasks/DEV-1-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "untracked.log").write_text("mystery evidence")
        
        code, out, err = self.cli("verify", "DEV-1", "--preflight")
        self.assertEqual(code, 1)
        self.assertIn("неучтённые материалы", out + err)
        self.assertIn("lifecycle register-material", out + err)

    def test_preflight_detects_missing_required_registry_check(self):
        # verification_plan only contains visual-check, but unit-tests is 'always' required in registry
        """Preflight обнаруживает отсутствующую обязательную проверку"""
        checks = [
            {
                "id": "visual-check",
                "kind": "manual",
                "purpose": "Visual button check",
                "registry_id": "visual-check",
                "inputs": ["docs/tasks/DEV-1.md"],
                "applicability": {"kind": "always"},
            }
        ]
        self.create_v2_task("DEV-1", checks=checks)
        code, out, err = self.cli("verify", "DEV-1", "--preflight")
        self.assertEqual(code, 1)
        self.assertIn("Отсутствует обязательная проверка из реестра: unit-tests", out + err)
        self.assertIn("Способ исправления:", out + err)

    def test_preflight_detects_unapproved_exclusion_of_failed_check(self):
        """Preflight обнаруживает неутверждённое исключение failed-проверки"""
        self.settings['task_workflow']['registry'].append({
            'id': 'opt-check', 'kind': 'command', 'purpose': 'Optional check',
            'argv': [sys.executable, '-c', 'raise SystemExit(1)'], 'timeout_seconds': 30,
            'applicability': {'kind': 'always'}})
        self.save_project()
        self.create_v2_task('DEV-1', status='draft', scope=['ai-dev-playbook'])
        card = self.root / 'docs/tasks/DEV-1.md'
        meta = verification.task_cards.metadata(card.read_text())
        meta['verification_plan']['checks'].append({
            'id': 'opt-check', 'kind': 'command', 'purpose': 'Optional check',
            'registry_id': 'opt-check', 'inputs': ['ai-dev-playbook/scripts/documents.py'],
            'applicability': {'kind': 'when_paths', 'paths': ['src/**']}})
        card.write_text(fixture.lifecycle._splice_meta(card.read_text(), meta))
        self.start_task()
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 1)
        # Preserve the real failed report and its baseline, then attempt to
        # weaken applicability and remove the failed check without approval.
        self.settings['task_workflow']['registry'][-1]['applicability'] = {
            'kind': 'when_paths', 'paths': ['src/**']}
        self.save_project()
        meta = verification.task_cards.metadata(card.read_text())
        meta['verification_plan']['checks'] = [c for c in meta['verification_plan']['checks']
                                                 if c['id'] != 'opt-check']
        card.write_text(fixture.lifecycle._splice_meta(card.read_text(), meta))
        code, out, err = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(code, 1)
        self.assertIn("Попытка исключить ранее проваленную проверку 'opt-check'", out + err)

    def test_preflight_does_not_require_future_reports(self):
        # A newly created ready task with no verification reports must pass preflight
        """Preflight не требует будущих отчётов"""
        self.create_v2_task("DEV-1", status="ready")
        code, out, err = self.cli("verify", "DEV-1", "--preflight")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("preflight: структура, материалы, блокировки, команды и лимиты проверены", out)


if __name__ == "__main__":
    unittest.main()

