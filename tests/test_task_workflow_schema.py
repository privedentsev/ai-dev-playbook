import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"

import sys
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import documents
import playbook
import verification

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "task_workflow_v2"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


class LegacyPreservationTest(unittest.TestCase):
    """Старые карточки, конфигурации и отчёты v1 работают по прежнему контракту."""

    def test_legacy_v1_task_meta(self):
        """Legacy v1 task meta принимается по прежнему контракту"""
        v1_meta = {
            "schema_version": 1,
            "relations": [{"relation": "based_on", "target": "spec:test"}],
            "materials": [],
            "moves": []
        }
        errors = []
        result = documents._validate_task_meta(v1_meta, "task.md", errors)
        self.assertEqual(errors, [])
        self.assertEqual(result["schema_version"], 1)
        self.assertIsNone(result.get("workflow"))
        self.assertIsNone(result.get("verification_plan"))

    def test_legacy_v1_report(self):
        """Legacy v1 report принимается по прежнему контракту"""
        v1_report = {
            "schema_version": 1,
            "run_id": "legacy-run-1",
            "task_id": "PB-001",
            "policy_revision": 1,
            "cwd": "/repo",
            "started_at": "2026-09-29T10:00:00Z",
            "status": "passed",
            "inputs": ["README.md"],
            "snapshot_before": {"files": {"README.md": "abc"}},
            "snapshot_after": {"files": {"README.md": "abc"}},
            "inputs_unchanged": True,
            "checks": [
                {"id": "unit-tests", "status": "passed", "exit_code": 0}
            ]
        }
        validated = verification.validate_report(v1_report, _require)
        self.assertEqual(validated["schema_version"], 1)

    def test_task_card_without_workflow_has_none_route(self):
        """Карточка без workflow имеет route=none"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tasks_dir = tmp_root / "docs" / "tasks"
            archive_dir = tmp_root / "docs" / "archive" / "tasks"
            tasks_dir.mkdir(parents=True)
            archive_dir.mkdir(parents=True)
            paths = {
                "tasks": tasks_dir,
                "archive": archive_dir,
                "task_index": tasks_dir / "index.md"
            }
            card_content = (
                "# TEST-01: Legacy Task\n\n"
                "ID: TEST-01\n"
                "Status: ready\n"
                "Priority: P2\n"
                "Depends-On: -\n"
                "Executor: -\n"
                "Model: -\n"
                "Created: 2026-10-23T08:00:00+03:00\n"
                "Closed: -\n\n"
                "<!-- playbook-meta:start -->\n"
                "{\n"
                '  "schema_version": 1,\n'
                '  "relations": [],\n'
                '  "materials": []\n'
                "}\n"
                "<!-- playbook-meta:end -->\n"
            )
            (tasks_dir / "TEST-01.md").write_text(card_content, encoding="utf-8")
            records = playbook.read_tasks(tmp_root, paths)
            self.assertIn("TEST-01", records)
            self.assertIsNone(records["TEST-01"]["route"])


class TaskWorkflowV2ConfigTest(unittest.TestCase):
    """Валидация опционального блока task_workflow в project.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "ai-dev-playbook").mkdir()
        self.valid_tw = json.loads(
            (FIXTURES_DIR / "valid" / "project.json").read_text(encoding="utf-8")
        )["task_workflow"]

    def test_valid_task_workflow(self):
        """Валидный task_workflow v2 проходит валидацию"""
        validated = verification.validate_task_workflow(
            self.root, self.valid_tw, playbook.safe_path, _require
        )
        self.assertEqual(validated["schema_version"], 2)
        self.assertEqual(len(validated["registry"]), 4)

    def test_unknown_field_in_task_workflow_rejected(self):
        """Неизвестное поле task_workflow отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["unknown_key"] = "bad"
        with self.assertRaisesRegex(ValueError, "Неизвестные поля в task_workflow"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_schema_version_not_2_rejected(self):
        """schema_version≠2 в task_workflow отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "неподдерживаемая schema_version"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_duplicate_registry_id_rejected(self):
        """Дубликат id в registry отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["registry"].append(copy.deepcopy(tw["registry"][0]))
        with self.assertRaisesRegex(ValueError, "Повторный ID проверки в реестре"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_command_check_with_scenario_rejected(self):
        """command-проверка со scenario отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["registry"][0]["scenario"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "scenario не допускается для command-проверки"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_manual_check_with_argv_rejected(self):
        """manual-проверка с argv отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["registry"][2]["argv"] = ["echo", "bad"]
        with self.assertRaisesRegex(ValueError, "argv не допускается для manual-проверки"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_work_path_in_default_inputs_rejected(self):
        """work/-путь в default_inputs отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["registry"][0]["default_inputs"].append("work/cache.json")
        with self.assertRaisesRegex(ValueError, "не должен указывать на корень проекта или work"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )

    def test_checkpoint_with_unknown_check_rejected(self):
        """Чекпоинт с неизвестной проверкой отклоняется"""
        tw = copy.deepcopy(self.valid_tw)
        tw["checkpoints"]["custom"] = ["nonexistent-check-id"]
        with self.assertRaisesRegex(ValueError, "ссылается на неизвестную проверку"):
            verification.validate_task_workflow(
                self.root, tw, playbook.safe_path, _require
            )


class TaskMetadataV2Test(unittest.TestCase):
    """Валидация метаданных задачи v2: маршрут и план проверок."""

    def setUp(self):
        self.valid_meta = json.loads(
            (FIXTURES_DIR / "valid" / "task_metadata_v2.json").read_text(encoding="utf-8")
        )
        self.registry = {"unit-tests", "lint", "visual-check", "self-check"}

    def test_valid_v2_metadata(self):
        """Валидные метаданные задачи v2 проходят валидацию"""
        errors = []
        result = documents._validate_task_meta(
            self.valid_meta, "task.md", errors, registry=self.registry
        )
        self.assertEqual(errors, [])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["workflow"]["route"], "quick")
        self.assertEqual(result["verification_plan"]["revision"], 1)

    def test_allowed_routes(self):
        """Все допустимые маршруты quick/small/medium/large принимаются"""
        for route in ("quick", "small", "medium", "large"):
            meta = copy.deepcopy(self.valid_meta)
            meta["workflow"]["route"] = route
            errors = []
            result = documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
            self.assertEqual(errors, [])
            self.assertEqual(result["workflow"]["route"], route)

    def test_unknown_route_rejected(self):
        """Неизвестный route отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["workflow"]["route"] = "ultra_fast"
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("неизвестный route 'ultra_fast'" in e for e in errors))

    def test_unknown_schema_version_rejected(self):
        """schema_version=3 в meta отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["schema_version"] = 3
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("неизвестная версия 3" in e for e in errors))

    def test_unknown_field_in_workflow_rejected(self):
        """Неизвестное поле workflow отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["workflow"]["unexpected_key"] = 123
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("неизвестные поля" in e for e in errors))

    def test_duplicate_check_id_rejected(self):
        """Дубликат check id в verification_plan отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        dup = copy.deepcopy(meta["verification_plan"]["checks"][0])
        meta["verification_plan"]["checks"].append(dup)
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("повторный ID проверки 'unit-tests'" in e for e in errors))

    def test_arbitrary_argv_in_card_rejected(self):
        """Произвольный argv в карточке отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["verification_plan"]["checks"][0]["argv"] = ["pytest", "-v"]
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("произвольный argv запрещён; используйте registry_id" in e for e in errors))

    def test_unknown_registry_id_rejected(self):
        """Неизвестный registry_id отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["verification_plan"]["checks"][0]["registry_id"] = "nonexistent-cmd"
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("неизвестная команда реестра 'nonexistent-cmd'" in e for e in errors))

    def test_escape_path_in_scope_rejected(self):
        """Путь выхода за пределы в scope отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["verification_plan"]["scope"].append("../escape")
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("содержит '..' (выход за корень)" in e for e in errors))

    def test_work_path_in_scope_rejected(self):
        """work/-путь в scope отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["verification_plan"]["scope"].append("work/tmp")
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("не должен находиться внутри каталога work" in e for e in errors))

    def test_escape_path_in_inputs_rejected(self):
        """Путь выхода в inputs отклоняется"""
        meta = copy.deepcopy(self.valid_meta)
        meta["verification_plan"]["checks"][0]["inputs"].append("../parent_file")
        errors = []
        documents._validate_task_meta(meta, "task.md", errors, registry=self.registry)
        self.assertTrue(any("содержит '..' (выход за корень)" in e for e in errors))


