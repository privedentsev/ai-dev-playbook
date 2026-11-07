"""Tests for verification runner v2, manual checks, and selective reuse (PB-033)."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-dev-playbook/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_close_lifecycle as fixture
from test_documents import tree_fingerprint
import verification


def req(ok, msg=""):
    if not ok:
        raise ValueError(msg)


class VerificationV2Test(fixture.CloseLifecycleCase):
    def setUp(self):
        super().setUp()
        self.project_file = self.root / "ai-dev-playbook/project.json"
        self.settings = json.loads(self.project_file.read_text(encoding="utf-8"))
        
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
            ],
            "checkpoints": {
                "release": ["unit-tests"],
            },
        }
        self.save_project()

    def save_project(self):
        self.project_file.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def add_registry_check(self, check_def):
        self.settings["task_workflow"]["registry"].append(check_def)
        self.save_project()

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
                "scope": scope if scope is not None else ["ai-dev-playbook"],
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

    def test_environment_change_invalidates_reuse_and_gate_is_ignored(self):
        self.settings["task_workflow"]["registry"][0]["argv"] = [
            sys.executable, "-c",
            "import os,sys; sys.exit(0 if os.getenv('PB033_MODE') == 'pass' else 9)"]
        self.save_project()
        self.create_v2_task(status="in_review")
        with patch.dict(os.environ, {"PB033_MODE": "pass"}):
            self.assertEqual(self.cli("verify", "DEV-1")[0], 0)
            code, out, err = self.cli("verify", "DEV-1")
            self.assertEqual(code, 0, err)
            self.assertIn("REUSE: unit-tests", out)
            original = verification.runtime_environment()
            with patch.dict(os.environ, {"PLAYBOOK_GATE": "different-token"}):
                self.assertEqual(original, verification.runtime_environment())
        with patch.dict(os.environ, {"PB033_MODE": "fail"}):
            code, out, err = self.cli("lifecycle", "handoff", "DEV-1", "--summary", "Changed environment", "--actor", "Codex", "--model", "GPT-6.1 Sol")
            self.assertEqual(code, 1, out)
            self.assertIn("coverage", err)
            code, out, err = self.cli("verify", "DEV-1")
            self.assertEqual(code, 1, err)
            self.assertNotIn("REUSE: unit-tests", out)
            reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
            self.assertTrue(any(json.loads(p.read_text())["results"][0].get("exit_code") == 9
                                for p in reports))
            self.assertNotIn('PB033_MODE', ''.join(p.read_text() for p in reports))

    def test_independent_registry_change_preserves_other_result(self):
        other = copy.deepcopy(self.settings["task_workflow"]["registry"][0])
        other["id"] = "other"
        self.add_registry_check(other)
        checks = [{"id": cid, "kind": "command", "purpose": cid,
                   "registry_id": cid, "inputs": ["docs/product.md"],
                   "applicability": {"kind": "always"}}
                  for cid in ("unit-tests", "other")]
        self.create_v2_task(checks=checks)
        self.assertEqual(self.cli("verify", "DEV-1")[0], 0)
        self.settings["task_workflow"]["registry"][0]["argv"][-1] = "print('changed')"
        self.save_project()
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, err)
        self.assertIn("RUN: unit-tests", out)
        self.assertIn("REUSE: other", out)
        self.settings["task_workflow"]["registry"][1]["applicability"] = {"kind": "checkpoint", "checkpoint": "release"}
        self.save_project()
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, err)
        self.assertIn("RUN: other", out)

    def test_terminal_manual_requires_terminal_but_not_browser(self):
        self.add_registry_check({"id": "terminal", "kind": "manual",
            "scenario": "Read local output in terminal", "required_environment": ["terminal"],
            "applicability": {"kind": "always"}})
        self.settings["task_workflow"]["registry"][0]["applicability"] = {"kind": "checkpoint", "checkpoint": "release"}
        self.save_project()
        self.create_v2_task(checks=[{"id": "terminal", "kind": "manual",
            "purpose": "Read output", "registry_id": "terminal",
            "inputs": ["docs/product.md"], "applicability": {"kind": "always"}}])
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1, err)
        self.assertIn("MANUAL: terminal", out, err)
        report = next((self.root / "work/verification/DEV-1").glob("*/report.json"))
        statement = {"check_id": "terminal", "status": "passed", "observation": "Read output",
            "environment": {"terminal": "local"},
            "candidate_snapshot": json.loads(report.read_text())["results"][0]["candidate_snapshot"],
            "engineer_confirmation": {"who": "Engineer", "at": "2026-11-06T15:00:00+03:00",
                                      "statement": "Confirmed"}}
        path = self.root / "statement.json"
        missing_environment = dict(statement, environment={})
        path.write_text(json.dumps(missing_environment))
        self.assertEqual(self.cli("verify", "DEV-1", "--record-manual", "statement.json")[0], 1)
        path.write_text(json.dumps(statement))
        code, out, err = self.cli("verify", "DEV-1", "--record-manual", "statement.json")
        self.assertEqual(code, 0, err)
        self.assertIn("passed", out)
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, err)
        self.assertIn("REUSE: terminal", out)

    def test_command_execution_allowed_argv_only(self):
        self.create_v2_task("DEV-1")
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("RUN: unit-tests", out)
        self.assertIn("passed: ", out)
        
        # Verify report was generated
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = verification.load_report(reports[0], req)
        self.assertEqual(rep["schema_version"], 2)
        self.assertEqual(rep["status"], "passed")
        self.assertEqual(len(rep["results"]), 1)
        res = rep["results"][0]
        self.assertEqual(res["check_id"], "unit-tests")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["snapshot_before"], res["snapshot_after"])
        
        # Verify log files exist
        self.assertTrue((self.root / res["stdout_log"]).is_file())
        self.assertTrue((self.root / res["stderr_log"]).is_file())

    def test_timeout_handling(self):
        self.add_registry_check({
            "id": "slow-tests",
            "kind": "command",
            "purpose": "Slow tests",
            "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
            "timeout_seconds": 1,
            "default_inputs": ["ai-dev-playbook"],
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            },
            {
                "id": "slow-tests",
                "kind": "command",
                "purpose": "Slow check",
                "registry_id": "slow-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            }
        ]
        self.create_v2_task("DEV-1", checks=checks)
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("failed: ", out)
        
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = verification.load_report(reports[0], req)
        self.assertEqual(rep["status"], "failed")
        slow_res = next(r for r in rep["results"] if r["check_id"] == "slow-tests")
        self.assertEqual(slow_res["status"], "timeout")

    def test_stale_during_run_detection(self):
        target = self.root / "target_input.txt"
        target.write_text("initial")
        self.add_registry_check({
            "id": "tamper-tests",
            "kind": "command",
            "purpose": "Tamper tests",
            "argv": [sys.executable, "-c", "from pathlib import Path; p = Path('target_input.txt'); p.write_text('tampered')"],
            "timeout_seconds": 30,
            "default_inputs": ["target_input.txt"],
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            },
            {
                "id": "tamper-tests",
                "kind": "command",
                "purpose": "Tamper check",
                "registry_id": "tamper-tests",
                "inputs": ["target_input.txt"],
                "applicability": {"kind": "always"},
            }
        ]
        self.create_v2_task("DEV-1", checks=checks, scope=["ai-dev-playbook", "target_input.txt"])
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("входы tamper-tests изменились во время прогона", out + err)
        self.assertIn("stale: ", out)
        
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = verification.load_report(reports[0], req)
        self.assertEqual(rep["status"], "stale")

    def test_manual_check_candidate_snapshot_and_not_run_without_confirmation(self):
        self.add_registry_check({
            "id": "visual-check",
            "kind": "manual",
            "purpose": "Manual visual check",
            "scenario": "Verify button UI in browser at 1920x1080",
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
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
        self.create_v2_task("DEV-1", checks=checks, scope=["ai-dev-playbook", "docs"])
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("MANUAL: visual-check: зафиксирован candidate_snapshot", out)
        self.assertIn("incomplete: ", out)
        
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = verification.load_report(reports[0], req)
        self.assertEqual(rep["status"], "incomplete")
        self.assertFalse(rep["coverage"]["satisfied"])
        self.assertIn("visual-check", rep["coverage"]["not_run"])
        
        manual_res = next(r for r in rep["results"] if r["check_id"] == "visual-check")
        self.assertEqual(manual_res["status"], "not_run")
        self.assertIn("candidate_snapshot", manual_res)
        self.assertIn("scenario_hash", manual_res)

    def test_manual_presentation_cannot_be_rebound_after_scenario_change(self):
        self.add_registry_check({
            "id": "visual", "kind": "manual", "purpose": "UI",
            "scenario": "Inspect color", "applicability": {"kind": "always"},
        })
        (self.root / "target.txt").write_text("unchanged")
        self.create_v2_task(checks=[{
            "id": "unit-tests", "kind": "command", "purpose": "Unit",
            "registry_id": "unit-tests", "inputs": ["target.txt"],
            "applicability": {"kind": "always"},
        }, {
            "id": "visual", "kind": "manual", "purpose": "UI",
            "registry_id": "visual", "inputs": ["target.txt"],
            "applicability": {"kind": "always"},
        }], scope=["target.txt"])
        self.cli("verify", "DEV-1")
        directory = self.root / "work/verification/DEV-1"
        report = json.loads(next(directory.glob("*/report.json")).read_text())
        old_candidate = next(r for r in report["results"] if r["check_id"] == "visual")["candidate_snapshot"]
        statement = {"check_id": "visual", "status": "passed",
                     "observation": "Reviewed", "environment": {"browser": "Chrome"},
                     "engineer_confirmation": {"who": "Engineer"},
                     "candidate_snapshot": old_candidate}
        self.settings["task_workflow"]["registry"][-1]["scenario"] = "Inspect keyboard focus"
        self.save_project()
        manual = self.root / "statement.json"
        manual.write_text(json.dumps(statement))
        self.assertEqual(self.cli("verify", "DEV-1", "--record-manual", "statement.json")[0], 1)
        self.assertEqual(self.cli("verify", "DEV-1")[0], 1)
        # Even after a new presentation, the original confirmation cannot be relabelled.
        self.assertEqual(self.cli("verify", "DEV-1", "--record-manual", "statement.json")[0], 1)
        reports = [json.loads(p.read_text()) for p in directory.glob("*/report.json")]
        fresh = next(result["candidate_snapshot"] for report in reports for result in report["results"]
                     if result["check_id"] == "visual" and result["candidate_snapshot"] != old_candidate)
        statement["candidate_snapshot"] = fresh
        manual.write_text(json.dumps(statement))
        result = self.cli("verify", "DEV-1", "--record-manual", "statement.json")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.cli("verify", "DEV-1")[0], 0)

    def test_record_manual_success_and_stale_candidate_rejection(self):
        self.add_registry_check({
            "id": "visual-check",
            "kind": "manual",
            "purpose": "Manual visual check",
            "scenario": "Verify button UI in browser at 1920x1080",
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
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
        self.create_v2_task("DEV-1", checks=checks, scope=["ai-dev-playbook", "docs"])
        # Initial verify captures candidate snapshot
        self.cli("verify", "DEV-1")
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        rep = verification.load_report(reports[0], req)
        cand_snap = next(r for r in rep["results"] if r["check_id"] == "visual-check")["candidate_snapshot"]
        
        # 1. Missing browser / confirmation produces not_run
        manual_statement_no_browser = {
            "check_id": "visual-check",
            "status": "passed",
            "observation": "Looks fine",
            "candidate_snapshot": cand_snap,
        }
        manual_file = self.root / "manual_statement.json"
        manual_file.write_text(json.dumps(manual_statement_no_browser), encoding="utf-8")
        code, out, err = self.cli("verify", "DEV-1", "--record-manual", "manual_statement.json")
        self.assertEqual(code, 1)
        self.assertIn("Нет браузера или подтверждения инженера", out)
        
        manual_statement_no_browser["engineer_confirmation"] = {
            "who": "Engineer", "at": "2026-11-06T15:00:00+03:00", "statement": "Confirmed"}
        manual_file.write_text(json.dumps(manual_statement_no_browser), encoding="utf-8")
        self.assertEqual(self.cli("verify", "DEV-1", "--record-manual", "manual_statement.json")[0], 1)

        # 2. Valid manual statement with browser and confirmation succeeds
        manual_statement_valid = {
            "check_id": "visual-check",
            "status": "passed",
            "observation": "Button has correct border radius and background color",
            "environment": {"browser": "Chrome 120"},
            "candidate_snapshot": cand_snap,
            "engineer_confirmation": {
                "who": "Engineer",
                "at": "2026-10-29T07:00:00+03:00",
                "statement": "Visual verification confirmed",
            },
        }
        manual_file.write_text(json.dumps(manual_statement_valid), encoding="utf-8")
        code, out, err = self.cli("verify", "DEV-1", "--record-manual", "manual_statement.json")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("Ручная проверка visual-check зарегистрирована со статусом passed", out)
        
        # Subsequent verify reuses the passed manual check!
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("REUSE: visual-check: свежий результат переиспользован", out)
        self.assertIn("passed: ", out)
        
        # 3. If file changes after candidate snapshot was taken, record-manual rejects!
        task_card = self.root / "docs/tasks/DEV-1.md"
        task_card.write_text(task_card.read_text(encoding="utf-8") + "\n<!-- extra edit -->\n", encoding="utf-8")
        code, out, err = self.cli("verify", "DEV-1", "--record-manual", "manual_statement.json")
        self.assertEqual(code, 1)
        self.assertIn("Файлы изменились после фиксации candidate_snapshot; требуется новый просмотр", out + err)

        # 4. Statement without candidate_snapshot after edit is strictly rejected (A-01)
        manual_statement_no_cand = {
            "check_id": "visual-check",
            "status": "passed",
            "observation": "Inspected changed file without candidate",
            "environment": {"browser": "Chrome 120"},
            "engineer_confirmation": {
                "who": "Engineer",
                "at": "2026-10-29T07:05:00+03:00",
                "statement": "Visual verification confirmed",
            },
        }
        manual_file.write_text(json.dumps(manual_statement_no_cand), encoding="utf-8")
        code, out, err = self.cli("verify", "DEV-1", "--record-manual", "manual_statement.json")
        self.assertEqual(code, 1)
        self.assertIn("Заявление со статусом passed требует candidate_snapshot", out + err)

    def test_independent_reuse_of_fresh_results(self):
        self.add_registry_check({
            "id": "independent-b",
            "kind": "command",
            "purpose": "Independent check B",
            "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
            "timeout_seconds": 30,
            "default_inputs": ["docs/index.md"],
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            },
            {
                "id": "independent-b",
                "kind": "command",
                "purpose": "Independent check B",
                "registry_id": "independent-b",
                "inputs": ["docs/index.md"],
                "applicability": {"kind": "always"},
            },
        ]
        self.create_v2_task("DEV-1", checks=checks, scope=["ai-dev-playbook", "docs"])
        
        # Run 1: both execute
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("RUN: unit-tests", out)
        self.assertIn("RUN: independent-b", out)
        
        # Run 2: both are reused without modifying inputs
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0)
        self.assertIn("REUSE: unit-tests: свежий результат переиспользован", out)
        self.assertIn("REUSE: independent-b: свежий результат переиспользован", out)
        
        # Modify input of unit-tests only
        doc_py = self.root / "ai-dev-playbook/scripts/documents.py"
        doc_py.write_text(doc_py.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
        
        # Run 3: unit-tests re-runs (with reason: inputs changed), independent-b is REUSED!
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0)
        self.assertIn("RUN: unit-tests (изменились входы: ai-dev-playbook/scripts/documents.py)", out)
        self.assertIn("REUSE: independent-b: свежий результат переиспользован", out)

    def test_failed_check_not_bypassed_by_reuse(self):
        self.add_registry_check({
            "id": "failing-check",
            "kind": "command",
            "purpose": "Failing check",
            "argv": [sys.executable, "-c", "import sys; sys.exit(42)"],
            "timeout_seconds": 30,
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests",
                "registry_id": "unit-tests",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            },
            {
                "id": "failing-check",
                "kind": "command",
                "purpose": "Failing check",
                "registry_id": "failing-check",
                "inputs": ["ai-dev-playbook/scripts/documents.py"],
                "applicability": {"kind": "always"},
            },
        ]
        self.create_v2_task("DEV-1", checks=checks)
        
        # Run 1: fails
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("failed: ", out)
        
        # Run 2: does NOT reuse the failure as passed, it re-runs
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("RUN: failing-check", out)
        self.assertNotIn("REUSE: failing-check", out)

    def test_index_sync_before_snapshot_in_regular_verify(self):
        self.create_v2_task("DEV-1")
        # Tamper with index to make it stale
        index = self.root / "docs/tasks/index.md"
        before_content = index.read_text(encoding="utf-8")
        index.write_text(before_content + "\n<!-- stale marker -->\n", encoding="utf-8")
        
        # Regular verify automatically syncs task index before snapshot
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        self.assertIn("Индекс задач синхронизирован перед проверкой.", out)
        # Verify index was rewritten back to valid format
        self.assertNotIn("<!-- stale marker -->", index.read_text(encoding="utf-8"))

    def test_v1_legacy_compatibility(self):
        # Create a v1 task (schema_version == 1)
        meta = {
            "schema_version": 1,
            "relations": [],
            "materials": [],
        }
        self.managed_task("DEV-1", status="in_progress", meta=meta)
        self.cli("index")
        
        # Running verify on v1 task uses legacy runner and produces v1 report
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, f"stdout: {out}\nstderr: {err}")
        reports = list((self.root / "work/verification/DEV-1").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        rep = verification.load_report(reports[0], req)
        self.assertEqual(rep["schema_version"], 1)

    def test_later_command_mutates_earlier_input_detected_as_stale(self):
        target_file = self.root / "target.txt"
        target_file.write_text("original", encoding="utf-8")
        indep_file = self.root / "independent.txt"
        indep_file.write_text("independent", encoding="utf-8")

        self.add_registry_check({
            "id": "mutator",
            "kind": "command",
            "purpose": "Mutator command",
            "argv": [sys.executable, "-c", "from pathlib import Path; Path('target.txt').write_text('mutated', encoding='utf-8')"],
            "timeout_seconds": 30,
            "applicability": {"kind": "always"},
        })
        checks = [
            {
                "id": "unit-tests",
                "kind": "command",
                "purpose": "Unit tests on target",
                "registry_id": "unit-tests",
                "inputs": ["target.txt"],
                "applicability": {"kind": "always"},
            },
            {
                "id": "mutator",
                "kind": "command",
                "purpose": "Mutates target.txt",
                "registry_id": "mutator",
                "inputs": ["independent.txt"],
                "applicability": {"kind": "always"},
            },
        ]
        self.create_v2_task("DEV-1", checks=checks, scope=["target.txt", "independent.txt"])

        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1, f"Expected verify failure due to stale input; out: {out}\nerr: {err}")
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ: входы проверки unit-tests изменились во время прогона: target.txt", out)
        self.assertIn("stale: ", out)
        self.assertEqual(target_file.read_text(encoding="utf-8"), "mutated")


if __name__ == "__main__":
    unittest.main()

