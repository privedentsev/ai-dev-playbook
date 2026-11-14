import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_playbook import playbook, write_card

updating = playbook.updating
REPO = Path(playbook.__file__).resolve().parents[2]
REPO_VERSION = (REPO / "ai-dev-playbook" / "VERSION").read_text(encoding="utf-8").strip()

PASS_CHECK = {"id": "unit", "argv": ["python3", "-c", "print('ok')"], "timeout_seconds": 30}
FAIL_CHECK = {"id": "fail", "argv": ["python3", "-c", "import sys; sys.exit(3)"],
              "timeout_seconds": 30}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = playbook.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliCase(unittest.TestCase):
    """Базовый case CLI: временный установленный комплект и запуск
    playbook.py."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def init(self, *args):
        return run_cli("init", str(self.root), *args)

    def cli(self, *args):
        return run_cli("--root", str(self.root), *args)

    def install(self, verification=None, version="0.0.2"):
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["name"] = "test-project"
        project["setup_status"] = "configured"
        project["commands"]["test"] = "python3 -m unittest discover -s tests -v"
        if verification is not None:
            project["verification"] = verification
        if version != REPO_VERSION:
            project.pop("documents", None)
            project["playbook_version"] = version
            path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            version_file = self.root / "ai-dev-playbook" / "VERSION"
            version_file.write_text(version + "\n", encoding="utf-8")
            manifest_path = self.root / "ai-dev-playbook" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["playbook_version"] = version
            manifest["files"]["ai-dev-playbook/VERSION"] = updating.sha256(version_file.read_bytes())
            manifest["release_id"] = updating.fingerprint(manifest["files"])
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        else:
            path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            doc = self.root / "docs" / name
            doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"),
                           encoding="utf-8")

    def add_task(self, ident="DEV-1", status="ready"):
        write_card(self.root / "docs" / "tasks", ident, status)
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def report(self, ident="DEV-1"):
        reports = list((self.root / "work" / "verification" / ident).glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        return json.loads(reports[0].read_text(encoding="utf-8")), reports[0].parent

    def build_release(self, dest, version, updates_from=None, mutate=None,
                      with_release_json=True):
        """Собрать фикстурный артефакт релиза из комплекта репозитория."""
        dest = Path(dest)
        kit = dest / "ai-dev-playbook"
        shutil.copytree(REPO / "ai-dev-playbook", kit,
                        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc",
                                                      "manifest.json"))
        for name in ("project.json", "resources.json", "manifest.json"):
            (kit / name).unlink(missing_ok=True)
        (kit / "VERSION").write_text(version + "\n", encoding="utf-8")
        updates = {"schema_version": 1, "updates_from": updates_from or {}}
        (kit / "updates.json").write_text(json.dumps(updates) + "\n", encoding="utf-8")
        if mutate:
            mutate(kit)
        if with_release_json:
            files = updating.managed_files(dest, playbook.safe_path, playbook.require)
            hashes = {rel: updating.sha256(b) for rel, b in files.items()}
            (dest / "release.json").write_text(json.dumps(
                {"schema_version": 1, "version": version,
                 "release_id": updating.fingerprint(hashes), "files": hashes,
                 "updates_from": updates["updates_from"]},
                indent=2) + "\n", encoding="utf-8")
        return dest

    def manifest(self):
        return json.loads((self.root / "ai-dev-playbook" / "manifest.json")
                          .read_text(encoding="utf-8"))

    def fixture_release(self, mutate=None, updates_from=None, version="0.0.3",
                        with_release_json=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if updates_from is None:
            updates_from = {"0.0.2": {"steps": []}}
        return self.build_release(tmp.name, version, mutate=mutate,
                                  updates_from=updates_from,
                                  with_release_json=with_release_json)


class InitTest(CliCase):
    """init: preview без записи, apply создаёт комплект, конфликты и
    повторное применение."""
    def test_preview_writes_nothing(self):
        """init без --apply — preview без записи файлов"""
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertIn("Предпросмотр", out)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_apply_creates_kit(self):
        """init --apply создаёт комплект в проекте"""
        code, out, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        for relative in ("ai-dev-playbook/VERSION", "ai-dev-playbook/project.json",
                         "ai-dev-playbook/resources.json", "ai-dev-playbook/scripts/playbook.py",
                         "ai-dev-playbook/rules/tasks.md", "ai-dev-playbook/templates/task.md",
                         "docs/tasks/index.md", "AGENTS.md", "CHANGELOG.md"):
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_conflict_aborts_before_writes(self):
        """Конфликт существующего файла отменяет init до записей"""
        mine = self.root / "AGENTS.md"
        mine.write_text("mine", encoding="utf-8")
        code, _, err = self.init("--apply")
        self.assertEqual(code, 1)
        self.assertIn("Конфликт", err)
        self.assertEqual(mine.read_text(encoding="utf-8"), "mine")
        self.assertFalse((self.root / "ai-dev-playbook").exists())
        self.assertFalse((self.root / "docs").exists())

    def test_reapply_conflicts_and_keeps_files(self):
        """Повторный init конфликтует и сохраняет существующие файлы"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.init("--apply")
        self.assertEqual(code, 1)
        self.assertIn("Конфликт", err)
        self.assertTrue((self.root / "ai-dev-playbook" / "VERSION").is_file())

    def test_install_over_source_rejected(self):
        """init поверх исходного репозитория комплекта отклоняется"""
        source = Path(playbook.__file__).resolve().parents[2]
        code, _, err = run_cli("init", str(source), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("исходного комплекта", err)


class CheckTest(CliCase):
    """check: свежая установка, готовность setup, битые ссылки,
    устаревший индекс, отсутствие комплекта."""
    def test_fresh_install_passes_check(self):
        """Свежая установка проходит check"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("OK:", out)

    def test_ready_rejects_unfinished_setup(self):
        """check --ready отклоняет незаконченную настройку"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 1)
        self.assertIn("Первичная настройка не завершена", err)

    def test_ready_passes_after_setup(self):
        """check --ready проходит после завершения настройки"""
        self.install()
        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)
        self.assertIn("OK:", out)

    def test_broken_link_detected(self):
        """Битая ссылка в документации обнаруживается check"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        doc = self.root / "docs" / "product.md"
        doc.write_text(doc.read_text(encoding="utf-8") + "\n[битая](missing.md)\n",
                       encoding="utf-8")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("битая", err)

    def test_stale_index_detected(self):
        """Устаревший индекс задач обнаруживается check"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "ready")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Индекс устарел", err)

    def test_missing_playbook_fails(self):
        """check без установленного playbook падает"""
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err)

    def test_check_warns_unconfirmed_model(self):
        """check предупреждает о неподтверждённой модели исполнителя"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review",
                   executor="Codex", model="GPT-6 (не подтверждено)")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("ВНИМАНИЕ: DEV-1", out)
        self.assertIn("не подтверждено", out)
        self.assertIn("OK:", out)

    def test_check_no_warning_for_marker_in_code(self):
        """Маркер модели в коде карточки не даёт предупреждения"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "ready")
        card.write_text(card.read_text(encoding="utf-8") +
                        "\nКонвенция: `модель: X (не подтверждено)` в коде.\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)
        self.assertNotIn("ВНИМАНИЕ", out)

    def test_check_no_warning_for_archived_card(self):
        """Архивная карточка не даёт предупреждения о модели"""
        self.install()
        write_card(self.root / "docs" / "archive" / "tasks", "DEV-1", "done")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)
        self.assertNotIn("ВНИМАНИЕ", out)


