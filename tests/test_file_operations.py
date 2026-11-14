import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path

from test_cli import CliCase, run_cli
from test_playbook import playbook, write_card

fops = playbook.file_operations
SCRIPTS = Path(playbook.__file__).resolve().parent


def engine_cli(root, *argv):
    """playbook.py как subprocess — для проверок блокировок между процессами."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "playbook.py"), "--root", str(root), *argv],
        capture_output=True, text=True)


class EngineCase(unittest.TestCase):
    """Минимальный проект без комплекта: движок не требует установки."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "a.md").write_text("OLD-A\n", encoding="utf-8")
        (self.root / "docs" / "b.md").write_text("OLD-B\n", encoding="utf-8")
        self._env_token = os.environ.pop(fops.GATE_ENV, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env_token is not None:
            os.environ[fops.GATE_ENV] = self._env_token

    def spec(self, **overrides):
        touched = ["docs/a.md", "docs/b.md", "docs/new/x.md",
                   "docs/old/c.md", "docs/old"]
        spec = {
            "operation": "test_op", "plan_id": "plan-1", "actor": "test",
            "writes": {"docs/a.md": b"NEW-A\n", "docs/new/x.md": b"NEW-X\n"},
            "removes": ["docs/b.md"],
            "inputs": fops.snapshot_paths(self.root, touched,
                                          playbook.safe_path, playbook.require),
        }
        spec.update(overrides)
        return spec

    def apply(self, spec):
        return fops.run_operation(self.root, spec, playbook.safe_path,
                                  playbook.require)

    def op_dirs(self):
        base = self.root / fops.WORK_DIR
        return sorted(path for path in base.iterdir() if path.is_dir()) \
            if base.is_dir() else []

    def journal(self, op_dir):
        return [json.loads(line) for line in
                (op_dir / "journal.jsonl").read_text("utf-8").splitlines()]

    def _interrupted_op(self):
        def crash(root):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(final_checks=[crash]))
        return self.op_dirs()[0]


