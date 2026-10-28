#!/usr/bin/env python3
"""Автономный зонд приёмки PB-028: совместимость, init, update и взаимные блокировки."""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ai-dev-playbook" / "scripts"))
import playbook
import file_operations as fops
import updating


def run_cli(root, *argv):
    cmd = [sys.executable, str(REPO / "ai-dev-playbook" / "scripts" / "playbook.py"),
           "--root", str(root), *argv]
    cwd = root if Path(root).exists() else REPO
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return res.returncode, res.stdout, res.stderr


def build_authentic_002_artifact(dest):
    dest = Path(dest)
    fixture_dir = REPO / "tests" / "fixtures" / "release-0.0.2"
    shutil.copytree(fixture_dir, dest, dirs_exist_ok=True)
    return dest


def tree_snapshot(root):
    result = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_file():
            result[rel] = updating.sha256(p.read_bytes())
        elif p.is_dir():
            result[rel] = "<dir>"
        elif p.is_symlink():
            result[rel] = f"<symlink:{os.readlink(p)}>"
    return result


def main():
    print("=== Старт зонда приёмки PB-028 ===")
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()

    # 1. Проверка совместимости 0.0.2 без documents
    print("\n1. Проверка совместимости project.json 0.0.2...")
    art1 = build_authentic_002_artifact(root / "art1")
    inst1 = root / "inst1"
    res = subprocess.run([sys.executable, str(art1 / "ai-dev-playbook/scripts/playbook.py"),
                          "init", str(inst1), "--apply"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    code, out, err = run_cli(inst1, "check")
    assert code == 0, f"check 0.0.2 failed: {err}"
    code, out, err = run_cli(inst1, "index")
    assert code == 0, f"index 0.0.2 failed: {err}"

    p1 = inst1 / "ai-dev-playbook" / "project.json"
    p1_data = json.loads(p1.read_text(encoding="utf-8"))
    p1_data["documents"] = {"schema_version": 99, "collections": {}}
    p1.write_text(json.dumps(p1_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    code, _, err = run_cli(inst1, "check")
    assert code == 1 and "schema_version" in err
    print("  [OK] 0.0.2 без documents проходит check и index; неверная версия documents отклоняется.")

    # 2. Проверка свежего init 0.0.3
    print("\n2. Проверка init 0.0.3: коллекции, индексы и конфликт...")
    inst2 = root / "inst2"
    code, out, err = run_cli(inst2, "init", str(inst2), "--apply")
    assert code == 0, f"init failed: {err}"
    for exp in ("docs/discussions/index.md", "docs/specs/index.md", "docs/designs/index.md",
                "docs/adr/index.md", "docs/plans/index.md"):
        assert (inst2 / exp).is_file(), f"Отсутствует индекс {exp}"
        assert "<!-- playbook-index:start -->" in (inst2 / exp).read_text(encoding="utf-8")
    for exp_dir in ("docs/archive/discussions", "docs/archive/specs", "docs/archive/designs",
                    "docs/archive/adr", "docs/archive/plans"):
        assert (inst2 / exp_dir).is_dir(), f"Отсутствует каталог архива {exp_dir}"
    print("  [OK] Все 5 коллекций, индексы и архивы созданы корректно.")

    # Проверка конфликта чужого файла
    conflict_dir = root / "conflict"
    (conflict_dir / "docs" / "specs").mkdir(parents=True)
    foreign = conflict_dir / "docs" / "specs" / "index.md"
    foreign.write_text("чужой файл", encoding="utf-8")
    code, out, err = run_cli(conflict_dir, "init", str(conflict_dir), "--apply")
    assert code == 1, "Конфликт не остановил запись"
    assert "Конфликт: docs/specs/index.md" in err, f"Неожиданная ошибка: {err}"
    assert foreign.read_text(encoding="utf-8") == "чужой файл"
    assert not (conflict_dir / "ai-dev-playbook").exists()
    print("  [OK] Чужой файл docs/specs/index.md остановил init без изменений на диске.")

    # 3. Обновление 0.0.2 -> 0.0.3 с custom paths и историческим архивом
    print("\n3. Обновление подлинного 0.0.2 с custom paths, архивом и evidence...")
    art3 = build_authentic_002_artifact(root / "art3")
    meta3 = updating.load_artifact(art3, playbook.safe_path, playbook.require)
    assert meta3["version"] == "0.0.2", f"Неверная версия артефакта 0.0.2: {meta3.get('version')}"

    cli_002 = art3 / "ai-dev-playbook" / "scripts" / "playbook.py"
    inst3 = root / "inst3"
    res = subprocess.run([sys.executable, str(cli_002), "init", str(inst3), "--apply"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    p3 = inst3 / "ai-dev-playbook" / "project.json"
    p3_data = json.loads(p3.read_text(encoding="utf-8"))
    p3_data["name"] = "probe-real-002"
    p3_data["setup_status"] = "configured"
    p3_data["commands"]["test"] = "python3 -c 'print(1)'"
    p3_data["paths"] = {
        "docs": "my_docs",
        "tasks": "my_docs/tasks",
        "archive": "my_docs/archive_tasks",
        "task_index": "my_docs/tasks/index.md"
    }
    p3_data["custom_marker"] = "secret_value"
    p3.write_text(json.dumps(p3_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (inst3 / "docs").rename(inst3 / "my_docs")
    for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
        doc = inst3 / "my_docs" / name
        doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")
    agents = inst3 / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8").replace("docs/", "my_docs/"), encoding="utf-8")

    # Исторический архив
    arch_dir = inst3 / "my_docs" / "archive_tasks"
    arch_dir.mkdir(parents=True, exist_ok=True)
    (arch_dir / "ARCH-1.md").write_text(
        "# ARCH-1: Историческая задача\n\nID: ARCH-1\nStatus: done\nPriority: P1\nDepends-On: -\n"
        "Created: 2026-08-01T10:00:00+03:00\nClosed: 2026-08-02T10:00:00+03:00\n\nАрхивная задача.\n",
        encoding="utf-8"
    )

    # Legacy задача
    (inst3 / "my_docs" / "tasks" / "DEV-1.md").write_text(
        "# DEV-1: Активная задача\n\nID: DEV-1\nStatus: in_progress\nPriority: P1\nDepends-On: -\n"
        "Created: 2026-09-01T10:00:00+03:00\nClosed: -\n\nАктивная задача.\n",
        encoding="utf-8"
    )

    inst_cli3 = inst3 / "ai-dev-playbook/scripts/playbook.py"
    # Индексация через 0.0.2 CLI
    res = subprocess.run([sys.executable, str(inst_cli3),
                          "--root", str(inst3), "index"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    evidence = inst3 / "my_docs" / "tasks" / "DEV-1-evidence" / "run.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("лог доказательств задачи\n", encoding="utf-8")

    foreign_doc = inst3 / "my_script.py"
    foreign_doc.write_text("print('user')\n", encoding="utf-8")

    # Проверка исходного состояния через 0.0.2 CLI
    res = subprocess.run([sys.executable, str(inst_cli3),
                          "--root", str(inst3), "check"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    before3 = tree_snapshot(inst3)

    # Выполняем update plan и update apply через установленный собственный CLI 0.0.2
    res = subprocess.run([sys.executable, str(inst_cli3),
                          "--root", str(inst3), "update", "plan", str(REPO), "--save"],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"update plan failed: {res.stderr}"
    assert "0.0.2 → 0.0.3" in res.stdout, f"Не найден переход в выводе: {res.stdout}"

    res = subprocess.run([sys.executable, str(inst_cli3),
                          "--root", str(inst3), "update", "apply"],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"update apply failed: {res.stderr}"

    p3_updated = json.loads(p3.read_text(encoding="utf-8"))
    assert p3_updated["playbook_version"] == "0.0.3"
    assert p3_updated["paths"]["docs"] == "my_docs"
    assert p3_updated["custom_marker"] == "secret_value"
    assert (arch_dir / "ARCH-1.md").is_file()
    assert (inst3 / "my_docs" / "tasks" / "DEV-1.md").is_file()
    assert evidence.read_text(encoding="utf-8") == "лог доказательств задачи\n"
    assert foreign_doc.read_text(encoding="utf-8") == "print('user')\n"

    after3 = tree_snapshot(inst3)
    protected3 = [p for p in before3 if not p.startswith("ai-dev-playbook/") and not p.startswith("work/")]
    for p in protected3:
        assert before3[p] == after3.get(p), f"Пользовательский файл изменён: {p}"

    manifest = json.loads((inst3 / "ai-dev-playbook" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 35
    for rel in manifest["files"]:
        assert rel.startswith("ai-dev-playbook/"), f"Чужой файл в manifest: {rel}"

    res = subprocess.run([sys.executable, str(inst_cli3),
                          "--root", str(inst3), "check", "--ready"],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"check --ready failed: {res.stderr}"
    print("  [OK] Обновление подлинного 0.0.2 → 0.0.3 сохранило custom paths, архив, доказательства и чужие файлы.")

    # 4. Необъявленный переход отклоняется
    print("\n4. Проверка отклонения необъявленного перехода...")
    bad_rel = root / "bad_release"
    shutil.copytree(REPO / "ai-dev-playbook", bad_rel / "ai-dev-playbook",
                    ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "manifest.json"))
    (bad_rel / "ai-dev-playbook" / "VERSION").write_text("0.0.4\n", encoding="utf-8")
    (bad_rel / "ai-dev-playbook" / "updates.json").write_text(
        json.dumps({"schema_version": 1, "updates_from": {"0.0.3": {"steps": []}}}) + "\n",
        encoding="utf-8"
    )
    managed = updating.managed_files(bad_rel, playbook.safe_path, playbook.require)
    hashes = {rel: updating.sha256(b) for rel, b in managed.items()}
    (bad_rel / "release.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.0.4", "release_id": updating.fingerprint(hashes),
                    "files": hashes, "updates_from": {"0.0.3": {"steps": []}}}, indent=2) + "\n",
        encoding="utf-8"
    )
    inst4 = root / "inst4"
    res = subprocess.run([sys.executable, str(cli_002), "init", str(inst4), "--apply"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    code, out, err = run_cli(inst4, "update", "plan", str(bad_rel))
    assert code == 1, "Необъявленный переход не был отклонён"
    assert "Неподдерживаемый переход 0.0.2 → 0.0.4" in err, f"Неожиданная ошибка: {err}"
    print("  [OK] Необъявленный переход 0.0.2 → 0.0.4 отклонён.")

    # 5. Взаимные блокировки
    print("\n5. Проверка взаимных блокировок между update и lifecycle...")
    inst5 = root / "inst5"
    res = subprocess.run([sys.executable, str(cli_002), "init", str(inst5), "--apply"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    # 5.1 update pending блокирует lifecycle
    (inst5 / "work" / "update").mkdir(parents=True, exist_ok=True)
    (inst5 / "work" / "update" / "pending.json").write_text(
        json.dumps({"op_id": "test-u", "op_dir": "work/update/test-u"}), encoding="utf-8"
    )
    code, out, err = run_cli(inst5, "lifecycle", "restore")
    assert code == 1 and "обновление" in err
    dummy_life = inst5 / "work" / "lifecycle" / "life-op"
    dummy_life.mkdir(parents=True, exist_ok=True)
    (dummy_life / "restore.py").write_text(fops.RESTORE_SCRIPT, encoding="utf-8")
    (dummy_life / "backup_index.json").write_text("{}", encoding="utf-8")
    res = subprocess.run([sys.executable, str(dummy_life / "restore.py")],
                         capture_output=True, text=True, cwd=inst5)
    assert res.returncode == 1 and "обновления" in res.stderr
    print("  [OK] work/update/pending.json блокирует CLI и автономный restore lifecycle.")

    # 5.2 lifecycle pending блокирует update
    (inst5 / "work" / "update" / "pending.json").unlink()
    (inst5 / "work" / "lifecycle").mkdir(parents=True, exist_ok=True)
    (inst5 / "work" / "lifecycle" / "pending.json").write_text(
        json.dumps({"operation_id": "test-l", "op_dir": "work/lifecycle/test-l"}), encoding="utf-8"
    )
    code, out, err = run_cli(inst5, "update", "plan", str(REPO))
    assert code == 1 and "lifecycle" in err
    code, out, err = run_cli(inst5, "update", "restore")
    assert code == 1 and "lifecycle" in err
    dummy_upd = inst5 / "work" / "update" / "upd-op"
    dummy_upd.mkdir(parents=True, exist_ok=True)
    (dummy_upd / "restore.py").write_text(updating.RESTORE_SCRIPT, encoding="utf-8")
    (dummy_upd / "backup_index.json").write_text("{}", encoding="utf-8")
    res = subprocess.run([sys.executable, str(dummy_upd / "restore.py")],
                         capture_output=True, text=True, cwd=inst5)
    assert res.returncode == 1 and "документов" in res.stderr
    print("  [OK] work/lifecycle/pending.json блокирует CLI update plan/restore и автономный restore.")

    # 6. Работоспособность автономного restore.py для update после реального SIGKILL
    print("\n6. Проверка восстановления автономным restore.py после SIGKILL...")
    art6 = build_authentic_002_artifact(root / "art6")
    inst6 = root / "inst6"
    res = subprocess.run([sys.executable, str(art6 / "ai-dev-playbook/scripts/playbook.py"),
                          "init", str(inst6), "--apply"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    code, out, err = run_cli(inst6, "update", "plan", str(REPO), "--save")
    assert code == 0

    before6 = tree_snapshot(inst6)
    scripts_dir = str(REPO / "ai-dev-playbook" / "scripts")
    child_code = r'''
import os, signal, sys
sys.path.insert(0, {scripts!r})
import updating, playbook

calls = []
orig = updating._write_file
def killer(source, target):
    calls.append(1)
    if len(calls) == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(source, target)

updating._write_file = killer
playbook.main(["--root", {root!r}, "update", "apply"])
'''.format(scripts=scripts_dir, root=str(inst6))

    res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
    assert res.returncode == -signal.SIGKILL
    assert (inst6 / "work" / "update" / "pending.json").exists()

    (op_dir,) = list((inst6 / "work" / "update").glob("20*"))
    res = subprocess.run([sys.executable, str(op_dir / "restore.py")],
                         capture_output=True, text=True, cwd=inst6)
    assert res.returncode == 0, res.stderr
    assert not (inst6 / "work" / "update" / "pending.json").exists()
    assert (inst6 / "ai-dev-playbook" / "VERSION").read_text().strip() == "0.0.2"

    after6 = tree_snapshot(inst6)
    for rel, digest in before6.items():
        if rel.startswith("work/"):
            continue
        assert after6.get(rel) == digest, f"Файл {rel} не совпадает побайтно"
    for rel in set(after6) - set(before6):
        assert rel.startswith("work/"), f"Лишний не-work файл: {rel}"

    restored_cli = inst6 / "ai-dev-playbook" / "scripts" / "playbook.py"
    res = subprocess.run([sys.executable, str(restored_cli), "--help"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    res = subprocess.run([sys.executable, str(restored_cli), "--root", str(inst6), "check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    print("  [OK] Автономный restore.py побайтно восстановил пред-образ 0.0.2 и работоспособность CLI.")

    # 7. Регрессия аудита PB-028-A1: проверка чистоты дерева при конфликте архивного каталога
    print("\n7. Проверка PB-028-A1: конфликт архивного пути не оставляет мусора на диске...")
    for kind in ("file", "symlink"):
        sub = root / f"conflict_{kind}"
        sub.mkdir()
        conflict = sub / "docs" / "archive" / "specs"
        conflict.parent.mkdir(parents=True)
        if kind == "file":
            conflict.write_text("archive conflict file\n", encoding="utf-8")
        else:
            outside = root / f"outside_{kind}"
            outside.mkdir()
            conflict.symlink_to(outside, target_is_directory=True)
        before_tree = sorted(str(p.relative_to(sub)) for p in sub.rglob("*"))
        code, out, err = run_cli(sub, "init", str(sub), "--apply")
        assert code == 1, "Конфликт не отклонён"
        assert "docs/archive/specs" in err
        after_tree = sorted(str(p.relative_to(sub)) for p in sub.rglob("*"))
        assert before_tree == after_tree, f"Остались лишние файлы: {set(after_tree) - set(before_tree)}"
        assert not (sub / "ai-dev-playbook").exists()
    print("  [OK] PB-028-A1: конфликт архивного пути не оставляет ни одного файла на диске.")

    # 8. Регрессия аудита PB-028-A2: реальный артефакт 0.0.2 и SIGKILL mid-write
    print("\n8. Проверка PB-028-A2: реальный артефакт 0.0.2 и прерывание SIGKILL mid-write...")
    art_dir = build_authentic_002_artifact(root / "authentic_002")
    cli_002 = art_dir / "ai-dev-playbook" / "scripts" / "playbook.py"
    res = subprocess.run([sys.executable, str(cli_002), "--help"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert cli_002.read_bytes() != (REPO / "ai-dev-playbook/scripts/playbook.py").read_bytes()

    inst8 = root / "inst8"
    res = subprocess.run([sys.executable, str(cli_002), "init", str(inst8), "--apply"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert not (inst8 / "ai-dev-playbook" / "scripts" / "documents.py").exists()

    code, out, err = run_cli(inst8, "update", "plan", str(REPO), "--save")
    assert code == 0

    before8 = tree_snapshot(inst8)
    child_code = r'''
import os, signal, sys
sys.path.insert(0, {scripts!r})
import updating, playbook

calls = []
orig = updating._write_file
def killer(source, target):
    calls.append(1)
    if len(calls) == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(source, target)

updating._write_file = killer
playbook.main(["--root", {root!r}, "update", "apply"])
'''.format(scripts=scripts_dir, root=str(inst8))

    res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
    assert res.returncode == -signal.SIGKILL, f"Returncode was {res.returncode}, stderr: {res.stderr}"

    # Проверка блокировки и восстановления
    assert (inst8 / "work" / "update" / "pending.json").exists()
    code, out, err = run_cli(inst8, "lifecycle", "restore")
    assert code == 1 and "обновление" in err

    code, out, err = run_cli(inst8, "update", "restore")
    assert code == 0, f"restore failed: {err}"
    assert not (inst8 / "work" / "update" / "pending.json").exists()
    assert (inst8 / "ai-dev-playbook" / "VERSION").read_text().strip() == "0.0.2"

    after8 = tree_snapshot(inst8)
    for rel, digest in before8.items():
        if rel.startswith("work/"):
            continue
        assert after8.get(rel) == digest, f"Файл {rel} не совпадает побайтно после restore"
    for rel in set(after8) - set(before8):
        assert rel.startswith("work/"), f"Лишний не-work файл: {rel}"

    restored_cli8 = inst8 / "ai-dev-playbook" / "scripts" / "playbook.py"
    res = subprocess.run([sys.executable, str(restored_cli8), "--help"], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    res = subprocess.run([sys.executable, str(restored_cli8), "--root", str(inst8), "check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    print("  [OK] PB-028-A2: подлинный 0.0.2 восстанавливается побайтно, и его CLI полностью работоспособен.")

    # 9. Регрессия аудита PB-028-A3: init в отсутствующий каталог и rollback при ошибке
    print("\n9. Проверка PB-028-A3: init в отсутствующий каталог и rollback...")
    absent1 = root / "absent_target"
    assert not absent1.exists()
    code, out, err = run_cli(root, "init", str(absent1), "--apply")
    assert code == 0, f"init absent target failed: {err}"
    assert absent1.is_dir()
    assert (absent1 / "ai-dev-playbook" / "VERSION").is_file()
    code, _, err = run_cli(absent1, "check")
    assert code == 0, f"check failed on absent target: {err}"

    nested = root / "parent_nested" / "child_nested"
    assert not (root / "parent_nested").exists()
    code, out, err = run_cli(root, "init", str(nested), "--apply")
    assert code == 0, f"init nested absent target failed: {err}"
    assert nested.is_dir()
    assert (nested / "ai-dev-playbook" / "VERSION").is_file()
    code, _, err = run_cli(nested, "check")
    assert code == 0, f"check failed on nested absent target: {err}"

    fail_parent = root / "fail_parent" / "fail_child"
    assert not (root / "fail_parent").exists()
    child_fail = r'''
import sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import playbook

orig_open = Path.open
def failing_open(self, *args, **kwargs):
    mode = args[0] if args else kwargs.get("mode", "r")
    if mode == "xb" and self.name == "CHANGELOG.md":
        raise OSError("Simulated write failure on CHANGELOG.md")
    return orig_open(self, *args, **kwargs)

Path.open = failing_open
sys.exit(playbook.main(["init", {target!r}, "--apply"]))
'''.format(scripts=scripts_dir, target=str(fail_parent))

    res = subprocess.run([sys.executable, "-c", child_fail], capture_output=True, text=True)
    assert res.returncode == 1
    assert "Simulated write failure" in res.stderr
    assert not (root / "fail_parent").exists(), "fail_parent не был удален при rollback"
    print("  [OK] PB-028-A3: init в отсутствующий и вложенный каталог успешен, при сбое запись откатывается чисто.")

    print("\n=== Все критерии приёмки и регрессии аудита (A1, A2, A3) успешно подтверждены! ===")
    tmp.cleanup()


if __name__ == "__main__":
    main()