class ReportV2Test(unittest.TestCase):
    """Валидация схемы отчёта verification v2."""

    def setUp(self):
        self.valid_report = json.loads(
            (FIXTURES_DIR / "valid" / "report_v2.json").read_text(encoding="utf-8")
        )

    def test_valid_report_v2(self):
        """Валидный отчёт verification v2 проходит валидацию"""
        validated = verification.validate_report(self.valid_report, _require)
        self.assertEqual(validated["schema_version"], 2)
        self.assertEqual(validated["route"], "quick")
        self.assertTrue(validated["coverage"]["satisfied"])

    def test_unknown_report_version_rejected(self):
        """schema_version=3 отчёта отклоняется"""
        rep = copy.deepcopy(self.valid_report)
        rep["schema_version"] = 3
        with self.assertRaisesRegex(ValueError, "Неподдерживаемая schema_version отчёта: 3"):
            verification.validate_report(rep, _require)

    def test_unknown_fields_in_report_rejected(self):
        """Неизвестные поля отчёта отклоняются"""
        rep = copy.deepcopy(self.valid_report)
        rep["extra_top_level"] = "bad"
        with self.assertRaisesRegex(ValueError, "Неизвестные поля в report v2"):
            verification.validate_report(rep, _require)

    def test_unknown_route_in_report_rejected(self):
        """Неизвестный route в отчёте отклоняется"""
        rep = copy.deepcopy(self.valid_report)
        rep["route"] = "mega_fast"
        with self.assertRaisesRegex(ValueError, "Неизвестный route в отчёте: 'mega_fast'"):
            verification.validate_report(rep, _require)

    def test_missing_reason_for_not_applicable_rejected(self):
        """not_applicable без reason отклоняется"""
        rep = copy.deepcopy(self.valid_report)
        rep["results"].append({
            "check_id": "integration-tests",
            "definition_hash": "def-hash-3",
            "kind": "command",
            "status": "not_applicable"
        })
        with self.assertRaisesRegex(ValueError, "для not_applicable обязательно непустое поле 'reason'"):
            verification.validate_report(rep, _require)

    def test_not_applicable_with_reason_accepted(self):
        """not_applicable с reason принимается"""
        rep = copy.deepcopy(self.valid_report)
        rep["results"].append({
            "check_id": "integration-tests",
            "definition_hash": "def-hash-3",
            "kind": "command",
            "status": "not_applicable",
            "reason": "База данных не затрагивается данным изменением"
        })
        rep["coverage"]["not_applicable"].append("integration-tests")
        rep["coverage"]["total_checks"] += 1
        validated = verification.validate_report(rep, _require)
        self.assertEqual(len(validated["results"]), 3)

    def test_missing_required_coverage_field_rejected(self):
        """Отсутствие coverage.satisfied отклоняется"""
        rep = copy.deepcopy(self.valid_report)
        del rep["coverage"]["satisfied"]
        with self.assertRaisesRegex(ValueError, "coverage: отсутствует поле 'satisfied'"):
            verification.validate_report(rep, _require)


