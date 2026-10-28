#!/usr/bin/env python3
"""PB-024: сценарный прогон реального CLI на временном configured-проекте.

Проверяет поведение команд document create/register и index --documents/--all:
preview без записи, apply через восстанавливаемую операцию, коллизии,
legacy-тело при регистрации, будущая схема метаданных, ручной индекс без
маркеров, устаревший индекс и обнаружение рассогласования check.

Запуск из корня репозитория: python3 docs/tasks/PB-024-evidence/cli-probe.py
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLAYBOOK = REPO / "ai-dev-playbook" / "scripts" / "playbook.py"

CHECKS = []


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def report(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)


def main():
    root = Path(tempfile.mkdtemp()).resolve()
    subprocess.run([sys.executable, str(PLAYBOOK), "init", str(root),
                    "--apply"], check=True, capture_output=True)
    pj = root / "ai-dev-playbook" / "project.json"
    project = json.loads(pj.read_text())
    project["name"] = "probe"
    project["setup_status"] = "configured"
    project["commands"]["test"] = "true"
    project["documents"] = {"schema_version": 1, "collections": {
        "spec": {"active": "docs/specs", "archive": "docs/archive/specs",
                 "index": "docs/specs/index.md"},
        "plan": {"active": "docs/plans", "archive": "docs/archive/plans",
                 "index": "docs/plans/index.md"}}}
    project["components"] = [{"name": "playbook", "code": "ai-dev-playbook",
                              "tests": ["tests"], "migrations": None}]
    pj.write_text(json.dumps(project, indent=2, ensure_ascii=False))
    (root / "tests").mkdir()
    for name in ("index.md", "product.md", "architecture.md",
                 "code-map.md", "roadmap.md"):
        page = root / "docs" / name
        page.write_text(page.read_text().replace("TODO:", "Заполнено:"),
                        encoding="utf-8")

    def run(*argv):
        proc = subprocess.run([sys.executable, str(PLAYBOOK), "--root",
                               str(root), *argv],
                              capture_output=True, text=True)
        print(f"$ {' '.join(argv)} -> {proc.returncode}")
        return proc

    # preview create не пишет — до первой изменяющей команды даже work/
    # не появляется.
    before = fingerprint(root)
    proc = run("document", "create", "spec", "alpha", "--title", "Alpha")
    report("create preview rc=0", proc.returncode == 0)
    report("create preview без записи",
           fingerprint(root) == before and not (root / "work").exists())
    report("create preview показывает diff",
           "spec:alpha" in proc.stdout and "Предпросмотр" in proc.stdout)

    # index --all создаёт индексы коллекций и задач.
    report("index --all", run("index", "--all").returncode == 0)
    report("index --all идемпотентен",
           "актуальн" in run("index", "--all").stdout)

    # apply create пишет документ и индекс одной операцией.
    proc = run("document", "create", "spec", "alpha", "--title", "Alpha",
               "--apply")
    doc = root / "docs" / "specs" / "alpha.md"
    report("create apply rc=0", proc.returncode == 0)
    report("create apply: документ и индекс",
           doc.is_file() and '"document_id": "spec:alpha"'
           in doc.read_text(encoding="utf-8")
           and "[Alpha](alpha.md)"
           in (root / "docs" / "specs" / "index.md").read_text("utf-8"))
    report("create apply: receipt операции",
           (root / "work" / "lifecycle").is_dir())

    # Повторное создание — отказ, файл не тронут.
    original = doc.read_bytes()
    proc = run("document", "create", "spec", "alpha", "--apply")
    report("create коллизия", proc.returncode == 1
           and doc.read_bytes() == original)

    # Регистрация legacy: preview + apply, тело сохранено.
    legacy = root / "docs" / "plans" / "legacy-plan.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Старый план\n\nТело legacy-документа.\n",
                      encoding="utf-8")
    meta = {"schema_version": 1, "document_id": "plan:legacy-plan",
            "kind": "plan", "state": "accepted",
            "created_at": "2026-01-01T00:00:00+03:00",
            "updated_at": "2026-01-02T00:00:00+03:00", "current_revision": 1,
            "relations": [], "tasks": [], "changes": [], "materials": [],
            "archive": None}
    meta_json = json.dumps(meta, ensure_ascii=False)
    before = fingerprint(root)
    proc = run("document", "register", "docs/plans/legacy-plan.md",
               "--metadata", meta_json)
    report("register preview rc=0 без записи",
           proc.returncode == 0 and fingerprint(root) == before)
    proc = run("document", "register", "docs/plans/legacy-plan.md",
               "--metadata", meta_json, "--apply")
    text = legacy.read_text(encoding="utf-8")
    report("register apply rc=0", proc.returncode == 0)
    report("register сохраняет тело",
           text.endswith("Тело legacy-документа.\n")
           and text.startswith("# Старый план\n\n<!-- playbook-meta:start -->"))
    report("register: не legacy в индексе",
           "| accepted |" in (root / "docs" / "plans" / "index.md")
           .read_text("utf-8"))

    # Повторная регистрация и будущая схема — отказ.
    proc = run("document", "register", "docs/plans/legacy-plan.md",
               "--metadata", meta_json, "--apply")
    report("register повторно — отказ", proc.returncode == 1)
    other = root / "docs" / "plans" / "other.md"
    other.write_text("# Другой\n\nТекст.\n", encoding="utf-8")
    bad = dict(meta, document_id="plan:other", schema_version=99)
    proc = run("document", "register", "docs/plans/other.md", "--metadata",
               json.dumps(bad), "--apply")
    report("register schema_version 99 — отказ",
           proc.returncode == 1 and "schema_version" in proc.stderr
           and "<!-- playbook-meta" not in other.read_text("utf-8"))

    # Файл вне коллекции и индекс коллекции — отказ.
    stray = root / "docs" / "stray.md"
    stray.write_text("# Посторонний\n", encoding="utf-8")
    proc = run("document", "register", "docs/stray.md",
               "--metadata", meta_json, "--apply")
    report("register вне коллекции — отказ", proc.returncode == 1)
    proc = run("document", "register", "docs/plans/index.md",
               "--metadata", meta_json, "--apply")
    report("register файла индекса — отказ", proc.returncode == 1)

    # Ручной индекс без маркеров не перезаписывается.
    index = root / "docs" / "specs" / "index.md"
    index.write_text("# Ручной индекс\n\nне трогать\n", encoding="utf-8")
    proc = run("index", "--documents")
    report("index: файл без маркеров — отказ без записи",
           proc.returncode == 1 and "playbook-index" in proc.stderr
           and index.read_text("utf-8") == "# Ручной индекс\n\nне трогать\n")

    # check обнаруживает повреждённый индекс и не исправляет его.
    proc = run("check", "--ready")
    report("check ловит повреждённый индекс, не чинит",
           proc.returncode == 1
           and index.read_text("utf-8") == "# Ручной индекс\n\nне трогать\n")

    # Область маркеров: ручное содержимое вне неё сохраняется.
    index.write_text("# Ручной индекс\n\n<!-- playbook-index:start -->\nOLD\n"
                     "<!-- playbook-index:end -->\n\nХвост.\n",
                     encoding="utf-8")
    proc = run("index", "--documents")
    text = index.read_text(encoding="utf-8")
    report("index обновляет только область",
           proc.returncode == 0 and "Ручной индекс" in text
           and "Хвост." in text and "OLD" not in text and "alpha" in text)

    # Устаревший индекс: check фиксирует, файл не меняется.
    stale = text.replace("| [Alpha](alpha.md)", "| [Beta](alpha.md)")
    index.write_text(stale, encoding="utf-8")
    proc = run("check", "--ready")
    report("check ловит устаревший индекс",
           proc.returncode == 1 and "устарел" in proc.stderr
           and index.read_text("utf-8") == stale)

    # Финальная согласованность после исправления.
    run("index", "--all")
    proc = run("check", "--ready")
    report("check --ready после исправления", proc.returncode == 0)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\nИТОГ: {len(CHECKS) - len(failed)}/{len(CHECKS)} пройдено")
    if failed:
        print("Провалы:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
