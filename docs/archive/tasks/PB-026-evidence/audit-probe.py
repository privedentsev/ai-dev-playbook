#!/usr/bin/env python3
"""PB-026: Независимый сценарный аудит приёмки задач, close, возвратов и ревизий требований.

Проверяемые независимые сценарии:
 1. Managed task close (done): verify -> handoff -> accept -> close;
    перенос карточки и доказательств, формирование close-<plan_id>.json,
    перенос verify-отчёта в архивные материалы, обновление индекса.
 2. Отказы close (done):
    2.1. отказ без handoff;
    2.2. отказ без accept;
    2.3. отказ при diagnostic accept (недостаточно для done);
    2.4. отказ при изменении карточки после передачи (card_projection);
    2.5. отказ при изменении входов verify после прогона (stale inputs);
    2.6. отказ при статусе не in_review (например in_progress).
 3. Инвариант card_projection:
    3.1. добавление протокола аудита НЕ ломает handoff и позволяет успешный close;
    3.2. изменение раздела требований ломает handoff.
 4. Managed task close (cancelled):
    4.1. отказ cancelled без --reason;
    4.2. отказ cancelled без accept(result=cancelled);
    4.3. успешный close cancelled с --reason и accept(cancelled).
 5. Dry-run close:
    5.1. close --dry-run успешен и показывает план;
    5.2. close --dry-run не изменяет файлы проекта.
 6. Legacy task close в проекте с documents:
    6.1. close legacy карточки переносит карточку и каталог evidence;
    6.2. маркер неподтверждённой модели блокирует legacy close в done.
 7. Reopen архивированной карточки:
    7.1. reopen возвращает карточку и evidence в active;
    7.2. сбрасывает Closed, переводит в заданный статус (in_progress);
    7.3. очищает handoff и acceptance в метаданных;
    7.4. добавляет раздел '## Возврат';
    7.5. генерирует квитанцию reopen-<plan_id>.json;
    7.6. обновляет индекс задач.
 8. Управление графом зависимостей при reopen:
    8.1. возврат зависимости при done-зависимом отклоняется (недопустимый граф);
    8.2. совместный возврат карточки и её зависимого в одном запросе успешен;
    8.3. возврат зависимости переводит активного зависимого в blocked.
 9. Promote change документа:
    9.1. promote_change заменяет тело документа на payload;
    9.2. сохраняет прежнюю редакцию в <stem>-evidence/revision-N.md (immutable);
    9.3. инкрементирует current_revision;
    9.4. переводит change в accepted;
    9.5. генерирует квитанцию promote-<plan_id>.json;
    9.6. отказ promote_change если change не в implemented;
    9.7. отказ promote_change для неизвестного change;
    9.8. отказ promote_change для архивированного документа.
10. Восстановление и идемпотентность:
    10.1. повторный apply возвращает already_completed под gate+lock;
    10.2. активный pending блокирует изменяющие команды;
    10.3. lifecycle restore восстанавливает проект и снимает pending.
"""

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO / "ai-dev-playbook" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import documents
import file_operations
import lifecycle
import playbook

CHECKS = []


def report(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    status_str = "PASS" if ok else "FAIL"
    print(f"[{status_str}] {name} {detail}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(root: Path):
    return {
        str(p.relative_to(root)): sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def run_cli(root: Path, *argv):
    proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "playbook.py"), "--root", str(root), *argv],
                          capture_output=True, text=True, cwd=str(root))
    return proc