class FixturesLoadTest(unittest.TestCase):
    """Сквозная проверка всех сохранённых файлов-фикстур."""

    def test_all_valid_fixtures(self):
        """Все valid-фикстуры загружаются"""
        valid_dir = FIXTURES_DIR / "valid"
        proj = json.loads((valid_dir / "project.json").read_text(encoding="utf-8"))
        verification.validate_task_workflow(
            ROOT, proj["task_workflow"], playbook.safe_path, _require
        )

        task_meta = json.loads((valid_dir / "task_metadata_v2.json").read_text(encoding="utf-8"))
        errors = []
        reg_map = {item["id"]: item for item in proj["task_workflow"]["registry"]}
        documents._validate_task_meta(task_meta, "task.json", errors, registry=reg_map, task_workflow_enabled=True)
        self.assertEqual(errors, [])

        report = json.loads((valid_dir / "report_v2.json").read_text(encoding="utf-8"))
        verification.validate_report(report, _require)

    def test_all_invalid_fixtures_fail(self):
        """Все invalid-фикстуры отклоняются"""
        invalid_dir = FIXTURES_DIR / "invalid"
        reg = {"unit-tests", "lint", "visual-check", "self-check"}

        meta_unknown_route = json.loads((invalid_dir / "unknown_route.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_unknown_route, "unknown_route.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("неизвестный route 'ultra_fast'" in e for e in errors))

        meta_unknown_schema = json.loads((invalid_dir / "unknown_schema_version.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_unknown_schema, "unknown_schema_version.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("неизвестная версия 3" in e for e in errors))

        meta_unknown_wf = json.loads((invalid_dir / "unknown_fields_workflow.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_unknown_wf, "unknown_fields_workflow.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("неизвестные поля" in e for e in errors))

        tw_unknown = json.loads((invalid_dir / "unknown_fields_task_workflow.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "Неизвестные поля в task_workflow"):
            verification.validate_task_workflow(ROOT, tw_unknown, playbook.safe_path, _require)

        meta_dup_id = json.loads((invalid_dir / "duplicate_check_id.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_dup_id, "duplicate_check_id.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("повторный ID проверки 'test-run'" in e for e in errors))

        meta_argv = json.loads((invalid_dir / "arbitrary_argv.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_argv, "arbitrary_argv.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("произвольный argv запрещён; используйте registry_id" in e for e in errors))

        meta_unknown_reg = json.loads((invalid_dir / "unknown_registry_id.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_unknown_reg, "unknown_registry_id.json", errors, registry={"unit-tests"}, task_workflow_enabled=True)
        self.assertTrue(any("неизвестная команда реестра 'nonexistent-command-id'" in e for e in errors))

        meta_escape_scope = json.loads((invalid_dir / "escape_path_scope.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_escape_scope, "escape_path_scope.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("содержит '..' (выход за корень)" in e for e in errors))

        meta_escape_input = json.loads((invalid_dir / "escape_path_input.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_escape_input, "escape_path_input.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("не должен находиться внутри каталога work" in e for e in errors))

        report_unknown = json.loads((invalid_dir / "unknown_report_version.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "Неподдерживаемая schema_version отчёта: 3"):
            verification.validate_report(report_unknown, _require)

        report_missing_reason = json.loads((invalid_dir / "missing_reason_not_applicable.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "для not_applicable обязательно непустое поле 'reason'"):
            verification.validate_report(report_missing_reason, _require)

        meta_missing_plan = json.loads((invalid_dir / "missing_plan.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_missing_plan, "missing_plan.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("verification_plan" in e for e in errors))

        meta_missing_inputs = json.loads((invalid_dir / "missing_inputs.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_missing_inputs, "missing_inputs.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("inputs" in e for e in errors))

        meta_file_no_path = json.loads((invalid_dir / "file_without_path.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_file_no_path, "file_without_path.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("path" in e for e in errors))

        meta_bad_env = json.loads((invalid_dir / "invalid_environment_names.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_bad_env, "invalid_environment_names.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("names" in e for e in errors))

        report_no_prov = json.loads((invalid_dir / "report_without_provenance.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "отсутствует поле происхождения"):
            verification.validate_report(report_no_prov, _require)

        report_no_scen = json.loads((invalid_dir / "manual_without_scenario_hash.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "scenario_hash обязателен для manual-результата"):
            verification.validate_report(report_no_scen, _require)

        tw_bad_inp = json.loads((invalid_dir / "registry_unknown_input_field.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "неизвестные поля во входе 'file'"):
            verification.validate_task_workflow(ROOT, tw_bad_inp, playbook.safe_path, _require)

        meta_incompat_kind = json.loads((invalid_dir / "incompatible_registry_kind.json").read_text(encoding="utf-8"))
        errors = []
        reg_map = {"unit-tests": {"id": "unit-tests", "kind": "command"}, "visual-check": {"id": "visual-check", "kind": "manual"}}
        documents._validate_task_meta(meta_incompat_kind, "incompatible_registry_kind.json", errors, registry=reg_map, task_workflow_enabled=True)
        self.assertTrue(any("несовместимый kind проверки 'c1': в карточке 'command', в реестре 'manual'" in e for e in errors))

        meta_null_plan = json.loads((invalid_dir / "null_plan.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_null_plan, "null_plan.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("verification_plan должен быть объектом" in e for e in errors))

        meta_null_inputs = json.loads((invalid_dir / "null_inputs.json").read_text(encoding="utf-8"))
        errors = []
        documents._validate_task_meta(meta_null_inputs, "null_inputs.json", errors, registry=reg, task_workflow_enabled=True)
        self.assertTrue(any("inputs должен быть списком" in e for e in errors))

        rep_no_cmd_snaps = json.loads((invalid_dir / "no_command_snapshots.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "snapshot_before обязателен для command-результата"):
            verification.validate_report(rep_no_cmd_snaps, _require)

        rep_bad_snaps = json.loads((invalid_dir / "invalid_snapshots_and_actor.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "actor.name должен быть непустой строкой"):
            verification.validate_report(rep_bad_snaps, _require)

        rep_bad_cand = json.loads((invalid_dir / "invalid_candidate_snapshot.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "candidate_snapshot.files должен быть объектом"):
            verification.validate_report(rep_bad_cand, _require)


class PlaybookReadTasksRouteIntegrationTest(unittest.TestCase):
    """Интеграция маршрута задачи в read_tasks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tasks_dir = self.root / "docs" / "tasks"
        self.archive_dir = self.root / "docs" / "archive" / "tasks"
        self.tasks_dir.mkdir(parents=True)
        self.archive_dir.mkdir(parents=True)
        self.paths = {
            "tasks": self.tasks_dir,
            "archive": self.archive_dir,
            "task_index": self.tasks_dir / "index.md"
        }

    def _write_v2_card(self, ident, route="quick"):
        content = (
            f"# {ident}: Sample Task\n\n"
            f"ID: {ident}\n"
            "Status: in_progress\n"
            "Priority: P2\n"
            "Depends-On: -\n"
            "Executor: Antigravity\n"
            "Model: Gemini 3.8 Flash (High)\n"
            "Created: 2026-10-23T08:00:00+03:00\n"
            "Closed: -\n\n"
            "<!-- playbook-meta:start -->\n"
            "```json\n"
            "{\n"
            '  "schema_version": 2,\n'
            '  "workflow": {\n'
            f'    "route": "{route}",\n'
            '    "reason": "Тест"\n'
            "  },\n"
            '  "verification_plan": {\n'
            '    "revision": 1,\n'
            '    "rationale": "Тест",\n'
            '    "scope": ["tests"],\n'
            '    "checks": [\n'
            "      {\n"
            '        "id": "c1",\n'
            '        "kind": "command",\n'
            '        "purpose": "Тест",\n'
            '        "registry_id": "unit-tests",\n'
            '        "inputs": ["tests"]\n'
            "      }\n"
            "    ]\n"
            "  }\n"
            "}\n"
            "```\n"
            "<!-- playbook-meta:end -->\n"
        )
        (self.tasks_dir / f"{ident}.md").write_text(content, encoding="utf-8")

    def test_read_tasks_extracts_route(self):
        """read_tasks извлекает route из v2-карточки"""
        self._write_v2_card("PB-100", route="quick")
        records = playbook.read_tasks(self.root, self.paths)
        self.assertIn("PB-100", records)
        self.assertEqual(records["PB-100"]["route"], "quick")

    def test_read_tasks_rejects_unknown_route(self):
        """read_tasks отклоняет неизвестный route"""
        self._write_v2_card("PB-101", route="invalid_route")
        with self.assertRaisesRegex(ValueError, "неизвестный route 'invalid_route'"):
            playbook.read_tasks(self.root, self.paths)


class AuditFindingsCoverageTest(unittest.TestCase):
    """Целевые тесты по замечаниям аудита PB-031-A1..A4."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Create minimal project layout
        (self.root / "docs" / "tasks").mkdir(parents=True)
        (self.root / "docs" / "archive" / "tasks").mkdir(parents=True)
        (self.root / "docs" / "specs").mkdir(parents=True)
        (self.root / "docs" / "archive" / "specs").mkdir(parents=True)
        (self.root / "tests").mkdir(parents=True)
        (self.root / "ai-dev-playbook").mkdir(parents=True)

        (self.root / "docs" / "tasks" / "index.md").write_text("# Индекс задач\n", encoding="utf-8")
        (self.root / "docs" / "specs" / "index.md").write_text("# Индекс specs\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Инструкции\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Readme\n", encoding="utf-8")
        (self.root / "ai-dev-playbook" / "VERSION").write_text((ROOT / "ai-dev-playbook" / "VERSION").read_text(), encoding="utf-8")
        (self.root / "ai-dev-playbook" / "manifest.json").write_text(
            (ROOT / "ai-dev-playbook" / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
        )

        self.project_config = {
            "schema_version": 1,
            "playbook_version": (ROOT / "ai-dev-playbook" / "VERSION").read_text().strip(),
            "name": "test-proj",
            "setup_status": "configured",
            "paths": {
                "docs": "docs",
                "tasks": "docs/tasks",
                "archive": "docs/archive/tasks",
                "task_index": "docs/tasks/index.md"
            },
            "documents": {
                "schema_version": 1,
                "collections": {
                    "spec": {
                        "active": "docs/specs",
                        "archive": "docs/archive/specs",
                        "index": "docs/specs/index.md"
                    }
                }
            },
            "components": [
                {"name": "core", "code": "ai-dev-playbook", "tests": ["tests"], "migrations": None}
            ],
            "commands": {
                "test": "python3 -m unittest"
            },
            "verification": {
                "inputs": ["README.md"],
                "checks": [{"id": "t", "argv": ["true"], "timeout_seconds": 10}]
            },
            "git": {
                "commit": "explicit_request",
                "push": "explicit_request",
                "merge": "explicit_request",
                "release": "explicit_request"
            }
        }
        (self.root / "ai-dev-playbook" / "project.json").write_text(
            json.dumps(self.project_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.paths = {
            "docs": self.root / "docs",
            "tasks": self.root / "docs" / "tasks",
            "archive": self.root / "docs" / "archive" / "tasks",
            "task_index": self.root / "docs" / "tasks" / "index.md"
        }

    def _write_card(self, ident, meta_dict):
        card = (
            f"# {ident}: Task\n\n"
            f"ID: {ident}\n"
            "Status: in_progress\n"
            "Priority: P2\n"
            "Depends-On: -\n"
            "Executor: Antigravity\n"
            "Model: Gemini 3.8 Flash (High)\n"
            "Created: 2026-10-23T08:00:00+03:00\n"
            "Closed: -\n\n"
            "<!-- playbook-meta:start -->\n"
            "```json\n"
            f"{json.dumps(meta_dict, ensure_ascii=False, indent=2)}\n"
            "```\n"
            "<!-- playbook-meta:end -->\n"
        )
        (self.root / "docs" / "tasks" / f"{ident}.md").write_text(card, encoding="utf-8")

    def test_PB_031_A1_symlink_in_scope_and_inputs_rejected(self):
        # Включаем task_workflow
        """PB-031-A1: symlink в scope/inputs отклонён"""
        config = copy.deepcopy(self.project_config)
        config["task_workflow"] = {
            "schema_version": 2,
            "registry": [
                {
                    "id": "t1",
                    "kind": "command",
                    "argv": ["true"],
                    "timeout_seconds": 10,
                    "default_inputs": ["tests"]
                }
            ]
        }
        (self.root / "ai-dev-playbook" / "project.json").write_text(json.dumps(config), encoding="utf-8")

        # Создаем симлинки: внешний, внутренний, и родительский каталог
        (self.root / "ext-link").symlink_to("/tmp", target_is_directory=True)
        (self.root / "int-link").symlink_to("tests", target_is_directory=True)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "dir-link").symlink_to(self.root / "tests", target_is_directory=True)

        meta = {
            "schema_version": 2,
            "workflow": {"route": "quick"},
            "verification_plan": {
                "revision": 1,
                "rationale": "Тест симлинков",
                "scope": ["tests"],
                "checks": [
                    {
                        "id": "c1",
                        "kind": "command",
                        "purpose": "Тест",
                        "registry_id": "t1",
                        "inputs": ["tests"]
                    }
                ]
            },
            "relations": [],
            "materials": []
        }

        # 1. Проверяем scope с внешним симлинком
        m1 = copy.deepcopy(meta)
        m1["verification_plan"]["scope"] = ["ext-link"]
        self._write_card("TEST-01", m1)
        data, paths = playbook.config(self.root)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, r"(?i)символическая ссылка в управляемом пути: ext-link"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

        # 2. Проверяем scope с внутренним симлинком
        m2 = copy.deepcopy(meta)
        m2["verification_plan"]["scope"] = ["int-link"]
        self._write_card("TEST-01", m2)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, r"(?i)символическая ссылка в управляемом пути: int-link"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

        # 3. Проверяем inputs с путем через промежуточный симлинк
        m3 = copy.deepcopy(meta)
        m3["verification_plan"]["checks"][0]["inputs"] = ["sub/dir-link/some_file.py"]
        self._write_card("TEST-01", m3)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, r"(?i)символическая ссылка в управляемом пути: sub/dir-link/some_file.py"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

        # 4. Проверяем объектный вход {"type": "file", "path": "ext-link"}
        m4 = copy.deepcopy(meta)
        m4["verification_plan"]["checks"][0]["inputs"] = [{"type": "file", "path": "ext-link"}]
        self._write_card("TEST-01", m4)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, r"(?i)символическая ссылка в управляемом пути: ext-link"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

        # 5. Проверяем absent-путь (не существует, но не симлинк) — разрешён
        m5 = copy.deepcopy(meta)
        m5["verification_plan"]["scope"] = ["tests/nonexistent_dir/absent_file.py"]
        m5["verification_plan"]["checks"][0]["inputs"] = ["tests/nonexistent_dir/absent_file.py"]
        self._write_card("TEST-01", m5)
        records = playbook.read_tasks(self.root, paths)
        # load_catalog должен успешно принять absent-путь без ошибки
        cat = documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)
        self.assertIsNotNone(cat)

    def test_PB_031_A4_v2_card_rejected_without_task_workflow_opt_in(self):
        # project_config НЕ содержит task_workflow
        """PB-031-A4: v2-карточка без opt-in отклонена"""
        meta = {
            "schema_version": 2,
            "workflow": {"route": "quick"},
            "verification_plan": {
                "revision": 1,
                "rationale": "Тест opt-in",
                "scope": ["tests"],
                "checks": [
                    {
                        "id": "c1",
                        "kind": "command",
                        "purpose": "Тест",
                        "registry_id": "DOES-NOT-EXIST",
                        "inputs": ["tests"]
                    }
                ]
            },
            "relations": [],
            "materials": []
        }
        self._write_card("TEST-02", meta)
        data, paths = playbook.config(self.root)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, "task_workflow v2 не включён в project.json"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

    def test_PB_031_A2_null_plan_and_null_inputs_rejected(self):
        # Включение task_workflow в project.json
        """PB-031-A2: null plan/inputs отклонены"""
        proj = json.loads((self.root / "ai-dev-playbook/project.json").read_text(encoding="utf-8"))
        tw = json.loads((FIXTURES_DIR / "valid/project.json").read_text(encoding="utf-8"))["task_workflow"]
        proj["task_workflow"] = tw
        (self.root / "ai-dev-playbook/project.json").write_text(json.dumps(proj), encoding="utf-8")
        data, paths = playbook.config(self.root)

        valid_meta = json.loads((FIXTURES_DIR / "valid/task_metadata_v2.json").read_text(encoding="utf-8"))

        # 1. verification_plan: null отклоняется
        m_null_plan = copy.deepcopy(valid_meta)
        m_null_plan["verification_plan"] = None
        self._write_card("TEST-003", m_null_plan)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, "verification_plan должен быть объектом"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

        # 2. checks[0].inputs: null отклоняется
        m_null_inp = copy.deepcopy(valid_meta)
        m_null_inp["verification_plan"]["checks"][0]["inputs"] = None
        self._write_card("TEST-004", m_null_inp)
        records = playbook.read_tasks(self.root, paths)
        with self.assertRaisesRegex(ValueError, "inputs должен быть списком"):
            documents.load_catalog(self.root, data, paths, records, playbook.safe_path, playbook.require)

    def test_PB_031_A3_report_snapshots_and_actor_strictly_validated(self):
        """PB-031-A3: snapshots и actor отчёта валидируются строго"""
        valid_report = json.loads((FIXTURES_DIR / "valid/report_v2.json").read_text(encoding="utf-8"))

        # 1. no command snapshots: passed без snapshot_before/snapshot_after отклоняется
        r1 = copy.deepcopy(valid_report)
        del r1["results"][0]["snapshot_before"]
        with self.assertRaisesRegex(ValueError, "snapshot_before обязателен для command-результата"):
            verification.validate_report(r1, playbook.require)

        r2 = copy.deepcopy(valid_report)
        del r2["results"][0]["snapshot_after"]
        with self.assertRaisesRegex(ValueError, "snapshot_after обязателен для command-результата"):
            verification.validate_report(r2, playbook.require)

        # 2. invalid snapshots and actor
        r3 = copy.deepcopy(valid_report)
        r3["results"][0]["actor"] = {"name": 42}
        with self.assertRaisesRegex(ValueError, "actor.name должен быть непустой строкой"):
            verification.validate_report(r3, playbook.require)

        r4 = copy.deepcopy(valid_report)
        r4["results"][0]["input_snapshot"] = {"files": 123}
        with self.assertRaisesRegex(ValueError, "input_snapshot.files должен быть объектом"):
            verification.validate_report(r4, playbook.require)

        r5 = copy.deepcopy(valid_report)
        r5["results"][0]["snapshot_before"] = "bad"
        with self.assertRaisesRegex(ValueError, "snapshot_before должен быть объектом"):
            verification.validate_report(r5, playbook.require)

        r6 = copy.deepcopy(valid_report)
        r6["results"][0]["snapshot_after"] = False
        with self.assertRaisesRegex(ValueError, "snapshot_after должен быть объектом"):
            verification.validate_report(r6, playbook.require)

        # 3. manual candidate_snapshot invalid
        r7 = copy.deepcopy(valid_report)
        r7["results"][1]["candidate_snapshot"] = {"files": 123}
        with self.assertRaisesRegex(ValueError, "candidate_snapshot.files должен быть объектом"):
            verification.validate_report(r7, playbook.require)


class OldValidatorRejectionTest(unittest.TestCase):
    """Старые версии CLI/валидаторов не принимают новые схемы молча."""

    def test_legacy_verification_validate_rejects_task_workflow(self):
        # Старый verification.validate ожидает {inputs, checks} и отклоняет task_workflow {registry}
        """Старый валидатор отклоняет task_workflow"""
        valid_tw = json.loads(
            (FIXTURES_DIR / "valid" / "project.json").read_text(encoding="utf-8")
        )["task_workflow"]
        with self.assertRaisesRegex(ValueError, "verification.inputs должен быть непустым списком"):
            verification.validate(ROOT, valid_tw, playbook.safe_path, _require)

    def test_legacy_v1_task_meta_rejects_v2_schema_when_schema_version_is_1(self):
        # Если v2 поля передаются в v1 схему (schema_version: 1), TASK_FIELDS_V1 отклоняет неизвестные поля
        """Старый валидатор отклоняет v2-поля при schema_version=1"""
        meta = {
            "schema_version": 1,
            "workflow": {"route": "quick"},
            "verification_plan": {"revision": 1, "rationale": "test", "scope": ["tests"], "checks": []},
            "relations": [],
            "materials": []
        }
        errors = []
        documents._validate_task_meta(meta, "task.md", errors)
        self.assertTrue(any("неизвестные поля" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
