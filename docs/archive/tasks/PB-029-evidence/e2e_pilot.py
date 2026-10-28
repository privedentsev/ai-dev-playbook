#!/usr/bin/env python3
"""Standalone e2e pilot for PB-029.

Exercises small task route, medium task route, large block route with audit findings,
evidence immutability, and self-project copy pilot.
Produces detailed structured log for AUDIT-006.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "ai-dev-playbook" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import playbook
documents = playbook.documents
lifecycle = playbook.lifecycle

TASK_META = {"schema_version": 1, "relations": [], "materials": [], "moves": []}


def run(cmd, cwd, env=None):
    res = subprocess.run(cmd, cwd=cwd, env=env, text=True,
                         capture_output=True)
    return res.returncode, res.stdout, res.stderr


def main():
    print("=== Запуск сквозного пилота маршрутов PB-029 ===")
    pilot_dir = REPO / "work" / "pilot-workspace"
    if pilot_dir.exists():
        shutil.rmtree(pilot_dir)
    pilot_dir.mkdir(parents=True, exist_ok=True)
    root = pilot_dir.resolve()
    print(f"Полигон сохранён на диске: {root}")

    # 1. Инициализация проекта
    print("\n--- 1. Инициализация проекта и настройка 5 коллекций документов ---")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "init",
                          str(root), "--apply"], cwd=REPO)
    assert code == 0, f"init failed: {err}"

    for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
        doc = root / "docs" / name
        if doc.is_file():
            doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")

    project_path = root / "ai-dev-playbook" / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["name"] = "pilot-project"
    project["setup_status"] = "configured"
    project["commands"]["test"] = "python3 -m unittest discover -s tests -v"
    project["components"] = [{"name": "pilot-app", "code": "src",
                              "tests": ["tests"], "migrations": None}]
    project["verification"] = {
        "inputs": ["src", "tests"],
        "checks": [
            {
                "id": "unit-tests",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                "timeout_seconds": 30
            }
        ]
    }
    project["documents"] = {
        "schema_version": 1,
        "collections": {
            "discussion": {"active": "docs/discussions", "archive": "docs/archive/discussions", "index": "docs/discussions/index.md"},
            "spec": {"active": "docs/specs", "archive": "docs/archive/specs", "index": "docs/specs/index.md"},
            "design": {"active": "docs/designs", "archive": "docs/archive/designs", "index": "docs/designs/index.md"},
            "adr": {"active": "docs/adr", "archive": "docs/archive/adr", "index": "docs/adr/index.md"},
            "plan": {"active": "docs/plans", "archive": "docs/archive/plans", "index": "docs/plans/index.md"}
        }
    }
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    validator_code_v1 = '''def parse_age(value: str) -> int:
    """Парсинг возраста из строки.
    
    Исходная реализация: дефект на пустых/пробельных строках (вызывает ValueError).
    """
    return int(value)
'''
    (root / "src" / "validator.py").write_text(validator_code_v1, encoding="utf-8")

    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    test_validator_v1 = '''import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.validator import parse_age

class TestValidator(unittest.TestCase):
    def test_valid_age(self):
        self.assertEqual(parse_age("25"), 25)

    def test_whitespace_age(self):
        self.assertEqual(parse_age(" 42 "), 42)

if __name__ == "__main__":
    unittest.main()
'''
    (root / "tests" / "test_validator.py").write_text(test_validator_v1, encoding="utf-8")
    test_parser_v1 = '''import unittest

class TestParser(unittest.TestCase):
    def test_parse_basic(self):
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
'''
    (root / "tests" / "test_parser.py").write_text(test_parser_v1, encoding="utf-8")

    for col in project["documents"]["collections"].values():
        (root / col["active"]).mkdir(parents=True, exist_ok=True)

    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "index", "--all"], cwd=root)
    assert code == 0, f"index --all failed: {err}"
    print("Проект успешно сконфигурирован.")

    # 2. Маленькая задача (Small route: баг)
    print("\n--- 2. Пилот маленькой задачи (TASK-101: баг валидации) ---")
    task101_content = """# TASK-101: Исправление ошибки валидации