class VerifyTest(CliCase):
    """verify: dry-run, блоки конфигурации, записи отчётов и логов
    прогонов."""
    def install_verified(self, checks):
        self.install(verification={"inputs": ["docs"], "checks": checks})

    def test_dry_run_prints_plan_without_work(self):
        """verify --dry-run печатает план без создания work"""
        self.install_verified([PASS_CHECK])
        write_card(self.root / "docs" / "tasks", "DEV-1", "ready")
        code, out, err = self.cli("verify", "DEV-1", "--dry-run")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["task_id"], "DEV-1")
        self.assertEqual([item["id"] for item in plan["checks"]], ["unit"])
        self.assertFalse((self.root / "work").exists())

    def test_missing_verification_block(self):
        """verify без блока верификации в project.json отклоняется"""
        self.install()
        self.add_task()
        code, _, err = self.cli("verify", "DEV-1", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("Нет verification", err)

    def test_unknown_task_rejected(self):
        """verify несуществующей задачи отклоняется"""
        self.install_verified([PASS_CHECK])
        code, _, err = self.cli("verify", "DEV-9")
        self.assertEqual(code, 1)
        self.assertIn("Нет задачи", err)

    def test_draft_task_rejected(self):
        """verify draft-задачи отклоняется"""
        self.install_verified([PASS_CHECK])
        self.add_task(status="draft")
        code, _, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("verify требует активную задачу", err)

    def test_passed_run_writes_report_and_logs(self):
        """Успешный прогон пишет report и логи проверок"""
        self.install_verified([PASS_CHECK])
        self.add_task()
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, err + out)
        report, directory = self.report()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["inputs_unchanged"])
        (result,) = report["checks"]
        self.assertEqual((result["status"], result["exit_code"]), ("passed", 0))
        self.assertIn("ok", (directory / "unit.stdout.log").read_text(encoding="utf-8"))

    def test_failed_check_skips_rest(self):
        """Упавшая проверка пропускает остальные в прогоне"""
        self.install_verified([FAIL_CHECK, PASS_CHECK])
        self.add_task()
        code, _, _ = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        report, _ = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][0]["status"], "failed")
        self.assertEqual(report["checks"][0]["exit_code"], 3)
        self.assertEqual(report["checks"][1]["status"], "skipped")

    def test_modified_input_marks_stale(self):
        """Изменённый вход помечает результат проверки stale"""
        change = {"id": "edit",
                  "argv": ["python3", "-c", "open('docs/tasks/DEV-1.md', 'a').write('x')"],
                  "timeout_seconds": 30}
        self.install_verified([change])
        self.add_task()
        code, _, _ = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        report, _ = self.report()
        self.assertEqual(report["status"], "stale")
        self.assertFalse(report["inputs_unchanged"])

    def test_timeout_check(self):
        """Превышение timeout проверки фиксируется в отчёте"""
        hang = {"id": "hang", "argv": ["python3", "-c", "import time; time.sleep(30)"],
                "timeout_seconds": 0.5}
        self.install_verified([hang])
        self.add_task()
        code, _, _ = self.cli("verify", "DEV-1")
        self.assertEqual(code, 1)
        report, _ = self.report()
        self.assertEqual(report["checks"][0]["status"], "timeout")
        self.assertEqual(report["status"], "failed")


class CloseTest(CliCase):
    """close: переходы done/cancelled, маркеры готовности к аудиту,
    обновление входящих ссылок."""
    def test_close_done_moves_stamps_and_indexes(self):
        """close done переносит карточку, штампует и обновляет индекс"""
        self.install()
        self.add_task(status="in_review")
        code, out, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)
        archived = self.root / "docs" / "archive" / "tasks" / "DEV-1.md"
        self.assertTrue(archived.is_file())
        self.assertFalse((self.root / "docs" / "tasks" / "DEV-1.md").exists())
        text = archived.read_text(encoding="utf-8")
        self.assertIn("Status: done", text)
        self.assertRegex(text, r"(?m)^Closed: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_close_done_requires_in_review(self):
        """close done требует статус in_review"""
        self.install()
        self.add_task(status="ready")
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("in_review", err)
        self.assertTrue((self.root / "docs" / "tasks" / "DEV-1.md").exists())
        self.assertFalse((self.root / "docs" / "archive" / "tasks" / "DEV-1.md").exists())

    def test_close_cancelled_from_draft(self):
        """close cancelled работает из draft"""
        self.install()
        self.add_task(status="draft")
        code, _, err = self.cli("close", "DEV-1", "--status", "cancelled")
        self.assertEqual(code, 0, err)
        text = (self.root / "docs" / "archive" / "tasks" / "DEV-1.md").read_text(encoding="utf-8")
        self.assertIn("Status: cancelled", text)

    def test_close_unknown_task(self):
        """close несуществующей задачи отклоняется"""
        self.install()
        code, _, err = self.cli("close", "DEV-9")
        self.assertEqual(code, 1)
        self.assertIn("Нет задачи", err)

    def test_close_failure_keeps_original_state(self):
        """Сбой close сохраняет исходное состояние карточки"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review",
                   created="2099-01-01T00:00:00+00:00")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("раньше Created", err)
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        self.assertTrue(card.is_file())
        self.assertFalse((self.root / "docs" / "archive" / "tasks" / "DEV-1.md").exists())
        self.assertIn("Status: in_review", card.read_text(encoding="utf-8"))
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_close_updates_incoming_links(self):
        """close обновляет входящие ссылки на карточку"""
        self.install()
        self.add_task(status="in_review")
        peer = self.root / "docs" / "tasks" / "DEV-2.md"
        write_card(self.root / "docs" / "tasks", "DEV-2", "draft")
        peer.write_text(peer.read_text(encoding="utf-8") + "\nСм. [DEV-1](DEV-1.md).\n",
                        encoding="utf-8")
        roadmap = self.root / "docs" / "roadmap.md"
        roadmap.write_text(roadmap.read_text(encoding="utf-8") + "\n[DEV-1](tasks/DEV-1.md)\n",
                           encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)
        self.assertIn("(archive/tasks/DEV-1.md)", roadmap.read_text(encoding="utf-8"))
        self.assertIn("(../archive/tasks/DEV-1.md)", peer.read_text(encoding="utf-8"))
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_close_relocates_card_own_links(self):
        """close переносит собственные ссылки карточки в архив"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        card.write_text(card.read_text(encoding="utf-8") + "\nСм. [Продукт](../product.md).\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)
        text = (self.root / "docs" / "archive" / "tasks" / "DEV-1.md").read_text(encoding="utf-8")
        self.assertIn("(../../product.md)", text)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_close_relocates_card_self_link(self):
        """close корректно обрабатывает самоссылку карточки"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        card.write_text(card.read_text(encoding="utf-8") + "\n[Сама карточка](DEV-1.md)\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)
        text = (self.root / "docs" / "archive" / "tasks" / "DEV-1.md").read_text(encoding="utf-8")
        self.assertIn("[Сама карточка](DEV-1.md)", text)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_close_done_requires_confirmed_model(self):
        """close done требует подтверждённую модель исполнителя"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review",
                   executor="Codex", model="GPT-6 (не подтверждено)")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("не подтверждена", err)
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        self.assertTrue(card.is_file())
        text = card.read_text(encoding="utf-8")
        card.write_text(text.replace("GPT-6 (не подтверждено)", "GPT-6 Sol"),
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)

    def test_close_done_blocked_by_audit_marker(self):
        """close done блокируется маркером аудита"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        card.write_text(card.read_text(encoding="utf-8") +
                        "\n## Аудит 2026-09-29T10:00:00+03:00; исполнитель: Codex; "
                        "модель: GPT-6 (не подтверждено).\n\nБез находок.\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("не подтверждена", err)
        self.assertTrue(card.is_file())

    def test_close_done_unknown_model_allowed(self):
        """Модель «неизвестна» допускается при close done"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review",
                   executor="Codex", model="неизвестна")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)

    def test_close_done_blocked_by_work_record_marker(self):
        """close done блокируется маркером записи о работе"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review",
                   executor="Devin", model="SWE-2 High")
        card.write_text(card.read_text(encoding="utf-8") +
                        "\n## Выполнение и проверки\n\nМодель: GPT-6 (не подтверждено)\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("не подтверждена", err)
        self.assertTrue(card.is_file())
        text = card.read_text(encoding="utf-8")
        card.write_text(text.replace("GPT-6 (не подтверждено)", "GPT-6 Sol"),
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)

    def test_close_done_marker_in_code_allowed(self):
        """Маркер в коде карточки не блокирует close done"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        card.write_text(card.read_text(encoding="utf-8") +
                        "\nКонвенция: `модель: X (не подтверждено)` — описание в коде.\n\n"
                        "```\nпример маркера: (не подтверждено)\n```\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)

    def test_close_done_marker_in_tilde_fence_allowed(self):
        """Маркер в tilde-fence не блокирует close done"""
        self.install()
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        card.write_text(card.read_text(encoding="utf-8") +
                        "\n~~~\nпример маркера: (не подтверждено)\n~~~\n",
                        encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err)

    def test_close_rolls_back_on_index_conflict(self):
        """Конфликт индекса откатывает close к исходному состоянию"""
        self.install()
        self.add_task(status="in_review")
        roadmap = self.root / "docs" / "roadmap.md"
        before = roadmap.read_text(encoding="utf-8") + "\n[DEV-1](tasks/DEV-1.md)\n"
        roadmap.write_text(before, encoding="utf-8")
        index = self.root / "docs" / "tasks" / "index.md"
        foreign = "# Чужой документ\n"
        index.write_text(foreign, encoding="utf-8")
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("чужой документ", err)
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        self.assertTrue(card.is_file())
        self.assertIn("Status: in_review", card.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "docs" / "archive" / "tasks" / "DEV-1.md").exists())
        self.assertEqual(roadmap.read_text(encoding="utf-8"), before)
        self.assertEqual(index.read_text(encoding="utf-8"), foreign)


