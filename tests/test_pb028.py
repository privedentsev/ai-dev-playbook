import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_cli import CliCase, REPO, REPO_VERSION, run_cli
from test_playbook import playbook, write_card

fops = playbook.file_operations
updating = playbook.updating
SCRIPTS = REPO / "ai-dev-playbook" / "scripts"


def build_authentic_002_artifact(dest):
    """Собрать рабочий эталонный релизный артефакт версии 0.0.2 из фикстуры."""
    dest = Path(dest)
    fixture_dir = REPO / "tests" / "fixtures" / "release-0.0.2"
    shutil.copytree(fixture_dir, dest, dirs_exist_ok=True)
    return dest


def tree_snapshot(root):
    """Полный снимок дерева: пути файлов/каталогов и хеши файлов."""
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


class Pb028CompatibilityAndUpdateTest(CliCase):
    """Полная проверка требований, приёмки и регрессий аудита PB-028."""

    def test_schema_backward_compatibility(self):
        """0.0.2 без documents работает; documents опционален с собственной схемой."""
        self.install(version="0.0.2")
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertNotIn("documents", project)

        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)
        self.add_task("PB-100", "ready")
        code, out, err = self.cli("index")
        self.assertEqual(code, 0, err)

        project["documents"] = {"schema_version": 99, "collections": {}}
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("schema_version", err)

    def test_init_fresh_creates_collections_and_indexes(self):
        """Init создаёт рабочие коллекции, 5 индексов и архивы коллекций."""
        code, out, err = self.init("--apply")
        self.assertEqual(code, 0, err)

        for expected in ("docs/discussions/index.md",
                         "docs/specs/index.md",
                         "docs/designs/index.md",
                         "docs/adr/index.md",
                         "docs/plans/index.md"):
            path = self.root / expected
            self.assertTrue(path.is_file(), f"Отсутствует {expected}")
            content = path.read_text(encoding="utf-8")
            self.assertIn("<!-- playbook-index:start -->", content)
            self.assertIn("<!-- playbook-index:end -->", content)

        for expected_dir in ("docs/archive/discussions",
                             "docs/archive/specs",
                             "docs/archive/designs",
                             "docs/archive/adr",
                             "docs/archive/plans"):
            self.assertTrue((self.root / expected_dir).is_dir(), f"Отсутствует каталог {expected_dir}")

        for expected_tpl in ("task.md", "adr.md"):
            self.assertTrue((self.root / "ai-dev-playbook" / "templates" / expected_tpl).is_file())

        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["name"] = "test-init-project"
        project["setup_status"] = "configured"
        project["commands"]["test"] = "python3 -c 'print(1)'"
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            doc = self.root / "docs" / name
            doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")

        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_init_foreign_file_conflict_stops_writes(self):
        """Конфликт чужого файла останавливает init до начала записи."""
        foreign = self.root / "docs" / "specs" / "index.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("чужой файл", encoding="utf-8")

        code, out, err = self.init("--apply")
        self.assertEqual(code, 1)
        self.assertIn("Конфликт: docs/specs/index.md", err)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "чужой файл")
        self.assertFalse((self.root / "ai-dev-playbook").exists())

    def test_init_archive_dir_conflict_leaves_clean_tree(self):
        """PB-028-A1: конфликт архивного пути (файл или symlink) отклоняется до записи без мусора на диске."""
        for kind in ("file", "symlink"):
            with tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                conflict = root / "docs/archive/specs"
                conflict.parent.mkdir(parents=True)
                outside = base / "outside"
                outside.mkdir()
                if kind == "file":
                    conflict.write_text("foreign archive file\n", encoding="utf-8")
                else:
                    conflict.symlink_to(outside, target_is_directory=True)

                before = tree_snapshot(root)
                code, out, err = run_cli("init", str(root), "--apply")
                self.assertEqual(code, 1)
                self.assertIn("docs/archive/specs", err)
                after = tree_snapshot(root)

                # Полное дерево до и после должно строго совпадать
                self.assertEqual(before, after, f"Остались лишние файлы/каталоги для {kind}: {set(after) - set(before)}")
                self.assertFalse((root / "ai-dev-playbook").exists())

    def test_real_002_artifact_update_with_custom_paths_and_evidence(self):
        """PB-028-A2: реальное обновление артефакта 0.0.2 до текущей версии с добавлением модулей и сохранением custom paths."""
        art_dir = tempfile.TemporaryDirectory()
        self.addCleanup(art_dir.cleanup)
        artifact_002 = build_authentic_002_artifact(art_dir.name)

        # 0. Проверяем целостность эталонного релизного артефакта 0.0.2 через updating.load_artifact
        meta = updating.load_artifact(artifact_002, playbook.safe_path, playbook.require)
        self.assertEqual(meta["version"], "0.0.2")

        # 1. Проверяем работоспособность исторического CLI 0.0.2 и устанавливаем проект его собственным кодом
        cli_002 = artifact_002 / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(cli_002), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"0.0.2 CLI --help failed: {res.stderr}")

        res = subprocess.run([sys.executable, str(cli_002), "init", str(self.root), "--apply"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"0.0.2 init failed: {res.stderr}")

        # Убеждаемся, что в 0.0.2 модулей документов физически не было
        self.assertFalse((self.root / "ai-dev-playbook" / "scripts" / "documents.py").exists())
        self.assertFalse((self.root / "ai-dev-playbook" / "scripts" / "lifecycle.py").exists())
        self.assertFalse((self.root / "ai-dev-playbook" / "templates" / "documents").exists())
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text().strip(), "0.0.2")

        # Проверяем исполняемость установленного CLI 0.0.2
        inst_cli_002 = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(inst_cli_002), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # 2. Настраиваем проект: custom paths, задача, исторический архив, доказательства, чужой файл
        project_path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["name"] = "real-002-project"
        project["setup_status"] = "configured"
        project["commands"]["test"] = "python3 -c 'print(1)'"
        project["paths"] = {
            "docs": "my_docs",
            "tasks": "my_docs/tasks",
            "archive": "my_docs/archive_tasks",
            "task_index": "my_docs/tasks/index.md"
        }
        project["custom_key"] = {"sub": 123}
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Переносим docs в my_docs и корректируем проектные инструкции AGENTS.md
        (self.root / "docs").rename(self.root / "my_docs")
        for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            doc = self.root / "my_docs" / name
            doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"), encoding="utf-8")
        agents_md = self.root / "AGENTS.md"
        agents_md.write_text(agents_md.read_text(encoding="utf-8").replace("docs/", "my_docs/"), encoding="utf-8")

        # Добавляем legacy карточку задачи
        write_card(self.root / "my_docs" / "tasks", "DEV-77", "in_progress")

        # Добавляем исторический архив задач
        arch_dir = self.root / "my_docs" / "archive_tasks"
        arch_dir.mkdir(parents=True, exist_ok=True)
        (arch_dir / "ARCH-1.md").write_text(
            "# ARCH-1: Историческая задача\n\nID: ARCH-1\nStatus: done\nPriority: P1\nDepends-On: -\n"
            "Executor: Old\nModel: GPT\nCreated: 2026-08-01T10:00:00+03:00\nClosed: 2026-08-02T10:00:00+03:00\n\n"
            "Историческая завершённая задача.\n",
            encoding="utf-8"
        )

        # Выполняем index через 0.0.2 CLI
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root), "index"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Добавляем evidence и посторонний скрипт
        ev_dir = self.root / "my_docs" / "tasks" / "DEV-77-evidence"
        ev_dir.mkdir(parents=True)
        (ev_dir / "test_run.log").write_text("proof of run\n", encoding="utf-8")

        foreign = self.root / "custom_script.py"
        foreign.write_text("print('foreign')\n", encoding="utf-8")

        # Проверяем проект через 0.0.2 CLI
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root), "check"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        before_tree = tree_snapshot(self.root)

        # 3. Планируем обновление на текущей версии (из REPO) через собственный установленный CLI 0.0.2
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root),
                              "update", "plan", str(REPO), "--save"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        out = res.stdout
        self.assertIn(f"0.0.2 → {REPO_VERSION}", out)
        self.assertIn("+ ai-dev-playbook/scripts/documents.py", out)
        self.assertIn("+ ai-dev-playbook/scripts/lifecycle.py", out)
        self.assertIn("+ ai-dev-playbook/templates/documents/spec.md", out)

        # 4. Применяем обновление через собственный установленный CLI 0.0.2
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root),
                              "update", "apply"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        # 5. Сверяем результат
        updated_project = json.loads(project_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_project["playbook_version"], REPO_VERSION)
        self.assertEqual(updated_project["paths"]["docs"], "my_docs")
        self.assertEqual(updated_project["paths"]["tasks"], "my_docs/tasks")
        self.assertEqual(updated_project["custom_key"], {"sub": 123})

        # Новые модули появились
        self.assertTrue((self.root / "ai-dev-playbook" / "scripts" / "documents.py").is_file())
        self.assertTrue((self.root / "ai-dev-playbook" / "scripts" / "lifecycle.py").is_file())
        self.assertTrue((self.root / "ai-dev-playbook" / "templates" / "documents" / "spec.md").is_file())
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text().strip(), REPO_VERSION)

        # Пользовательские файлы, исторический архив и доказательства в полной сохранности
        after_tree = tree_snapshot(self.root)
        protected = [p for p in before_tree if not p.startswith("ai-dev-playbook/") and not p.startswith("work/")]
        for p in protected:
            self.assertEqual(before_tree[p], after_tree.get(p), f"Пользовательский файл изменён: {p}")
        self.assertEqual((ev_dir / "test_run.log").read_text(encoding="utf-8"), "proof of run\n")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "print('foreign')\n")
        self.assertTrue((self.root / "my_docs" / "tasks" / "DEV-77.md").is_file())
        self.assertTrue((arch_dir / "ARCH-1.md").is_file())

        # manifest содержит новые файлы формата карточек и ни одного пользовательского файла
        manifest = self.manifest()
        self.assertEqual(manifest["playbook_version"], REPO_VERSION)
        self.assertIn("ai-dev-playbook/scripts/task_cards.py", manifest["files"])
        self.assertIn("ai-dev-playbook/rules/task-card-format.md", manifest["files"])
        for rel in manifest["files"]:
            self.assertTrue(rel.startswith("ai-dev-playbook/"), f"Чужой файл в manifest: {rel}")

        # check --ready проходит через обновлённый установленный CLI
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root),
                              "check", "--ready"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_unannounced_update_transition_rejected(self):
        """Необъявленный переход отклоняется до записи."""
        self.install(version="0.0.2")
        release = self.fixture_release(
            version="0.0.4",
            updates_from={
                "0.0.3": {"steps": []}
            }
        )
        code, out, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("Неподдерживаемый переход 0.0.2 → 0.0.4", err)

    def test_real_sigkill_interrupted_update_locks_lifecycle_and_cli_restores(self):
        """PB-028-A2: реальное прерывание update (SIGKILL mid-write) блокирует lifecycle и восстанавливается через CLI update restore."""
        art_dir = tempfile.TemporaryDirectory()
        self.addCleanup(art_dir.cleanup)
        artifact_002 = build_authentic_002_artifact(art_dir.name)

        cli_002 = artifact_002 / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(cli_002), "init", str(self.root), "--apply"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Добавляем исторический архив и evidence
        arch = self.root / "docs" / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "OLD.md").write_text("# OLD: test\nID: OLD\nStatus: done\nPriority: P1\nDepends-On: -\n", encoding="utf-8")
        ev = self.root / "docs" / "tasks" / "DEV-1-evidence"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "run.log").write_text("proof\n", encoding="utf-8")

        release = self.fixture_release()
        inst_cli_002 = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root),
                              "update", "plan", str(release), "--save"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        before = tree_snapshot(self.root)

        # Фатальное прерывание SIGKILL во время применения обновления
        child_code = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import updating

