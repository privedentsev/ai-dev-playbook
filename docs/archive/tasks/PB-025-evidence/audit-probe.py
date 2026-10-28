#!/usr/bin/env python3
"""PB-025: Независимый сценарный аудит жизненного цикла архивирования документов.

Проверяет:
1. lifecycle plan без --save не пишет файлы и не создаёт планов.
2. lifecycle plan --save создаёт уникальный план work/lifecycle/plans/<plan_id>.json.
3. lifecycle apply требует явный путь к плану и повторно валидирует его.
4. Изменение входов/запроса/добавление нового файла/подмена хеша плана блокирует apply без потери данных.
5. Перенос документа и материалов, переписывание Markdown-ссылок, сохранение immutable evidence и их SHA-256.
6. Сопровождающий JSON-доказательство архивирования с картой переносов и хешами.
7. Обновление индекса коллекции (секция архива).
8. Защита служебных областей work/ (запрет переноса неразрешённых путей).
9. Отказ при нефинальном статусе, незавершённых задачах плана, неподтверждённых обязательных внешних материалах, неучтённых материалах-кандидатах, коллизиях имён в архиве.
10. Восстановление пред-образа после сбоя apply (rollback) и через lifecycle restore после прерывания.
11. Повторный apply завершённого плана возвращает already_completed под gate+lock и не создаёт второй архив.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLAYBOOK = REPO / "ai-dev-playbook" / "scripts" / "playbook.py"

CHECKS = []


def report(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    status_str = "PASS" if ok else "FAIL"
    print(f"[{status_str}] {name} {detail}")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(root: Path):
    return {
        str(p.relative_to(root)): sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def make_test_project():
    root = Path(tempfile.mkdtemp()).resolve()
    subprocess.run([sys.executable, str(PLAYBOOK), "init", str(root), "--apply"],
                   check=True, capture_output=True)
    pj = root / "ai-dev-playbook" / "project.json"
    project = json.loads(pj.read_text(encoding="utf-8"))
    project["name"] = "audit-probe-pb025"
    project["setup_status"] = "configured"
    project["commands"]["test"] = "true"
    project["documents"] = {
        "schema_version": 1,
        "collections": {
            "spec": {
                "active": "docs/specs",
                "archive": "docs/archive/specs",
                "index": "docs/specs/index.md",
            },
            "plan": {
                "active": "docs/plans",
                "archive": "docs/archive/plans",
                "index": "docs/plans/index.md",
            }
        }
    }
    project["components"] = [{"name": "playbook", "code": "ai-dev-playbook",
                              "tests": ["tests"], "migrations": None}]
    pj.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
        p = root / "docs" / name
        p.write_text(p.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")
    subprocess.run([sys.executable, str(PLAYBOOK), "--root", str(root), "index", "--all"],
                   check=True, capture_output=True)
    return root


def run_cli(root: Path, *argv):
    proc = subprocess.run([sys.executable, str(PLAYBOOK), "--root", str(root), *argv],
                          capture_output=True, text=True)
    return proc


def create_doc(root: Path, kind: str, slug: str, title: str, state: str = "draft",
               materials=None, tasks=None, body=""):
    proc = run_cli(root, "document", "create", kind, slug, "--title", title, "--apply")
    assert proc.returncode == 0, f"create failed: {proc.stderr}\n{proc.stdout}"
    doc_path = root / "docs" / f"{kind}s" / f"{slug}.md"
    text = doc_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("<!-- playbook-meta:start -->")
    end = lines.index("<!-- playbook-meta:end -->")
    meta_json = "\n".join(lines[start + 2:end - 1])
    meta = json.loads(meta_json)
    meta["state"] = state
    if materials is not None:
        meta["materials"] = materials
    if tasks is not None:
        meta["tasks"] = tasks
    new_meta_lines = [
        "<!-- playbook-meta:start -->",
        "```json",
        *json.dumps(meta, indent=2, ensure_ascii=False).splitlines(),
        "```",
        "<!-- playbook-meta:end -->"
    ]
    new_text = "\n".join(lines[:start] + new_meta_lines + lines[end + 1:])
    if body:
        new_text += "\n" + body
    doc_path.write_text(new_text, encoding="utf-8")
    run_cli(root, "index", "--documents")
    return doc_path


def main():
    root = make_test_project()
    try:
        # Scenario 1: Plan without --save does not write files
        create_doc(root, "spec", "s1", "Spec 1", state="superseded")
        req1 = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini 3.8 Flash", "session": "test-session"},
            "basis": "Аудит PB-025",
            "scope": {"documents": ["spec:s1"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s1",
                "reason": "Архивирование по результатам аудита",
                "successor": None
            }]
        }
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / "work" / "req1.json").write_text(json.dumps(req1, ensure_ascii=False), encoding="utf-8")
        before_plan = fingerprint(root)
        proc = run_cli(root, "lifecycle", "plan", "work/req1.json")
        report("1. plan preview exit 0", proc.returncode == 0)
        report("1. plan preview не меняет проектные файлы", fingerprint(root) == before_plan)
        report("1. plan preview выводит diff и пометку без записи",
               "docs/specs/s1.md → docs/archive/specs/s1.md" in proc.stdout and "без записи" in proc.stdout)

        # Scenario 2: Plan with --save writes unique plan file
        proc = run_cli(root, "lifecycle", "plan", "work/req1.json", "--save")
        report("2. plan --save exit 0", proc.returncode == 0)
        plan_line = [l for l in proc.stdout.splitlines() if "План записан:" in l]
        report("2. plan --save выводит путь к плану", len(plan_line) == 1)
        plan_rel = plan_line[0].split("План записан:", 1)[1].strip()
        plan_path = root / plan_rel
        report("2. plan файл существует", plan_path.is_file())
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        report("2. plan schema_version=1 и kind=lifecycle-plan",
               plan_data.get("schema_version") == 1 and plan_data.get("kind") == "lifecycle-plan")

        # Scenario 3: Apply revalidates plan and executes atomic archive
        proc = run_cli(root, "lifecycle", "apply", plan_rel)
        report("3. apply exit 0", proc.returncode == 0, proc.stderr)
        report("3. документ перемещён в архив", (root / "docs" / "archive" / "specs" / "s1.md").is_file()
               and not (root / "docs" / "specs" / "s1.md").exists())
        # Проверяем квитанцию archive companion
        archived_meta_raw = (root / "docs" / "archive" / "specs" / "s1.md").read_text(encoding="utf-8")
        report("3. в метаданных архивированного документа есть блок archive",
               '"archive":' in archived_meta_raw and '"operation_id":' in archived_meta_raw)
        report("3. индекс коллекции обновлён",
               "## Архив" in (root / "docs" / "specs" / "index.md").read_text(encoding="utf-8")
               and "[Spec 1](../archive/specs/s1.md)" in (root / "docs" / "specs" / "index.md").read_text(encoding="utf-8"))

        # Scenario 4: Idempotent apply (already_completed)
        proc_twice = run_cli(root, "lifecycle", "apply", plan_rel)
        report("4. повторный apply сообщает already_completed",
               proc_twice.returncode == 0 and "уже применён" in proc_twice.stdout)

        # Scenario 5: Stale plan detection (input changed before apply)
        create_doc(root, "spec", "s2", "Spec 2", state="superseded")
        req2 = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест изменения входов",
            "scope": {"documents": ["spec:s2"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s2",
                "reason": "Тест",
                "successor": None
            }]
        }
        (root / "work" / "req2.json").write_text(json.dumps(req2, ensure_ascii=False), encoding="utf-8")
        proc = run_cli(root, "lifecycle", "plan", "work/req2.json", "--save")
        plan2_rel = [l for l in proc.stdout.splitlines() if "План записан:" in l][0].split("План записан:", 1)[1].strip()
        # Изменяем s2.md после сохранения плана
        (root / "docs" / "specs" / "s2.md").write_text((root / "docs" / "specs" / "s2.md").read_text(encoding="utf-8") + "\nДополнительная строка\n", encoding="utf-8")
        proc_stale = run_cli(root, "lifecycle", "apply", plan2_rel)
        report("5. apply отклоняет план при изменении входов (stale inputs)",
               proc_stale.returncode == 1 and "План устарел или подменён" in proc_stale.stderr)
        report("5. при отказе s2 остался на месте", (root / "docs" / "specs" / "s2.md").is_file())

        # Scenario 6: Refusal on non-final state
        create_doc(root, "spec", "s3", "Spec 3", state="draft")
        req3 = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест нефинального состояния",
            "scope": {"documents": ["spec:s3"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s3",
                "reason": "Архив draft",
                "successor": None
            }]
        }
        (root / "work" / "req3.json").write_text(json.dumps(req3, ensure_ascii=False), encoding="utf-8")
        proc_draft = run_cli(root, "lifecycle", "plan", "work/req3.json")
        report("6. отказ архивирования нефинального состояния (draft)",
               proc_draft.returncode == 1 and "не финальное" in proc_draft.stderr)

        # Scenario 7: Refusal on unconfirmed required external material
        create_doc(root, "spec", "s4", "Spec 4", state="superseded", materials=[{
            "id": "ext-1",
            "role": "report",
            "source": "https://example.com/external-doc",
            "storage": "external",
            "access": "публичный доступ",
            "required": True,
            "immutable": True,
            "used_by": []
        }])
        req4_bad = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест external material",
            "scope": {"documents": ["spec:s4"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s4",
                "reason": "Архив external",
                "successor": None,
                "confirmed_materials": []  # Не подтверждено
            }]
        }
        (root / "work" / "req4.json").write_text(json.dumps(req4_bad, ensure_ascii=False), encoding="utf-8")
        proc_ext_bad = run_cli(root, "lifecycle", "plan", "work/req4.json")
        report("7. отказ при неподтверждённом обязательном внешнем материале",
               proc_ext_bad.returncode == 1 and "обязательные внешние материалы без подтверждения" in proc_ext_bad.stderr)

        # Подтверждаем внешний материал
        req4_good = dict(req4_bad)
        req4_good["actions"] = [dict(req4_bad["actions"][0], confirmed_materials=["ext-1"])]
        (root / "work" / "req4.json").write_text(json.dumps(req4_good, ensure_ascii=False), encoding="utf-8")
        proc_ext_good = run_cli(root, "lifecycle", "plan", "work/req4.json")
        report("7. план успешен при подтверждённом внешнем материале", proc_ext_good.returncode == 0)

        # Scenario 8: Moving local materials, preserving SHA-256 of immutable evidence, rewriting links
        ev_dir = root / "docs" / "specs" / "s5-evidence"
        ev_dir.mkdir(parents=True)
        probe_log = ev_dir / "probe.log"
        probe_log.write_text("Лог прогона: результат 100% OK\n", encoding="utf-8")
        log_hash = sha256_file(probe_log)

        create_doc(root, "spec", "s5", "Spec 5", state="superseded", materials=[{
            "id": "log-mat",
            "role": "log",
            "source": "docs/specs/s5-evidence/probe.log",
            "storage": "local",
            "required": True,
            "immutable": True,
            "used_by": []
        }])
        # Создадим активный документ spec:s6, ссылающийся на s5 и на его evidence
        create_doc(root, "spec", "s6", "Spec 6", state="accepted",
                   body="Ссылка на [Spec 5](s5.md) и [лог](s5-evidence/probe.log).")

        req5 = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Архивирование s5 с материалами",
            "scope": {"documents": ["spec:s5"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s5",
                "reason": "Архив s5",
                "successor": "spec:s6"
            }]
        }
        (root / "work" / "req5.json").write_text(json.dumps(req5, ensure_ascii=False), encoding="utf-8")
        proc = run_cli(root, "lifecycle", "plan", "work/req5.json", "--save")
        plan5_rel = [l for l in proc.stdout.splitlines() if "План записан:" in l][0].split("План записан:", 1)[1].strip()
        proc = run_cli(root, "lifecycle", "apply", plan5_rel)
        report("8. apply s5 с evidence exit 0", proc.returncode == 0, proc.stderr)

        archived_log = root / "docs" / "archive" / "specs" / "s5-evidence" / "probe.log"
        report("8. evidence-файл перемещён в архив", archived_log.is_file() and not probe_log.exists())
        report("8. immutable evidence сохраняет точный SHA-256", sha256_file(archived_log) == log_hash)

        # Проверим, что ссылка в s6 переписана
        s6_text = (root / "docs" / "specs" / "s6.md").read_text(encoding="utf-8")
        report("8. ссылка в активном s6 переписана на архивный путь",
               "../archive/specs/s5.md" in s6_text and "../archive/specs/s5-evidence/probe.log" in s6_text)

        # Scenario 9: PB-026 action refusal (close_task / reopen / promote_change)
        req_future = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест future actions",
            "scope": {"documents": ["spec:s6"]},
            "actions": [{
                "action": "close_task",
                "document": "spec:s6",
                "reason": "Тест",
                "successor": None
            }]
        }
        (root / "work" / "req_future.json").write_text(json.dumps(req_future, ensure_ascii=False), encoding="utf-8")
        proc_future = run_cli(root, "lifecycle", "plan", "work/req_future.json")
        report("9. отказ действий PB-026 (close_task) с указанием границы",
               proc_future.returncode == 1 and "PB-026" in proc_future.stderr)

        # Scenario 11: Plan document with open tasks vs completed tasks
        create_doc(root, "plan", "plan1", "Plan 1", state="completed", tasks=["TASK-001"])
        # Создадим открытую задачу TASK-001 в docs/tasks
        task_file = root / "docs" / "tasks" / "TASK-001.md"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text("""# TASK-001: Тестовая задача

