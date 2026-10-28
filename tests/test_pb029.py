import hashlib
import json
import os
import shutil
import tempfile
import unittest
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_cli import CliCase, REPO, REPO_VERSION, run_cli, PASS_CHECK
from test_close_lifecycle import managed_card, TASK_META, promote_request, promotion_receipt
from test_playbook import playbook, write_card

documents = playbook.documents
lifecycle = playbook.lifecycle
fops = playbook.file_operations
updating = playbook.updating

CARD_TEMPLATE = ("# {ident}: {title}\n\n"
                 "ID: {ident}\n"
                 "Status: {status}\n"
                 "Priority: P2\n"
                 "Depends-On: {deps}\n"
                 "Executor: {executor}\n"
                 "Model: {model}\n"
                 "Created: {created}\n"
                 "Closed: {closed}\n\n"
                 "<!-- playbook-meta:start -->\n"
                 "```json\n"
                 "{meta_json}\n"
                 "```\n"
                 "<!-- playbook-meta:end -->\n\n"
                 "{body}\n")


def make_card_text(ident, title="test", status="ready", deps="-",
                   executor="Antigravity", model="Gemini 3.8 Flash (High)",
                   created="2026-10-22T08:00:00+03:00", closed="-",
                   meta=None, body=""):
    meta_dict = meta if meta is not None else dict(TASK_META)
    meta_json = json.dumps(meta_dict, ensure_ascii=False, indent=2)
    return CARD_TEMPLATE.format(ident=ident, title=title, status=status,
                                deps=deps, executor=executor, model=model,
                                created=created, closed=closed,
                                meta_json=meta_json, body=body)