class ManifestTest(CliCase):
    """manifest внедрения: запись при init, подстановка VERSION,
    проверка config и резервные пути."""
    def test_init_writes_manifest(self):
        """init записывает manifest внедрения комплекта"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        manifest = self.manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["playbook_version"],
                         (self.root / "ai-dev-playbook" / "VERSION").read_text().strip())
        self.assertEqual(manifest["origin"], "init")
        self.assertTrue(manifest["release_id"].startswith("sha256:"))
        for rel, digest in manifest["files"].items():
            self.assertTrue(rel.startswith("ai-dev-playbook/"), rel)
            self.assertNotIn(rel, ("ai-dev-playbook/project.json",
                                   "ai-dev-playbook/resources.json",
                                   "ai-dev-playbook/manifest.json"))
            self.assertEqual(digest, updating.sha256((self.root / rel).read_bytes()), rel)
        self.assertNotIn("docs/product.md", manifest["files"])
        self.assertNotIn("AGENTS.md", manifest["files"])

    def test_init_substitutes_version_from_VERSION(self):
        """init подставляет VERSION из файла версии"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        project = json.loads((self.root / "ai-dev-playbook" / "project.json")
                             .read_text(encoding="utf-8"))
        version = (self.root / "ai-dev-playbook" / "VERSION").read_text().strip()
        self.assertEqual(project["playbook_version"], version)
        template = (REPO / "ai-dev-playbook" / "templates" / "project.json") \
            .read_text(encoding="utf-8")
        self.assertNotIn(f'"{version}"', template)

    def test_release_id_deterministic(self):
        """release_id в manifest детерминирован"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        first = self.manifest()["release_id"]
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        code, _, err = run_cli("init", str(other), "--apply")
        self.assertEqual(code, 0, err)
        second = json.loads((other / "ai-dev-playbook" / "manifest.json")
                            .read_text(encoding="utf-8"))["release_id"]
        self.assertEqual(first, second)

    def test_check_requires_manifest(self):
        """check требует наличия manifest"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        (self.root / "ai-dev-playbook" / "manifest.json").unlink()
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("adopt", err)

    def test_corrupt_manifest_rejected(self):
        """Повреждённый manifest отклоняется"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        path = self.root / "ai-dev-playbook" / "manifest.json"
        path.write_text('{"schema_version": 1, "playbook_version": "0.0.2", "files": {}}',
                        encoding="utf-8")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("manifest", err)

    def rewrite_manifest(self, mutate, recompute=True):
        path = self.root / "ai-dev-playbook" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        if recompute:
            manifest["release_id"] = updating.fingerprint(manifest["files"])
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return manifest

    def test_manifest_reserved_path_rejected_any_release_id(self):
        """AUDIT-005-A8: зарезервированный путь в files отклоняется при любом release_id."""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        settings_rel = "ai-dev-playbook/resources.json"
        digest = updating.sha256((self.root / settings_rel).read_bytes())
        # Вариант 1: путь добавлен без пересчёта release_id — несогласованность.
        self.rewrite_manifest(lambda m: m["files"].__setitem__(settings_rel, digest),
                              recompute=False)
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("manifest", err)
        code, _, err = self.cli("update", "plan", str(REPO))
        self.assertEqual(code, 1)
        # Вариант 2: release_id пересчитан под испорченный состав — всё равно отказ.
        self.rewrite_manifest(lambda m: m["files"].__setitem__(settings_rel, digest))
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("зарезервирован", err)
        code, _, err = self.cli("update", "plan", str(REPO))
        self.assertEqual(code, 1)
        self.assertIn("зарезервирован", err)
        # Настройки не тронуты, отказ произошёл без записи.
        self.assertTrue((self.root / settings_rel).is_file())
        self.assertFalse((self.root / "work").exists())

    def test_manifest_release_id_mismatch_rejected(self):
        """Несовпадение release_id manifest отклоняется"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        victim = "ai-dev-playbook/rules/tasks.md"
        self.rewrite_manifest(
            lambda m: m["files"].__setitem__(victim, "sha256:" + "0" * 64),
            recompute=False)
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("release_id", err)

    def test_manifest_noncanonical_path_rejected(self):
        """Неканонический путь в manifest отклоняется"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        self.rewrite_manifest(
            lambda m: m["files"].__setitem__("ai-dev-playbook/evil.txt",
                                           "sha256:" + "0" * 64))
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("канонического", err)

    def test_manifest_missing_required_core_rejected(self):
        """Отсутствие обязательного core-файла в manifest отклоняется"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        self.rewrite_manifest(
            lambda m: m["files"].pop("ai-dev-playbook/updates.json"))
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("обязательного", err)


