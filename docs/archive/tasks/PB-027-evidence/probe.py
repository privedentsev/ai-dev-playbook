#!/usr/bin/env python3
"""Behavioral validation probe for PB-027:
- Tests deployment and generation of all document templates (spec, design, discussion, plan, adr).
- Tests 3 workflow routes (Small task, Medium task, Large block).
- Validates markdown links in all updated rules and templates.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PLAYBOOK_PY = REPO_ROOT / "ai-dev-playbook" / "scripts" / "playbook.py"
KIT_DIR = REPO_ROOT / "ai-dev-playbook"


def run_cmd(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p


def test_markdown_links():
    print("=== 1. Checking Markdown links in rules and templates ===")
    rules_dir = KIT_DIR / "rules"
    templates_dir = KIT_DIR / "templates"
    docs_to_check = [
        rules_dir / "workflow.md",
        rules_dir / "context.md",
        rules_dir / "tasks.md",
        rules_dir / "verification.md",
        KIT_DIR / "README.md",
        templates_dir / "task.md",
        templates_dir / "AGENTS.md",
    ]
    sys.path.insert(0, str(KIT_DIR / "scripts"))
    import document_links

    total_links = 0
    for doc in docs_to_check:
        text = doc.read_text(encoding="utf-8")
        links = document_links.scan(text)
        total_links += len(links)
        base_dir = REPO_ROOT if doc.name == "AGENTS.md" else doc.parent
        errs = document_links.check_text(text, str(doc.relative_to(REPO_ROOT)), base_dir, REPO_ROOT)
        if errs:
            raise AssertionError(f"Link check failed for {doc}:\n" + "\n".join(errs))
    print(f"OK: Verified {len(docs_to_check)} documents with {total_links} links; all targets exist.")


def test_document_templates_and_cli():
    print("=== 2. Testing document create and templates for all kinds ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Create minimal project layout
        shutil.copytree(KIT_DIR, tmp_path / "ai-dev-playbook")
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "index.md").write_text("# Индекс\n\nРазделы документации.\n", encoding="utf-8")
        
        # Setup project.json with all document collections
        project_config = {
            "schema_version": 1,
            "playbook_version": "0.0.2",
            "name": "test-project",
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
                    "spec": {"active": "docs/specs", "archive": "docs/archive/specs", "index": "docs/specs/index.md"},
                    "design": {"active": "docs/designs", "archive": "docs/archive/designs", "index": "docs/designs/index.md"},
                    "discussion": {"active": "docs/discussions", "archive": "docs/archive/discussions", "index": "docs/discussions/index.md"},
                    "plan": {"active": "docs/plans", "archive": "docs/archive/plans", "index": "docs/plans/index.md"},
                    "adr": {"active": "docs/adr", "archive": "docs/archive/adr", "index": "docs/adr/index.md"}
                }
            },
            "components": [],
            "commands": {},
            "verification": {
                "inputs": ["docs"],
                "checks": [
                    {"id": "syntax", "argv": ["python3", "-c", "print('OK')"], "timeout_seconds": 10}
                ]
            },
            "git": {"commit": "explicit_request", "push": "explicit_request", "merge": "explicit_request", "release": "explicit_request"}
        }
        (tmp_path / "ai-dev-playbook" / "project.json").write_text(json.dumps(project_config, ensure_ascii=False, indent=2), encoding="utf-8")
        (tmp_path / "docs" / "tasks").mkdir(parents=True)
        (tmp_path / "docs" / "archive" / "tasks").mkdir(parents=True)
        (tmp_path / "docs" / "tasks" / "index.md").write_text("# Индекс задач\n\nСгенерировано playbook.py; статусы изменять в карточках.\n\n## Активные\n\n## Архив\n", encoding="utf-8")

        # Initialize indexes for all collections
        for coll in ["spec", "design", "discussion", "plan", "adr"]:
            active = tmp_path / "docs" / coll
            if coll == "adr":
                active = tmp_path / "docs" / "adr"
            else:
                active = tmp_path / "docs" / f"{coll}s"
            active.mkdir(parents=True, exist_ok=True)
            idx = active / "index.md"
            idx.write_text(f"# Индекс {coll}\n<!-- playbook-index:start -->\n<!-- playbook-index:end -->\n", encoding="utf-8")

        # Test document create for all kinds
        cli = [sys.executable, str(tmp_path / "ai-dev-playbook" / "scripts" / "playbook.py")]
        kinds = [
            ("spec", "notifications", "Спецификация уведомлений"),
            ("design", "notifications", "Дизайн уведомлений"),
            ("discussion", "channels", "Обсуждение каналов связи"),
            ("plan", "notifications", "План реализации уведомлений"),
            ("adr", "0001-queue", "Выбор очереди сообщений")
        ]
        for kind, slug, title in kinds:
            p = run_cmd([*cli, "document", "create", kind, slug, "--title", title, "--apply"], cwd=tmp_path)
            print(f"Created document: {kind}:{slug}")

        # Verify documents catalog loads cleanly
        import playbook
        import documents
        data, paths = playbook.config(tmp_path)
        records = playbook.read_tasks(tmp_path, paths)
        catalog = documents.load_catalog(tmp_path, data, paths, records, playbook.safe_path, playbook.require)
        assert len(catalog["documents"]) == 5, f"Expected 5 documents, got {len(catalog['documents'])}"
        for kind, slug, _ in kinds:
            doc_id = f"{kind}:{slug}"
            assert doc_id in catalog["documents"], f"Missing {doc_id} in catalog"
            doc = catalog["documents"][doc_id]
            text = doc["path"].read_text(encoding="utf-8")
            assert "playbook-meta:start" in text
            assert "playbook-meta:end" in text
            # Verify body sections were created from the updated template
            if kind == "spec":
                assert "1. Проблема, цель и контекст" in text
                assert "5. Планируемые изменения и требования" in text
            elif kind == "design":
                assert "2. Предлагаемое техническое решение" in text
            elif kind == "discussion":
                assert "2. Согласованные решения" in text
            elif kind == "plan":
                assert "2. Этапы и зависимости" in text
            elif kind == "adr":
                assert "2. Принятое решение" in text
        print("OK: All 5 document templates instantiated with metadata and structured sections.")


def test_three_workflow_routes():
    print("=== 3. Testing 3 Workflow Routes (Small, Medium, Large) ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copytree(KIT_DIR, tmp_path / "ai-dev-playbook")
        cli = [sys.executable, str(tmp_path / "ai-dev-playbook" / "scripts" / "playbook.py")]
        
        # Configure collections
        project_config = {
            "schema_version": 1,
            "playbook_version": "0.0.2",
            "name": "test-project",
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
                    "spec": {"active": "docs/specs", "archive": "docs/archive/specs", "index": "docs/specs/index.md"},
                    "plan": {"active": "docs/plans", "archive": "docs/archive/plans", "index": "docs/plans/index.md"}
                }
            },
            "components": [],
            "commands": {},
            "verification": {
                "inputs": ["docs/tasks"],
                "checks": [
                    {"id": "syntax", "argv": ["python3", "-c", "print('Verify passed')"], "timeout_seconds": 10}
                ]
            },
            "git": {"commit": "explicit_request", "push": "explicit_request", "merge": "explicit_request", "release": "explicit_request"}
        }
        (tmp_path / "ai-dev-playbook" / "project.json").write_text(json.dumps(project_config, indent=2), encoding="utf-8")
        (tmp_path / "docs" / "tasks").mkdir(parents=True)
        (tmp_path / "docs" / "archive" / "tasks").mkdir(parents=True)
        (tmp_path / "docs" / "specs").mkdir(parents=True)
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        (tmp_path / "docs" / "tasks" / "index.md").write_text("# Индекс задач\n\nСгенерировано playbook.py; статусы изменять в карточках.\n\n## Активные\n\n## Архив\n", encoding="utf-8")
        (tmp_path / "docs" / "specs" / "index.md").write_text("# Индекс спецификаций\n<!-- playbook-index:start -->\n<!-- playbook-index:end -->\n", encoding="utf-8")
        (tmp_path / "docs" / "plans" / "index.md").write_text("# Индекс планов\n<!-- playbook-index:start -->\n<!-- playbook-index:end -->\n", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("# Project Instructions\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Test Project\n", encoding="utf-8")

        # --- Route 1: Small task ---
        print("--- Testing Route 1: Small Task ---")
        small_card = """# TASK-001: Локальный багфикс