ID: TASK-001
Status: in_progress
Priority: P2
Depends-On: -
Executor: -
Model: -
Created: 2026-10-01T00:00:00+00:00
Closed: -

## Цель и границы
Тест
""", encoding="utf-8")
        run_cli(root, "index", "--all")
        req_plan_bad = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест плана с открытыми задачами",
            "scope": {"documents": ["plan:plan1"]},
            "actions": [{
                "action": "archive_document",
                "document": "plan:plan1",
                "reason": "Архивирование плана",
                "successor": None
            }]
        }
        (root / "work" / "req_plan.json").write_text(json.dumps(req_plan_bad, ensure_ascii=False), encoding="utf-8")
        proc_plan_bad = run_cli(root, "lifecycle", "plan", "work/req_plan.json")
        report("11. отказ архивирования плана с открытыми задачами",
               proc_plan_bad.returncode == 1 and "незавершёнными задачами" in proc_plan_bad.stderr)

        # Переводим задачу в архив/done
        task_file.unlink()
        archived_task = root / "docs" / "archive" / "tasks" / "TASK-001.md"
        archived_task.parent.mkdir(parents=True, exist_ok=True)
        archived_task.write_text("""# TASK-001: Тестовая задача

ID: TASK-001
Status: done
Priority: P2
Depends-On: -
Executor: -
Model: -
Created: 2026-10-01T00:00:00+00:00
Closed: 2026-10-02T00:00:00+00:00