class EngineTest(EngineCase):
    """Engine file_operations: apply/reapply, перепроверка inputs,
    staged/final checks, recovery."""
    def test_apply_writes_removes_and_creates_dirs(self):
        """apply пишет, удаляет и создаёт каталоги по спецификации"""
        result = self.apply(self.spec())
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"NEW-A\n")
        self.assertEqual((self.root / "docs/new/x.md").read_bytes(), b"NEW-X\n")
        self.assertFalse((self.root / "docs/b.md").exists())
        self.assertIsNone(fops.pending_info(self.root))
        op_dir = self.root / result["op_dir"]
        index = json.loads((op_dir / "backup_index.json").read_text("utf-8"))
        self.assertEqual(index["op_id"], op_dir.name)
        self.assertEqual(sorted(index["backed"]), ["docs/a.md", "docs/b.md"])
        self.assertEqual(index["added"], ["docs/new/x.md"])
        self.assertEqual(index["created_dirs"], ["docs/new"])
        self.assertTrue((op_dir / "backup/docs/a.md").is_file())
        self.assertTrue((op_dir / "restore.py").is_file())
        receipt = json.loads((op_dir / "receipt.json").read_text("utf-8"))
        self.assertEqual((receipt["result"], receipt["plan_id"]),
                         ("completed", "plan-1"))
        events = [row["event"] for row in self.journal(op_dir)]
        self.assertEqual(events[0], "start")
        self.assertIn("backup_complete", events)
        self.assertIn("rechecked", events)
        self.assertEqual(events[-2:], ["verified", "done"])
        self.assertLess(events.index("rechecked"), events.index("write"))

    def test_repeated_plan_is_idempotent(self):
        """Повторный apply того же плана идемпотентен"""
        first = self.apply(self.spec())
        second = self.apply(self.spec())
        self.assertEqual(second["status"], "already_completed")
        self.assertEqual(second["op_id"], first["op_id"])
        self.assertEqual(len(self.op_dirs()), 1)

    def test_remove_dirs(self):
        """apply удаляет каталоги рекурсивно"""
        (self.root / "docs/old").mkdir()
        (self.root / "docs/old/c.md").write_text("OLD-C\n", encoding="utf-8")
        spec = self.spec(writes={}, removes=["docs/old/c.md"],
                         remove_dirs=["docs/old"])
        result = self.apply(spec)
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.root / "docs/old").exists())

    def test_spec_validation_rejects_bad_input(self):
        """Невалидная спецификация операции отклоняется до записи"""
        base = self.spec()
        cases = [
            dict(base, unknown_field=1),
            dict(base, writes={"../escape.md": b"x"}),
            dict(base, writes={"/abs.md": b"x"}),
            dict(base, writes={"docs/a.md": "not-bytes"}),
            dict(base, writes={"docs/a.md": b"x"}, removes=["docs/a.md"]),
            dict(base, writes={"work/lifecycle/x": b"x"}),
            dict(base, writes={"work/gate.lock": b"x"}),
            dict(base, remove_dirs=["docs"], writes={"docs/x.md": b"x"}),
            dict(base, writes={}, removes=[]),
            dict(base, inputs={}),
            dict(base, inputs={"docs/a.md": "bogus"}),
            dict(base, staged_checks="not-a-list"),
            dict(base, removes=["docs"]),  # каталог в removes — не файл
        ]
        for spec in cases:
            with self.subTest(spec=str(sorted(spec))):
                with self.assertRaises((ValueError, TypeError)):
                    self.apply(spec)
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertEqual(self.op_dirs(), [])

    def test_inputs_rechecked_before_write(self):
        """Входы перепроверяются перед записью — чужое изменение отклоняет"""
        spec = self.spec()
        (self.root / "docs/b.md").write_text("FOREIGN\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Входы изменились"):
            self.apply(spec)
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertEqual((self.root / "docs/b.md").read_bytes(), b"FOREIGN\n")
        self.assertIsNone(fops.pending_info(self.root))

    def test_staged_check_failure_aborts_before_write(self):
        """Сбой staged check отменяет apply до записи"""
        def check(staged):
            playbook.require((staged / "docs/new/x.md").is_file()
                             and not (staged / "docs/b.md").exists(),
                             "staged composition")
            playbook.require(False, "staged check failed")
        with self.assertRaisesRegex(ValueError, "staged check failed"):
            self.apply(self.spec(staged_checks=[check]))
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertTrue((self.root / "docs/b.md").exists())
        self.assertFalse((self.root / "docs/new").exists())
        self.assertIsNone(fops.pending_info(self.root))
        self.assertEqual(self.op_dirs(), [])  # no backup: no durable operation history

    def test_final_check_failure_restores_preimage(self):
        """Сбой final check восстанавливает пред-образ"""
        os.chmod(self.root / "docs/a.md", 0o640)
        def check(root):
            playbook.require(False, "post-write check failed")
        with self.assertRaisesRegex(ValueError, "восстановлено"):
            self.apply(self.spec(final_checks=[check]))
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertEqual(stat.S_IMODE((self.root / "docs/a.md").stat().st_mode),
                         0o640)
        self.assertTrue((self.root / "docs/b.md").exists())
        self.assertFalse((self.root / "docs/new").exists())
        self.assertIsNone(fops.pending_info(self.root))
        events = [row["event"] for row in self.journal(self.op_dirs()[0])]
        self.assertEqual(events[-2:], ["failed", "restored"])

    def test_write_error_mid_apply_rolls_back(self):
        """Ошибка записи в середине apply откатывает изменения"""
        original = fops._atomic_write
        calls = []

        def flaky(target, content, op_id):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("injected write failure")
            return original(target, content, op_id)

        saved = fops._atomic_write
        fops._atomic_write = flaky
        try:
            with self.assertRaisesRegex(ValueError, "восстановлено"):
                self.apply(self.spec())
        finally:
            fops._atomic_write = saved
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertTrue((self.root / "docs/b.md").exists())
        self.assertIsNone(fops.pending_info(self.root))

    def test_interrupted_operation_restored_by_engine(self):
        """Прерванная операция восстанавливается движком"""
        def crash(root):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(final_checks=[crash]))
        pending = fops.pending_info(self.root)
        self.assertIsNotNone(pending)
        result = fops.restore_operation(self.root, playbook.safe_path,
                                        playbook.require)
        self.assertEqual(result["status"], "restored")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertTrue((self.root / "docs/b.md").exists())
        self.assertFalse((self.root / "docs/new").exists())
        self.assertIsNone(fops.pending_info(self.root))

    def test_pending_blocks_new_operation(self):
        """Pending-маркер блокирует новую операцию"""
        def crash(root):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(final_checks=[crash]))
        with self.assertRaisesRegex(ValueError, "Незавершённая операция"):
            self.apply(self.spec(plan_id="plan-2"))

    def test_restore_without_pending_refused(self):
        """restore без pending отклоняется"""
        with self.assertRaisesRegex(ValueError, "Нет незавершённой"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)

    def test_late_external_edit_preserved_as_conflict(self):
        """Поздняя внешняя правка сохраняется как конфликт при restore"""
        op_dir = self._interrupted_op()
        # Посторонняя правка после прерывания: не совпадает ни с пред-образом,
        # ни с планом — сохраняется в conflicts/, пред-образ возвращается.
        (self.root / "docs/a.md").write_text("LATE-EDIT\n", encoding="utf-8")
        result = fops.restore_operation(self.root, playbook.safe_path,
                                        playbook.require)
        self.assertIn("docs/a.md", result["conflicts"])
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertEqual((op_dir / "conflicts/docs/a.md").read_bytes(),
                         b"LATE-EDIT\n")
        self.assertIsNone(fops.pending_info(self.root))

    def test_restore_rejects_corrupt_backup_copy(self):
        """Restore отклоняет повреждённую копию backup"""
        op_dir = self._interrupted_op()
        (op_dir / "backup/docs/a.md").write_text("CORRUPT\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "повреждена"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_restore_rejects_incomplete_backup_index(self):
        """Restore отклоняет неполный backup_index"""
        op_dir = self._interrupted_op()
        index = json.loads((op_dir / "backup_index.json").read_text("utf-8"))
        del index["hashes"]["docs/b.md"]
        (op_dir / "backup_index.json").write_text(json.dumps(index),
                                                  encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "неполна|hashes"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_restore_rejects_foreign_pending(self):
        """Restore отклоняет чужой pending-маркер"""
        op_dir = self._interrupted_op()
        marker = self.root / fops.PENDING_FILE
        data = json.loads(marker.read_text("utf-8"))
        data["op_id"] = "other-operation"
        marker.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "чужой каталог"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_restore_refuses_live_operation(self):
        """Restore отклоняется при живой операции"""
        self._interrupted_op()
        lock = fops._operation_lock(self.root, playbook.safe_path)
        self.assertIsNotNone(lock)
        try:
            with self.assertRaisesRegex(ValueError, "другим процессом"):
                fops.restore_operation(self.root, playbook.safe_path,
                                       playbook.require)
        finally:
            lock.close()
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_pre_write_interruption_clears_marker(self):
        # KeyboardInterrupt в staged-проверке: маркер остаётся, записи не было.
        """Прерывание до записи очищает pending-маркер"""
        def crash(staged):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(staged_checks=[crash]))
        op_dir = self.op_dirs()[0]
        self.assertFalse((op_dir / "backup_index.json").exists())
        result = fops.restore_operation(self.root, playbook.safe_path,
                                        playbook.require)
        self.assertEqual(result["status"], "aborted_before_write")
        self.assertIsNone(fops.pending_info(self.root))

    def test_missing_index_with_write_journal_refuses(self):
        # Индекс удалён внешне ПОСЛЕ записи: журнал показывает события write —
        # восстановление отклоняется, маркер сохраняется.
        """Отсутствие индекса при журнале записи отклоняет restore"""
        op_dir = self._interrupted_op()
        (op_dir / "backup_index.json").unlink()
        with self.assertRaisesRegex(ValueError, "пред-образ утрачен"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))

    # PB-023-A1: файл с похожим именем temp не перезаписывается операцией
    def test_foreign_temp_like_file_untouched(self):
        """Чужой temp-подобный файл не трогается"""
        stray = self.root / "docs/a.md.fop-tmp"
        stray.write_bytes(b"USER DATA")
        result = self.apply(self.spec())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(stray.read_bytes(), b"USER DATA")

    # PB-023-A1: остаток temp этой операции убирается restore, чужой — нет
    def test_own_temp_leftover_cleaned_by_restore(self):
        """Свой temp-остаток очищается restore"""
        op_dir = self._interrupted_op()
        stray = self.root / "docs" / f".a.md.fop-{op_dir.name}-deadbeef.tmp"
        stray.write_bytes(b"PARTIAL")
        foreign = self.root / "docs" / ".a.md.fop-otherop9-deadbeef.tmp"
        foreign.write_bytes(b"FOREIGN")
        fops.restore_operation(self.root, playbook.safe_path,
                               playbook.require)
        self.assertFalse(stray.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")

    # PB-023-A2 (повторный аудит): kill restore до нейтрализации receipt —
    # повтор плана обязан отказаться по pending, а не «already_completed»
    def test_restore_kill_before_neutralize_blocks_retry(self):
        """Kill до нейтрализации блокирует повторный restore"""
        saved_release = fops._release_pending
        fops._release_pending = lambda *a, **k: (_ for _ in ()).throw(
            KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.apply(self.spec())
        finally:
            fops._release_pending = saved_release
        op_dir = self.op_dirs()[0]
        self.assertEqual(json.loads(
            (op_dir / "receipt.json").read_text("utf-8"))["result"],
            "completed")
        # restore прерван до нейтрализации квитанции
        saved_neutral = fops._neutralize_receipt
        fops._neutralize_receipt = lambda d: (_ for _ in ()).throw(
            KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                fops.restore_operation(self.root, playbook.safe_path,
                                       playbook.require)
        finally:
            fops._neutralize_receipt = saved_neutral
        self.assertIsNotNone(fops.pending_info(self.root))
        # pending исключает «успешный» ответ: повтор отклоняется
        with self.assertRaisesRegex(ValueError, "Незавершённая"):
            self.apply(self.spec())
        # завершённый restore нейтрализует квитанцию; повтор применяет план
        fops.restore_operation(self.root, playbook.safe_path,
                               playbook.require)
        rerun = self.apply(self.spec())
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"NEW-A\n")

    # PB-023-A2 (третий аудит): квитанция читается только под gate, после
    # проверки pending — restore не может вклиниться между чтением и ответом
    def test_completed_decision_under_gate_after_pending(self):
        """Решение completed под gate после pending"""
        self.apply(self.spec())
        calls = []
        saved = (fops.acquire_gate, fops.pending_info, fops.find_completed)

        def gate_probe(*a, **k):
            calls.append("gate")
            return saved[0](*a, **k)

        def pending_probe(*a, **k):
            calls.append("pending")
            return saved[1](*a, **k)

        def completed_probe(*a, **k):
            # пока повтор держит gate, конкурирующая команда отклоняется
            competing = engine_cli(self.root, "index")
            calls.append("gate-held" if competing.returncode != 0
                         else "gate-free")
            calls.append("find_completed")
            return saved[2](*a, **k)

        fops.acquire_gate, fops.pending_info, fops.find_completed = \
            gate_probe, pending_probe, completed_probe
        try:
            result = self.apply(self.spec())
        finally:
            (fops.acquire_gate, fops.pending_info,
             fops.find_completed) = saved
        self.assertEqual(result["status"], "already_completed")
        self.assertEqual(calls,
                         ["gate", "pending", "gate-held", "find_completed"])

    # PB-023-A2: kill после receipt до снятия маркера — откат + квитанция
    # перестаёт считаться применением
    def test_receipt_after_rollback_is_not_completed(self):
        """Receipt после отката не считается completed"""
        saved = fops._release_pending
        fops._release_pending = lambda *a, **k: (_ for _ in ()).throw(
            KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.apply(self.spec())
        finally:
            fops._release_pending = saved
        op_dir = self.op_dirs()[0]
        receipt = json.loads((op_dir / "receipt.json").read_text("utf-8"))
        self.assertEqual(receipt["result"], "completed")
        fops.restore_operation(self.root, playbook.safe_path,
                               playbook.require)
        receipt = json.loads((op_dir / "receipt.json").read_text("utf-8"))
        self.assertEqual(receipt["result"], "restored")
        rerun = self.apply(self.spec())
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"NEW-A\n")

    # PB-023-A3: журнал прошёл точку backup — отсутствие индекса не «до записи»
    def test_missing_index_after_backup_refuses(self):
        """Отсутствие индекса после backup отклоняет restore"""
        def crash(staged):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(staged_checks=[crash]))
        op_dir = self.op_dirs()[0]
        with (op_dir / "journal.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ts": "x", "event": "backup_complete"})
                         + "\n")
        with self.assertRaisesRegex(ValueError, "пред-образ утрачен"):
            fops.restore_operation(self.root, playbook.safe_path,
                                   playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))

    # PB-023-A4: откат восстанавливает режим удалённого каталога
    def test_removed_dir_mode_restored_on_rollback(self):
        """Права удалённого каталога восстанавливаются при откате"""
        directory = self.root / "docs" / "private"
        directory.mkdir()
        (directory / "x.md").write_text("x\n", encoding="utf-8")
        os.chmod(directory, 0o700)
        inputs = fops.snapshot_paths(self.root, ["docs/private",
                                                 "docs/private/x.md"],
                                     playbook.safe_path, playbook.require)

        def boom(root):
            playbook.require(False, "post-write failure")
        with self.assertRaisesRegex(ValueError, "восстановлено"):
            self.apply(self.spec(writes={}, removes=["docs/private/x.md"],
                                 remove_dirs=["docs/private"], inputs=inputs,
                                 final_checks=[boom]))
        self.assertTrue(directory.is_dir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_removed_dir_mode_restored_by_engine_restore(self):
        """Права удалённого каталога восстанавливаются engine restore"""
        directory = self.root / "docs" / "private"
        directory.mkdir()
        (directory / "x.md").write_text("x\n", encoding="utf-8")
        os.chmod(directory, 0o700)
        inputs = fops.snapshot_paths(self.root, ["docs/private",
                                                 "docs/private/x.md"],
                                     playbook.safe_path, playbook.require)

        def crash(root):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(self.spec(writes={}, removes=["docs/private/x.md"],
                                 remove_dirs=["docs/private"], inputs=inputs,
                                 final_checks=[crash]))
        self.assertFalse(directory.exists())
        fops.restore_operation(self.root, playbook.safe_path,
                               playbook.require)
        self.assertTrue(directory.is_dir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual((directory / "x.md").read_bytes(), b"x\n")

    def test_snapshot_marks_missing_and_dir(self):
        """Snapshot помечает отсутствующие пути и каталоги"""
        (self.root / "docs/old").mkdir()
        snap = fops.snapshot_paths(self.root, ["docs/a.md", "docs/old",
                                               "docs/absent.md"],
                                   playbook.safe_path, playbook.require)
        self.assertTrue(snap["docs/a.md"].startswith("sha256:"))
        self.assertEqual(snap["docs/old"], "dir")
        self.assertEqual(snap["docs/absent.md"], "missing")


class SubprocessTest(EngineCase):
    """Прерывание реальным SIGKILL и автономный restore.py без импортов."""

    CHILD = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
import file_operations as fops
import playbook
root = Path({root!r})
hook = {hook!r}
if hook == "write":
    calls = []
    original = fops._atomic_write
    def killer(target, content, op_id):
        calls.append(1)
        if len(calls) == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        return original(target, content, op_id)
    fops._atomic_write = killer
elif hook == "replace":
    original_replace = Path.replace
    def kill_on_replace(self, target):
        if ".fop-" in self.name and self.name.endswith(".tmp"):
            os.kill(os.getpid(), signal.SIGKILL)
        return original_replace(self, target)
    Path.replace = kill_on_replace
elif hook == "receipt":
    fops._release_pending = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
elif hook == "staged":
    def boom(staged):
        os.kill(os.getpid(), signal.SIGKILL)
else:
    boom = None
inputs = fops.snapshot_paths(root, ["docs/a.md", "docs/b.md", "docs/new/x.md"],
                             playbook.safe_path, playbook.require)
spec = {{"operation": "kill_test", "plan_id": "kill-1", "actor": "test",
        "writes": {{"docs/a.md": b"NEW-A\n", "docs/new/x.md": b"NEW-X\n"}},
        "removes": ["docs/b.md"], "inputs": inputs}}
if hook == "staged":
    spec["staged_checks"] = [boom]
fops.run_operation(root, spec, playbook.safe_path, playbook.require)
'''

    def kill_child(self, hook):
        child = self.CHILD.format(scripts=str(SCRIPTS), root=str(self.root),
                                  hook=hook)
        result = subprocess.run([sys.executable, "-c", child],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, -9, result.stderr)
        return self.op_dirs()[0]

    def run_restore(self, op_dir):
        return subprocess.run([sys.executable, str(op_dir / "restore.py")],
                              capture_output=True, text=True)

    def test_sigkill_mid_write_then_standalone_restore(self):
        """SIGKILL посреди записи → автономный restore"""
        op_dir = self.kill_child("write")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"NEW-A\n")
        self.assertIsNotNone(fops.pending_info(self.root))
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("маркер снят", result.stdout)
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertTrue((self.root / "docs/b.md").exists())
        self.assertFalse((self.root / "docs/new").exists())
        self.assertIsNone(fops.pending_info(self.root))
        # Повторный restore — безопасный отказ
        again = self.run_restore(op_dir)
        self.assertEqual(again.returncode, 1)
        self.assertIn("уже выполнено", again.stderr)

    def test_sigkill_before_backup_clears_marker(self):
        """SIGKILL до backup очищает pending-маркер"""
        op_dir = self.kill_child("staged")
        self.assertIsNotNone(fops.pending_info(self.root))
        self.assertFalse((op_dir / "backup_index.json").exists())
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("прервана до записи", result.stdout)
        self.assertIsNone(fops.pending_info(self.root))
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")

    def test_standalone_restore_needs_no_kit(self):
        """Автономный restore работает без установленного комплекта"""
        op_dir = self.kill_child("write")
        # Комплект в проекте отсутствует изначально: restore.py самодостаточен.
        self.assertFalse((self.root / "ai-dev-playbook").exists())
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_standalone_restore_refuses_live_operation(self):
        """Автономный restore отклоняется при живой операции"""
        op_dir = self.kill_child("write")
        lock = fops._operation_lock(self.root, playbook.safe_path)
        self.assertIsNotNone(lock)
        try:
            result = self.run_restore(op_dir)
        finally:
            lock.close()
        self.assertEqual(result.returncode, 1)
        self.assertIn("выполняется", result.stderr)
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_standalone_restore_refuses_foreign_marker(self):
        """Автономный restore отклоняет чужой маркер"""
        op_dir = self.kill_child("write")
        marker = self.root / fops.PENDING_FILE
        data = json.loads(marker.read_text("utf-8"))
        data["op_id"] = "other-operation"
        marker.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("другой операции", result.stderr)
        self.assertIsNotNone(fops.pending_info(self.root))

    # PB-023-A1 (повторный аудит): standalone сохраняет чужой файл, чьё имя
    # лишь содержит .fop-<op_id>-, но не является temp-именем операции
    def test_standalone_restore_preserves_foreign_fop_name(self):
        """Автономный restore сохраняет чужой fop-файл"""
        op_dir = self._interrupted_op()
        foreign = self.root / "docs" / f".notes.fop-{op_dir.name}-manual.tmp"
        foreign.write_bytes(b"FOREIGN")
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(foreign.read_bytes(), b"FOREIGN")

    # PB-023-A1: kill между созданием temp и replace — restore убирает остаток
    def test_sigkill_during_replace_cleans_own_temp(self):
        """SIGKILL при replace очищает свой temp-файл"""
        op_dir = self.kill_child("replace")
        temps = list((self.root / "docs").glob("*.fop-*.tmp"))
        self.assertTrue(temps)
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "docs").glob("*.fop-*.tmp")), [])
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        self.assertIsNone(fops.pending_info(self.root))

    # PB-023-A2: kill после receipt — restore возвращает пред-образ,
    # квитанция перестаёт считаться применением, повторный запуск работает
    def test_sigkill_after_receipt_then_rerun(self):
        """SIGKILL после receipt → повторный запуск"""
        op_dir = self.kill_child("receipt")
        self.assertIsNotNone(fops.pending_info(self.root))
        receipt = json.loads((op_dir / "receipt.json").read_text("utf-8"))
        self.assertEqual(receipt["result"], "completed")
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((op_dir / "receipt.json").read_text("utf-8"))
        self.assertEqual(receipt["result"], "restored")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"OLD-A\n")
        rerun = self.apply(self.spec(plan_id="kill-1"))
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual((self.root / "docs/a.md").read_bytes(), b"NEW-A\n")

    # PB-023-A3: kill после backup и удаление индекса — отказ, маркер остаётся
    def test_sigkill_lost_index_refuses_restore(self):
        """SIGKILL с потерянным индексом отклоняет restore"""
        op_dir = self.kill_child("write")
        (op_dir / "backup_index.json").unlink()
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("утрачен", result.stderr)
        self.assertIsNotNone(fops.pending_info(self.root))

    def test_standalone_restore_refuses_corrupt_backup(self):
        """Автономный restore отклоняет повреждённый backup"""
        op_dir = self.kill_child("write")
        (op_dir / "backup/docs/b.md").write_text("CORRUPT\n", encoding="utf-8")
        result = self.run_restore(op_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("повреждена", result.stderr)
        self.assertIsNotNone(fops.pending_info(self.root))


class GateTest(CliCase):
    """Общий gate и блокировка команд по pending-маркерам (CLI-уровень)."""

    def setUp(self):
        super().setUp()
        self._env_token = os.environ.pop(fops.GATE_ENV, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env_token is not None:
            os.environ[fops.GATE_ENV] = self._env_token

    def gate(self):
        return fops.acquire_gate(self.root, playbook.safe_path,
                                 playbook.require, owner="test")

    def test_mutating_commands_share_gate(self):
        """Мутирующие команды разделяют общий gate"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        self.cli("index")
        gate = self.gate()
        try:
            for argv in (("index",), ("close", "DEV-1"),
                         ("verify", "DEV-1"), ("adopt", ".", "--apply"),
                         ("update", "apply"), ("update", "plan", ".", "--save")):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 1, argv)
                self.assertIn("блокировку", err, argv)
        finally:
            gate.close()
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def test_read_only_check_while_gate_held(self):
        """Read-only check работает при удерживаемом gate"""
        self.install()
        gate = self.gate()
        try:
            code, _, err = self.cli("check", "--ready")
            self.assertEqual(code, 0, err)
        finally:
            gate.close()

    def test_nested_token_allows_child(self):
        """Вложенный токен разрешает дочернюю команду"""
        self.install()
        gate = self.gate()
        try:
            os.environ[fops.GATE_ENV] = gate.token
            code, _, err = self.cli("index")
            self.assertEqual(code, 0, err)
        finally:
            os.environ.pop(fops.GATE_ENV, None)
            gate.close()

    def test_stale_token_does_not_bypass_busy_gate(self):
        """Устаревший токен не обходит занятый gate"""
        self.install()
        os.environ[fops.GATE_ENV] = "stale-token"
        gate = self.gate()
        try:
            code, _, err = self.cli("index")
            self.assertEqual(code, 1)
            self.assertIn("блокировку", err)
        finally:
            gate.close()
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def test_update_pending_blocks_commands(self):
        """Update-pending блокирует команды"""
        self.install()
        update_dir = self.root / "work" / "update"
        update_dir.mkdir(parents=True)
        (update_dir / "pending.json").write_text(
            json.dumps({"op_id": "op", "op_dir": "work/update/op"}),
            encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 1)
        self.assertIn("Незавершённое обновление", err)
        code, out, _ = self.cli("update", "status")
        self.assertEqual(code, 0)
        self.assertIn("Незавершённая операция", out)

    def test_lifecycle_pending_blocks_commands(self):
        """Lifecycle-pending блокирует команды"""
        self.install()
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_review")
        self.cli("index")
        op_dir = self.root / "work" / "lifecycle" / "op-1"
        op_dir.mkdir(parents=True)
        (self.root / "work" / "lifecycle" / "pending.json").write_text(
            json.dumps({"op_id": "op-1", "op_dir": "work/lifecycle/op-1"}),
            encoding="utf-8")
        for argv in (("index",), ("check",), ("close", "DEV-1"),
                     ("verify", "DEV-1"), ("adopt", ".", "--apply"),
                     ("update", "apply"), ("update", "plan", ".", "--save")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("restore.py", err, argv)
        code, out, _ = self.cli("update", "status")
        self.assertEqual(code, 0)
        self.assertIn("операция документов", out)

    def test_verify_holds_gate_and_nests_children(self):
        """Verify держит gate и вкладывает дочерние команды"""
        scripts = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        nested_index = {
            "id": "nested-index",
            "argv": ["python3", str(scripts), "--root", str(self.root), "index"],
            "timeout_seconds": 60}
        token_probe = {
            "id": "token",
            "argv": ["python3", "-c",
                     "import os, sys; sys.exit(0 if os.environ.get('PLAYBOOK_GATE') else 3)"],
            "timeout_seconds": 30}
        self.install(verification={"inputs": ["ai-dev-playbook"],
                                   "checks": [token_probe, nested_index]})
        self.add_task()
        code, out, err = self.cli("verify", "DEV-1")
        self.assertEqual(code, 0, err + out)
        report, _ = self.report()
        self.assertEqual(report["status"], "passed")
        statuses = {item["id"]: item["status"] for item in report["checks"]}
        self.assertEqual(statuses, {"token": "passed", "nested-index": "passed"})

    def test_update_apply_lock_order_with_gate(self):
        """update apply держит специализированный lock под общим gate."""
        self.install()
        release = self.fixture_release()
        code, _, err = self.cli("update", "plan", str(release), "--save")
        self.assertEqual(code, 0, err)
        lock = playbook.updating._update_lock(self.root, playbook.safe_path)
        self.assertIsNotNone(lock)
        try:
            code, _, err = self.cli("update", "apply")
            self.assertEqual(code, 1)
            self.assertIn("другим процессом", err)
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main()