class AdoptTest(CliCase):
    """adopt установок без manifest: разрешения конфликтов версий и
    managed-файлов."""
    def legacy_install(self):
        """Установка без manifest — состояние до механизма обновления."""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        (self.root / "ai-dev-playbook" / "manifest.json").unlink()

    def test_adopt_clean(self):
        """adopt чистой legacy-установки без manifest создаёт manifest"""
        self.legacy_install()
        code, out, err = self.cli("adopt", str(REPO))
        self.assertEqual(code, 0, err + out)
        self.assertIn("Предпросмотр", out)
        self.assertFalse((self.root / "ai-dev-playbook" / "manifest.json").exists())
        code, out, err = self.cli("adopt", str(REPO), "--apply")
        self.assertEqual(code, 0, err + out)
        manifest = self.manifest()
        self.assertEqual(manifest["origin"], "adopt:checkout")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_adopt_requires_resolutions(self):
        """adopt требует разрешений для изменённых файлов"""
        self.legacy_install()
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("local edit", encoding="utf-8")
        (self.root / "ai-dev-playbook" / "rules" / "layout.md").unlink()
        code, out, err = self.cli("adopt", str(REPO), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--resolve", err + out)
        self.assertIn("tasks.md", out)
        self.assertIn("layout.md", out)
        self.assertFalse((self.root / "ai-dev-playbook" / "manifest.json").exists())

    def test_adopt_release_restores_and_keep_preserves(self):
        """adopt: release восстанавливает, keep сохраняет локальные правки"""
        self.legacy_install()
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("local edit", encoding="utf-8")
        (self.root / "ai-dev-playbook" / "rules" / "layout.md").unlink()
        code, out, err = self.cli(
            "adopt", str(REPO), "--apply",
            "--resolve", "ai-dev-playbook/rules/layout.md=release",
            "--resolve", "ai-dev-playbook/rules/tasks.md=keep")
        self.assertEqual(code, 0, err + out)
        manifest = self.manifest()
        self.assertEqual(manifest["kept"], ["ai-dev-playbook/rules/tasks.md"])
        self.assertEqual((self.root / "ai-dev-playbook" / "rules" / "tasks.md")
                         .read_text(encoding="utf-8"), "local edit")
        self.assertTrue((self.root / "ai-dev-playbook" / "rules" / "layout.md").is_file())
        reference = (REPO / "ai-dev-playbook" / "rules" / "layout.md").read_bytes()
        self.assertEqual((self.root / "ai-dev-playbook" / "rules" / "layout.md")
                         .read_bytes(), reference)

    def test_adopt_conflicting_version(self):
        """adopt с конфликтующей версией требует явного разрешения"""
        self.legacy_install()
        conflicting_version = REPO_VERSION + "-conflict"
        version_path = self.root / "ai-dev-playbook" / "VERSION"
        version_path.write_text(conflicting_version + "\n", encoding="utf-8")
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["playbook_version"] = conflicting_version
        project_path.write_text(json.dumps(project) + "\n", encoding="utf-8")
        code, out, err = self.cli("adopt", str(REPO), "--apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("VERSION", out + err)
        self.assertEqual(version_path.read_text().strip(), conflicting_version)
        self.assertFalse((self.root / "ai-dev-playbook" / "manifest.json").exists())
        code, out, err = self.cli("adopt", str(REPO), "--apply",
                                  "--resolve", "ai-dev-playbook/VERSION=release")
        self.assertEqual(code, 0, err + out)
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text(encoding="utf-8").strip(), REPO_VERSION)
        project = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertEqual(project["playbook_version"], REPO_VERSION)
        self.assertEqual(self.manifest()["playbook_version"], REPO_VERSION)

    def test_adopt_refuses_managed_and_force_overwrites(self):
        """adopt отклоняет managed-файлы, --force перезаписывает"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("adopt", str(REPO))
        self.assertEqual(code, 1)
        self.assertIn("update", err)
        code, _, err = self.cli("adopt", str(REPO), "--apply", "--force")
        self.assertEqual(code, 0, err)

    def test_adopt_version_keep_forbidden(self):
        """keep для VERSION-файла запрещён в adopt"""
        self.legacy_install()
        (self.root / "ai-dev-playbook" / "VERSION").write_text(REPO_VERSION + "-conflict\n", encoding="utf-8")
        code, _, err = self.cli("adopt", str(REPO), "--apply",
                                "--resolve", "ai-dev-playbook/VERSION=keep")
        self.assertEqual(code, 1)
        self.assertIn("VERSION", err + " ".join([]))


class UpdatePlanTest(CliCase):
    """update plan: предпросмотр без записи, --save, устаревание и
    блокировки изменений."""
    def test_empty_plan_for_same_state(self):
        """update plan для той же версии даёт пустой план"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "plan", str(REPO))
        self.assertEqual(code, 0, err + out)
        self.assertIn("План пуст", out)
        self.assertFalse((self.root / "work").exists())

    def test_plan_lists_changes_without_writes(self):
        """update plan перечисляет изменения без записи"""
        self.install(version="0.0.2")

        def mutate(kit):
            (kit / "rules" / "new-rule.md").write_text("# Новое", encoding="utf-8")
            (kit / "rules" / "layout.md").unlink()
            (kit / "rules" / "tasks.md").write_text(
                (kit / "rules" / "tasks.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        before = {p: p.read_bytes() for p in (self.root / "ai-dev-playbook").rglob("*")
                  if p.is_file()}
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 0, err + out)
        self.assertIn("+ ai-dev-playbook/rules/new-rule.md", out)
        self.assertIn("~ ai-dev-playbook/rules/tasks.md", out)
        self.assertIn("- ai-dev-playbook/rules/layout.md", out)
        self.assertIn("0.0.2 → 0.0.3", out)
        self.assertFalse((self.root / "work").exists())
        after = {p: p.read_bytes() for p in (self.root / "ai-dev-playbook").rglob("*")
                 if p.is_file()}
        self.assertEqual(before, after)

    def test_plan_save_and_stale_detection(self):
        """update plan --save сохраняет план и отслеживает устаревание"""
        self.install(version="0.0.2")
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        plan = json.loads((self.root / "work" / "update" / "plan.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(plan["schema_version"], 1)
        self.assertTrue(plan["before_fingerprint"].startswith("sha256:"))
        self.assertEqual(plan["artifact"]["provenance"], "release")
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("drift", encoding="utf-8")
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)

    def test_modified_managed_file_blocks_apply(self):
        """Изменённый managed-файл блокирует update apply"""
        self.install(version="0.0.2")
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("local edit", encoding="utf-8")

        def mutate(kit):
            (kit / "rules" / "tasks.md").write_text(
                (kit / "rules" / "tasks.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, out, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err + out)
        self.assertIn("конфликт ai-dev-playbook/rules/tasks.md", out)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("Неразреш", err)
        self.assertEqual((self.root / "ai-dev-playbook" / "rules" / "tasks.md")
                         .read_text(encoding="utf-8"), "local edit")

    def test_unsupported_transition_blocked(self):
        """Переход вне updates_from релиза блокируется"""
        self.install(version="0.0.2")
        release = self.fixture_release(updates_from={})
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("Неподдерживаемый переход", err)

    def test_downgrade_blocked(self):
        """Переход на более старую версию блокируется"""
        self.install(version="0.0.2")
        release = self.fixture_release(version="0.0.1")
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("downgrade", err)

    def test_same_version_different_content_rejected(self):
        """Та же версия с иным содержимым отклоняется"""
        self.install(version="0.0.2")

        def mutate(kit):
            (kit / "rules" / "tasks.md").write_text(
                (kit / "rules" / "tasks.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(version="0.0.2", mutate=mutate)
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("Неоднозначный релиз", err)

    def test_tampered_release_json_rejected(self):
        """Подделанный release.json отклоняется"""
        self.install(version="0.0.2")
        release = self.fixture_release()
        meta = json.loads((release / "release.json").read_text(encoding="utf-8"))
        meta["version"] = "0.0.9"
        (release / "release.json").write_text(json.dumps(meta), encoding="utf-8")
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("release.json", err)


class MigrationTest(CliCase):
    """Миграции схем project.json: шаги, совместимость CLI,
    идемпотентность."""
    def test_migration_steps_applied_and_values_preserved(self):
        """Шаги миграции применяются, значения сохраняются"""
        self.install()
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["custom_field"] = {"nested": 42}
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        steps = [{"op": "json_set", "file": "ai-dev-playbook/project.json",
                  "key": "commands.audit", "value": "make audit"},
                 {"op": "note", "text": "Проверьте поле custom_field вручную."}]
        release = self.fixture_release(updates_from={"0.0.2": {"steps": steps}})
        code, out, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err + out)
        self.assertIn('"audit": "make audit"', out)
        self.assertIn("custom_field", out)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        project = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertEqual(project["commands"]["audit"], "make audit")
        self.assertEqual(project["custom_field"], {"nested": 42})
        self.assertEqual(project["playbook_version"], "0.0.3")
        self.assertEqual(project["name"], "test-project")

    def test_schema_change_without_steps_blocked(self):
        """Смена schema_version без шагов миграции блокируется"""
        self.install()

        def mutate(kit):
            template = json.loads((kit / "templates" / "project.json")
                                  .read_text(encoding="utf-8"))
            template["schema_version"] = 2
            (kit / "templates" / "project.json").write_text(
                json.dumps(template) + "\n", encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("схемы", err)

    def schema3_release(self, steps=None, cli_accepts=False):
        steps = steps or [{"op": "json_set", "file": "ai-dev-playbook/project.json",
                           "key": "schema_version", "value": 3}]

        def mutate(kit):
            template = json.loads((kit / "templates" / "project.json")
                                  .read_text(encoding="utf-8"))
            template["schema_version"] = 3
            (kit / "templates" / "project.json").write_text(
                json.dumps(template) + "\n", encoding="utf-8")
            if cli_accepts:
                script = kit / "scripts" / "playbook.py"
                text = script.read_text(encoding="utf-8")
                assert 'data.get("schema_version") in {1, 2}' in text
                script.write_text(text.replace(
                    'data.get("schema_version") in {1, 2}',
                    'data.get("schema_version") in {1, 2, 3}'), encoding="utf-8")

        return self.fixture_release(
            updates_from={"0.0.2": {"steps": steps}}, mutate=mutate)

    def test_schema_migration_incompatible_cli_rejected(self):
        """AUDIT-005-A5: смена схемы без поддержки в целевом CLI отклоняется до записи."""
        self.install()
        release = self.schema3_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("Целевой CLI", err)
        project = json.loads((self.root / "ai-dev-playbook" / "project.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(project["schema_version"], 1)
        self.assertEqual(project["playbook_version"], "0.0.2")
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.2")
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_schema_migration_compatible_cli_applies(self):
        """Артефакт с CLI, принимающим новую схему, применяется; установленный CLI работает."""
        self.install()
        release = self.schema3_release(cli_accepts=True)
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        project = json.loads((self.root / "ai-dev-playbook" / "project.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(project["schema_version"], 3)
        # Установленный новый CLI выдерживает следующий запуск check/index/plan.
        script = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        for argv in (("check",), ("index",), ("update", "plan", str(release))):
            result = subprocess.run(
                [sys.executable, "-B", str(script), "--root", str(self.root), *argv],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, str(argv) + result.stderr)

    def test_card_format_change_noted(self):
        """Смена card_format фиксируется в плане обновления"""
        self.install(version="0.0.2")

        def mutate(kit):
            task = kit / "templates" / "task.md"
            task.write_text(task.read_text(encoding="utf-8")
                            .replace("Closed: -", "Closed: -\nReviewed: -"),
                            encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 0, err + out)
        self.assertIn("Формат карточек", out)

    def test_migrations_idempotent(self):
        """Шаги миграций идемпотентны при повторном применении"""
        steps = [{"op": "json_set", "file": "ai-dev-playbook/project.json",
                  "key": "a.b", "value": 1},
                 {"op": "json_delete", "file": "ai-dev-playbook/project.json",
                  "key": "commands.test"},
                 {"op": "json_rename", "file": "ai-dev-playbook/project.json",
                  "key": "commands.check", "to": "commands.verify"}]
        content = json.dumps({"schema_version": 1, "name": "x",
                              "commands": {"test": "t", "check": "c"}})
        once = updating.migrate_settings(content, steps, "9.9.9", playbook.require)
        twice = updating.migrate_settings(json.dumps(once), steps, "9.9.9",
                                          playbook.require)
        self.assertEqual(once, twice)
        self.assertEqual(once["a"], {"b": 1})
        self.assertEqual(once["commands"], {"verify": "c"})
        self.assertEqual(once["name"], "x")

    def migration_step(self, op, key, **fields):
        return {"op": op, "file": "ai-dev-playbook/project.json",
                "key": key, **fields}

    def test_rename_occupied_destination_rejected(self):
        """AUDIT-005-A4: rename в занятое назначение с иным значением отклоняется."""
        self.install()
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["custom"] = {"old": "A", "new": "B"}
        project_path.write_text(json.dumps(project), encoding="utf-8")
        steps = [self.migration_step("json_rename", "custom.old", to="custom.new")]
        release = self.fixture_release(updates_from={"0.0.2": {"steps": steps}})
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 1)
        self.assertIn("другое значение", err)
        self.assertFalse((self.root / "work" / "update").exists())
        # Прямая проверка операции: значения не тронуты, повторный прогон не скрывает коллизию.
        content = json.dumps({"custom": {"old": "A", "new": "B"}})
        for _ in range(2):
            with self.assertRaises(ValueError):
                updating.migrate_settings(content, steps, "9.9.9", playbook.require)

    def test_rename_converged_destination_allowed(self):
        """Равные значения в источнике и назначении — схождение без потерь, идемпотентно."""
        steps = [self.migration_step("json_rename", "custom.old", to="custom.new")]
        content = json.dumps({"custom": {"old": 5, "new": 5}})
        once = updating.migrate_settings(content, steps, "9.9.9", playbook.require)
        self.assertEqual(once["custom"], {"new": 5})
        twice = updating.migrate_settings(json.dumps(once), steps, "9.9.9",
                                          playbook.require)
        self.assertEqual(twice, once)

    def test_rename_intersecting_paths_rejected(self):
        """AUDIT-005-A4: перенос внутрь собственного поддерева отклоняется."""
        steps = [self.migration_step("json_rename", "custom", to="custom.new")]
        content = json.dumps({"custom": {"x": 1}, "other": 2})
        with self.assertRaises(ValueError) as ctx:
            updating.migrate_settings(content, steps, "9.9.9", playbook.require)
        self.assertIn("пересекаются", str(ctx.exception))
        steps = [self.migration_step("json_rename", "custom.new", to="custom")]
        with self.assertRaises(ValueError):
            updating.migrate_settings(content, steps, "9.9.9", playbook.require)

    def test_set_through_scalar_parent_rejected(self):
        """AUDIT-005-A4: вложенный set через скалярного родителя отклоняется."""
        self.install()
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["custom"] = "scalar-value"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        steps = [self.migration_step("json_set", "custom.child", value=1)]
        release = self.fixture_release(updates_from={"0.0.2": {"steps": steps}})
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 1)
        self.assertIn("не-объектом", err)
        self.assertFalse((self.root / "work" / "update").exists())
        self.assertEqual(json.loads(project_path.read_text())["custom"], "scalar-value")

    def test_set_over_null_parent_rejected(self):
        """Явный null в родителе — тоже коллизия, а не заменяемый плейсхолдер."""
        steps = [self.migration_step("json_set", "custom.child", value=1)]
        content = json.dumps({"custom": None})
        with self.assertRaises(ValueError):
            updating.migrate_settings(content, steps, "9.9.9", playbook.require)


class UpdateApplyTest(CliCase):
    """update apply: end-to-end, no-op при пустом плане, keep/release
    разрешения, чужие файлы."""
    def test_apply_end_to_end(self):
        """update apply end-to-end применяет изменения комплекта"""
        self.install()

        def mutate(kit):
            (kit / "rules" / "new-rule.md").write_text("# Новое", encoding="utf-8")
            (kit / "rules" / "context.md").write_text(
                (kit / "rules" / "context.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.3")
        self.assertTrue((self.root / "ai-dev-playbook" / "rules" / "new-rule.md").is_file())
        self.assertIn("+1", (self.root / "ai-dev-playbook" / "rules" / "context.md")
                        .read_text(encoding="utf-8"))
        manifest = self.manifest()
        self.assertEqual(manifest["playbook_version"], "0.0.3")
        self.assertEqual(manifest["origin"], "update")
        project = json.loads((self.root / "ai-dev-playbook" / "project.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(project["playbook_version"], "0.0.3")
        self.assertEqual(project["name"], "test-project")
        ops = list((self.root / "work" / "update").glob("20*"))
        self.assertEqual(len(ops), 1)
        self.assertTrue((ops[0] / "journal.jsonl").is_file())
        self.assertTrue((ops[0] / "restore.py").is_file())
        self.assertTrue((ops[0] / "backup_index.json").is_file())
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        journal = (ops[0] / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "done"', journal)
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_apply_is_noop_when_plan_empty(self):
        """update apply при пустом плане — no-op"""
        code, _, err = self.init("--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "plan", str(REPO), "--save")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertIn("нечего", out)

    def test_second_plan_after_update_is_empty(self):
        """План после применения обновления пуст"""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 0, err + out)
        self.assertIn("План пуст", out)

    def test_keep_resolution_preserves_local_edit(self):
        """keep-разрешение сохраняет локальную правку"""
        self.install()
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("local edit", encoding="utf-8")

        def mutate(kit):
            (kit / "rules" / "tasks.md").write_text(
                (kit / "rules" / "tasks.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, _, err = self.cli(
            "update", "plan", str(release), "--save",
            "--resolve", "ai-dev-playbook/rules/tasks.md=keep")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "ai-dev-playbook" / "rules" / "tasks.md")
                         .read_text(encoding="utf-8"), "local edit")
        self.assertEqual(self.manifest()["kept"], ["ai-dev-playbook/rules/tasks.md"])
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.3")

    def test_release_resolution_overwrites_local_edit(self):
        """release-разрешение перезаписывает локальную правку"""
        self.install()
        (self.root / "ai-dev-playbook" / "rules" / "tasks.md") \
            .write_text("local edit", encoding="utf-8")

        def mutate(kit):
            (kit / "rules" / "tasks.md").write_text(
                (kit / "rules" / "tasks.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, _, err = self.cli(
            "update", "plan", str(release), "--save",
            "--resolve", "ai-dev-playbook/rules/tasks.md=release")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        reference = (release / "ai-dev-playbook" / "rules" / "tasks.md").read_bytes()
        self.assertEqual((self.root / "ai-dev-playbook" / "rules" / "tasks.md")
                         .read_bytes(), reference)
        self.assertNotIn("kept", self.manifest())

    def test_unknown_kit_files_untouched(self):
        """Неизвестные файлы комплекта не трогаются apply"""
        self.install()
        local = self.root / "ai-dev-playbook" / "notes.md"
        local.write_text("project note", encoding="utf-8")
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(local.read_text(encoding="utf-8"), "project note")

    def test_drift_allowed_by_plan_survives_apply(self):
        """AUDIT-005-A2: нетронутый обновлением drift сохраняется и не ломает apply."""
        self.install()
        target = self.root / "ai-dev-playbook" / "rules" / "git.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nlocal drift\n",
                          encoding="utf-8")
        release = self.fixture_release()  # меняются только VERSION и updates.json
        code, out, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err + out)
        self.assertIn("изменён локально", out)
        self.assertNotIn("конфликт ai-dev-playbook/rules/git.md", out)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertIn("local drift", target.read_text(encoding="utf-8"))
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.3")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 0, err + out)
        self.assertIn("План пуст", out)

    def test_keep_removed_file_exits_management_and_survives(self):
        """AUDIT-005-A3: keep для удаляемого из релиза файла выводит его из управления."""
        self.install()
        rel = "ai-dev-playbook/templates/adr.md"
        target = self.root / rel
        target.write_text("local content", encoding="utf-8")

        def mutate(kit):
            (kit / "templates" / "adr.md").unlink()

        release = self.fixture_release(mutate=mutate)
        code, out, err = self.cli("update", "plan", str(release), "--save",
                                  "--resolve", rel + "=keep")
        self.assertEqual(code, 0, err + out)
        self.assertIn("modified-remove", out)
        self.assertIn("выходит из управления", out)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(target.read_text(encoding="utf-8"), "local content")
        manifest = self.manifest()
        self.assertNotIn(rel, manifest["files"])
        self.assertNotIn(rel, manifest.get("kept", []))
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 0, err + out)
        self.assertIn("План пуст", out)

    def test_apply_aborts_on_failed_staged_check(self):
        """Упавшая staged-проверка отменяет apply"""
        self.install(verification={"inputs": ["docs"],
                                   "checks": [{"id": "fail",
                                               "argv": ["python3", "-c",
                                                        "import sys; sys.exit(3)"],
                                               "timeout_seconds": 30}]})
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        before = (self.root / "ai-dev-playbook" / "VERSION").read_text()
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("не прошли", err + out)
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text(), before)
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        self.assertEqual(self.manifest()["playbook_version"], "0.0.2")

    def test_apply_passes_staged_check(self):
        """Проходящая staged-проверка пропускает apply"""
        self.install(verification={"inputs": ["docs"], "checks": [PASS_CHECK]})
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.3")

    def test_write_failure_restores_previous_state(self):
        """Сбой записи восстанавливает прежнее состояние"""
        self.install()

        def mutate(kit):
            (kit / "rules" / "new-rule.md").write_text("# Новое", encoding="utf-8")
            (kit / "rules" / "context.md").write_text(
                (kit / "rules" / "context.md").read_text(encoding="utf-8") + "\n+1\n",
                encoding="utf-8")

        release = self.fixture_release(mutate=mutate)
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        context = self.root / "ai-dev-playbook" / "rules" / "context.md"
        before_context = context.read_bytes()
        calls = []
        original = updating._write_file

        def boom(source, target):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError("внедрённый сбой записи")
            return original(source, target)

        updating._write_file = boom
        try:
            code, _, err = self.cli("update", "apply")
        finally:
            updating._write_file = original
        self.assertEqual(code, 1)
        self.assertIn("внедрённый сбой", err)
        self.assertIn("восстановлено", err.lower())
        self.assertEqual(context.read_bytes(), before_context)
        self.assertFalse((self.root / "ai-dev-playbook" / "rules" / "new-rule.md").exists())
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text().strip(),
                         "0.0.2")
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        journals = list((self.root / "work" / "update").glob("20*/journal.jsonl"))
        self.assertEqual(len(journals), 1)
        text = journals[0].read_text(encoding="utf-8")
        self.assertIn('"event": "failed"', text)
        self.assertIn('"event": "restored"', text)

    def test_pending_blocks_commands_and_restore_recovers(self):
        """Pending блокирует команды, restore восстанавливает"""
        self.install()
        pending_dir = self.root / "work" / "update"
        op = pending_dir / "op-x"
        (op / "backup" / "ai-dev-playbook" / "rules").mkdir(parents=True)
        target = self.root / "ai-dev-playbook" / "rules" / "tasks.md"
        shutil.copyfile(target, op / "backup" / "ai-dev-playbook" / "rules" / "tasks.md")
        before = target.read_bytes()
        target.write_text("tampered", encoding="utf-8")
        added = self.root / "ai-dev-playbook" / "rules" / "added-tmp.md"
        added.write_text("x", encoding="utf-8")
        (op / "backup_index.json").write_text(json.dumps(
            {"backed": ["ai-dev-playbook/rules/tasks.md"],
             "added": ["ai-dev-playbook/rules/added-tmp.md"]}), encoding="utf-8")
        (pending_dir / "pending.json").write_text(json.dumps(
            {"op_id": "op-x", "started_at": "2026-01-01T00:00:00+00:00",
             "op_dir": "work/update/op-x"}), encoding="utf-8")
        for argv in (("check",), ("index",), ("update", "plan", str(REPO)),
                     ("update", "apply")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("Незаверш", err, argv)
        code, out, err = self.cli("update", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("op-x", out)
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(added.exists())
        self.assertFalse((pending_dir / "pending.json").exists())
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_restore_missing_backup_file_fails_and_keeps_pending(self):
        """AUDIT-005-A6: неполная резервная копия — отказ до записи, маркер сохранён."""
        self.install()
        pending_dir = self.root / "work" / "update"
        op = pending_dir / "op-x"
        (op / "backup" / "ai-dev-playbook" / "rules").mkdir(parents=True)
        target = self.root / "ai-dev-playbook" / "rules" / "tasks.md"
        backup_file = op / "backup" / "ai-dev-playbook" / "rules" / "tasks.md"
        shutil.copyfile(target, backup_file)
        target.write_text("tampered", encoding="utf-8")
        # Индекс ссылается на файл, которого нет в backup/.
        (op / "backup_index.json").write_text(json.dumps(
            {"backed": ["ai-dev-playbook/rules/tasks.md",
                        "ai-dev-playbook/rules/context.md"],
             "added": [],
             "hashes": {"ai-dev-playbook/rules/tasks.md":
                        updating.sha256(backup_file.read_bytes())}}),
            encoding="utf-8")
        (pending_dir / "pending.json").write_text(json.dumps(
            {"op_id": "op-x", "op_dir": "work/update/op-x"}), encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("неполна", err)
        self.assertTrue((pending_dir / "pending.json").exists())
        # Нечего восстанавливать частично: проверка пред-образа до записи.
        self.assertEqual(target.read_text(encoding="utf-8"), "tampered")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)

    def test_restore_corrupt_backup_file_fails_and_keeps_pending(self):
        """AUDIT-005-A6: повреждённая копия (хеш не совпадает) — отказ, маркер сохранён."""
        self.install()
        pending_dir = self.root / "work" / "update"
        op = pending_dir / "op-x"
        (op / "backup" / "ai-dev-playbook" / "rules").mkdir(parents=True)
        backup_file = op / "backup" / "ai-dev-playbook" / "rules" / "tasks.md"
        target = self.root / "ai-dev-playbook" / "rules" / "tasks.md"
        shutil.copyfile(target, backup_file)
        backup_file.write_text("corrupted copy", encoding="utf-8")
        (op / "backup_index.json").write_text(json.dumps(
            {"backed": ["ai-dev-playbook/rules/tasks.md"], "added": [],
             "hashes": {"ai-dev-playbook/rules/tasks.md":
                        updating.sha256(target.read_bytes())}}), encoding="utf-8")
        (pending_dir / "pending.json").write_text(json.dumps(
            {"op_id": "op-x", "op_dir": "work/update/op-x"}), encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("повреждена", err)
        self.assertTrue((pending_dir / "pending.json").exists())

    def test_restore_missing_index_fails_then_recovers(self):
        """AUDIT-005-A6: без backup_index — отказ и маркер; с индексом — восстановление."""
        self.install()
        pending_dir = self.root / "work" / "update"
        op = pending_dir / "op-x"
        (op / "backup" / "ai-dev-playbook" / "rules").mkdir(parents=True)
        target = self.root / "ai-dev-playbook" / "rules" / "tasks.md"
        shutil.copyfile(target, op / "backup" / "ai-dev-playbook" / "rules" / "tasks.md")
        before = target.read_bytes()
        target.write_text("tampered", encoding="utf-8")
        (pending_dir / "pending.json").write_text(json.dumps(
            {"op_id": "op-x", "op_dir": "work/update/op-x"}), encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("backup_index", err)
        self.assertTrue((pending_dir / "pending.json").exists())
        # Пред-образ найден позже — восстановление успешно и снимает маркер.
        (op / "backup_index.json").write_text(json.dumps(
            {"backed": ["ai-dev-playbook/rules/tasks.md"], "added": []}),
            encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((pending_dir / "pending.json").exists())

    def test_standalone_restore_script(self):
        """Автономный restore-скрипт восстанавливает состояние"""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        # Симулируем прерванную операцию поверх: портим файл и ставим маркер;
        # резервная копия операции хранит пред-образ 0.0.2.
        ops = list((self.root / "work" / "update").glob("20*"))
        self.assertEqual(len(ops), 1)
        op = ops[0]
        target = self.root / "ai-dev-playbook" / "VERSION"
        target.write_text("0.0.0\n", encoding="utf-8")
        (self.root / "work" / "update" / "pending.json").write_text(json.dumps(
            {"op_id": op.name, "op_dir": f"work/update/{op.name}"}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(op / "restore.py")],
                                capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text().strip(), "0.0.2")
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())

    def test_standalone_restore_missing_index_fails_and_keeps_pending(self):
        """AUDIT-005-A6: автономный скрипт без индекса возвращает ненулевой код."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        (op,) = list((self.root / "work" / "update").glob("20*"))
        (op / "backup_index.json").unlink()
        pending = self.root / "work" / "update" / "pending.json"
        pending.write_text(json.dumps(
            {"op_id": op.name, "op_dir": f"work/update/{op.name}"}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(op / "restore.py")],
                                capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("backup_index", result.stderr)
        self.assertTrue(pending.exists())
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)


class UpdateRestoreGuardTest(CliCase):
    """AUDIT-005-A6/A7 (повтор): строгая проверка индекса и владельца маркера."""

    def _completed_op(self):
        """Реальный apply; возвращает каталог завершённой операции."""
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        (op,) = [d for d in (self.root / "work" / "update").glob("20*")
                 if d.is_dir()]
        return op

    def _pending_for(self, op):
        marker = self.root / "work" / "update" / "pending.json"
        marker.write_text(json.dumps(
            {"op_id": op.name, "op_dir": f"work/update/{op.name}"}),
            encoding="utf-8")
        return marker

    def _run_restore_py(self, op):
        return subprocess.run([sys.executable, "-B", str(op / "restore.py")],
                              capture_output=True, text=True, cwd=self.root)

    def test_index_entry_dropped_from_backed_rejected_then_restored(self):
        """A6: копия и хеш сохранились, но путь выпал из backed — отказ."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        op = self._completed_op()
        # Имитация прерванной операции: пред-образ 0.0.2 в backup операции,
        # файл остался в «новом» состоянии; маркер этой операции.
        target = self.root / "ai-dev-playbook" / "VERSION"
        self.assertEqual(target.read_text().strip(), "0.0.3")
        marker = self._pending_for(op)
        index_path = op / "backup_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["backed"].remove("ai-dev-playbook/VERSION")
        index_path.write_text(json.dumps(index) + "\n", encoding="utf-8")
        for runner in ("cli", "standalone"):
            with self.subTest(runner=runner):
                if runner == "cli":
                    code = self.cli("update", "restore")[0]
                else:
                    code = self._run_restore_py(op).returncode
                self.assertEqual(code, 1)
                self.assertTrue(marker.exists())
                self.assertEqual(target.read_text().strip(), "0.0.3")
        index["backed"].append("ai-dev-playbook/VERSION")
        index_path.write_text(json.dumps(index) + "\n", encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 0, err)
        self.assertEqual(target.read_text().strip(), "0.0.2")
        self.assertFalse(marker.exists())
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_index_inconsistent_variants_rejected(self):
        """A6: дубли, пересечение backed/added, лишняя копия и пустой backed."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        op = self._completed_op()
        marker = self._pending_for(op)
        index_path = op / "backup_index.json"
        original = json.loads(index_path.read_text(encoding="utf-8"))
        first = original["backed"][0]
        cases = {
            "duplicate": {**original, "backed": original["backed"] + [first]},
            "overlap": {**original, "added": [first]},
            "empty_backed": {"backed": [], "added": [], "hashes": {}},
        }
        for name, broken in cases.items():
            with self.subTest(case=name):
                index_path.write_text(json.dumps(broken), encoding="utf-8")
                self.assertEqual(self.cli("update", "restore")[0], 1)
                self.assertTrue(marker.exists())
        stray = op / "backup" / "ai-dev-playbook" / "rules" / "stray.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("x", encoding="utf-8")
        index_path.write_text(json.dumps(original) + "\n", encoding="utf-8")
        with self.subTest(case="unlisted_copy"):
            self.assertEqual(self.cli("update", "restore")[0], 1)
            self.assertTrue(marker.exists())
        stray.unlink()
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 0, err)
        self.assertFalse(marker.exists())

    def test_stale_standalone_restore_refused_and_own_restores(self):
        """A7: restore.py чужой операции отказывает; своей — восстанавливает."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        old = self._completed_op()  # 0.0.2 -> 0.0.3
        release2 = self.fixture_release(version="0.0.4",
                                        updates_from={"0.0.3": {"steps": []}})
        code, _, err = self.cli("update", "plan", str(release2), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err)
        current = next(d for d in (self.root / "work" / "update").glob("20*")
                       if d.is_dir() and d != old)
        # Имитация прерванной второй операции: её маркер, VERSION уже новая.
        marker = self._pending_for(current)
        version = self.root / "ai-dev-playbook" / "VERSION"
        self.assertEqual(version.read_text().strip(), "0.0.4")
        # Старый автономный восстановитель: отказ без изменения файлов/маркера.
        result = self._run_restore_py(old)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(version.read_text().strip(), "0.0.4")
        # Восстановитель текущей операции возвращает её пред-образ (0.0.3).
        result = self._run_restore_py(current)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(version.read_text().strip(), "0.0.3")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_standalone_restore_without_pending_refused(self):
        """A7: без маркера автономный восстановитель не откатывает проект."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        op = self._completed_op()
        version = self.root / "ai-dev-playbook" / "VERSION"
        self.assertEqual(version.read_text().strip(), "0.0.3")
        result = self._run_restore_py(op)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(version.read_text().strip(), "0.0.3")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_cli_restore_rejects_marker_outside_ops_dir(self):
        """A7: CLI не следует маркеру за пределы work/update/<op_id>."""
        self.install()
        marker = self.root / "work" / "update" / "pending.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps(
            {"op_id": "evil", "op_dir": "../outside"}), encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertTrue(marker.exists())
        marker.write_text(json.dumps(
            {"op_id": "other-op", "op_dir": "work/update/other-op"}),
            encoding="utf-8")
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("Нет каталога операции", err)
        self.assertTrue(marker.exists())


class UpdatePlanProtectionTest(CliCase):
    """AUDIT-005-A1/A7: план защищён от изменений затрагиваемых путей до записи."""

    def test_new_release_path_created_after_plan_rejects_apply(self):
        """Новый путь релиза после плана отклоняет apply"""
        rel = "ai-dev-playbook/rules/new.md"
        release = self.fixture_release(
            mutate=lambda kit: (kit / "rules" / "new.md")
            .write_text("release content", encoding="utf-8"))
        self.install()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        target = self.root / rel
        target.write_text("user content created AFTER preview", encoding="utf-8")
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "user content created AFTER preview")
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())

    def test_collision_change_after_resolution_rejects_apply(self):
        """Изменение коллизии после разрешения отклоняет apply"""
        rel = "ai-dev-playbook/rules/new.md"
        release = self.fixture_release(
            mutate=lambda kit: (kit / "rules" / "new.md")
            .write_text("release content", encoding="utf-8"))
        self.install()
        target = self.root / rel
        target.write_text("local collision", encoding="utf-8")
        code, out, err = self.cli(
            "update", "plan", str(release), "--save", "--resolve", rel + "=keep")
        self.assertEqual(code, 0, err + out)
        self.assertIn("collision", out)
        target.write_text("local collision EDITED", encoding="utf-8")
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "local collision EDITED")

    def test_managed_path_removed_after_plan_rejects_apply(self):
        """Удаление managed-пути после плана отклоняет apply"""
        release = self.fixture_release()
        self.install()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        target = self.root / "ai-dev-playbook" / "rules" / "tasks.md"
        before = target.read_bytes()
        target.unlink()
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertFalse(target.exists())
        target.write_bytes(before)

    def test_edit_during_staged_checks_rejects_apply(self):
        """Правка во время staged-проверок отклоняет apply"""
        target = self.root / "ai-dev-playbook" / "rules" / "git.md"
        editor = {"id": "editor",
                  "argv": [sys.executable, "-c",
                           "from pathlib import Path; Path(" + repr(str(target)) +
                           ").write_text('user edit DURING staged checks')"],
                  "timeout_seconds": 10}
        self.install(verification={"inputs": ["ai-dev-playbook"], "checks": [editor]})
        release = self.fixture_release(
            mutate=lambda kit: (kit / "rules" / "git.md")
            .write_text((kit / "rules" / "git.md").read_text(encoding="utf-8")
                        + "\nrelease change\n", encoding="utf-8"))
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "user edit DURING staged checks")
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.2")
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        journals = list((self.root / "work" / "update").glob("20*/journal.jsonl"))
        self.assertEqual(len(journals), 1)
        text = journals[0].read_text(encoding="utf-8")
        self.assertIn('"event": "abort"', text)
        self.assertNotIn('"event": "done"', text)
        # Пред-образ сохранён: повторный план и применение работают штатно.
        code, _, err = self.cli(
            "update", "plan", str(release), "--save",
            "--resolve", "ai-dev-playbook/rules/git.md=release")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 0, err + out)
        self.assertIn("release change", target.read_text(encoding="utf-8"))
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_parallel_apply_rejected(self):
        """Параллельный apply отклоняется"""
        slow = {"id": "slow",
                "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 15}
        self.install(verification={"inputs": ["docs"], "checks": [slow]})
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        script = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        first = subprocess.Popen(
            [sys.executable, "-B", str(script), "--root", str(self.root),
             "update", "apply"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        marker = self.root / "work" / "update" / "pending.json"
        for _ in range(100):
            if marker.exists() or first.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(),
                        "первый apply не создал маркер либо завершился раньше времени")
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)
        out, serr = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, serr + out)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION")
                         .read_text().strip(), "0.0.3")
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_restore_refused_for_running_apply(self):
        """Restore отклоняется при работающем apply"""
        marker = self.root / "work" / "check-started"
        gate = self.root / "work" / "check-continue"
        script = ("from pathlib import Path; import time; "
                  "Path(" + repr(str(marker)) + ").touch(); "
                  "gate=Path(" + repr(str(gate)) + ");\n"
                  "for i in range(200):\n"
                  "    if gate.exists(): break\n"
                  "    time.sleep(0.05)\n"
                  "else:\n"
                  "    raise RuntimeError('gate timeout')\n")
        self.install(verification={"inputs": ["docs"], "checks": [
            {"id": "gate", "argv": [sys.executable, "-c", script],
             "timeout_seconds": 15}]})
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        playbook = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        first = subprocess.Popen(
            [sys.executable, "-B", str(playbook), "--root", str(self.root),
             "update", "apply"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        end = time.monotonic() + 10
        while not marker.exists() and first.poll() is None \
                and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "apply не дошёл до staged-проверки")
        pending = self.root / "work" / "update" / "pending.json"
        self.assertTrue(pending.exists())
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("выполняется", err)
        self.assertTrue(pending.exists(), "restore снял маркер живой операции")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)
        code, _, err = self.cli("update", "apply")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)
        gate.touch()
        out, serr = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, serr + out)
        self.assertFalse(pending.exists())
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("Нет незаверш", err)
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_pending_release_checks_owner(self):
        """Pending-проверки подтверждают владельца релиза"""
        pending = self.root / "work" / "update" / "pending.json"
        pending.parent.mkdir(parents=True)
        pending.write_text(json.dumps({"op_id": "foreign"}), encoding="utf-8")
        self.assertFalse(updating._release_pending(self.root, "ours"))
        self.assertTrue(pending.exists())
        pending.write_text("не json", encoding="utf-8")
        self.assertFalse(updating._release_pending(self.root, "ours"))
        self.assertTrue(pending.exists())
        pending.write_text(json.dumps({"op_id": "ours"}), encoding="utf-8")
        self.assertTrue(updating._release_pending(self.root, "ours"))
        self.assertFalse(pending.exists())
        self.assertTrue(updating._release_pending(self.root, "ours"))


if __name__ == "__main__":
    unittest.main()