calls = []
orig = updating._write_file
def killer(source, target):
    calls.append(1)
    if len(calls) == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(source, target)

updating._write_file = killer
import playbook
playbook.main(["--root", {root!r}, "update", "apply"])
'''.format(scripts=str(SCRIPTS), root=str(self.root))

        res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
        self.assertEqual(res.returncode, -signal.SIGKILL)

        # Проверяем, что обновление действительно находится в прерванном состоянии
        pending = self.root / "work" / "update" / "pending.json"
        self.assertTrue(pending.exists())

        # 1. CLI lifecycle блокируется прерванным обновлением
        code, _, err = self.cli("lifecycle", "restore")
        self.assertEqual(code, 1)
        self.assertIn("обновление", err)

        # 2. Автономный restore.py lifecycle блокируется прерванным обновлением
        dummy_life_op = self.root / "work" / "lifecycle" / "life-1"
        dummy_life_op.mkdir(parents=True, exist_ok=True)
        (dummy_life_op / "restore.py").write_text(fops.RESTORE_SCRIPT, encoding="utf-8")
        (dummy_life_op / "backup_index.json").write_text("{}", encoding="utf-8")
        res = subprocess.run([sys.executable, str(dummy_life_op / "restore.py")],
                             capture_output=True, text=True, cwd=self.root)
        self.assertEqual(res.returncode, 1)
        self.assertIn("обновления", res.stderr)

        # 3. CLI update restore восстанавливает пред-образ
        code, out, err = self.cli("update", "restore")
        self.assertEqual(code, 0, err + out)
        self.assertFalse(pending.exists())

        # Побайтовое совпадение всех файлов до и после прерывания (вне каталога work)
        after = tree_snapshot(self.root)
        for rel, digest in before.items():
            if rel.startswith("work/"):
                continue
            self.assertEqual(after.get(rel), digest, f"Файл {rel} не совпадает побайтно после restore")
        for rel in set(after) - set(before):
            self.assertTrue(rel.startswith("work/"), f"Лишний не-work файл: {rel}")
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text().strip(), "0.0.2")

        # Работоспособность восстановленного 0.0.2 CLI подтверждена
        restored_cli = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(restored_cli), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run([sys.executable, str(restored_cli), "--root", str(self.root), "check"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_real_sigkill_interrupted_update_standalone_script_restores(self):
        """PB-028-A2: автономный restore.py восстанавливает реальное SIGKILL-прерывание update."""
        art_dir = tempfile.TemporaryDirectory()
        self.addCleanup(art_dir.cleanup)
        artifact_002 = build_authentic_002_artifact(art_dir.name)

        cli_002 = artifact_002 / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(cli_002), "init", str(self.root), "--apply"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Добавляем исторический архив и evidence
        arch = self.root / "docs" / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "OLD.md").write_text("# OLD: test\nID: OLD\nStatus: done\nPriority: P1\nDepends-On: -\n", encoding="utf-8")
        ev = self.root / "docs" / "tasks" / "DEV-1-evidence"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "run.log").write_text("proof\n", encoding="utf-8")

        release = self.fixture_release()
        inst_cli_002 = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(inst_cli_002), "--root", str(self.root),
                              "update", "plan", str(release), "--save"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        before = tree_snapshot(self.root)

        child_code = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import updating

calls = []
orig = updating._write_file
def killer(source, target):
    calls.append(1)
    if len(calls) == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(source, target)

updating._write_file = killer
import playbook
playbook.main(["--root", {root!r}, "update", "apply"])
'''.format(scripts=str(SCRIPTS), root=str(self.root))

        res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
        self.assertEqual(res.returncode, -signal.SIGKILL)

        (op_dir,) = list((self.root / "work" / "update").glob("20*"))
        restore_py = op_dir / "restore.py"
        self.assertTrue(restore_py.is_file())

        # Запуск автономного скрипта восстановления
        res = subprocess.run([sys.executable, str(restore_py)],
                             capture_output=True, text=True, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.root / "work" / "update" / "pending.json").exists())
        self.assertEqual((self.root / "ai-dev-playbook" / "VERSION").read_text().strip(), "0.0.2")

        # Побайтовое совпадение всех файлов до и после прерывания (вне каталога work)
        after = tree_snapshot(self.root)
        for rel, digest in before.items():
            if rel.startswith("work/"):
                continue
            self.assertEqual(after.get(rel), digest, f"Файл {rel} не совпадает побайтно после restore")
        for rel in set(after) - set(before):
            self.assertTrue(rel.startswith("work/"), f"Лишний не-work файл: {rel}")

        # Работоспособность восстановленного 0.0.2 CLI подтверждена
        restored_cli = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        res = subprocess.run([sys.executable, str(restored_cli), "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = subprocess.run([sys.executable, str(restored_cli), "--root", str(self.root), "check"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_init_absent_destination_creates_target_directory(self):
        """PB-028-A3: init успешно устанавливает комплект в отсутствующий целевой каталог."""
        target = self.root / "brand-new-project"
        self.assertFalse(target.exists())
        code, out, err = self.cli("init", str(target), "--apply")
        self.assertEqual(code, 0, err)
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "ai-dev-playbook" / "VERSION").is_file())
        code, _, err = self.cli("--root", str(target), "check")
        self.assertEqual(code, 0, err)

    def test_init_nested_absent_destination_creates_parents(self):
        """PB-028-A3: init создаёт вложенные отсутствующие родительские каталоги целевого пути."""
        target = self.root / "nested" / "sub" / "deep-project"
        self.assertFalse((self.root / "nested").exists())
        code, out, err = self.cli("init", str(target), "--apply")
        self.assertEqual(code, 0, err)
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "ai-dev-playbook" / "VERSION").is_file())
        code, _, err = self.cli("--root", str(target), "check")
        self.assertEqual(code, 0, err)

    def test_init_absent_destination_rollback_on_failure_cleans_up_created_dirs(self):
        """PB-028-A3: при ошибке записи в отсутствующий целевой каталог все созданные каталоги очищаются."""
        target = self.root / "fail_parent" / "fail_child"
        self.assertFalse((self.root / "fail_parent").exists())

        child_code = r'''
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
'''.format(scripts=str(SCRIPTS), target=str(target))

        res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("Simulated write failure", res.stderr)
        self.assertFalse((self.root / "fail_parent").exists(), "fail_parent не был удален при rollback")

    def test_real_sigkill_interrupted_lifecycle_locks_update_and_restores(self):
        """PB-028-A2: реальное SIGKILL-прерывание lifecycle блокирует update plan/apply/restore и восстанавливается автономно."""
        self.install(version="0.0.2")

        # Проводим операцию lifecycle с реальным прерыванием SIGKILL mid-write
        (self.root / "docs" / "a.md").write_text("OLD-A\n", encoding="utf-8")
        (self.root / "docs" / "b.md").write_text("OLD-B\n", encoding="utf-8")

        child_code = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import file_operations as fops