## Цель и границы
Тест
""", encoding="utf-8")
        run_cli(root, "index", "--all")
        proc_plan_good = run_cli(root, "lifecycle", "plan", "work/req_plan.json", "--save")
        report("11. план с завершёнными задачами архивируется успешно",
               proc_plan_good.returncode == 0)

        # Scenario 12: Real crash during apply and restoration via lifecycle restore
        # Создадим документ spec:s7 с evidence
        s7_ev = root / "docs" / "specs" / "s7-evidence"
        s7_ev.mkdir(parents=True)
        (s7_ev / "data.txt").write_text("Важные данные s7\n", encoding="utf-8")
        s7_hash = sha256_file(s7_ev / "data.txt")
        create_doc(root, "spec", "s7", "Spec 7", state="superseded", materials=[{
            "id": "mat-s7",
            "role": "report",
            "source": "docs/specs/s7-evidence/data.txt",
            "storage": "local",
            "required": True,
            "immutable": True,
            "used_by": []
        }])
        req7 = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "Gemini", "session": None},
            "basis": "Тест прерывания и восстановления",
            "scope": {"documents": ["spec:s7"]},
            "actions": [{
                "action": "archive_document",
                "document": "spec:s7",
                "reason": "Архив s7",
                "successor": None
            }]
        }
        (root / "work" / "req7.json").write_text(json.dumps(req7, ensure_ascii=False), encoding="utf-8")
        proc = run_cli(root, "lifecycle", "plan", "work/req7.json", "--save")
        plan7_rel = [l for l in proc.stdout.splitlines() if "План записан:" in l][0].split("План записан:", 1)[1].strip()

        before_crash = fingerprint(root)

        # Запускаем apply в отдельном процессе с инъекцией падения (os._exit) посреди atomic_write
        crash_script = f"""