ID: TASK-001
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-01T10:00:00+03:00
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
Исправление локальной ошибки.

## Приоритет и зависимости
P2.

## Контекст
Локальный код.

## Приёмка
- [x] Ошибка исправлена.

## Ресурсы
Antigravity / Gemini 3.8 Flash (High).

## Выполнение и проверки
Запуск тестов и фиксация доказательств.

## Передача
Результат готов к приёмке.
"""
        (tmp_path / "docs" / "tasks" / "TASK-001.md").write_text(small_card, encoding="utf-8")
        
        run_cmd([*cli, "index"], cwd=tmp_path)
        # Verify
        run_cmd([*cli, "verify", "TASK-001"], cwd=tmp_path)
        # Handoff
        run_cmd([*cli, "lifecycle", "handoff", "TASK-001", "--summary", "Багфикс готов", "--actor", "Antigravity", "--model", "Gemini 3.8 Flash (High)"], cwd=tmp_path)
        # Executor transitions task to in_review
        card_p = tmp_path / "docs" / "tasks" / "TASK-001.md"
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        # Accept
        run_cmd([*cli, "lifecycle", "accept", "TASK-001", "--result", "accepted", "--kind", "acceptance", "--actor", "Auditor", "--model", "AuditorModel"], cwd=tmp_path)
        # Close
        run_cmd([*cli, "close", "TASK-001"], cwd=tmp_path)
        assert (tmp_path / "docs" / "archive" / "tasks" / "TASK-001.md").is_file(), "TASK-001 not archived"
        assert (tmp_path / "docs" / "archive" / "tasks" / "TASK-001-evidence").is_dir(), "Evidence dir not archived"
        print("OK: Route 1 (Small task) verified.")

        # --- Route 2: Medium task with Spec and Promotion ---
        print("--- Testing Route 2: Medium Task (Spec + Promotion) ---")
        run_cmd([*cli, "document", "create", "spec", "feature-x", "--title", "Спецификация X", "--apply"], cwd=tmp_path)
        spec_file = tmp_path / "docs" / "specs" / "feature-x.md"
        spec_text = spec_file.read_text(encoding="utf-8")
        
        # Add a draft change to spec
        spec_text = spec_text.replace('"changes": []', '''"changes": [
    {
      "id": "add-sms",
      "state": "implemented",
      "section": "sms-channel",
      "basis": {
        "type": "engineer_approval",
        "who": "Engineer",
        "at": "2026-10-01T10:00:00+03:00",
        "detail": "Согласовано добавление SMS-канала",
        "source": "docs/discussions/channels.md"
      },
      "tasks": ["TASK-002"],
      "acceptance": null
    }
  ]''')
        spec_file.write_text(spec_text, encoding="utf-8")

        medium_card = """# TASK-002: Реализация SMS-канала