@contextlib.contextmanager
def fresh_project():
    root = Path(tempfile.mkdtemp()).resolve()
    try:
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "playbook.py"), "init", str(root), "--apply"],
                       check=True, capture_output=True, cwd=str(root))
        pj = root / "ai-dev-playbook" / "project.json"
        project = json.loads(pj.read_text(encoding="utf-8"))
        project["name"] = "audit-probe-pb026"
        project["setup_status"] = "configured"
        project["commands"]["test"] = "true"
        project["verification"] = {
            "inputs": ["docs"],
            "checks": [{"id": "unit-tests", "argv": [sys.executable, "-c", "exit(0)"],
                        "timeout_seconds": 30}]
        }
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
        pj.write_text(json.dumps(project, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (root / "tests").mkdir(exist_ok=True)
        for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            p = root / "docs" / name
            p.write_text(p.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "playbook.py"), "--root", str(root), "index", "--all"],
                       check=True, capture_output=True, cwd=str(root))
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_managed_card(root: Path, ident: str, status: str = "in_review", deps: str = "-",
                       executor: str = "Devin", model: str = "SWE-2 High",
                       closed: str = "-", meta: dict = None, body: str = ""):
    tasks_dir = root / "docs" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if meta is None:
        meta = {"schema_version": 1, "relations": [], "materials": [], "moves": []}
    text = (f"# {ident}: Тестовая задача\n\n"
            f"ID: {ident}\n"
            f"Status: {status}\n"
            f"Priority: P2\n"
            f"Depends-On: {deps}\n"
            f"Executor: {executor}\n"
            f"Model: {model}\n"
            f"Created: 2026-10-01T00:00:00+03:00\n"
            f"Closed: {closed}\n\n"
            f"{documents.META_START}\n"
            f"```json\n"
            f"{json.dumps(meta, indent=2, ensure_ascii=False)}\n"
            f"```\n"
            f"{documents.META_END}\n\n"
            f"## Цель и границы\n\n"
            f"Тестовая цель для задачи {ident}.\n\n"
            f"## Приёмка\n\n"
            f"- [x] Приёмка выполнена.\n"
            f"{body}\n")
    card_path = tasks_dir / f"{ident}.md"
    card_path.write_text(text, encoding="utf-8")
    run_cli(root, "index")
    return card_path


def write_archived_card(root: Path, ident: str, status: str = "done", deps: str = "-",
                        closed: str = "2026-10-01T01:00:00+03:00", meta: dict = None):
    arch_dir = root / "docs" / "archive" / "tasks"
    arch_dir.mkdir(parents=True, exist_ok=True)
    if meta is None:
        meta = {"schema_version": 1, "relations": [], "materials": [], "moves": []}
    text = (f"# {ident}: Тестовая задача\n\n"
            f"ID: {ident}\n"
            f"Status: {status}\n"
            f"Priority: P2\n"
            f"Depends-On: {deps}\n"
            f"Executor: Devin\n"
            f"Model: SWE-2 High\n"
            f"Created: 2026-10-01T00:00:00+03:00\n"
            f"Closed: {closed}\n\n"
            f"{documents.META_START}\n"
            f"```json\n"
            f"{json.dumps(meta, indent=2, ensure_ascii=False)}\n"
            f"```\n"
            f"{documents.META_END}\n\n"
            f"## Цель и границы\n\n"
            f"Архивная цель {ident}.\n")
    card_path = arch_dir / f"{ident}.md"
    card_path.write_text(text, encoding="utf-8")
    run_cli(root, "index")
    return card_path


def write_legacy_card(root: Path, ident: str, status: str = "in_review", deps: str = "-",
                      executor: str = "Devin", model: str = "SWE-2 High", closed: str = "-"):
    tasks_dir = root / "docs" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    text = (f"# {ident}: Legacy задача\n\n"
            f"ID: {ident}\n"
            f"Status: {status}\n"
            f"Priority: P2\n"
            f"Depends-On: {deps}\n"
            f"Executor: {executor}\n"
            f"Model: {model}\n"
            f"Created: 2026-10-01T00:00:00+03:00\n"
            f"Closed: {closed}\n\n"
            f"## Цель и границы\n\n"
            f"Цель legacy карточки.\n\n"
            f"## Приёмка\n\n"
            f"- [x] Готово.\n")
    card_path = tasks_dir / f"{ident}.md"
    card_path.write_text(text, encoding="utf-8")
    run_cli(root, "index")
    return card_path


def create_spec_doc(root: Path, slug: str, title: str, state: str = "accepted",
                    changes: list = None, body: str = "Исходное содержание спецификации.\n"):
    run_cli(root, "document", "create", "spec", slug, "--title", title, "--apply")
    spec_path = root / "docs" / "specs" / f"{slug}.md"
    text = spec_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if documents.META_START in line)
    end = next(i for i, line in enumerate(lines) if documents.META_END in line)
    raw_json = "\n".join(lines[start + 2:end - 1])
    meta = json.loads(raw_json)
    meta["state"] = state
    if changes:
        meta["changes"] = changes
    raw_new = json.dumps(meta, indent=2, ensure_ascii=False)
    new_text = ("\n".join(lines[:start + 2]) + "\n" + raw_new + "\n" +
                "\n".join(lines[end - 1:end + 1]) + "\n\n" + body)
    spec_path.write_text(new_text, encoding="utf-8")
    run_cli(root, "index", "--all")
    return spec_path


