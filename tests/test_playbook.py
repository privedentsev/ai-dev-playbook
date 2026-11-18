import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ai-dev-playbook" / "scripts" / "playbook.py"

spec = importlib.util.spec_from_file_location("playbook", SCRIPT)
playbook = importlib.util.module_from_spec(spec)
sys.modules["playbook"] = playbook
spec.loader.exec_module(playbook)

CARD = "# {title}\n\nID: {ident}\nStatus: {status}\nPriority: P2\nDepends-On: {deps}\n{extra}"


def write_card(directory, ident, status, deps="-", executor=None, model=None,
               created="2026-01-01T00:00:00Z", closed=None):
    directory.mkdir(parents=True, exist_ok=True)
    extra = ""
    if executor is not None:
        extra += f"Executor: {executor}\n"
    if model is not None:
        extra += f"Model: {model}\n"
    if closed is None:
        closed = "unknown" if status in {"done", "cancelled"} else "-"
    extra += f"Created: {created}\nClosed: {closed}\n"
    (directory / f"{ident}.md").write_text(
        CARD.format(title=f"{ident}: test", ident=ident, status=status, deps=deps, extra=extra),
        encoding="utf-8")


class TaskPlacementTest(unittest.TestCase):
    """Размещение задач: done/open в tasks/archive, зависимости,
    index, executor/model."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tasks = self.root / "docs" / "tasks"
        self.archive = self.root / "docs" / "archive" / "tasks"
        self.tasks.mkdir(parents=True)
        self.archive.mkdir(parents=True)
        self.paths = {"tasks": self.tasks, "archive": self.archive,
                      "task_index": self.tasks / "index.md"}

    def read(self):
        return playbook.read_tasks(self.root, self.paths)

    def test_done_in_tasks_rejected(self):
        """done-карточка в tasks/ отклоняется"""
        write_card(self.tasks, "DEV-1", "done")
        with self.assertRaisesRegex(ValueError, "вне архива"):
            self.read()

    def test_done_in_archive_accepted(self):
        """done-карточка в archive/ принимается"""
        write_card(self.archive, "DEV-1", "done")
        self.assertTrue(self.read()["DEV-1"]["archived"])

    def test_open_card_in_archive_rejected(self):
        """Открытая карточка в архиве отклоняется"""
        write_card(self.archive, "DEV-1", "in_progress")
        with self.assertRaisesRegex(ValueError, "в архиве"):
            self.read()

    def test_dependency_on_archived_done(self):
        """Зависимость на архивную done-карточку допустима"""
        write_card(self.tasks, "DEV-2", "ready", deps="DEV-1")
        write_card(self.archive, "DEV-1", "done")
        self.assertEqual(self.read()["DEV-2"]["deps"], ["DEV-1"])

    def render(self):
        return playbook.render_index(self.read(), self.tasks)

    def test_render_index_lists_active_and_archive(self):
        """render index перечисляет активные и архив"""
        write_card(self.tasks, "DEV-2", "ready")
        write_card(self.archive, "DEV-1", "done")
        index = self.render()
        self.assertIn("(DEV-2.md)", index.split("## Архив")[0])
        self.assertIn("(../archive/tasks/DEV-1.md)", index.split("## Архив")[1])

    def test_executor_model_parsed(self):
        """Executor/model парсятся из карточки"""
        write_card(self.tasks, "DEV-1", "in_progress", executor="Devin", model="SWE-2 High")
        task = self.read()["DEV-1"]
        self.assertEqual((task["executor"], task["model"]), ("Devin", "SWE-2 High"))

    def test_executor_model_absent(self):
        """Отсутствие executor/model допустимо"""
        write_card(self.tasks, "DEV-1", "ready")
        task = self.read()["DEV-1"]
        self.assertIsNone(task["executor"])
        self.assertIsNone(task["model"])

    def test_render_index_executor_column(self):
        """Индекс рендерит колонку исполнителя"""
        write_card(self.tasks, "DEV-1", "in_progress", executor="Devin", model="SWE-2 High")
        write_card(self.tasks, "DEV-2", "in_progress", executor="Devin")
        write_card(self.tasks, "DEV-3", "ready")
        index = self.render().split("## Архив")[0]
        self.assertIn("| DEV-1 |", index)
        self.assertIn("| Devin / SWE-2 High |", index)
        self.assertIn("| Devin |", index)
        self.assertIn("| — | — |", index)

    def test_render_index_model_without_executor_dash(self):
        """Модель без исполнителя — прочерк в индексе"""
        write_card(self.tasks, "DEV-1", "ready", model="SWE-2 High")
        active = self.render().split("## Архив")[0]
        self.assertIn("| — |", active)
        self.assertNotIn("SWE-2 High", active)

    def test_render_index_archive_newest_first(self):
        """Архив в индексе — новейшие первыми"""
        write_card(self.archive, "DEV-1", "done")
        write_card(self.archive, "DEV-3", "done")
        write_card(self.archive, "DEV-12", "done")
        archive = self.render().split("## Архив")[1]
        positions = [archive.index(f"DEV-{n} |") for n in (12, 3, 1)]
        self.assertEqual(positions, sorted(positions))

    def test_created_closed_parsed(self):
        """Created/Closed парсятся из карточки"""
        write_card(self.archive, "DEV-1", "done",
                   created="2026-01-01T10:00:00Z", closed="2026-02-03T12:30:00+00:00")
        task = self.read()["DEV-1"]
        self.assertEqual((task["created"], task["closed"]),
                         ("2026-01-01T10:00:00Z", "2026-02-03T12:30:00+00:00"))

    def test_missing_created_closed_lines_rejected(self):
        """Отсутствие строк Created/Closed отклоняется"""
        self.tasks.mkdir(parents=True, exist_ok=True)
        (self.tasks / "DEV-1.md").write_text(
            "# DEV-1: test\n\nID: DEV-1\nStatus: ready\nPriority: P2\nDepends-On: -\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "шапка"):
            self.read()

    def test_created_unset_rejected(self):
        """Created=«-» отклоняется"""
        write_card(self.tasks, "DEV-1", "ready", created="-")
        with self.assertRaisesRegex(ValueError, "Created обязателен"):
            self.read()

    def test_bad_timestamp_rejected(self):
        """Невалидный timestamp отклоняется"""
        write_card(self.tasks, "DEV-1", "ready", created="вчера")
        with self.assertRaisesRegex(ValueError, "Created"):
            self.read()

    def test_timestamp_without_timezone_rejected(self):
        """Timestamp без timezone отклоняется"""
        write_card(self.tasks, "DEV-1", "ready", created="2026-01-01T10:00:00")
        with self.assertRaisesRegex(ValueError, "Created"):
            self.read()

    def test_active_card_with_closed_rejected(self):
        """Активная карточка с Closed отклоняется"""
        write_card(self.tasks, "DEV-1", "in_review", closed="2026-02-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "активная карточка с Closed"):
            self.read()

    def test_done_without_closed_rejected(self):
        """done без Closed отклоняется"""
        write_card(self.archive, "DEV-1", "done", closed="-")
        with self.assertRaisesRegex(ValueError, "без Closed"):
            self.read()

    def test_closed_before_created_rejected(self):
        """Closed раньше Created отклоняется"""
        write_card(self.archive, "DEV-1", "done",
                   created="2026-02-01T00:00:00Z", closed="2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "раньше Created"):
            self.read()

    def test_render_index_stamp_columns(self):
        """Индекс рендерит колонки штампов времени"""
        write_card(self.tasks, "DEV-1", "ready", created="2026-01-05T10:00:00Z")
        write_card(self.archive, "DEV-2", "done",
                   created="2026-01-01T00:00:00Z", closed="2026-02-10T00:00:00Z")
        index = self.render()
        self.assertIn("| Создана | Закрыта |", index)
        self.assertIn("| 2026-01-05 10:00 | — |", index.split("## Архив")[0])
        self.assertIn("| 2026-01-01 00:00 | 2026-02-10 00:00 |", index.split("## Архив")[1])

    def test_render_index_archive_sorted_by_closed(self):
        """Архив в индексе сортирован по Closed"""
        write_card(self.archive, "DEV-1", "done", closed="2026-03-01T00:00:00Z")
        write_card(self.archive, "DEV-12", "done", closed="2026-01-01T00:00:00Z")
        write_card(self.archive, "DEV-3", "done", closed="unknown")
        archive = self.render().split("## Архив")[1]
        positions = [archive.index(f"DEV-{n} |") for n in (1, 12, 3)]
        self.assertEqual(positions, sorted(positions))


class UnconfirmedLinesTest(unittest.TestCase):
    """unconfirmed-строки: inline code и fence-игнорирование,
    грамматика меток."""
    MARKER = playbook.MODEL_UNCONFIRMED

    def test_plain_text_flagged(self):
        """Обычный текст с неподтверждённой моделью помечается"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"строка\nМодель: GPT-6 {self.MARKER}\n"), [2])

    def test_inline_code_ignored(self):
        """Inline code с маркером игнорируется"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"конвенция: `модель: X {self.MARKER}`\n"), [])

    def test_backtick_fence_ignored(self):
        """Backtick fence с маркером игнорируется"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"```\n{self.MARKER}\n```\n"), [])

    def test_tilde_fence_ignored(self):
        """Tilde fence с маркером игнорируется"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"~~~\n{self.MARKER}\n~~~\n"), [])

    def test_other_fence_inside_block_is_content(self):
        """Чужой fence внутри блока — содержимое"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"```\n~~~\n{self.MARKER}\n```\n{self.MARKER}\n"), [5])

    def test_shorter_fence_does_not_close(self):
        """Короткий fence не закрывает длинный"""
        self.assertEqual(playbook.unconfirmed_lines(
            f"````\n```\n{self.MARKER}\n````\n{self.MARKER}\n"), [5])


if __name__ == "__main__":
    unittest.main()