ID: TASK-002
Status: in_progress
Priority: P2
Depends-On: -
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-01T11:00:00+03:00
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
Реализация SMS-канала по спецификации [feature-x](../specs/feature-x.md).

## Контекст
[Спецификация X](../specs/feature-x.md).

## Приёмка
- [x] SMS-канал реализован.

## Выполнение и проверки
Тесты пройдены.

## Передача
Готово к проверке.
"""
        (tmp_path / "docs" / "tasks" / "TASK-002.md").write_text(medium_card, encoding="utf-8")
        run_cmd([*cli, "index"], cwd=tmp_path)
        run_cmd([*cli, "verify", "TASK-002"], cwd=tmp_path)
        run_cmd([*cli, "lifecycle", "handoff", "TASK-002", "--summary", "SMS-канал готов"], cwd=tmp_path)
        card_p = tmp_path / "docs" / "tasks" / "TASK-002.md"
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        run_cmd([*cli, "lifecycle", "accept", "TASK-002", "--result", "accepted", "--kind", "acceptance"], cwd=tmp_path)

        # Plan promoting change and closing task
        request = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "model": "AuditorModel", "role": "auditor"},
            "basis": "Согласовано инженером",
            "scope": {
                "tasks": ["TASK-002"],
                "documents": ["spec:feature-x"]
            },
            "actions": [
                {"action": "promote_change", "document": "spec:feature-x", "change": "add-sms", "payload": "## Действующая редакция\n\nSMS-канал успешно внедрён.\n", "acceptance": "TASK-002: принят аудит SMS"},
                {"action": "close_task", "task": "TASK-002", "outcome": "done", "reason": "Успешная реализация", "acceptance": "TASK-002: приёмка"}
            ]
        }
        req_file = tmp_path / "req.json"
        req_file.write_text(json.dumps(request), encoding="utf-8")
        p = run_cmd([*cli, "lifecycle", "plan", "req.json", "--save"], cwd=tmp_path)
        # extract saved plan
        plan_path = None
        for line in p.stdout.splitlines():
            if "План записан:" in line or "План сохранён:" in line:
                plan_path = line.split(":", 1)[1].strip()
        assert plan_path, f"No saved plan in output:\n{p.stdout}"
        run_cmd([*cli, "lifecycle", "apply", plan_path], cwd=tmp_path)
        
        # Verify change was promoted
        spec_after = spec_file.read_text(encoding="utf-8")
        assert '"state": "accepted"' in spec_after, "Change not promoted to accepted"
        assert '"current_revision": 2' in spec_after, "Revision not bumped to 2"
        print("OK: Route 2 (Medium task with change promotion) verified.")

        # --- Route 3: Large Block (Subtasks + Block Audit) ---
        print("--- Testing Route 3: Large Block (Subtasks + Block Audit) ---")
        run_cmd([*cli, "document", "create", "plan", "big-migration", "--title", "План миграции", "--apply"], cwd=tmp_path)
        plan_file = tmp_path / "docs" / "plans" / "big-migration.md"

        # Create subtask cards first
        for tid, dep, st in [("TASK-010", "-", "in_progress"), ("TASK-011", "TASK-010", "blocked")]:
            card = f"""# {tid}: Подзадача блока