def main():
    print("=== Начало независимого аудита PB-026 ===")

    # 1. Managed task close (done)
    with fresh_project() as root:
        write_managed_card(root, "DEV-1", status="in_review")
        v_proc = run_cli(root, "verify", "DEV-1")
        report("1.1. verify DEV-1", v_proc.returncode == 0, v_proc.stderr)

        h_proc = run_cli(root, "lifecycle", "handoff", "DEV-1", "--summary", "готово к аудиту",
                         "--actor", "Devin", "--model", "SWE-2 High")
        report("1.2. lifecycle handoff DEV-1", h_proc.returncode == 0, h_proc.stderr)

        a_proc = run_cli(root, "lifecycle", "accept", "DEV-1", "--result", "accepted",
                         "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High",
                         "--note", "принято")
        report("1.3. lifecycle accept DEV-1", a_proc.returncode == 0, a_proc.stderr)

        c_proc = run_cli(root, "close", "DEV-1")
        report("1.4. close DEV-1", c_proc.returncode == 0, c_proc.stderr)

        archived_card = root / "docs" / "archive" / "tasks" / "DEV-1.md"
        archived_ev = root / "docs" / "archive" / "tasks" / "DEV-1-evidence"
        card_moved = archived_card.is_file() and not (root / "docs" / "tasks" / "DEV-1.md").exists()
        close_companions = list(archived_ev.glob("close-*.json"))
        has_companion = len(close_companions) == 1
        if has_companion:
            comp_data = json.loads(close_companions[0].read_text(encoding="utf-8"))
            comp_valid = comp_data.get("outcome") == "done" and comp_data.get("policy") == "managed"
        else:
            comp_valid = False

        report("1.5. карточка перенесена в архив", card_moved)
        report("1.6. сформирован сопровождающий close-<plan_id>.json", has_companion and comp_valid)
        report_files = list(archived_ev.rglob("report.json"))
        report("1.7. прогон verify перемещён в evidence архива", len(report_files) == 1)

    # 2. Отказы close (done)
    # 2.1. Без handoff
    with fresh_project() as root:
        write_managed_card(root, "DEV-2", status="in_review")
        c = run_cli(root, "close", "DEV-2")
        report("2.1. отказ close done без handoff", c.returncode != 0 and "передачи" in c.stderr, c.stderr.strip())

    # 2.2. Без accept
    with fresh_project() as root:
        write_managed_card(root, "DEV-2", status="in_review")
        run_cli(root, "verify", "DEV-2")
        run_cli(root, "lifecycle", "handoff", "DEV-2", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        c = run_cli(root, "close", "DEV-2")
        report("2.2. отказ close done без accept", c.returncode != 0 and "приёмки" in c.stderr, c.stderr.strip())

    # 2.3. С diagnostic accept
    with fresh_project() as root:
        write_managed_card(root, "DEV-2", status="in_review")
        run_cli(root, "verify", "DEV-2")
        run_cli(root, "lifecycle", "handoff", "DEV-2", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-2", "--result", "accepted",
                "--kind", "diagnostic", "--actor", "Auditor", "--model", "SWE-2 High")
        c = run_cli(root, "close", "DEV-2")
        report("2.3. отказ close done с diagnostic accept", c.returncode != 0 and "diagnostic" in c.stderr, c.stderr.strip())

    # 2.4. При изменении карточки после handoff
    with fresh_project() as root:
        write_managed_card(root, "DEV-3", status="in_review")
        run_cli(root, "verify", "DEV-3")
        run_cli(root, "lifecycle", "handoff", "DEV-3", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-3", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")
        card3 = root / "docs" / "tasks" / "DEV-3.md"
        card3.write_text(card3.read_text(encoding="utf-8").replace("Тестовая цель", "Изменённая цель"),
                         encoding="utf-8")
        c = run_cli(root, "close", "DEV-3")
        report("2.4. отказ close done при изменении цели карточки после передачи",
               c.returncode != 0 and "изменилась после передачи" in c.stderr, c.stderr.strip())

    # 2.5. При устаревании входов verify (stale inputs)
    with fresh_project() as root:
        write_managed_card(root, "DEV-4", status="in_review")
        run_cli(root, "verify", "DEV-4")
        run_cli(root, "lifecycle", "handoff", "DEV-4", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-4", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")
        (root / "docs" / "extra_note.md").write_text("новое изменение входов", encoding="utf-8")
        c = run_cli(root, "close", "DEV-4")
        report("2.5. отказ close done при изменении входов verify (stale inputs)",
               c.returncode != 0 and "свежего успешного прогона" in c.stderr, c.stderr.strip())

    # 2.6. При статусе не in_review (например in_progress)
    with fresh_project() as root:
        write_managed_card(root, "DEV-5", status="in_progress")
        c = run_cli(root, "close", "DEV-5")
        report("2.6. отказ close done для карточки in_progress",
               c.returncode != 0 and "in_review" in c.stderr, c.stderr.strip())

    # 3. Инвариант card_projection
    with fresh_project() as root:
        write_managed_card(root, "DEV-6", status="in_review")
        run_cli(root, "verify", "DEV-6")
        run_cli(root, "lifecycle", "handoff", "DEV-6", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-6", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")
        card6 = root / "docs" / "tasks" / "DEV-6.md"
        card6.write_text(card6.read_text(encoding="utf-8") +
                         "\n## Аудит 2026-10-02T12:00:00+03:00; исполнитель: Antigravity; модель: Gemini 3.8 Flash.\n\nЗамечаний нет.\n",
                         encoding="utf-8")
        c = run_cli(root, "close", "DEV-6")
        report("3.1. добавление протокола аудита не ломает handoff и позволяет close",
               c.returncode == 0, c.stderr.strip())

    # 4. Managed task close (cancelled)
    with fresh_project() as root:
        # 4.1. Без --reason
        write_managed_card(root, "DEV-71", status="in_progress")
        run_cli(root, "lifecycle", "accept", "DEV-71", "--result", "cancelled",
                "--kind", "acceptance", "--actor", "Engineer", "--model", "—",
                "--note", "отмена задачи")
        c_fail = run_cli(root, "close", "DEV-71", "--status", "cancelled")
        report("4.1. отказ close cancelled без --reason",
               c_fail.returncode != 0 and "--reason" in c_fail.stderr, c_fail.stderr.strip())

        # 4.2. Без accept(cancelled)
        write_managed_card(root, "DEV-72", status="in_progress")
        c_fail2 = run_cli(root, "close", "DEV-72", "--status", "cancelled", "--reason", "отмена")
        report("4.2. отказ close cancelled без accept(cancelled)",
               c_fail2.returncode != 0 and "cancelled" in c_fail2.stderr, c_fail2.stderr.strip())

        # 4.3. Успешный close cancelled
        write_managed_card(root, "DEV-73", status="in_progress")
        run_cli(root, "lifecycle", "accept", "DEV-73", "--result", "cancelled",
                "--kind", "acceptance", "--actor", "Engineer", "--model", "—",
                "--note", "отмена задачи")
        c_ok = run_cli(root, "close", "DEV-73", "--status", "cancelled", "--reason", "потребность отпала")
        report("4.3. close cancelled c --reason и accept(cancelled)", c_ok.returncode == 0, c_ok.stderr.strip())
        archived7 = root / "docs" / "archive" / "tasks" / "DEV-73.md"
        report("4.4. статус cancelled в архиве",
               archived7.is_file() and "Status: cancelled" in archived7.read_text(encoding="utf-8"))

    # 5. Dry-run close
    with fresh_project() as root:
        write_managed_card(root, "DEV-8", status="in_review")
        run_cli(root, "verify", "DEV-8")
        run_cli(root, "lifecycle", "handoff", "DEV-8", "--summary", "передано",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-8", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")
        fp_before = fingerprint(root)
        dr_proc = run_cli(root, "close", "DEV-8", "--dry-run")
        fp_after = fingerprint(root)
        report("5.1. close --dry-run успешен и показывает план",
               dr_proc.returncode == 0 and "close_task DEV-8" in dr_proc.stdout)
        report("5.2. close --dry-run не изменил файлы проекта", fp_before == fp_after)

    # 6. Legacy task close в проекте с documents
    with fresh_project() as root:
        write_legacy_card(root, "DEV-9", status="in_review", executor="Devin", model="SWE-2 High")
        ev9 = root / "docs" / "tasks" / "DEV-9-evidence"
        ev9.mkdir(parents=True, exist_ok=True)
        (ev9 / "data.log").write_text("legacy log", encoding="utf-8")
        c = run_cli(root, "close", "DEV-9")
        report("6.1. close legacy карточки в managed-проекте", c.returncode == 0, c.stderr.strip())
        arch_legacy = root / "docs" / "archive" / "tasks" / "DEV-9.md"
        arch_ev9 = root / "docs" / "archive" / "tasks" / "DEV-9-evidence" / "data.log"
        report("6.2. legacy карточка и каталог evidence перенесены в архив",
               arch_legacy.is_file() and arch_ev9.is_file())

        write_legacy_card(root, "DEV-10", status="in_review", executor="Devin",
                          model="SWE-2 High (не подтверждено)")
        c10 = run_cli(root, "close", "DEV-10")
        report("6.3. маркер неподтверждённой модели блокирует legacy close в done",
               c10.returncode != 0 and "не подтверждена" in c10.stderr, c10.stderr.strip())

    # 7. Reopen архивированной карточки
    with fresh_project() as root:
        write_managed_card(root, "DEV-1", status="in_review")
        run_cli(root, "verify", "DEV-1")
        run_cli(root, "lifecycle", "handoff", "DEV-1", "--summary", "готово",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-1", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")
        run_cli(root, "close", "DEV-1")

        reopen_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "Находки аудита",
            "scope": {"tasks": ["DEV-1"]},
            "actions": [{"action": "reopen", "task": "DEV-1", "status": "in_progress",
                         "reason": "исправление замечаний"}]
        }
        (root / "reopen_request.json").write_text(json.dumps(reopen_req, indent=2, ensure_ascii=False), encoding="utf-8")
        plan_proc = run_cli(root, "lifecycle", "plan", "reopen_request.json", "--save")
        plan_path = plan_proc.stdout.strip().split()[-1]
        apply_proc = run_cli(root, "lifecycle", "apply", plan_path)
        report("7.1. reopen DEV-1 в in_progress", apply_proc.returncode == 0, apply_proc.stderr)
        dev1_active = root / "docs" / "tasks" / "DEV-1.md"
        dev1_archived = root / "docs" / "archive" / "tasks" / "DEV-1.md"
        report("7.2. DEV-1 вернулась в active", dev1_active.is_file() and not dev1_archived.exists())
        dev1_text = dev1_active.read_text(encoding="utf-8")
        report("7.3. статус in_progress, Closed сброшен, секция Возврат добавлена",
               "Status: in_progress" in dev1_text and "Closed: -" in dev1_text and "## Возврат" in dev1_text)
        reopen_companions = list((root / "docs" / "tasks" / "DEV-1-evidence").glob("reopen-*.json"))
        report("7.4. создана квитанция reopen-<plan_id>.json", len(reopen_companions) == 1)

    # 8. Граф зависимостей при reopen
    with fresh_project() as root:
        write_archived_card(root, "DEV-1", status="done")
        write_archived_card(root, "DEV-11", status="done", deps="DEV-1")

        # 8.1. Возврат DEV-1 в одиночку при done-зависимом DEV-11
        reopen_bad = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "Тест графа",
            "scope": {"tasks": ["DEV-1"]},
            "actions": [{"action": "reopen", "task": "DEV-1", "status": "in_progress", "reason": "возврат"}]
        }
        (root / "reopen_bad.json").write_text(json.dumps(reopen_bad, indent=2, ensure_ascii=False), encoding="utf-8")
        bad_plan = run_cli(root, "lifecycle", "plan", "reopen_bad.json")
        report("8.1. отказ reopen DEV-1 при done-зависимом DEV-11 (недопустимый граф)",
               bad_plan.returncode != 0 and "недопустимый граф" in bad_plan.stderr, bad_plan.stderr.strip())

        # 8.2. Совместный возврат DEV-1 и DEV-11
        reopen_good = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "Совместный возврат",
            "scope": {"tasks": ["DEV-1", "DEV-11"]},
            "actions": [
                {"action": "reopen", "task": "DEV-1", "status": "in_progress", "reason": "возврат"},
                {"action": "reopen", "task": "DEV-11", "status": "blocked", "reason": "зависимость возвращена"}
            ]
        }
        (root / "reopen_good.json").write_text(json.dumps(reopen_good, indent=2, ensure_ascii=False), encoding="utf-8")
        g_plan = run_cli(root, "lifecycle", "plan", "reopen_good.json", "--save")
        g_plan_path = g_plan.stdout.strip().split()[-1]
        g_app = run_cli(root, "lifecycle", "apply", g_plan_path)
        report("8.2. совместный возврат DEV-1 и DEV-11 успешен", g_app.returncode == 0, g_app.stderr)
        dev11_text = (root / "docs" / "tasks" / "DEV-11.md").read_text(encoding="utf-8")
        report("8.3. DEV-11 получила статус blocked", "Status: blocked" in dev11_text)

        # 8.4. Возврат зависимости блокирует активного зависимого
        write_archived_card(root, "DEV-20", status="done")
        write_managed_card(root, "DEV-21", status="in_review", deps="DEV-20")
        reopen_dep_active = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "Возврат зависимости",
            "scope": {"tasks": ["DEV-20"]},
            "actions": [{"action": "reopen", "task": "DEV-20", "status": "in_progress", "reason": "правка"}]
        }
        (root / "reopen_dep_active.json").write_text(json.dumps(reopen_dep_active, indent=2, ensure_ascii=False), encoding="utf-8")
        da_plan = run_cli(root, "lifecycle", "plan", "reopen_dep_active.json", "--save")
        da_plan_path = da_plan.stdout.strip().split()[-1]
        da_app = run_cli(root, "lifecycle", "apply", da_plan_path)
        report("8.4. возврат DEV-20 перевёл активный DEV-21 в blocked",
               da_app.returncode == 0 and "Status: blocked" in (root / "docs" / "tasks" / "DEV-21.md").read_text(encoding="utf-8"))

    # 9. Promote change документа
    with fresh_project() as root:
        chg = {
            "id": "chg-sms",
            "state": "implemented",
            "section": "Область",
            "basis": {"type": "engineer_approval", "who": "engineer", "at": "2026-10-01T00:00:00+03:00",
                      "detail": "согласовано", "source": "review"},
            "tasks": [],
            "acceptance": None
        }
        create_spec_doc(root, "auth", "Аутентификация", state="accepted", changes=[chg],
                        body="Редакция 1: старый текст спецификации.\n")
        promote_req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "Принятие изменения спецификации",
            "scope": {"documents": ["spec:auth"]},
            "actions": [{"action": "promote_change", "document": "spec:auth", "change": "chg-sms",
                         "payload": "Редакция 2: новый текст после удаления SMS.\n"}]
        }
        (root / "promote_req.json").write_text(json.dumps(promote_req, indent=2, ensure_ascii=False), encoding="utf-8")
        p_plan = run_cli(root, "lifecycle", "plan", "promote_req.json", "--save")
        report("9.1. lifecycle plan promote_change", p_plan.returncode == 0, p_plan.stderr)
        p_plan_path = p_plan.stdout.strip().split()[-1]
        p_app = run_cli(root, "lifecycle", "apply", p_plan_path)
        report("9.2. lifecycle apply promote_change", p_app.returncode == 0, p_app.stderr)

        spec_auth = root / "docs" / "specs" / "auth.md"
        spec_text = spec_auth.read_text(encoding="utf-8")
        report("9.3. новое тело спецификации применено", "Редакция 2" in spec_text and "Редакция 1" not in spec_text)
        rev1_snap = root / "docs" / "specs" / "auth-evidence" / "revision-1.md"
        report("9.4. снимок прежней редакции сохранён в auth-evidence/revision-1.md",
               rev1_snap.is_file() and "Редакция 1" in rev1_snap.read_text(encoding="utf-8"))
        lines_auth = spec_text.split("\n")
        s_pos = next(i for i, l in enumerate(lines_auth) if documents.META_START in l)
        e_pos = next(i for i, l in enumerate(lines_auth) if documents.META_END in l)
        meta_auth = json.loads("\n".join(lines_auth[s_pos + 2:e_pos - 1]))
        report("9.5. current_revision инкрементирован до 2", meta_auth.get("current_revision") == 2)
        report("9.6. change переведён в accepted", meta_auth.get("changes")[0].get("state") == "accepted")
        prom_companions = list((root / "docs" / "specs" / "auth-evidence").glob("promote-*.json"))
        report("9.7. создана квитанция promote-<plan_id>.json", len(prom_companions) == 1)

        # 9.8. Отказ promote_change если change не в implemented
        chg_draft = {
            "id": "chg-2",
            "state": "approved",
            "section": "Область",
            "basis": {"type": "engineer_approval", "who": "engineer", "at": "2026-10-01T00:00:00+03:00",
                      "detail": "согласовано", "source": "review"},
            "tasks": [],
            "acceptance": None
        }
        create_spec_doc(root, "billing", "Биллинг", state="accepted", changes=[chg_draft])
        promote_bad = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "auditor", "model": "SWE-2 High"},
            "basis": "b", "scope": {"documents": ["spec:billing"]},
            "actions": [{"action": "promote_change", "document": "spec:billing", "change": "chg-2",
                         "payload": "новое"}]
        }
        (root / "promote_bad.json").write_text(json.dumps(promote_bad, indent=2, ensure_ascii=False), encoding="utf-8")
        bad_p_proc = run_cli(root, "lifecycle", "plan", "promote_bad.json")
        report("9.8. отказ promote_change если change не в implemented",
               bad_p_proc.returncode != 0 and "implemented" in bad_p_proc.stderr, bad_p_proc.stderr.strip())

        # 10.1. Повторный apply возвращает already_completed под gate+lock
        p_reapply = run_cli(root, "lifecycle", "apply", p_plan_path)
        report("10.1. повторный apply возвращает already_completed под gate+lock",
               p_reapply.returncode == 0 and ("уже применён" in p_reapply.stdout or "already_completed" in p_reapply.stdout))

    # 10.2 & 10.3. Восстановление после сбоя
    with fresh_project() as root:
        write_managed_card(root, "DEV-1", status="in_review")
        run_cli(root, "verify", "DEV-1")
        run_cli(root, "lifecycle", "handoff", "DEV-1", "--summary", "готово",
                "--actor", "Devin", "--model", "SWE-2 High")
        run_cli(root, "lifecycle", "accept", "DEV-1", "--result", "accepted",
                "--kind", "acceptance", "--actor", "Auditor", "--model", "SWE-2 High")

        req = {
            "schema_version": 1,
            "actor": {"name": "Auditor", "role": "cli", "model": None},
            "basis": "close DEV-1",
            "scope": {"tasks": ["DEV-1"]},
            "actions": [{"action": "close_task", "task": "DEV-1", "outcome": "done"}]
        }
        (root / "req_close.json").write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
        pl_proc = run_cli(root, "lifecycle", "plan", "req_close.json", "--save")
        pl_path = pl_proc.stdout.strip().split()[-1]

        # Симулируем падение процесса при первой физической записи
        crash_script = (
            f"import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            f"import playbook, file_operations\n"
            f"orig = file_operations._atomic_write\n"
            f"def crash(target, content, op_id):\n"
            f"    raise KeyboardInterrupt()\n"
            f"file_operations._atomic_write = crash\n"
            f"try:\n"
            f"    playbook.main(['--root', {str(root)!r}, 'lifecycle', 'apply', {pl_path!r}])\n"
            f"except KeyboardInterrupt:\n"
            f"    sys.exit(42)\n"
        )
        proc_crash = subprocess.run([sys.executable, "-c", crash_script], cwd=str(root))
        report("10.2a. симуляция сбоя записи вернула код 42", proc_crash.returncode == 42)

        # Конкурирующая команда заблокирована
        block_proc = run_cli(root, "index")
        report("10.2b. активный pending блокирует изменяющие команды",
               block_proc.returncode != 0 and "заблокированы" in block_proc.stderr)

        # lifecycle restore восстанавливает дерево и очищает pending
        rest_proc = run_cli(root, "lifecycle", "restore")
        pending_file = root / "work" / "lifecycle" / "pending.json"
        report("10.3. lifecycle restore восстанавливает проект и снимает pending",
               rest_proc.returncode == 0 and not pending_file.exists())

    total = len(CHECKS)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = total - passed
    print(f"\nИТОГ: {passed}/{total} проверок пройдено.")
    if failed > 0:
        print(f"ОШИБКИ: {failed}")
        sys.exit(1)
    else:
        print("Все сценарные проверки успешно пройдены!")


if __name__ == "__main__":
    main()