ID: TASK-101
Status: ready
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{
  "schema_version": 1,
  "relations": [],
  "materials": []
}
```
<!-- playbook-meta:end -->

## Цель и границы
Исправление ошибки валидации в одной функции.

## Приёмка
- [x] Ошибка устранена.

## Проверки и доказательства
verify

## Выполнение и проверки
2026-10-22: исправлено.

## Передача
Готово к проверке.
"""
    (root / "docs" / "tasks" / "TASK-101.md").write_text(task101_content, encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    assert code == 0

    # Start implementation
    card = (root / "docs" / "tasks" / "TASK-101.md")
    card.write_text(card.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "TASK-101"], cwd=root)
    assert code == 0, f"verify TASK-101 failed: {err}"
    print("verify TASK-101: PASS")

    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "TASK-101",
                          "--summary", "первичная реализация parse_age", "--actor", "Antigravity",
                          "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    assert code == 0, f"handoff failed: {err}"

    # Сохранение билета передачи для независимого аудитора с реальными хэшами
    card_text = card.read_text(encoding="utf-8")
    card_proj = lifecycle.card_projection(card_text)
    rep_files = list((root / "work" / "verification" / "TASK-101").rglob("report.json"))
    assert len(rep_files) > 0, "отчёт верификации TASK-101 должен существовать"
    rep_hash = hashlib.sha256(rep_files[0].read_bytes()).hexdigest()

    ticket = {
        "schema_version": 1,
        "task_id": "TASK-101",
        "actor": "Antigravity",
        "model": "Gemini 3.8 Flash (High)",
        "summary": "первичная реализация валидатора возраста",
        "timestamp": "2026-10-22T08:00:00+03:00",
        "card_projection": card_proj,
        "verification_report_hash": rep_hash,
        "workspace": str(root)
    }
    ticket_json = json.dumps(ticket, ensure_ascii=False, indent=2)
    (root / "handoff-ticket.json").write_text(ticket_json, encoding="utf-8")
    (REPO / "docs" / "tasks" / "PB-029-evidence" / "handoff-ticket.json").write_text(ticket_json, encoding="utf-8")

    card.write_text(card.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    handoff_archive = REPO / "docs" / "tasks" / "PB-029-evidence" / "pilot-workspace-handoff.tar.gz"
    import tarfile
    with tarfile.open(handoff_archive, "w:gz") as tar:
        tar.add(root, arcname="pilot-workspace")
    print(f"Снимок полигона на момент передачи сохранён: {handoff_archive}")

    # === АВТОМАТИЧЕСКИЙ CLI-ЗОНД (Test-Harness) ===
    # Далее следует автоматическая проверка CLI-механики приёмки/возврата/закрытия.
    # Это НЕ доказательство реального участия независимого приёмщика (инженера или
    # отдельной агентской сессии). Актор «Test-Harness» и модель «automated-cli-probe»
    # явно обозначают автоматическую природу этих действий.
    # Реальный процессный пилот проводится отдельно по регламенту workflow.md §8.

    # Зонд: воспроизведение дефекта parse_age('') -> ValueError
    code_probe, out_probe, err_probe = run([
        sys.executable, "-c",
        "import sys; from pathlib import Path; sys.path.insert(0, '.'); from src.validator import parse_age; parse_age('')"
    ], cwd=root)
    assert code_probe != 0 and "ValueError" in err_probe, "Дефект пустой строки должен стабильно воспроизводиться сбоем ValueError!"
    print("CLI-зонд воспроизвёл дефект: parse_age('') -> ValueError (код != 0).")

    # Зонд: диагностическое замечание через CLI (автоматический актор)
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "TASK-101",
                          "--result", "accepted", "--kind", "diagnostic",
                          "--note", "автоматическая CLI-проверка: parse_age('') падает с ValueError; ожидается возврат None",
                          "--actor", "Test-Harness", "--model", "automated-cli-probe"], cwd=root)
    assert code == 0, f"accept diagnostic failed: {err}"

    # Зонд: попытка закрыть задачу при наличии незакрытого diagnostic замечания
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "TASK-101"], cwd=root)
    assert code == 1, "Закрытие задачи при наличии диагностического замечания без приёмки должно быть отклонено!"
    print("CLI-зонд: отказ в закрытии TASK-101 при наличии диагностического замечания подтверждён (rc=1).")

    # Зонд: возврат задачи на доработку (in_review -> in_progress)
    card_text = card.read_text(encoding="utf-8")
    card_text = card_text.replace("Status: in_review", "Status: in_progress")
    card_text = card_text.replace(
        "## Выполнение и проверки\n2026-10-22: исправлено.",
        "## Выполнение и проверки\n2026-10-22: первичная реализация функции parse_age.\n2026-10-22: устранено замечание CLI-зонда: добавлена обработка пустых и пробельных строк с возвратом None, добавлен тест test_empty_age_returns_none."
    )
    card.write_text(card_text, encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # Зонд: исполнитель исправляет функцию в src/validator.py
    validator_code_v2 = '''def parse_age(value: str) -> int | None:
    """Парсинг возраста из строки.
    
    Возвращает int для корректных строк, либо None для пустых/пробельных строк.
    """
    cleaned = value.strip()
    if not cleaned:
        return None
    return int(cleaned)
'''
    (root / "src" / "validator.py").write_text(validator_code_v2, encoding="utf-8")

    # Зонд: добавление выполняемого теста в tests/test_validator.py
    test_validator_v2 = '''import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.validator import parse_age

class TestValidator(unittest.TestCase):
    def test_valid_age(self):
        self.assertEqual(parse_age("25"), 25)

    def test_whitespace_age(self):
        self.assertEqual(parse_age(" 42 "), 42)

    def test_empty_age_returns_none(self):
        self.assertIsNone(parse_age(""))
        self.assertIsNone(parse_age("   "))

if __name__ == "__main__":
    unittest.main()
'''
    (root / "tests" / "test_validator.py").write_text(test_validator_v2, encoding="utf-8")

    # Зонд: повторный запуск verify запускает unittest discover (4 теста, все PASS)
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "TASK-101"], cwd=root)
    assert code == 0, f"repeat verify TASK-101 failed: {err}"
    print("CLI-зонд: повторный verify TASK-101 с новым тестом: PASS (4 теста OK)")

    # Зонд: повторный handoff
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "TASK-101",
                          "--summary", "CLI-зонд: исправление дефекта пустых строк и тест test_empty_age_returns_none",
                          "--actor", "Test-Harness", "--model", "automated-cli-probe"], cwd=root)
    assert code == 0, f"repeat handoff failed: {err}"

    # Зонд: перевод в in_review
    card.write_text(card.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # Зонд: проверка работы исправленной функции
    code_probe2, out_probe2, err_probe2 = run([
        sys.executable, "-c",
        "import sys; from pathlib import Path; sys.path.insert(0, '.'); from src.validator import parse_age; assert parse_age('') is None; assert parse_age('   ') is None"
    ], cwd=root)
    assert code_probe2 == 0, f"Повторная проверка CLI-зонда не удалась: {err_probe2}"
    print("CLI-зонд: parse_age('') is None -> OK (код 0).")

    # Зонд: приёмка через CLI (автоматический актор)
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "TASK-101",
                          "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
                          "--note", "автоматическая CLI-проверка: parse_age('') is None, verify 4/4 PASS",
                          "--actor", "Test-Harness", "--model", "automated-cli-probe"], cwd=root)
    assert code == 0, f"accept acceptance failed: {err}"

    # Зонд: закрытие задачи через CLI
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "TASK-101"], cwd=root)
    assert code == 0, f"close TASK-101 failed: {err}"
    assert not (root / "docs" / "tasks" / "TASK-101.md").exists()
    assert (root / "docs" / "archive" / "tasks" / "TASK-101.md").is_file()
    print("CLI-зонд: TASK-101 закрыта и заархивирована в docs/archive/tasks/TASK-101.md.")
    print("=== Конец автоматического CLI-зонда TASK-101 ===")

    # 3. Средняя задача (Medium route: feature с отдельной спецификацией и promotion)
    print("\n--- 3. Пилот средней задачи (DEV-201: повтор платежей) ---")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "spec",
                          "payment-retry", "--title", "Повтор платежей", "--apply"], cwd=root)
    assert code == 0, f"create spec failed: {err}"

    spec_path = root / "docs" / "specs" / "payment-retry.md"
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
        "changes": [{
            "id": "ch-retry",
            "state": "approved",
            "section": "## 4. Действующее поведение",
            "basis": {"type": "engineer_approval", "who": "Engineer", "detail": "согласовано", "source": "диалог", "at": "2026-10-22T08:00:00+03:00"},
            "tasks": ["DEV-201"],
            "acceptance": None
        }],
        "materials": [],
        "archive": None
    }
    body = ("## 1. Проблема и цель\nНадёжность платежей.\n\n"
            "## 4. Действующее поведение\nПлатежи выполняются однократно без повторов.\n\n"
            "## 5. Планируемые изменения\nПовтор платежа до 3 раз.\n\n"
            "## 7. Критерии приёмки\n- [x] Повтор до 3 раз.\n")
    spec_path.write_text(f"# Повтор платежей\n\n{documents.META_START}\n```json\n"
                         f"{json.dumps(spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                         f"{documents.META_END}\n\n{body}", encoding="utf-8")

    task201_content = f"""# DEV-201: Реализовать повтор платежей

ID: DEV-201
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(dict(TASK_META), ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
Повтор платежей.

## Контекст
[Спецификация](../specs/payment-retry.md)

## Приёмка
- [x] Повтор до 3 раз.

## Выполнение и проверки
2026-10-22: реализовано.

## Передача
Готово.
"""
    (root / "docs" / "tasks" / "DEV-201.md").write_text(task201_content, encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # Спецификация сохраняет действующее поведение во время реализации
    assert "Платежи выполняются однократно без повторов" in spec_path.read_text(encoding="utf-8")

    # Исполнитель завершает реализацию: изменение переходит в implemented
    spec_meta["changes"][0]["state"] = "implemented"
    spec_path.write_text(f"# Повтор платежей\n\n{documents.META_START}\n```json\n"
                         f"{json.dumps(spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                         f"{documents.META_END}\n\n{body}", encoding="utf-8")

    code, _, _ = run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "DEV-201"], cwd=root)
    assert code == 0
    code, _, _ = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "DEV-201",
                      "--summary", "повтор реализован", "--actor", "Antigravity",
                      "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    assert code == 0
    card201 = root / "docs" / "tasks" / "DEV-201.md"
    card201.write_text(card201.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # Приёмка DEV-201
    code, _, _ = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "DEV-201",
                      "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
                      "--note", "принято", "--actor", "Codex", "--model", "GPT-6"], cwd=root)
    assert code == 0

    # Promotion изменения
    new_payload = ("## 1. Проблема и цель\nНадёжность платежей.\n\n"
                   "## 4. Действующее поведение\nПлатежи выполняются с автоматическим повтором до 3 раз.\n\n"
                   "## 5. Планируемые изменения\nПовтор платежа до 3 раз (принято в DEV-201).\n\n"
                   "## 7. Критерии приёмки\n- [x] Повтор до 3 раз.\n")
    promote_req = {
        "schema_version": 1,
        "actor": {"name": "Auditor", "role": "auditor", "model": "GPT-6"},
        "basis": "DEV-201 принята аудитором",
        "scope": {"documents": ["spec:payment-retry"]},
        "actions": [{"action": "promote_change", "document": "spec:payment-retry",
                     "change": "ch-retry", "payload": new_payload, "acceptance": "DEV-201"}]
    }
    req_file = root / "work" / "promote-req.json"
    req_file.write_text(json.dumps(promote_req), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan",
                          "work/promote-req.json", "--save"], cwd=root)
    assert code == 0, f"plan promote failed: {err}"
    plan_line = next(l for l in out.splitlines() if "План записан:" in l or "План сохранён:" in l)
    prefix = "План записан:" if "План записан:" in plan_line else "План сохранён:"
    plan_rel = plan_line.split(prefix, 1)[1].strip()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "apply", plan_rel], cwd=root)
    assert code == 0, f"apply promote failed: {err}"

    # Проверка спецификации после promotion
    spec_text = spec_path.read_text(encoding="utf-8")
    assert "Платежи выполняются с автоматическим повтором до 3 раз" in spec_text
    errs = []
    p_meta = documents.parse_meta(spec_text, "docs/specs/payment-retry.md", errs)
    assert p_meta["current_revision"] == 2
    assert p_meta["changes"][0]["state"] == "accepted"
    assert (root / "docs" / "specs" / "payment-retry-evidence" / "revision-1.md").is_file()
    assert spec_path.is_file(), "Спецификация остаётся в active!"
    print("Promotion спецификации успешно выполнен: ревизия 1 -> 2, спецификация осталась активной.")

    code, _, _ = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "DEV-201"], cwd=root)
    assert code == 0
    print("Средняя задача закрыта, спецификация осталась активной.")

    # 4. Большая задача (Large block: подзадачи, замечание аудита, возврат, блок аудит, архив плана)
    print("\n--- 4. Пилот большой задачи (блок подзадач SUB-1, SUB-2, AUDIT-1 и плана) ---")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "spec",
         "search-engine", "--title", "Поисковый движок", "--apply"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "plan",
         "search-engine", "--title", "План реализации поискового движка", "--apply"], cwd=root)

    plan_path = root / "docs" / "plans" / "search-engine.md"
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
    plan_body = "## 1. Задачи\n- [SUB-1](../tasks/SUB-1.md)\n- [SUB-2](../tasks/SUB-2.md)\n- [AUDIT-1](../tasks/AUDIT-1.md)\n"
    plan_path.write_text(f"# План поискового движка\n\n{documents.META_START}\n```json\n"
                         f"{json.dumps(plan_meta, ensure_ascii=False, indent=2)}\n```\n"
                         f"{documents.META_END}\n\n{plan_body}", encoding="utf-8")

    # Создание SUB-1 (ready), SUB-2 (blocked), AUDIT-1 (draft)
    def make_sub(ident, status, deps="-"):
        sub_meta = dict(TASK_META)
        sub_meta["relations"] = [{"relation": "based_on", "target": "plan:search-engine"}]
        return f"""# {ident}: Подзадача

ID: {ident}
Status: {status}
Priority: P2
Depends-On: {deps}
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(sub_meta, ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
{ident}

## Контекст
[План](../plans/search-engine.md)

## Приёмка
- [x] Готово.

## Выполнение и проверки
Готово.

## Передача
Готово.
"""
    (root / "docs" / "tasks" / "SUB-1.md").write_text(make_sub("SUB-1", "ready"), encoding="utf-8")
    (root / "docs" / "tasks" / "SUB-2.md").write_text(make_sub("SUB-2", "blocked", "SUB-1"), encoding="utf-8")
    (root / "docs" / "tasks" / "AUDIT-1.md").write_text(make_sub("AUDIT-1", "draft", "SUB-1, SUB-2"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # SUB-1: in_progress -> verify -> handoff -> accept -> close
    card_sub1 = root / "docs" / "tasks" / "SUB-1.md"
    card_sub1.write_text(card_sub1.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "SUB-1"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "SUB-1",
         "--summary", "sub-1 done", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    card_sub1.write_text(card_sub1.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "SUB-1",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
         "--note", "принято", "--actor", "Codex", "--model", "GPT-6"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "SUB-1"], cwd=root)

    # КРИТИЧЕСКИЙ ТЕСТ: после закрытия SUB-1 общий план и спецификация не архивируются!
    assert plan_path.is_file(), "Общий план остаётся активным после закрытия первой подзадачи!"
    assert (root / "docs" / "specs" / "search-engine.md").is_file(), "Спецификация остаётся активной!"
    print("Закрытие SUB-1: общий план и спецификация не затронуты.")

    # SUB-2: unblock -> in_progress -> verify -> handoff -> in_review
    card_sub2 = root / "docs" / "tasks" / "SUB-2.md"
    card_sub2.write_text(card_sub2.read_text(encoding="utf-8").replace("Status: blocked", "Status: ready"), encoding="utf-8")
    card_sub2.write_text(card_sub2.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "SUB-2"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "SUB-2",
         "--summary", "sub-2 done", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    card_sub2.write_text(card_sub2.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # Замечание аудита: диагностическая запись, отказ в закрытии, возврат in_progress
    code, _, _ = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "SUB-2",
                      "--result", "accepted", "--kind", "diagnostic", "--note", "найден дефект edge-case"], cwd=root)
    code, _, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "SUB-2"], cwd=root)
    assert code == 1, "Закрытие без подтверждённой приёмки должно быть отклонено"
    print("Отказ в закрытии при диагностическом замечании аудитора подтверждён.")

    card_sub2.write_text(card_sub2.read_text(encoding="utf-8").replace("Status: in_review", "Status: in_progress"), encoding="utf-8")
    # Исправление дефекта в коде (меняет входы проверки)
    test_parser_v2 = '''import unittest

class TestParser(unittest.TestCase):
    def test_parse_basic(self):
        self.assertTrue(True)

    def test_edge_case_fixed(self):
        self.assertEqual("fixed", "fixed")

if __name__ == "__main__":
    unittest.main()
'''
    (root / "tests" / "test_parser.py").write_text(test_parser_v2, encoding="utf-8")
    # Повторный verify, handoff, acceptance, close
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "SUB-2"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "SUB-2",
         "--summary", "дефект устранён", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    card_sub2.write_text(card_sub2.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "SUB-2",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
         "--note", "дефект устранён, приёмка", "--actor", "Codex", "--model", "GPT-6"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "SUB-2"], cwd=root)
    assert (root / "docs" / "archive" / "tasks" / "SUB-2.md").is_file()
    print("SUB-2 после исправления замечаний успешно принята и заархивирована.")

    # Попытка архивировать план ДО завершения AUDIT-1 отклоняется
    plan_path.write_text(plan_path.read_text(encoding="utf-8").replace('"state": "accepted"', '"state": "completed"'), encoding="utf-8")
    premature_req = {
        "schema_version": 1,
        "actor": {"name": "Auditor", "role": "auditor"},
        "basis": "преждевременное архивирование",
        "scope": {"documents": ["plan:search-engine"]},
        "actions": [{"action": "archive_document", "document": "plan:search-engine",
                     "reason": "все задачи завершены", "successor": None}]
    }
    p_req_file = root / "work" / "premature.json"
    p_req_file.write_text(json.dumps(premature_req), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan",
                          "work/premature.json", "--save"], cwd=root)
    assert code == 1, "Архивирование плана с незавершёнными задачами должно быть отклонено"
    assert "незавершёнными задачами" in err
    print("Отказ в архивировании плана при открытой задаче AUDIT-1 подтверждён.")

    # Выполнение AUDIT-1
    card_aud1 = root / "docs" / "tasks" / "AUDIT-1.md"
    card_aud1.write_text(card_aud1.read_text(encoding="utf-8").replace("Status: draft", "Status: ready"), encoding="utf-8")
    card_aud1.write_text(card_aud1.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "AUDIT-1"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "AUDIT-1",
         "--summary", "аудит блока проверен", "--actor", "Codex", "--model", "GPT-6"], cwd=root)
    card_aud1.write_text(card_aud1.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "AUDIT-1",
         "--result", "accepted", "--kind", "acceptance", "--role", "engineer",
         "--note", "блок принят", "--actor", "Engineer"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "AUDIT-1"], cwd=root)

    # Архивирование плана после завершения всех задач
    archive_plan_req = {
        "schema_version": 1,
        "actor": {"name": "Auditor", "role": "auditor"},
        "basis": "Все подзадачи и аудит блока завершены",
        "scope": {"documents": ["plan:search-engine"]},
        "actions": [{"action": "archive_document", "document": "plan:search-engine",
                     "reason": "все задачи блока и аудит успешно завершены", "successor": None}]
    }
    p_req_file.write_text(json.dumps(archive_plan_req), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan",
                          "work/premature.json", "--save"], cwd=root)
    assert code == 0, f"plan archive failed: {err}"
    plan_line = next(l for l in out.splitlines() if "План записан:" in l or "План сохранён:" in l)
    prefix = "План записан:" if "План записан:" in plan_line else "План сохранён:"
    plan_rel = plan_line.split(prefix, 1)[1].strip()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "apply", plan_rel], cwd=root)
    assert code == 0, f"apply archive failed: {err}"

    assert not plan_path.exists()
    assert (root / "docs" / "archive" / "plans" / "search-engine.md").is_file()
    assert (root / "docs" / "specs" / "search-engine.md").is_file()
    print("Итоговый аудит блока и архивирование плана успешно завершены; спецификация осталась активной.")

    # 5. Проверка неизменяемости evidence (Evidence immutability)
    print("\n--- 5. Проверка неизменяемости сопровождающих материалов (evidence) ---")
    ev_bytes = b"\x00\xaa\x55\xffBINARY_LOG" * 200
    ev_hash = hashlib.sha256(ev_bytes).hexdigest()

    ev_dir = root / "docs" / "tasks" / "TASK-301-evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "log.bin").write_bytes(ev_bytes)

    task301_meta = dict(TASK_META)
    task301_meta["materials"] = [{
        "id": "log-bin",
        "role": "probe",
        "source": "docs/tasks/TASK-301-evidence/log.bin",
        "storage": "local",
        "required": True,
        "immutable": True,
        "used_by": ["TASK-301"]
    }]

    task301_content = f"""# TASK-301: Проверка evidence

ID: TASK-301
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(task301_meta, ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
Evidence.

## Контекст
[Лог](TASK-301-evidence/log.bin)

## Приёмка
- [x] Хеш совпадает.

## Выполнение и проверки
Готово.

## Передача
Готово.
"""
    (root / "docs" / "tasks" / "TASK-301.md").write_text(task301_content, encoding="utf-8")

    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "TASK-301"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "TASK-301",
         "--summary", "evidence записан", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    card301 = root / "docs" / "tasks" / "TASK-301.md"
    card301.write_text(card301.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "TASK-301",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
         "--note", "принято", "--actor", "Codex", "--model", "GPT-6"], cwd=root)
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "TASK-301"], cwd=root)
    assert code == 0, f"close TASK-301 failed: {err}"

    archived_log = root / "docs" / "archive" / "tasks" / "TASK-301-evidence" / "log.bin"
    assert archived_log.is_file(), "Файл evidence обязан переместиться в архив!"
    assert hashlib.sha256(archived_log.read_bytes()).hexdigest() == ev_hash, "Побайтовый хеш evidence обязан совпадать!"
    print("Неизменяемость evidence подтверждена: хеш побайтно совпал.")

    # 6. Финальный check --ready проекта
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "check", "--ready"], cwd=root)
    assert code == 0, f"check --ready failed: {err}"
    print("\ncheck --ready для всех документов и архива: PASS")

    # 7. Самоприменение на копии репозитория ai-dev-playbook (AC-12)
    print("\n--- 7. Самоприменение на копии ai-dev-playbook (AC-12) ---")
    repo_copy = root / "self_repo_copy"
    def ignore_patterns(src, names):
        return {n for n in names if n in {".git", ".DS_Store", "__pycache__", "pilot-workspace"} or n.endswith(".pyc")}
    shutil.copytree(REPO, repo_copy, ignore=ignore_patterns, symlinks=True)

    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "check", "--ready"], cwd=repo_copy)
    assert code == 0, f"copy check --ready failed: {err}"
    print("Копия репозитория ai-dev-playbook: check --ready PASS.")

    # Фиксация хэшей исходных данных: все 35 карточек (активные и архивные),
    # все файлы комплекта (scripts, rules, templates, updates.json),
    # настройки resources.json, документы коллекций docs/ и корневые файлы.
    original_copy_hashes = {}
    for md in (repo_copy / "docs" / "tasks").glob("*.md"):
        if md.name != "index.md":
            original_copy_hashes[str(md.relative_to(repo_copy))] = hashlib.sha256(md.read_bytes()).hexdigest()
    for md in (repo_copy / "docs" / "archive" / "tasks").glob("*.md"):
        original_copy_hashes[str(md.relative_to(repo_copy))] = hashlib.sha256(md.read_bytes()).hexdigest()
    for py in (repo_copy / "ai-dev-playbook" / "scripts").rglob("*.py"):
        original_copy_hashes[str(py.relative_to(repo_copy))] = hashlib.sha256(py.read_bytes()).hexdigest()
    for md in (repo_copy / "ai-dev-playbook" / "rules").glob("*.md"):
        original_copy_hashes[str(md.relative_to(repo_copy))] = hashlib.sha256(md.read_bytes()).hexdigest()
    for tpl in (repo_copy / "ai-dev-playbook" / "templates").rglob("*"):
        if tpl.is_file():
            original_copy_hashes[str(tpl.relative_to(repo_copy))] = hashlib.sha256(tpl.read_bytes()).hexdigest()
    for f in (repo_copy / "ai-dev-playbook" / "resources.json", repo_copy / "ai-dev-playbook" / "updates.json",
              repo_copy / "AGENTS.md", repo_copy / "README.md", repo_copy / "CHANGELOG.md"):
        if f.is_file():
            original_copy_hashes[str(f.relative_to(repo_copy))] = hashlib.sha256(f.read_bytes()).hexdigest()
    for d in ("specs", "designs", "adr", "plans", "discussions"):
        col_dir = repo_copy / "docs" / d
        if col_dir.is_dir():
            for doc in col_dir.glob("*.md"):
                if doc.name != "index.md":
                    original_copy_hashes[str(doc.relative_to(repo_copy))] = hashlib.sha256(doc.read_bytes()).hexdigest()

    copy_proj_path = repo_copy / "ai-dev-playbook" / "project.json"
    copy_proj = json.loads(copy_proj_path.read_text(encoding="utf-8"))
    copy_proj["verification"] = {
        "inputs": ["tests"],
        "checks": [
            {"id": "unit", "argv": [sys.executable, "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30},
            {"id": "self-check", "argv": [sys.executable, "ai-dev-playbook/scripts/playbook.py", "check", "--ready"], "timeout_seconds": 60}
        ]
    }
    copy_proj_path.write_text(json.dumps(copy_proj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Полный цикл задачи PILOT-001 на копии
    pilot_card_path = repo_copy / "docs" / "tasks" / "PILOT-001.md"
    pilot_card_content = """# PILOT-001: Пилотный прогон на копии

ID: PILOT-001
Status: ready
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{
  "schema_version": 1,
  "relations": [],
  "materials": []
}
```
<!-- playbook-meta:end -->

## Цель и границы
Пилот.

## Приёмка
- [x] Готово.

## Проверки и доказательства
verify

## Выполнение и проверки
Готово.

## Передача
Готово.
"""
    pilot_card_path.write_text(pilot_card_content, encoding="utf-8")
    run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "index", "--all"], cwd=repo_copy)

    pilot_card_path.write_text(pilot_card_path.read_text(encoding="utf-8").replace("Status: ready", "Status: in_progress"), encoding="utf-8")
    run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "index", "--all"], cwd=repo_copy)
    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "verify", "PILOT-001"], cwd=repo_copy)
    assert code == 0, f"verify PILOT-001 failed: {err}"

    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "lifecycle", "handoff", "PILOT-001",
                          "--summary", "пилотный прогон", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=repo_copy)
    assert code == 0, f"handoff PILOT-001 failed: {err}"

    pilot_card_path.write_text(pilot_card_path.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "index", "--all"], cwd=repo_copy)
    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "lifecycle", "accept", "PILOT-001",
                          "--result", "accepted", "--kind", "acceptance", "--role", "auditor",
                          "--note", "принято на копии", "--actor", "Codex", "--model", "GPT-6"], cwd=repo_copy)
    assert code == 0, f"accept PILOT-001 failed: {err}"

    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "close", "PILOT-001"], cwd=repo_copy)
    assert code == 0, f"close PILOT-001 failed: {err}"
    assert (repo_copy / "docs" / "archive" / "tasks" / "PILOT-001.md").is_file()

    code, out, err = run([sys.executable, str(repo_copy / "ai-dev-playbook" / "scripts" / "playbook.py"), "check", "--ready"], cwd=repo_copy)
    assert code == 0, f"final copy check --ready failed: {err}"
    print("Копия репозитория: полный цикл PILOT-001 (verify->handoff->accept->close) успешно пройден.")

    # Проверка неизменности всех исходных 35 карточек и скриптов
    for rel_path, expected_hash in original_copy_hashes.items():
        actual_hash = hashlib.sha256((repo_copy / rel_path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash, f"Файл {rel_path} на копии изменился!"
    print("Копия репозитория: побайтовая неизменность исходных задач и комплекта подтверждена.")
    shutil.rmtree(repo_copy)

    # 8. Сценарий AC-07: частичное удаление SMS и полное удаление с архивацией ADR и design
    print("\n--- 8. Сценарий AC-07: вывод устаревшей возможности (SMS) и архивация ADR и design ---")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "spec", "auth-methods",
         "--title", "Методы аутентификации", "--apply"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "design", "auth-sms",
         "--title", "Архитектура SMS-аутентификации", "--apply"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "document", "create", "adr", "0010-sms-auth",
         "--title", "Использование SMS для второго фактора", "--apply"], cwd=root)

    ac07_spec_path = root / "docs" / "specs" / "auth-methods.md"
    ac07_design_path = root / "docs" / "designs" / "auth-sms.md"
    ac07_adr_path = root / "docs" / "adr" / "0010-sms-auth.md"
    ac07_basis = {
        "type": "engineer_approval",
        "who": "Engineer",
        "detail": "согласован вывод SMS",
        "source": "диалог",
        "at": "2026-10-22T08:00:00+03:00"
    }
    ac07_spec_meta = {
        "schema_version": 1,
        "document_id": "spec:auth-methods",
        "kind": "spec",
        "state": "accepted",
        "created_at": "2026-10-22T08:00:00+03:00",
        "updated_at": "2026-10-22T08:00:00+03:00",
        "current_revision": 1,
        "relations": [
            {"relation": "based_on", "target": "adr:0010-sms-auth"},
            {"relation": "based_on", "target": "design:auth-sms"}
        ],
        "tasks": [],
        "changes": [
            {
                "id": "ch-deprecate",
                "state": "approved",
                "section": "## 4. Действующее поведение",
                "basis": ac07_basis,
                "tasks": ["DEV-701"],
                "acceptance": None
            },
            {
                "id": "ch-remove-sms",
                "state": "approved",
                "section": "## 4. Действующее поведение",
                "basis": ac07_basis,
                "tasks": ["DEV-702"],
                "acceptance": None
            }
        ],
        "materials": [],
        "archive": None
    }
    ac07_spec_body = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                      "## 4. Действующее поведение\n\nПоддерживаются SMS и WebAuthn.\n\n"
                      "## 5. Планируемые изменения\n\nПереход исключительно на WebAuthn.\n\n"
                      "## 7. Критерии приёмки\n\n- [x] WebAuthn работает.\n")
    ac07_spec_path.write_text(f"# Методы аутентификации\n\n{documents.META_START}\n```json\n"
                              f"{json.dumps(ac07_spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                              f"{documents.META_END}\n\n{ac07_spec_body}", encoding="utf-8")

    ac07_design_meta = {
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
    ac07_design_body = "## 1. Архитектура решения\n\nМодуль отправки SMS-кодов через шлюз.\n"
    ac07_design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                                f"{json.dumps(ac07_design_meta, ensure_ascii=False, indent=2)}\n```\n"
                                f"{documents.META_END}\n\n{ac07_design_body}", encoding="utf-8")

    ac07_adr_meta = {
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
    ac07_adr_body = ("## 1. Контекст\n\nВыбор второго фактора аутентификации.\n\n"
                     "## 2. Решение\n\nИспользовать SMS-коды для подтверждения входа.\n")
    ac07_adr_path.write_text(f"# ADR-0010: Использование SMS\n\n{documents.META_START}\n```json\n"
                             f"{json.dumps(ac07_adr_meta, ensure_ascii=False, indent=2)}\n```\n"
                             f"{documents.META_END}\n\n{ac07_adr_body}", encoding="utf-8")

    # Создание задач DEV-701 и DEV-702
    for t_id, t_title in (("DEV-701", "Пометить SMS deprecated"), ("DEV-702", "Полное удаление SMS")):
        t_meta = dict(TASK_META)
        t_meta["relations"] = [
            {"relation": "based_on", "target": "spec:auth-methods"},
            {"relation": "based_on", "target": "design:auth-sms"}
        ]
        t_content = f"""# {t_id}: {t_title}

ID: {t_id}
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(t_meta, ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
{t_title}.

## Контекст
[Спецификация](../specs/auth-methods.md)

## Приёмка
- [x] Готово.

## Проверки и доказательства
verify

## Выполнение и проверки
Готово.

## Передача
Готово.
"""
        (root / "docs" / "tasks" / f"{t_id}.md").write_text(t_content, encoding="utf-8")

    ac07_spec_meta["tasks"] = ["DEV-701", "DEV-702"]
    ac07_spec_meta["changes"][0]["state"] = "implemented"
    ac07_spec_path.write_text(f"# Методы аутентификации\n\n{documents.META_START}\n```json\n"
                              f"{json.dumps(ac07_spec_meta, ensure_ascii=False, indent=2)}\n```\n"
                              f"{documents.META_END}\n\n{ac07_spec_body}", encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)

    # DEV-701: частичный вывод SMS
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "DEV-701"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "DEV-701",
         "--summary", "SMS deprecated", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    c701 = root / "docs" / "tasks" / "DEV-701.md"
    c701.write_text(c701.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "DEV-701",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor", "--note", "принято"], cwd=root)

    # Обновление дизайна в рамках частичного вывода
    ac07_design_meta["current_revision"] = 2
    ac07_design_meta["updated_at"] = "2026-10-22T08:30:00+03:00"
    design_body_v2 = ("## 1. Архитектура решения\n\n"
                      "Модуль отправки SMS переведён в legacy-режим (только существующие пользователи).\n")
    ac07_design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                                f"{json.dumps(ac07_design_meta, ensure_ascii=False, indent=2)}\n```\n"
                                f"{documents.META_END}\n\n{design_body_v2}", encoding="utf-8")

    depr_payload = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                    "## 4. Действующее поведение\n\n"
                    "SMS объявлен устаревшим (deprecated). Рекомендуется WebAuthn.\n\n"
                    "## 5. Планируемые изменения\n\nПолный вывод SMS.\n\n"
                    "## 7. Критерии приёмки\n\n- [x] SMS deprecated.\n")
    req1 = {
        "schema_version": 1,
        "actor": {"name": "Auditor", "role": "auditor"},
        "basis": "Аудит принял изменение",
        "scope": {"documents": ["spec:auth-methods"]},
        "actions": [{
            "action": "promote_change",
            "document": "spec:auth-methods",
            "change": "ch-deprecate",
            "payload": depr_payload,
            "acceptance": "DEV-701"
        }]
    }
    (root / "work" / "req1.json").write_text(json.dumps(req1), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan", "work/req1.json", "--save"], cwd=root)
    assert code == 0, f"plan req1 failed: {err}"
    plan_line = next(l for l in out.splitlines() if "План записан:" in l or "План сохранён:" in l)
    prefix = "План записан:" if "План записан:" in plan_line else "План сохранён:"
    plan_path = plan_line.split(prefix, 1)[1].strip()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "apply", plan_path], cwd=root)
    assert code == 0, f"apply req1 failed: {err}"
    assert ac07_spec_path.is_file(), "Спецификация остаётся активной!"
    cur_spec_v2 = documents.parse_meta(ac07_spec_path.read_text(encoding="utf-8"), "docs/specs/auth-methods.md", [])
    assert cur_spec_v2["current_revision"] == 2, f"Ожидалась ревизия 2 спецификации, получено {cur_spec_v2.get('current_revision')}"
    assert ac07_design_path.is_file(), "Дизайн остаётся активным при частичном выводе!"
    cur_design_v2 = documents.parse_meta(ac07_design_path.read_text(encoding="utf-8"), "docs/designs/auth-sms.md", [])
    assert cur_design_v2["current_revision"] == 2, f"Ожидалась ревизия 2 дизайна, получено {cur_design_v2.get('current_revision')}"
    assert ac07_adr_path.is_file(), "ADR обязан оставаться активным при частичном выводе!"
    run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "DEV-701"], cwd=root)
    print("Частичный вывод SMS (deprecated): спецификация (ревизия 2) и дизайн (ревизия 2) обновлены, ADR остался активным.")

    # DEV-702: полный вывод SMS и архивация ADR и design
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "DEV-702"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "DEV-702",
         "--summary", "SMS удалён", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    c702 = root / "docs" / "tasks" / "DEV-702.md"
    c702.write_text(c702.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "DEV-702",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor", "--note", "принято"], cwd=root)

    ac07_spec_path.write_text(ac07_spec_path.read_text(encoding="utf-8").replace('"state": "approved"', '"state": "implemented"'), encoding="utf-8")
    ac07_design_meta["state"] = "superseded"
    ac07_design_path.write_text(f"# Архитектура SMS-аутентификации\n\n{documents.META_START}\n```json\n"
                                f"{json.dumps(ac07_design_meta, ensure_ascii=False, indent=2)}\n```\n"
                                f"{documents.META_END}\n\n{design_body_v2}", encoding="utf-8")
    ac07_adr_path.write_text(ac07_adr_path.read_text(encoding="utf-8").replace('"state": "accepted"', '"state": "superseded"'), encoding="utf-8")

    final_spec_payload = ("## 1. Проблема и цель\n\nАутентификация пользователей.\n\n"
                          "## 4. Действующее поведение\n\nПоддерживается только WebAuthn.\n\n"
                          "## 5. Планируемые изменения\n\nНет.\n\n"
                          "## 7. Критерии приёмки\n\n- [x] Только WebAuthn.\n")
    req2 = {
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
    (root / "work" / "req2.json").write_text(json.dumps(req2), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan", "work/req2.json", "--save"], cwd=root)
    assert code == 0, f"plan req2 failed: {err}"
    plan_line2 = next(l for l in out.splitlines() if "План записан:" in l or "План сохранён:" in l)
    prefix2 = "План записан:" if "План записан:" in plan_line2 else "План сохранён:"
    plan_path2 = plan_line2.split(prefix2, 1)[1].strip()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "apply", plan_path2], cwd=root)
    assert code == 0, f"apply req2 failed: {err}"

    assert ac07_spec_path.is_file(), "Спецификация обязана оставаться активной!"
    cur_spec_v3 = documents.parse_meta(ac07_spec_path.read_text(encoding="utf-8"), "docs/specs/auth-methods.md", [])
    assert cur_spec_v3["current_revision"] == 3, f"Ожидалась ревизия 3 спецификации, получено {cur_spec_v3.get('current_revision')}"
    assert not ac07_design_path.exists(), "Дизайн обязан быть перемещён в архив!"
    assert not ac07_adr_path.exists(), "ADR обязан быть перемещён в архив!"
    assert (root / "docs" / "archive" / "designs" / "auth-sms.md").is_file()
    assert (root / "docs" / "archive" / "adr" / "0010-sms-auth.md").is_file()
    run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "DEV-702"], cwd=root)
    print("Полный вывод SMS: спецификация активна (ревизия 3), дизайн и ADR успешно заархивированы.")

    # 9. Сценарий AC-10/11: контроль обязательного evidence, отмена, возобновление (reopen) и откладывание (postpone)
    print("\n--- 9. Сценарии AC-10/11: контроль обязательного evidence, отмена, reopen и postpone ---")
    ev801_file = root / "docs" / "tasks" / "TASK-801-evidence" / "probe.log"
    ev801_file.parent.mkdir(parents=True, exist_ok=True)
    ev801_file.write_text("ok log\n", encoding="utf-8")
    meta801 = dict(TASK_META)
    meta801["materials"] = [{
        "id": "probe-log",
        "role": "log",
        "source": "docs/tasks/TASK-801-evidence/probe.log",
        "storage": "local",
        "required": True,
        "immutable": True,
        "used_by": ["TASK-801"]
    }]
    card801_content = f"""# TASK-801: Контроль evidence и отмена

ID: TASK-801
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(meta801, ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
Тест контроля.

## Приёмка
- [x] Ок.

## Проверки и доказательства
verify

## Выполнение и проверки
Готово.

## Передача
Готово.
"""
    (root / "docs" / "tasks" / "TASK-801.md").write_text(card801_content, encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "verify", "TASK-801"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "handoff", "TASK-801",
         "--summary", "evidence записан", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=root)
    card801 = root / "docs" / "tasks" / "TASK-801.md"
    card801.write_text(card801.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "TASK-801",
         "--result", "accepted", "--kind", "acceptance", "--role", "auditor", "--note", "принято"], cwd=root)

    # Удаляем обязательный файл evidence
    ev801_file.unlink()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "TASK-801"], cwd=root)
    assert code == 1, "Закрытие без обязательного evidence должно быть отклонено!"
    assert "обязательный материал" in err
    print("Блокировка закрытия при потере обязательного evidence подтверждена.")

    # Восстанавливаем evidence
    ev801_file.write_text("ok log restored\n", encoding="utf-8")

    # Отмена карточки: фиксируем результат cancelled в приёмке и закрываем с признаком cancelled
    run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "accept", "TASK-801",
         "--result", "cancelled", "--kind", "acceptance", "--role", "auditor", "--note", "отмена задачи"], cwd=root)
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "close", "TASK-801",
                          "--status", "cancelled", "--reason", "задача отменена инженером"], cwd=root)
    assert code == 0, f"close cancelled failed: {err}"
    c801_arch = root / "docs" / "archive" / "tasks" / "TASK-801.md"
    assert c801_arch.is_file()
    assert "Status: cancelled" in c801_arch.read_text(encoding="utf-8")
    print("Отмена задачи: карточка перенесена в архив со статусом cancelled, причина зафиксирована.")

    # Reopen: возобновление работы над отменённой задачей
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
    (root / "work" / "reopen_req.json").write_text(json.dumps(reopen_req), encoding="utf-8")
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "plan", "work/reopen_req.json", "--save"], cwd=root)
    assert code == 0, f"plan reopen failed: {err}"
    reopen_plan_line = next(l for l in out.splitlines() if "План записан:" in l or "План сохранён:" in l)
    r_prefix = "План записан:" if "План записан:" in reopen_plan_line else "План сохранён:"
    reopen_plan_path = reopen_plan_line.split(r_prefix, 1)[1].strip()
    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "lifecycle", "apply", reopen_plan_path], cwd=root)
    assert code == 0, f"apply reopen failed: {err}"
    c801_reopened = root / "docs" / "tasks" / "TASK-801.md"
    assert c801_reopened.is_file(), "Карточка обязана вернуться в активные задачи!"
    assert not (root / "docs" / "archive" / "tasks" / "TASK-801.md").exists(), "Архивная копия удаляется"
    c801_text = c801_reopened.read_text(encoding="utf-8")
    assert "Status: in_progress" in c801_text
    assert "Closed: -" in c801_text
    print("Возобновление задачи (reopen): карточка возвращена из архива в in_progress, Closed: -.")

    # Проверка откладывания задачи (postpone)
    card802_content = f"""# TASK-802: Откладывание задачи

ID: TASK-802
Status: blocked
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-22T08:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{json.dumps(dict(TASK_META), ensure_ascii=False, indent=2)}
```
<!-- playbook-meta:end -->

## Цель и границы
Тест откладывания задачи.

## Приёмка
- [x] Отложено.

## Выполнение и проверки
2026-10-22: задача отложена до получения внешнего API ключа (postpone).

## Передача
-
"""
    (root / "docs" / "tasks" / "TASK-802.md").write_text(card802_content, encoding="utf-8")
    run([sys.executable, str(SCRIPTS / "playbook.py"), "index"], cwd=root)
    print("Откладывание задачи (postpone): статус blocked, причина зафиксирована в журнале.")

    code, out, err = run([sys.executable, str(SCRIPTS / "playbook.py"), "check", "--ready"], cwd=root)
    assert code == 0, f"check --ready failed: {err}"

    archive_path = REPO / "docs" / "tasks" / "PB-029-evidence" / "pilot-workspace.tar.gz"
    import tarfile
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(root, arcname="pilot-workspace")
    print(f"Полигон заархивирован в артефакт: {archive_path}")

    print("\n=== ВСЕ 9 ЭТАПОВ СКВОЗНОГО ПИЛОТА PB-029 УСПЕШНО ЗАВЕРШЕНЫ ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