import playbook

calls = []
orig = fops._atomic_write
def killer(target, content, op_id):
    calls.append(1)
    if len(calls) == 1:
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(target, content, op_id)

fops._atomic_write = killer
root = Path({root!r})
inputs = fops.snapshot_paths(root, ["docs/a.md", "docs/b.md"], playbook.safe_path, playbook.require)
spec = {{"operation": "probe_kill", "plan_id": "pk-1", "actor": "tester",
        "writes": {{"docs/a.md": b"NEW-A\n"}}, "removes": ["docs/b.md"], "inputs": inputs}}
fops.run_operation(root, spec, playbook.safe_path, playbook.require)
'''.format(scripts=str(SCRIPTS), root=str(self.root))

        res = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True)
        self.assertEqual(res.returncode, -signal.SIGKILL)

        # Проверяем наличие маркера lifecycle
        life_pending = self.root / "work" / "lifecycle" / "pending.json"
        self.assertTrue(life_pending.exists())

        release = self.fixture_release()

        # 1. CLI update plan блокируется
        code, _, err = self.cli("update", "plan", str(release))
        self.assertEqual(code, 1)
        self.assertIn("lifecycle", err)

        # 2. CLI update restore блокируется
        code, _, err = self.cli("update", "restore")
        self.assertEqual(code, 1)
        self.assertIn("lifecycle", err)

        # 3. Автономный restore.py update блокируется
        dummy_upd_op = self.root / "work" / "update" / "upd-1"
        dummy_upd_op.mkdir(parents=True, exist_ok=True)
        (dummy_upd_op / "restore.py").write_text(updating.RESTORE_SCRIPT, encoding="utf-8")
        (dummy_upd_op / "backup_index.json").write_text("{}", encoding="utf-8")
        res = subprocess.run([sys.executable, str(dummy_upd_op / "restore.py")],
                             capture_output=True, text=True, cwd=self.root)
        self.assertEqual(res.returncode, 1)
        self.assertIn("документов", res.stderr)

        # 4. Автономный restore.py lifecycle восстанавливает состояние
        (life_op,) = list((self.root / "work" / "lifecycle").glob("20*"))
        res = subprocess.run([sys.executable, str(life_op / "restore.py")],
                             capture_output=True, text=True, cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(life_pending.exists())
        self.assertEqual((self.root / "docs" / "a.md").read_text(encoding="utf-8"), "OLD-A\n")
        self.assertEqual((self.root / "docs" / "b.md").read_text(encoding="utf-8"), "OLD-B\n")


if __name__ == "__main__":
    unittest.main()