ID: {tid}
Status: {st}
Priority: P2
Depends-On: {dep}
Executor: Antigravity
Model: Gemini 3.8 Flash (High)
Created: 2026-10-01T12:00:00+03:00
Closed: -

<!-- playbook-meta:start -->
```json
{{
  "schema_version": 1,
  "relations": [],
  "materials": []
}}
```
<!-- playbook-meta:end -->

## Цель и границы
Часть блока [big-migration](../plans/big-migration.md).

## Приёмка
- [x] Готово.

## Выполнение и проверки
Запуск тестов.

## Передача
Готово.
"""
            (tmp_path / "docs" / "tasks" / f"{tid}.md").write_text(card, encoding="utf-8")

        audit_card = """# AUDIT-010: Аудит блока миграции

ID: AUDIT-010
Status: draft
Priority: P2
Depends-On: TASK-010, TASK-011
Executor: Auditor
Model: AuditorModel
Created: 2026-10-01T13:00:00+03:00
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
Сквозная проверка блока [big-migration](../plans/big-migration.md).

## Приёмка
- [x] Сквозные проверки пройдены.

## Выполнение и проверки
Сквозной аудит.

## Передача
Аудит завершён без замечаний.
"""
        (tmp_path / "docs" / "tasks" / "AUDIT-010.md").write_text(audit_card, encoding="utf-8")

        # Now link tasks in plan
        plan_text = plan_file.read_text(encoding="utf-8")
        plan_text = plan_text.replace('"tasks": []', '"tasks": ["TASK-010", "TASK-011", "AUDIT-010"]')
        plan_file.write_text(plan_text, encoding="utf-8")
        run_cmd([*cli, "index"], cwd=tmp_path)

        # 1. Execute TASK-010
        run_cmd([*cli, "verify", "TASK-010"], cwd=tmp_path)
        run_cmd([*cli, "lifecycle", "handoff", "TASK-010", "--summary", "TASK-010 готов"], cwd=tmp_path)
        card_p = tmp_path / "docs" / "tasks" / "TASK-010.md"
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        run_cmd([*cli, "lifecycle", "accept", "TASK-010", "--result", "accepted", "--kind", "acceptance"], cwd=tmp_path)
        run_cmd([*cli, "close", "TASK-010"], cwd=tmp_path)

        # 2. Unblock and execute TASK-011
        card_p = tmp_path / "docs" / "tasks" / "TASK-011.md"
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: blocked", "Status: in_progress"), encoding="utf-8")
        run_cmd([*cli, "index"], cwd=tmp_path)
        run_cmd([*cli, "verify", "TASK-011"], cwd=tmp_path)
        run_cmd([*cli, "lifecycle", "handoff", "TASK-011", "--summary", "TASK-011 готов"], cwd=tmp_path)
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        run_cmd([*cli, "lifecycle", "accept", "TASK-011", "--result", "accepted", "--kind", "acceptance"], cwd=tmp_path)
        run_cmd([*cli, "close", "TASK-011"], cwd=tmp_path)

        # Plan big-migration still active!
        assert plan_file.is_file(), "Plan should still be active when subtasks are closed!"

        # 3. Unblock and execute block audit
        card_p = tmp_path / "docs" / "tasks" / "AUDIT-010.md"
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: draft", "Status: in_progress"), encoding="utf-8")
        run_cmd([*cli, "index"], cwd=tmp_path)
        run_cmd([*cli, "verify", "AUDIT-010"], cwd=tmp_path)
        run_cmd([*cli, "lifecycle", "handoff", "AUDIT-010", "--summary", "Сквозной аудит проведён"], cwd=tmp_path)
        card_p.write_text(card_p.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review"), encoding="utf-8")
        run_cmd([*cli, "lifecycle", "accept", "AUDIT-010", "--result", "accepted", "--kind", "acceptance"], cwd=tmp_path)
        run_cmd([*cli, "close", "AUDIT-010"], cwd=tmp_path)

        # Update plan state to completed in metadata before archiving
        plan_text = plan_file.read_text(encoding="utf-8").replace('"state": "draft"', '"state": "completed"')
        plan_file.write_text(plan_text, encoding="utf-8")

        # Archive completed plan
        block_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "model": "AuditorModel", "role": "auditor"},
            "basis": "Блок полностью завершён",
            "scope": {
                "documents": ["plan:big-migration"]
            },
            "actions": [
                {"action": "archive_document", "document": "plan:big-migration", "reason": "Все задачи блока выполнены и приняты"}
            ]
        }
        (tmp_path / "block_req.json").write_text(json.dumps(block_req), encoding="utf-8")
        p = run_cmd([*cli, "lifecycle", "plan", "block_req.json", "--save"], cwd=tmp_path)
        plan_path = None
        for line in p.stdout.splitlines():
            if "План записан:" in line or "План сохранён:" in line:
                plan_path = line.split(":", 1)[1].strip()
        assert plan_path, f"No saved plan in output:\n{p.stdout}"
        run_cmd([*cli, "lifecycle", "apply", plan_path], cwd=tmp_path)

        assert (tmp_path / "docs" / "archive" / "plans" / "big-migration.md").is_file(), "Plan not archived"
        assert (tmp_path / "docs" / "archive" / "tasks" / "AUDIT-010.md").is_file(), "AUDIT-010 not archived"
        print("OK: Route 3 (Large block) verified.")


def main():
    print("Starting PB-027 behavioral probe...")
    test_markdown_links()
    test_document_templates_and_cli()
    test_three_workflow_routes()
    print("\nALL PB-027 BEHAVIORAL VERIFICATIONS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