class Pb029EndToEndRoutesTest(CliCase):
    """Сквозные проверки трёх маршрутов (маленькая, средняя, большая задачи),

    независимого аудита, неизменяемости evidence, ссылок и самоприменения (PB-029).
    """

    def configure_full_project(self):
        """Развернуть полностью настроенный проект с разделом documents (все 5 коллекций)."""
        self.install()
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["name"] = "pilot-project"
        project["setup_status"] = "configured"
        project["commands"]["test"] = "python3 -m unittest discover -s tests -v"
        project["components"] = [{"name": "playbook",
                                  "code": "ai-dev-playbook",
                                  "tests": ["tests"], "migrations": None}]
        # Входы проверки — директория tests
        project["verification"] = {"inputs": ["tests"],
                                   "checks": [PASS_CHECK]}
        project["documents"] = {
            "schema_version": 1,
            "collections": {
                "discussion": {
                    "active": "docs/discussions",
                    "archive": "docs/archive/discussions",
                    "index": "docs/discussions/index.md"
                },
                "spec": {
                    "active": "docs/specs",
                    "archive": "docs/archive/specs",
                    "index": "docs/specs/index.md"
                },
                "design": {
                    "active": "docs/designs",
                    "archive": "docs/archive/designs",
                    "index": "docs/designs/index.md"
                },
                "adr": {
                    "active": "docs/adr",
                    "archive": "docs/archive/adr",
                    "index": "docs/adr/index.md"
                },
                "plan": {
                    "active": "docs/plans",
                    "archive": "docs/archive/plans",
                    "index": "docs/plans/index.md"
                }
            }
        }
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        # Создать каталоги коллекций и tests с dummy тестом
        (self.root / "tests").mkdir(parents=True, exist_ok=True)
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        for col in project["documents"]["collections"].values():
            (self.root / col["active"]).mkdir(parents=True, exist_ok=True)
        code, _, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err)

    def tasks_dir(self):
        return self.root / "docs" / "tasks"

    def archive_tasks_dir(self):
        return self.root / "docs" / "archive" / "tasks"

    def add_task_file(self, ident, title="test", status="ready", deps="-",
                      executor="-", model="-", meta=None, body=""):
        self.tasks_dir().mkdir(parents=True, exist_ok=True)
        content = make_card_text(ident=ident, title=title, status=status,
                                 deps=deps, executor=executor, model=model,
                                 meta=meta, body=body)
        (self.tasks_dir() / f"{ident}.md").write_text(content, encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        return self.tasks_dir() / f"{ident}.md"

    def update_task_text(self, ident, old_str, new_str):
        path = self.tasks_dir() / f"{ident}.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old_str, text)
        path.write_text(text.replace(old_str, new_str), encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def verify_task(self, ident):
        code, out, err = self.cli('index', '--all')
        self.assertEqual(code, 0, f'prepare indexes failed:\n{out}\n{err}')
        code, out, err = self.cli("verify", ident)
        self.assertEqual(code, 0, f"verify failed:\nout={out}\nerr={err}")
        return out

    def handoff_task(self, ident, summary="готово к приёмке",
                     actor="Antigravity", model="Gemini 3.8 Flash (High)"):
        code, out, err = self.cli("lifecycle", "handoff", ident,
                                 "--summary", summary,
                                 "--actor", actor, "--model", model)
        self.assertEqual(code, 0, f"handoff failed:\nout={out}\nerr={err}")
        return out

    def accept_task(self, ident, result="accepted", kind="acceptance",
                    role="auditor", note="принято", actor="Codex", model="GPT-6"):
        args = ["lifecycle", "accept", ident, "--result", result,
                "--kind", kind, "--role", role, "--actor", actor,
                "--model", model]
        if note:
            args += ["--note", note]
        code, out, err = self.cli(*args)
        return code, out, err

    def close_task_cli(self, ident, status="done"):
        return self.cli("close", ident, "--status", status)

    def save_and_apply_lifecycle_request(self, payload):
        for action in payload["actions"]:
            if action["action"] == "promote_change":
                kind, slug = action["document"].split(":", 1)
                promotion_receipt(self.root, self.root / "docs" / {"spec": "specs", "design": "designs"}[kind] / (slug + ".md"), action)
        req_path = self.root / "work" / "test-request.json"
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        code, out, err = self.cli("lifecycle", "plan", "work/test-request.json",
                                 "--save")
        self.assertEqual(code, 0, f"plan failed:\nout={out}\nerr={err}")
        lines = out.splitlines()
        plan_line = next((l for l in lines if "План записан:" in l or "План сохранён:" in l), None)
        self.assertIsNotNone(plan_line, f"No plan line in out: {out}")
        prefix = "План записан:" if "План записан:" in plan_line else "План сохранён:"
        plan_rel = plan_line.split(prefix, 1)[1].strip()
        code, out, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, f"apply failed:\nout={out}\nerr={err}")
        return out

    def test_small_task_route_e2e(self):
        """AC-03: Маленькая задача (баг) в одной карточке, без отдельного spec/plan;

        verify -> handoff -> независимый аудит в той же карточке -> close.
        Автосоздания аудитора нет.
        """
        self.configure_full_project()

        body = ("## Цель и границы\n\nИсправление локального бага.\n\n"
                "## Контекст\n\n[Архитектура](../architecture.md)\n\n"
                "## Приёмка\n\n- [x] Дефект исправлен.\n\n"
                "## Проверки и доказательства\n\nКоманда: verify\n\n"
                "## Выполнение и проверки\n\n2026-10-22: исправлено.\n\n"
                "## Передача\n\nГотово к аудиту.\n")

        initial_tasks = set(self.tasks_dir().glob("*.md"))

        # 1. Постановка карточки
        card_file = self.add_task_file("TASK-101", title="Исправить баг валидации",
                                       status="ready", body=body)
        self.assertTrue(card_file.is_file())

        # 2. Старт выполнения
        self.update_task_text("TASK-101", "Status: ready", "Status: in_progress")
        self.update_task_text("TASK-101", "Executor: -", "Executor: Antigravity")
        self.update_task_text("TASK-101", "Model: -", "Model: Gemini 3.8 Flash (High)")

        # 3. Самопроверка verify
        self.verify_task("TASK-101")

        # 4. Передача (handoff) и перевод в in_review
        self.handoff_task("TASK-101", summary="Дефект устранён и проверен")
        self.update_task_text("TASK-101", "Status: in_progress", "Status: in_review")

        # Проверка: автосоздания карточки аудитора нет!
        current_tasks = set(self.tasks_dir().glob("*.md"))
        self.assertEqual(current_tasks - initial_tasks, {card_file},
                         "Автосоздания аудитора быть не должно")

        # 5. Независимый аудит в ТОЙ ЖЕ карточке
        code, out, err = self.accept_task("TASK-101", result="accepted",
                                          kind="acceptance", role="auditor",
                                          note="Проверено в отдельной сессии")
        self.assertEqual(code, 0, err)

        # 6. Закрытие карточки
        code, out, err = self.close_task_cli("TASK-101")
        self.assertEqual(code, 0, err)

        # 7. Проверка результата
        self.assertFalse((self.tasks_dir() / "TASK-101.md").exists())
        archived_card = self.archive_tasks_dir() / "TASK-101.md"
        self.assertTrue(archived_card.is_file())
        text = archived_card.read_text(encoding="utf-8")
        self.assertIn("Status: done", text)
        self.assertNotIn("Closed: -", text)

        evidence_dir = self.archive_tasks_dir() / "TASK-101-evidence"
        self.assertTrue(evidence_dir.is_dir())
        receipts = list(evidence_dir.glob("close-*.json"))
        self.assertEqual(len(receipts), 1)

        # Проверка индекса и ready
        index_text = (self.root / "docs" / "tasks" / "index.md").read_text(encoding="utf-8")
        self.assertIn("[TASK-101: Исправить баг валидации](../archive/tasks/TASK-101.md)", index_text)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_medium_task_route_e2e(self):
        """AC-04, AC-07: Средняя задача (feature) с отдельной спецификацией;

        действующее поведение сохраняется до приёмки; promotion обновляет
        только затронутые требования, старая ревизия сохраняется в evidence,
        спецификация остаётся активной.
        """
        self.configure_full_project()

        # 1. Создание спецификации через CLI
        code, out, err = self.cli("document", "create", "spec", "payment-retry",
                                  "--title", "Повтор платежей", "--apply")
        self.assertEqual(code, 0, err)
        spec_path = self.root / "docs" / "specs" / "payment-retry.md"
        self.assertTrue(spec_path.is_file())

        # 2. Наполнение спецификации действующим поведением и планируемым изменением
        spec_meta = {
            "schema_version": 1,
            "document_id": "spec:payment-retry",
            "kind": "spec",
            "state": "accepted",
            "created_at": "2026-10-22T08:00:00+03:00",
            "updated_at": "2026-10-22T08:00:00+03:00",
            "current_revision": 1,
            "relations": [],
            "tasks": ["DEV-201"],
            "changes": [
                {
                    "id": "ch-retry",
                    "state": "approved",
                    "section": "## 4. Действующее поведение",
                    "basis": {
                        "type": "engineer_approval",
                        "who": "Engineer",
                        "detail": "согласовано расширение",
                        "source": "диалог",
                        "at": "2026-10-22T08:00:00+03:00"
                    },
                    "tasks": ["DEV-201"],
                    "acceptance": None
                }
            ],
            "materials": [],
            "archive": None
        }
        body = ("## 1. Проблема и цель\n\nУлучшение надёжности платежей.\n\n"
                "## 4. Действующее поведение\n\n"
                "Платежи выполняются однократно без автоматических повторов.\n\n"
                "## 5. Планируемые изменения\n\n"
                "Автоматический повтор платежа до 3 раз при сбоях сети.\n\n"
                "## 7. Критерии приёмки\n\n- [x] Повтор до 3 раз.\n")
        spec_path.write_text(f"# Повтор платежей\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{body}", encoding="utf-8")

        # 3. Создание задачи DEV-201 со ссылкой на спецификацию
        task_body = ("## Цель и границы\n\nРеализация повтора платежей.\n\n"
                     "## Контекст\n\n[Спецификация](../specs/payment-retry.md)\n\n"
                     "## Приёмка\n\n- [x] Повтор платежа до 3 раз.\n\n"
                     "## Выполнение и проверки\n\n2026-10-22: код написан.\n\n"
                     "## Передача\n\nГотово.\n")
        self.add_task_file("DEV-201", title="Реализовать повтор платежей",
                           status="in_progress", executor="Antigravity",
                           model="Gemini 3.8 Flash (High)", body=task_body)

        # Проверка: во время выполнения спецификация по-прежнему отражает действующее поведение
        spec_text = spec_path.read_text(encoding="utf-8")
        self.assertIn("Платежи выполняются однократно без автоматических повторов", spec_text)

        # 4. Завершение реализации: статус изменения переходит в implemented
        spec_meta["changes"][0]["state"] = "implemented"
        spec_path.write_text(f"# Повтор платежей\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{body}", encoding="utf-8")

        # 5. Verify и handoff задачи
        self.verify_task("DEV-201")
        self.handoff_task("DEV-201", summary="Повтор платежей реализован")
        self.update_task_text("DEV-201", "Status: in_progress", "Status: in_review")

        # 6. Независимый аудит принимает задачу
        code, out, err = self.accept_task("DEV-201", result="accepted",
                                          kind="acceptance", role="auditor",
                                          note="Тесты и сценарии проверены")
        self.assertEqual(code, 0, err)

        # 7. Promotion изменения спецификации
        new_payload = ("## 1. Проблема и цель\n\nУлучшение надёжности платежей.\n\n"
                       "## 4. Действующее поведение\n\n"
                       "Платежи выполняются с автоматическим повтором до 3 раз.\n\n"
                       "## 5. Планируемые изменения\n\n"
                       "Автоматический повтор платежа до 3 раз (принято в DEV-201).\n\n"
                       "## 7. Критерии приёмки\n\n- [x] Повтор до 3 раз.\n")
        req = promote_request("spec:payment-retry", "ch-retry",
                              payload=new_payload, acceptance="DEV-201")
        self.save_and_apply_lifecycle_request(req)

        # 8. Проверка результатов promotion
        spec_text = spec_path.read_text(encoding="utf-8")
        self.assertIn("Платежи выполняются с автоматическим повтором до 3 раз", spec_text)
        errors = []
        promoted_meta = documents.parse_meta(spec_text, "docs/specs/payment-retry.md", errors)
        self.assertEqual(errors, [])
        self.assertEqual(promoted_meta["current_revision"], 2)
        self.assertEqual(promoted_meta["changes"][0]["state"], "accepted")

        # Проверка: снимок ревизии 1 сохранён в evidence спецификации
        rev1_file = self.root / "docs" / "specs" / "payment-retry-evidence" / "revision-1.md"
        self.assertTrue(rev1_file.is_file())
        self.assertIn("Платежи выполняются однократно без автоматических повторов",
                      rev1_file.read_text(encoding="utf-8"))

        # Проверка: сама спецификация осталась в docs/specs/ (АКТИВНА!)
        self.assertTrue(spec_path.is_file())
        self.assertFalse((self.root / "docs" / "archive" / "specs" / "payment-retry.md").exists())

        # 9. Закрытие задачи DEV-201
        code, out, err = self.close_task_cli("DEV-201")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.archive_tasks_dir() / "DEV-201.md").is_file())

        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_large_block_route_e2e(self):
        """AC-05, AC-06, AC-08: Большая задача (блок подзадач + общий аудит);

        закрытие подзадачи не архивирует общий план/спецификацию;
        замечание аудита возвращает работу на исправление (in_review -> in_progress);
        после исправления требуется повторный verify и handoff;
        итоговый аудит блока принимает блок и архивирует план.
        """
        self.configure_full_project()

        # 1. Создание спецификации и плана блока
        code, _, err = self.cli("document", "create", "spec", "search-engine",
                                "--title", "Поисковый движок", "--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("document", "create", "plan", "search-engine",
                                "--title", "План реализации поискового движка",
                                "--apply")
        self.assertEqual(code, 0, err)

        plan_path = self.root / "docs" / "plans" / "search-engine.md"
        plan_meta = {
            "schema_version": 1,
            "document_id": "plan:search-engine",
            "kind": "plan",
            "state": "accepted",
            "created_at": "2026-10-22T08:00:00+03:00",
            "updated_at": "2026-10-22T08:00:00+03:00",
            "current_revision": 1,
            "relations": [{"relation": "based_on", "target": "spec:search-engine"}],
            "tasks": ["SUB-1", "SUB-2", "AUDIT-1"],
            "changes": [],
            "materials": [],
            "archive": None
        }
        plan_body = ("## 1. Этапы\n\n"
                     "- [SUB-1](../tasks/SUB-1.md): Индексатор\n"
                     "- [SUB-2](../tasks/SUB-2.md): Парсер запросов\n"
                     "- [AUDIT-1](../tasks/AUDIT-1.md): Итоговый аудит блока\n")
        plan_path.write_text(f"# План реализации поискового движка\n\n"
                             f"{documents.META_START}\n```json\n"
                             f"{json.dumps(plan_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{plan_body}", encoding="utf-8")

        # 2. Создание карточек подзадач и карточки аудита блока
        task_meta = dict(TASK_META)
        task_meta["relations"] = [{"relation": "based_on", "target": "plan:search-engine"}]

        sub1_body = ("## Цель и границы\n\nМодуль индексации.\n\n"
                     "## Контекст\n\n[План](../plans/search-engine.md)\n\n"
                     "## Приёмка\n\n- [x] Индексатор готов.\n\n"
                     "## Выполнение и проверки\n\nГотово.\n\n## Передача\n\nГотово.\n")
        self.add_task_file("SUB-1", title="Модуль индексации", status="ready",
                           meta=task_meta, body=sub1_body)

        sub2_body = ("## Цель и границы\n\nМодуль парсинга запросов.\n\n"
                     "## Контекст\n\n[План](../plans/search-engine.md)\n\n"
                     "## Приёмка\n\n- [x] Парсер готов.\n\n"
                     "## Выполнение и проверки\n\nГотово.\n\n## Передача\n\nГотово.\n")
        # SUB-2 зависит от SUB-1, поэтому создаётся в статусе blocked
        self.add_task_file("SUB-2", title="Модуль парсинга запросов", status="blocked",
                           deps="SUB-1", meta=task_meta, body=sub2_body)

        audit_body = ("## Цель и границы\n\nСквозная приёмка блока поиска.\n\n"
                      "## Контекст\n\n[План](../plans/search-engine.md)\n\n"
                      "## Приёмка\n\n- [x] Все подсистемы работают совместно.\n\n"
                      "## Выполнение и проверки\n\nОжидает подзадач.\n\n## Передача\n\nОжидает.\n")
        self.add_task_file("AUDIT-1", title="Сквозной аудит блока поиска", status="draft",
                           deps="SUB-1, SUB-2", meta=task_meta, body=audit_body)

        # 3. Выполнение и закрытие SUB-1
        self.update_task_text("SUB-1", "Status: ready", "Status: in_progress")
        self.update_task_text("SUB-1", "Executor: -", "Executor: Antigravity")
        self.update_task_text("SUB-1", "Model: -", "Model: Gemini 3.8 Flash (High)")
        self.verify_task("SUB-1")
        self.handoff_task("SUB-1", summary="Индексатор готов")
        self.update_task_text("SUB-1", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("SUB-1", result="accepted", kind="acceptance")
        self.assertEqual(code, 0, err)
        code, out, err = self.close_task_cli("SUB-1")
        self.assertEqual(code, 0, err)

        # КРИТИЧЕСКАЯ ПРОВЕРКА: закрытие подзадачи SUB-1 НЕ архивирует общий план и спецификацию!
        self.assertTrue(plan_path.is_file(), "Общий план обязан остаться активным!")
        self.assertTrue((self.root / "docs" / "specs" / "search-engine.md").is_file(),
                        "Спецификация обязана остаться активной!")
        self.assertFalse((self.root / "docs" / "archive" / "plans" / "search-engine.md").exists())

        # 4. Выполнение SUB-2 и замечание аудитора (возврат на исправление)
        # SUB-1 завершён -> SUB-2 разблокируется
        self.update_task_text("SUB-2", "Status: blocked", "Status: ready")
        self.update_task_text("SUB-2", "Status: ready", "Status: in_progress")
        self.update_task_text("SUB-2", "Executor: -", "Executor: Antigravity")
        self.update_task_text("SUB-2", "Model: -", "Model: Gemini 3.8 Flash (High)")
        self.verify_task("SUB-2")
        self.handoff_task("SUB-2", summary="Парсер готов")
        self.update_task_text("SUB-2", "Status: in_progress", "Status: in_review")

        # Попытка закрыть SUB-2 без acceptance отклоняется
        code, out, err = self.close_task_cli("SUB-2")
        self.assertEqual(code, 1)
        self.assertIn("приёмки", err)

        # Аудитор находит замечание: регистрирует диагностический протокол и возвращает карточку
        code, out, err = self.accept_task("SUB-2", result="accepted", kind="diagnostic",
                                          note="Замечание: не учтены пустые запросы")
        self.assertEqual(code, 0, err)

        # Карточка возвращается в in_progress
        self.update_task_text("SUB-2", "Status: in_review", "Status: in_progress")
        # Попытка закрыть карточку в in_progress отклоняется
        code, out, err = self.close_task_cli("SUB-2")
        self.assertEqual(code, 1)

        # Исполнитель исправляет дефект: файл теста меняется
        (self.root / "tests" / "test_parser.py").write_text("# обработка пустых запросов\n",
                                                            encoding="utf-8")
        # Старый прогон verify устарел (входы tests изменились)
        self.verify_task("SUB-2")
        self.handoff_task("SUB-2", summary="Исправлена обработка пустых запросов")
        self.update_task_text("SUB-2", "Status: in_progress", "Status: in_review")

        # Повторный аудит принимает работу
        code, out, err = self.accept_task("SUB-2", result="accepted", kind="acceptance",
                                          note="Замечание устранено, приёмка подтверждена")
        self.assertEqual(code, 0, err)
        code, out, err = self.close_task_cli("SUB-2")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.archive_tasks_dir() / "SUB-2.md").is_file())

        # 5. Попытка архивировать план ДО завершения AUDIT-1 отклоняется
        plan_text = plan_path.read_text(encoding="utf-8").replace('"state": "accepted"', '"state": "completed"')
        plan_path.write_text(plan_text, encoding="utf-8")

        premature_archive_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor"},
            "basis": "попытка преждевременного архивирования плана",
            "scope": {"documents": ["plan:search-engine"]},
            "actions": [{"action": "archive_document",
                         "document": "plan:search-engine",
                         "reason": "все задачи завершены",
                         "successor": None}]
        }
        req_file = self.root / "work" / "premature-archive.json"
        req_file.parent.mkdir(parents=True, exist_ok=True)
        req_file.write_text(json.dumps(premature_archive_req), encoding="utf-8")
        code, out, err = self.cli("lifecycle", "plan", "work/premature-archive.json", "--save")
        self.assertEqual(code, 1)
        self.assertIn("незавершёнными задачами", err)

        # 6. Выполнение и закрытие карточки итогового аудита блока AUDIT-1
        self.update_task_text("AUDIT-1", "Status: draft", "Status: ready")
        self.update_task_text("AUDIT-1", "Status: ready", "Status: in_progress")
        self.update_task_text("AUDIT-1", "Executor: -", "Executor: Codex")
        self.update_task_text("AUDIT-1", "Model: -", "Model: GPT-6")
        self.verify_task("AUDIT-1")
        self.handoff_task("AUDIT-1", summary="Сквозной аудит блока проверен",
                          actor="Codex", model="GPT-6")
        self.update_task_text("AUDIT-1", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("AUDIT-1", result="accepted", kind="acceptance",
                                          role="engineer", note="Инженер утвердил блок")
        self.assertEqual(code, 0, err)
        code, out, err = self.close_task_cli("AUDIT-1")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.archive_tasks_dir() / "AUDIT-1.md").is_file())

        # 7. Теперь все задачи плана завершены -> архивирование плана разрешено!
        archive_plan_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor"},
            "basis": "Все подзадачи и аудит блока завершены",
            "scope": {"documents": ["plan:search-engine"]},
            "actions": [{"action": "archive_document",
                         "document": "plan:search-engine",
                         "reason": "все задачи блока и аудит успешно завершены",
                         "successor": None}]
        }
        self.save_and_apply_lifecycle_request(archive_plan_req)

        # Проверка: план переместился в архив, спецификация осталась активной
        self.assertFalse(plan_path.exists())
        archived_plan = self.root / "docs" / "archive" / "plans" / "search-engine.md"
        self.assertTrue(archived_plan.is_file())
        self.assertTrue((self.root / "docs" / "specs" / "search-engine.md").is_file())

        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_link_and_archive_immutability_e2e(self):
        """AC-08, AC-09: Перемещение в архив сохраняет целостность ссылок;

        сопровождающие материалы (evidence) перемещаются вместе с владельцем;
        содержимое бинарных/статических файлов evidence побайтно неизменно.
        """
        self.configure_full_project()

        evidence_payload = b"\x00\x01\x02\x03BINARY_LOG_DATA\xff\xfe\xfd" * 100
        expected_hash = hashlib.sha256(evidence_payload).hexdigest()

        # Запись статического файла в evidence карточки
        local_ev_dir = self.tasks_dir() / "TASK-301-evidence"
        local_ev_dir.mkdir(parents=True, exist_ok=True)
        (local_ev_dir / "run.bin").write_bytes(evidence_payload)

        body = ("## Цель и границы\n\nПроверка неизменяемости evidence.\n\n"
                "## Контекст\n\n[Архитектура](../architecture.md)\n\n"
                "## Приёмка\n\n- [x] Хэши совпадают.\n\n"
                "## Выполнение и проверки\n\nГотово.\n\n"
                "## Передача\n\nГотово.\n")

        # Создаём карточку с зарегистрированным evidence в materials
        ev_meta = dict(TASK_META)
        ev_meta["materials"] = [{
            "id": "run-bin",
            "role": "probe",
            "source": "docs/tasks/TASK-301-evidence/run.bin",
            "storage": "local",
            "required": True,
            "immutable": True,
            "used_by": ["TASK-301"]
        }]
        self.add_task_file("TASK-301", title="Неизменяемость evidence", status="in_progress",
                           executor="Antigravity", model="Gemini 3.8 Flash (High)",
                           meta=ev_meta, body=body)

        # Выполнение, verify, handoff, accept, close
        self.verify_task("TASK-301")
        self.handoff_task("TASK-301", summary="Evidence подготовлен")
        self.update_task_text("TASK-301", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("TASK-301", result="accepted", kind="acceptance")
        self.assertEqual(code, 0, err)

        code, out, err = self.close_task_cli("TASK-301")
        self.assertEqual(code, 0, err)

        # Проверка: evidence переместился в архив
        archived_ev_dir = self.archive_tasks_dir() / "TASK-301-evidence"
        self.assertTrue(archived_ev_dir.is_dir())
        archived_bin = archived_ev_dir / "run.bin"
        self.assertTrue(archived_bin.is_file())

        # КРИТИЧЕСКАЯ ПРОВЕРКА: хеш побайтно неизменен (immutable!)
        actual_hash = hashlib.sha256(archived_bin.read_bytes()).hexdigest()
        self.assertEqual(actual_hash, expected_hash)

        # Проверка ссылок
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_self_project_copy_pilot(self):
        """AC-12: Копия собственного проекта ai-dev-playbook со всеми незакоммиченными

        документами проходит цикл проверок; подключение схемы documents
        не нарушает существующие задачи и архив. Исходный репозиторий не затронут.
        """
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        copy_root = Path(tmp_dir.name).resolve() / "repo_copy"

        # Копируем текущий репозиторий, исключая .git, .DS_Store, __pycache__
        def ignore_patterns(src, names):
            ignored = set()
            for name in names:
                if name in {".git", ".DS_Store", "__pycache__", ".pytest_cache", "pilot-workspace"}:
                    ignored.add(name)
                elif name.endswith(".pyc"):
                    ignored.add(name)
            return ignored

        shutil.copytree(REPO, copy_root, ignore=ignore_patterns, symlinks=True)

        # 1. Запуск check --ready на точной копии текущего репозитория
        code, out, err = run_cli("--root", str(copy_root), "check", "--ready")
        self.assertEqual(code, 0, f"check --ready failed on copy:\nout={out}\nerr={err}")
        self.assertIn("OK: структура, ссылки", out)

        # 2. Фиксация хэшей исходных данных: все исходные карточки (активные и архивные),
        # все файлы комплекта (scripts, rules, templates, updates.json),
        # настройки resources.json, документы коллекций docs/ и корневые файлы.
        def protected_hash(path):
            content = path.read_bytes()
            # Generated criterion markers may be reset when clone inputs change;
            # requirements, IDs, journal, metadata and every other byte stay protected.
            if path.parent in {copy_root / 'docs/tasks', copy_root / 'docs/archive/tasks'}:
                text = content.decode('utf-8')
                if playbook.task_cards.version(text) == 2:
                    content = playbook.task_cards.normalize_checkboxes(text).encode('utf-8')
            return hashlib.sha256(content).hexdigest()

        original_hashes = {}
        for md in (copy_root / "docs" / "tasks").glob("*.md"):
            if md.name != "index.md":
                original_hashes[str(md.relative_to(copy_root))] = protected_hash(md)
        for md in (copy_root / "docs" / "archive" / "tasks").glob("*.md"):
            original_hashes[str(md.relative_to(copy_root))] = protected_hash(md)
        for py in (copy_root / "ai-dev-playbook" / "scripts").rglob("*.py"):
            original_hashes[str(py.relative_to(copy_root))] = hashlib.sha256(py.read_bytes()).hexdigest()
        for md in (copy_root / "ai-dev-playbook" / "rules").glob("*.md"):
            original_hashes[str(md.relative_to(copy_root))] = hashlib.sha256(md.read_bytes()).hexdigest()
        for tpl in (copy_root / "ai-dev-playbook" / "templates").rglob("*"):
            if tpl.is_file():
                original_hashes[str(tpl.relative_to(copy_root))] = hashlib.sha256(tpl.read_bytes()).hexdigest()
        for f in (copy_root / "ai-dev-playbook" / "resources.json", copy_root / "ai-dev-playbook" / "updates.json",
                  copy_root / "AGENTS.md", copy_root / "README.md", copy_root / "CHANGELOG.md"):
            if f.is_file():
                original_hashes[str(f.relative_to(copy_root))] = hashlib.sha256(f.read_bytes()).hexdigest()
        for d in ("specs", "designs", "adr", "plans", "discussions"):
            col_dir = copy_root / "docs" / d
            if col_dir.is_dir():
                for doc in col_dir.glob("*.md"):
                    if doc.name != "index.md":
                        original_hashes[str(doc.relative_to(copy_root))] = hashlib.sha256(doc.read_bytes()).hexdigest()

        # Настраиваем локальную verification для копии, исключая рекурсивный запуск всего test suite
        copy_proj_path = copy_root / "ai-dev-playbook" / "project.json"
        copy_proj = json.loads(copy_proj_path.read_text(encoding="utf-8"))
        copy_proj["verification"] = {
            "inputs": ["tests"],
            "checks": [
                {"id": "unit", "argv": [sys.executable, "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30},
                {"id": "self-check", "argv": [sys.executable, "ai-dev-playbook/scripts/playbook.py", "check", "--ready"], "timeout_seconds": 60}
            ]
        }
        copy_proj_path.write_text(json.dumps(copy_proj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # 3. Полный жизненный цикл задачи PILOT-001 на копии self-project
        pilot_card_path = copy_root / "docs" / "tasks" / "PILOT-001.md"
        pilot_body = ("## Цель и границы\n\nПроверка полного цикла на копии репозитория.\n\n"
                      "## Контекст\n\n[Архитектура](../architecture.md)\n\n"
                      "## Приёмка\n\n- [x] Проверка пройдена.\n\n"
                      "## Проверки и доказательства\n\nverify\n\n"
                      "## Выполнение и проверки\n\nГотово к аудиту.\n\n"
                      "## Передача\n\nГотово.\n")
        pilot_meta = {
            "schema_version": 1,
            "relations": [],
            "materials": []
        }
        pilot_text = make_card_text("PILOT-001", title="Пилот на копии", status="ready",
                                    meta=pilot_meta, body=pilot_body)
        pilot_card_path.write_text(pilot_text, encoding="utf-8")
        code, out, err = run_cli("--root", str(copy_root), "index", "--all")
        self.assertEqual(code, 0, err)

        # Перевод в in_progress, обновление индекса, verify, handoff
        pilot_card_path.write_text(pilot_card_path.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
        code, out, err = run_cli("--root", str(copy_root), "index", "--all")
        self.assertEqual(code, 0, err)

        # New clone documents/index invalidate source task evidence. Reset only
        # the derived view, without rebinding checks or rewriting their history.
        for source_card in (copy_root / 'docs/tasks').glob('*.md'):
            if source_card.name == 'index.md':
                continue
            card_text = source_card.read_text(encoding='utf-8')
            if playbook.task_cards.version(card_text) == 2:
                parsed = playbook.task_cards.parse(card_text)
                if any(v is not None for v in parsed['criterion_markers'].values()):
                    lifecycle.sync_criteria_command(copy_root, source_card.stem, True,
                        playbook._lifecycle_ctx(None), playbook.safe_path, playbook.require, bind=False)

        code, out, err = run_cli("--root", str(copy_root), "verify", "PILOT-001")
        self.assertEqual(code, 0, f"verify PILOT-001 failed:\nout={out}\nerr={err}")

        code, out, err = run_cli("--root", str(copy_root), "lifecycle", "handoff", "PILOT-001",
                                 "--summary", "пилотный прогон на копии репозитория",
                                 "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)")
        self.assertEqual(code, 0, f"handoff PILOT-001 failed:\nout={out}\nerr={err}")

        # Перевод в in_review, обновление индекса, независимый accept, close
        pilot_card_path.write_text(pilot_card_path.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        code, out, err = run_cli("--root", str(copy_root), "index", "--all")
        self.assertEqual(code, 0, err)

        code, out, err = run_cli("--root", str(copy_root), "lifecycle", "accept", "PILOT-001",
                                 "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
                                 "--note", "принято на копии", "--actor", "Codex", "--model", "GPT-6")
        self.assertEqual(code, 0, f"accept PILOT-001 failed:\nout={out}\nerr={err}")

        code, out, err = run_cli("--root", str(copy_root), "close", "PILOT-001")
        self.assertEqual(code, 0, f"close PILOT-001 failed:\nout={out}\nerr={err}")

        # Карточка переместилась в архив
        self.assertFalse(pilot_card_path.exists())
        self.assertTrue((copy_root / "docs" / "archive" / "tasks" / "PILOT-001.md").is_file())

        # 4. Проверка с разделом documents на копии
        code, out, err = run_cli("--root", str(copy_root), "check", "--ready")
        self.assertEqual(code, 0, f"check --ready with documents failed:\nout={out}\nerr={err}")

        # 5. Проверка document list на копии
        code, out, err = run_cli("--root", str(copy_root), "document", "list")
        self.assertEqual(code, 0, f"document list failed:\nout={out}\nerr={err}")
        self.assertIn("Каталог документов", out)

        # 6. Требования, история, метаданные и код неизменны; допустимы только вычисляемые маркеры.
        for rel_path, expected_hash in original_hashes.items():
            actual_hash = protected_hash(copy_root / rel_path)
            self.assertEqual(actual_hash, expected_hash, f"Файл {rel_path} изменился!")

        # Индекс задач на копии содержит закрытую пилотную задачу
        copy_index_text = (copy_root / "docs" / "tasks" / "index.md").read_text(encoding="utf-8")
        self.assertIn("PILOT-001", copy_index_text)

    def test_ac07_feature_deprecation_and_adr_archival_e2e(self):
        """AC-07: Частичное удаление (SMS) обновляет общую спецификацию и design;

        полное удаление после приёмки архивирует утратившие силу документы, включая ADR.
        """
        self.configure_full_project()

        # 1. Создание спецификации auth-methods, дизайна auth-sms и ADR 0010-sms-auth
        code, _, err = self.cli("document", "create", "spec", "auth-methods",
                                "--title", "Методы аутентификации", "--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("document", "create", "design", "auth-sms",
                                "--title", "Архитектура SMS-аутентификации", "--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("document", "create", "adr", "0010-sms-auth",
                                "--title", "Использование SMS для второго фактора",
                                "--apply")
        self.assertEqual(code, 0, err)

        spec_path = self.root / "docs" / "specs" / "auth-methods.md"
        design_path = self.root / "docs" / "designs" / "auth-sms.md"
        adr_path = self.root / "docs" / "adr" / "0010-sms-auth.md"

        basis = {
            "type": "engineer_approval",
            "who": "Engineer",
            "detail": "согласовано удаление SMS",
            "source": "диалог",
            "at": "2026-10-22T08:00:00+03:00"
        }
        spec_meta = {
            "schema_version": 1,
            "document_id": "spec:auth-methods",
            "kind": "spec",
            "state": "accepted",
            "created_at": "2026-10-22T08:00:00+03:00",
            "updated_at": "2026-10-22T08:00:00+03:00",
            "current_revision": 1,
            "relations": [
                {"relation": "based_on", "target": "adr:0010-sms-auth"}
            ],
            "tasks": ["DEV-701", "DEV-702"],
            "changes": [
                {
                    "id": "ch-deprecate",
                    "state": "approved",
                    "section": "## 4. Действующее поведение",
                    "basis": basis,
                    "tasks": ["DEV-701"],
                    "acceptance": None
                },
                {
                    "id": "ch-remove-sms",
                    "state": "approved",
                    "section": "## 4. Действующее поведение",
                    "basis": basis,
                    "tasks": ["DEV-702"],
                    "acceptance": None
                }
            ],
            "materials": [],
            "archive": None
        }
        spec_body = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                     "## 4. Действующее поведение\n\nПоддерживаются SMS и WebAuthn.\n\n"
                     "## 5. Планируемые изменения\n\nПереход исключительно на WebAuthn.\n\n"
                     "## 7. Критерии приёмки\n\n- [x] WebAuthn работает.\n")
        spec_path.write_text(f"# Методы аутентификации\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{spec_body}", encoding="utf-8")

        design_meta = {
            "schema_version": 1,
            "document_id": "design:auth-sms",
            "kind": "design",
            "state": "accepted",
            "created_at": "2026-10-22T08:00:00+03:00",
            "updated_at": "2026-10-22T08:00:00+03:00",
            "current_revision": 1,
            "relations": [
                {"relation": "based_on", "target": "spec:auth-methods"},
                {"relation": "based_on", "target": "adr:0010-sms-auth"}
            ],
            "tasks": ["DEV-701", "DEV-702"],
            "changes": [],
            "materials": [],
            "archive": None
        }
        design_body = "## 1. Архитектура решения\n\nМодуль отправки SMS-кодов через шлюз.\n"
        design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                               f"{json.dumps(design_meta, ensure_ascii=False, indent=2)}\n```\n"
                               f"{documents.META_END}\n\n{design_body}", encoding="utf-8")

        adr_meta = {
            "schema_version": 1,
            "document_id": "adr:0010-sms-auth",
            "kind": "adr",
            "state": "accepted",
            "created_at": "2026-10-22T08:00:00+03:00",
            "updated_at": "2026-10-22T08:00:00+03:00",
            "current_revision": 1,
            "relations": [],
            "tasks": [],
            "changes": [],
            "materials": [],
            "archive": None
        }
        adr_body = ("## 1. Контекст\n\nВыбор второго фактора аутентификации.\n\n"
                    "## 2. Решение\n\nИспользовать SMS-коды для подтверждения входа.\n")
        adr_path.write_text(f"# ADR-0010: Использование SMS\n\n{documents.META_START}\n```json\n"
                            f"{json.dumps(adr_meta, ensure_ascii=False, indent=2)}\n```\n"
                            f"{documents.META_END}\n\n{adr_body}", encoding="utf-8")

        # 2. Создание карточек задач DEV-701 и DEV-702 с привязкой к spec:auth-methods и design:auth-sms
        task_meta = dict(TASK_META)
        task_meta["relations"] = [
            {"relation": "based_on", "target": "spec:auth-methods"},
            {"relation": "based_on", "target": "design:auth-sms"}
        ]
        dev701_body = ("## Цель и границы\n\nЧастичный вывод SMS: запрет новых подключений.\n\n"
                       "## Контекст\n\n[Спецификация](../specs/auth-methods.md)\n"
                       "[Дизайн](../designs/auth-sms.md)\n\n"
                       "## Приёмка\n\n- [x] Регистрация новых пользователей по SMS запрещена.\n\n"
                       "## Выполнение и проверки\n\nГотово.\n\n## Передача\n\nГотово.\n")
        self.add_task_file("DEV-701", title="Частичный вывод SMS", status="in_progress",
                           executor="Antigravity", model="Gemini 3.8 Flash (High)",
                           meta=task_meta, body=dev701_body)

        dev702_body = ("## Цель и границы\n\nПолное удаление SMS.\n\n"
                       "## Контекст\n\n[Спецификация](../specs/auth-methods.md)\n\n"
                       "## Приёмка\n\n- [x] SMS удалён полностью.\n\n"
                       "## Выполнение и проверки\n\nГотово.\n\n## Передача\n\nГотово.\n")
        self.add_task_file("DEV-702", title="Полное удаление SMS", status="in_progress",
                           executor="Antigravity", model="Gemini 3.8 Flash (High)",
                           meta=task_meta, body=dev702_body)

        # 3. Задача DEV-701: Частичное удаление / запрет новых подключений SMS
        self.verify_task("DEV-701")
        self.handoff_task("DEV-701", summary="SMS переведён в legacy-режим")
        self.update_task_text("DEV-701", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("DEV-701", result="accepted", kind="acceptance")
        self.assertEqual(code, 0, err)

        # Переводим ch-deprecate в implemented перед promotion
        errs = []
        cur_spec_meta = documents.parse_meta(spec_path.read_text(encoding="utf-8"), str(spec_path), errs)
        for ch in cur_spec_meta["changes"]:
            if ch["id"] == "ch-deprecate":
                ch["state"] = "implemented"
        spec_body_text = spec_path.read_text(encoding="utf-8").split(documents.META_END, 1)[1].lstrip("\n")
        spec_path.write_text(f"# Методы аутентификации\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(cur_spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{spec_body_text}", encoding="utf-8")

        # Promotion изменения ch-deprecate в спецификации и обновление дизайна
        depr_payload = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                        "## 4. Действующее поведение\n\n"
                        "SMS переведён в legacy-режим: регистрация новых пользователей запрещена, "
                        "вход по SMS сохранён только для существующих пользователей. Рекомендуется WebAuthn.\n\n"
                        "## 5. Планируемые изменения\n\nПолный вывод SMS.\n\n"
                        "## 7. Критерии приёмки\n\n- [x] Регистрация по SMS заблокирована.\n")
        req1 = promote_request("spec:auth-methods", "ch-deprecate", payload=depr_payload, acceptance="DEV-701")
        self.save_and_apply_lifecycle_request(req1)

        # Обновление дизайна в рамках DEV-701: повышаем ревизию 1 -> 2
        design_meta["current_revision"] = 2
        design_meta["updated_at"] = "2026-10-22T08:30:00+03:00"
        design_body_v2 = ("## 1. Архитектура решения\n\n"
                          "Модуль отправки SMS переведён в legacy-режим (только существующие пользователи).\n")
        design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                               f"{json.dumps(design_meta, ensure_ascii=False, indent=2)}\n```\n"
                               f"{documents.META_END}\n\n{design_body_v2}", encoding="utf-8")

        # Проверка: ревизия 2 для spec и design, спецификация и дизайн активны, ADR всё ещё активен!
        self.assertTrue(spec_path.is_file(), "Спецификация обязана оставаться активной!")
        self.assertTrue(design_path.is_file(), "Дизайн обязан оставаться активным при частичном выводе!")
        self.assertTrue(adr_path.is_file(), "При частичном выводе ADR обязан оставаться активным!")
        cur_spec_v2 = documents.parse_meta(spec_path.read_text(encoding="utf-8"), str(spec_path), errs)
        self.assertEqual(cur_spec_v2["current_revision"], 2, "Спецификация обязана быть на ревизии 2!")
        cur_design_v2 = documents.parse_meta(design_path.read_text(encoding="utf-8"), str(design_path), errs)
        self.assertEqual(cur_design_v2["current_revision"], 2, "Дизайн обязан быть на ревизии 2!")
        code, _, err = self.close_task_cli("DEV-701")
        self.assertEqual(code, 0, err)

        # 4. Задача DEV-702: Полный вывод SMS и архивирование design:auth-sms и ADR 0010-sms-auth
        self.verify_task("DEV-702")
        self.handoff_task("DEV-702", summary="SMS удалён полностью")
        self.update_task_text("DEV-702", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("DEV-702", result="accepted", kind="acceptance")
        self.assertEqual(code, 0, err)

        # Переводим ch-remove-sms в implemented, design и ADR в superseded перед архивацией
        cur_spec_meta = documents.parse_meta(spec_path.read_text(encoding="utf-8"), str(spec_path), errs)
        for ch in cur_spec_meta["changes"]:
            if ch["id"] == "ch-remove-sms":
                ch["state"] = "implemented"
        spec_body_text = spec_path.read_text(encoding="utf-8").split(documents.META_END, 1)[1].lstrip("\n")
        spec_path.write_text(f"# Методы аутентификации\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(cur_spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{spec_body_text}", encoding="utf-8")

        cur_design_meta = documents.parse_meta(design_path.read_text(encoding="utf-8"), str(design_path), errs)
        cur_design_meta["state"] = "superseded"
        d_body = design_path.read_text(encoding="utf-8").split(documents.META_END, 1)[1].lstrip("\n")
        design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                               f"{json.dumps(cur_design_meta, ensure_ascii=False, indent=2)}\n```\n"
                               f"{documents.META_END}\n\n{d_body}", encoding="utf-8")

        cur_adr_meta = documents.parse_meta(adr_path.read_text(encoding="utf-8"), str(adr_path), errs)
        cur_adr_meta["state"] = "superseded"
        a_body = adr_path.read_text(encoding="utf-8").split(documents.META_END, 1)[1].lstrip("\n")
        adr_path.write_text(f"# ADR-0010: Использование SMS\n\n{documents.META_START}\n```json\n"
                            f"{json.dumps(cur_adr_meta, ensure_ascii=False, indent=2)}\n```\n"
                            f"{documents.META_END}\n\n{a_body}", encoding="utf-8")

        # Promotion ch-remove-sms и одновременное архивирование design:auth-sms и ADR 0010-sms-auth
        final_spec_payload = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                              "## 4. Действующее поведение\n\nПоддерживается только WebAuthn.\n\n"
                              "## 5. Планируемые изменения\n\nНет.\n\n"
                              "## 7. Критерии приёмки\n\n- [x] Только WebAuthn.\n")
        combined_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor"},
            "basis": "Полный вывод SMS и архивация решения",
            "scope": {"documents": ["spec:auth-methods", "design:auth-sms", "adr:0010-sms-auth"]},
            "actions": [
                {
                    "action": "promote_change",
                    "document": "spec:auth-methods",
                    "change": "ch-remove-sms",
                    "payload": final_spec_payload,
                    "acceptance": "DEV-702"
                },
                {
                    "action": "archive_document",
                    "document": "design:auth-sms",
                    "reason": "полный вывод SMS из эксплуатации",
                    "successor": None
                },
                {
                    "action": "archive_document",
                    "document": "adr:0010-sms-auth",
                    "reason": "полный вывод SMS из эксплуатации",
                    "successor": None
                }
            ]
        }
        self.save_and_apply_lifecycle_request(combined_req)

        # 5. Проверка: спецификация активна (ревизия 3), дизайн и ADR архивированы!
        self.assertTrue(spec_path.is_file(), "Спецификация остаётся активной!")
        cur_spec_v3 = documents.parse_meta(spec_path.read_text(encoding="utf-8"), str(spec_path), errs)
        self.assertEqual(cur_spec_v3["current_revision"], 3, "Спецификация обязана быть на ревизии 3!")
        self.assertFalse(design_path.exists(), "Дизайн должен быть перемещён в архив!")
        self.assertFalse(adr_path.exists(), "ADR должен быть перемещён в архив!")
        archived_design = self.root / "docs" / "archive" / "designs" / "auth-sms.md"
        archived_adr = self.root / "docs" / "archive" / "adr" / "0010-sms-auth.md"
        self.assertTrue(archived_design.is_file(), "Дизайн обязан находиться в архиве!")
        self.assertTrue(archived_adr.is_file(), "ADR обязан находиться в архиве!")

        code, _, err = self.close_task_cli("DEV-702")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_ac10_ac11_lifecycle_controls_and_missing_evidence(self):
        """AC-10, AC-11: Потерянное обязательное доказательство блокирует закрытие;

        отмена/откладывание фиксируют причину и не выдаются за реализацию.
        """
        self.configure_full_project()

        ev_file = self.tasks_dir() / "TASK-801-evidence" / "probe.log"
        ev_file.parent.mkdir(parents=True, exist_ok=True)
        ev_file.write_text("ok log\n", encoding="utf-8")

        ev_meta = dict(TASK_META)
        ev_meta["materials"] = [{
            "id": "probe-log",
            "role": "log",
            "source": "docs/tasks/TASK-801-evidence/probe.log",
            "storage": "local",
            "required": True,
            "immutable": True,
            "used_by": ["TASK-801"]
        }]
        body = ("## Цель и границы\n\nПроверка обязательного evidence.\n\n"
                "## Контекст\n\n[Архитектура](../architecture.md)\n\n"
                "## Приёмка\n\n- [x] Проверено.\n\n"
                "## Выполнение и проверки\n\nГотово.\n\n## Передача\n\nГотово.\n")
        self.add_task_file("TASK-801", title="Проверка контроля материалов", status="in_progress",
                           executor="Antigravity", model="Gemini 3.8 Flash (High)",
                           meta=ev_meta, body=body)

        self.verify_task("TASK-801")
        self.handoff_task("TASK-801", summary="Готово к приёмке")
        self.update_task_text("TASK-801", "Status: in_progress", "Status: in_review")
        code, out, err = self.accept_task("TASK-801", result="accepted", kind="acceptance")
        self.assertEqual(code, 0, err)

        # Удаляем обязательный файл evidence
        ev_file.unlink()

        # Попытка закрыть задачу без обязательного evidence блокируется!
        code, out, err = self.close_task_cli("TASK-801")
        self.assertEqual(code, 1)
        self.assertIn("обязательный материал", err)

        # Восстанавливаем evidence
        ev_file.write_text("ok log restored\n", encoding="utf-8")

        # Отмена карточки с метаданными требует регистрации приёмки с result=cancelled
        code, out, err = self.accept_task("TASK-801", result="cancelled", kind="acceptance",
                                          role="auditor", note="Задача отменена инженером")
        self.assertEqual(code, 0, err)

        # Закрытие с признаком cancelled
        code, out, err = self.cli("close", "TASK-801", "--status", "cancelled",
                                  "--reason", "Задача отменена инженером")
        self.assertEqual(code, 0, err)

        # Проверка: карточка в архиве имеет статус cancelled, а не done
        archived_card = self.archive_tasks_dir() / "TASK-801.md"
        self.assertTrue(archived_card.is_file())
        text = archived_card.read_text(encoding="utf-8")
        self.assertIn("Status: cancelled", text)
        self.assertNotIn("Status: done", text)

        # Reopen: возвращение отменённой задачи из архива обратно в работу (in_progress)
        reopen_req = {
            "schema_version": 1,
            "actor": {"name": "Engineer", "role": "engineer"},
            "basis": "Возобновление работ по требованию инженера",
            "scope": {"tasks": ["TASK-801"]},
            "actions": [{
                "action": "reopen",
                "task": "TASK-801",
                "status": "in_progress",
                "reason": "Возобновление работ по требованию инженера"
            }]
        }
        self.save_and_apply_lifecycle_request(reopen_req)

        # Проверка: карточка вернулась из архива в docs/tasks/, статус in_progress, Closed: -
        reopened_card = self.tasks_dir() / "TASK-801.md"
        self.assertTrue(reopened_card.is_file(), "Карточка обязана вернуться в активные задачи!")
        self.assertFalse(archived_card.exists(), "Карточка обязана быть удалена из архива при reopen!")
        card_text = reopened_card.read_text(encoding="utf-8")
        self.assertIn("Status: in_progress", card_text)
        self.assertIn("Closed: -", card_text)
        self.assertIn("## Возврат", card_text)

        # Проверка создания companion record о reopen в evidence
        reopen_records = list((self.tasks_dir() / "TASK-801-evidence").glob("reopen-*.json"))
        self.assertEqual(len(reopen_records), 1, "Запись о reopen обязана быть создана в evidence!")

        # Postpone: откладывание задачи в blocked с явной причиной в журнале выполнения
        postpone_body = ("## Цель и границы\n\nТест откладывания задачи.\n\n"
                         "## Приёмка\n\n- [x] Отложено.\n\n"
                         "## Выполнение и проверки\n\n2026-10-22: задача отложена до внешнего API (postpone).\n\n"
                         "## Передача\n\n-\n")
        postpone_meta = dict(TASK_META)
        self.add_task_file("TASK-802", title="Откладывание задачи", status="blocked",
                           executor="Antigravity", model="Gemini 3.8 Flash (High)",
                           meta=postpone_meta, body=postpone_body)
        code, out, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err)

        card802 = self.tasks_dir() / "TASK-802.md"
        self.assertTrue(card802.is_file(), "Карточка TASK-802 обязана существовать")
        self.assertIn("Status: blocked", card802.read_text(encoding="utf-8"))
        self.assertIn("postpone", card802.read_text(encoding="utf-8"))

        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_ac02_discussion_lifecycle_and_transition_to_spec(self):
        """AC-02: Обсуждения без файлов по умолчанию; CLI create создаёт discussion и index;

        переход к требованиям в spec не требует промежуточных заметок.
        """
        self.configure_full_project()

        # 1. По умолчанию каталог обсуждений не содержит документов тем (кроме index.md)
        discussions_dir = self.root / "docs" / "discussions"
        topics = [f for f in discussions_dir.glob("*.md") if f.name != "index.md"]
        self.assertEqual(len(topics), 0)

        # 2. Сохранение значимого решения создаёт discussion-документ и обновляет index
        code, out, err = self.cli("document", "create", "discussion", "auth-strategy",
                                  "--title", "Стратегия аутентификации", "--apply")
        self.assertEqual(code, 0, err)
        disc_path = discussions_dir / "auth-strategy.md"
        self.assertTrue(disc_path.is_file())
        disc_text = disc_path.read_text(encoding="utf-8")
        self.assertIn('"state": "open"', disc_text)
        self.assertIn("auth-strategy", disc_text)

        # Индекс обсуждений обновлён
        code, out, err = self.cli("index", "--documents")
        self.assertEqual(code, 0, err)
        disc_index = discussions_dir / "index.md"
        self.assertTrue(disc_index.is_file())
        self.assertIn("auth-strategy", disc_index.read_text(encoding="utf-8"))

        # 3. Переход от обсуждения к требованиям в спецификацию создаёт спецификацию
        # напрямую без обязательной промежуточной Obsidian-заметки
        code, out, err = self.cli("document", "create", "spec", "auth-spec",
                                  "--title", "Спецификация аутентификации", "--apply")
        self.assertEqual(code, 0, err)
        spec_path = self.root / "docs" / "specs" / "auth-spec.md"
        self.assertTrue(spec_path.is_file())

        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