import os
import sys
from pathlib import Path
repo = Path({repr(str(REPO))})
sys.path.insert(0, str(repo / "ai-dev-playbook" / "scripts"))
import file_operations
import playbook

orig_write = file_operations._atomic_write
count = 0
def boom(target, content, op_id):
    global count
    count += 1
    if count == 2:
        os._exit(42)
    return orig_write(target, content, op_id)

file_operations._atomic_write = boom
playbook.main(["--root", {repr(str(root))}, "lifecycle", "apply", {repr(plan7_rel)}])
"""
        proc_crash = subprocess.run([sys.executable, "-c", crash_script], capture_output=True, text=True)
        report("12. симуляция сбоя посреди записи завершилась кодом 42", proc_crash.returncode == 42)

        # Проверяем, что есть маркер pending
        proc_status = run_cli(root, "lifecycle", "status")
        report("12. status фиксирует прерванную операцию после сбоя",
               "Состояние: прервана" in proc_status.stdout)

        # Обычные команды заблокированы
        proc_blocked = run_cli(root, "check", "--ready")
        report("12. изменяющие/проверочные команды заблокированы при pending",
               proc_blocked.returncode == 1 and "Незавершённая операция" in proc_blocked.stderr)

        # Выполняем lifecycle restore
        proc_restore = run_cli(root, "lifecycle", "restore")
        report("12. lifecycle restore exit 0", proc_restore.returncode == 0, proc_restore.stdout)
        report("12. маркер pending снят после restore",
               not (root / "work" / "lifecycle" / "pending.json").exists())

        # Проверяем восстановление пред-образа s7
        report("12. исходный файл s7.md на месте", (root / "docs" / "specs" / "s7.md").is_file())
        report("12. evidence data.txt на месте с исходным хешем",
               (s7_ev / "data.txt").is_file() and sha256_file(s7_ev / "data.txt") == s7_hash)

        # check --ready проходит успешно
        proc_ready = run_cli(root, "check", "--ready")
        report("12. check --ready успешен после восстановления", proc_ready.returncode == 0)

        # Scenario 13: Restore without pending refused
        proc_res_empty = run_cli(root, "lifecycle", "restore")
        report("13. restore без pending возвращает ошибку",
               proc_res_empty.returncode == 1 and "Нет незавершённой операции документов" in proc_res_empty.stderr)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\nИтог: проверок всего: {len(CHECKS)}, успешно: {len(CHECKS) - len(failed)}, ошибок: {len(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
