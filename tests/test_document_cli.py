import json
import unittest
from pathlib import Path

from test_cli import CliCase
from test_documents import CatalogCase, doc_meta, tree_fingerprint
from test_playbook import playbook, write_card

documents = playbook.documents


class DocumentCliCase(CatalogCase):
    """Временный проект с коллекциями spec и plan и готовыми индексами."""

    def setUp(self):
        super().setUp()
        self.configure({"spec": {"active": "docs/specs",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/specs/index.md"},
                        "plan": {"active": "docs/plans",
                                 "archive": "docs/archive/plans",
                                 "index": "docs/plans/index.md"}})
        (self.root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        code, _, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err)


class DocumentCreateTest(DocumentCliCase):
    def test_preview_writes_nothing(self):
        before = tree_fingerprint(self.root)
        code, out, err = self.cli("document", "create", "spec", "alpha",
                                  "--title", "Alpha spec")
        self.assertEqual(code, 0, err)
        self.assertIn("Предпросмотр", out)
        self.assertIn("spec:alpha", out)
        self.assertEqual(tree_fingerprint(self.root), before)

    def test_apply_creates_document_and_index(self):
        code, out, err = self.cli("document", "create", "spec", "alpha",
                                  "--title", "Alpha spec", "--apply")
        self.assertEqual(code, 0, err)
        doc = self.specs / "alpha.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        self.assertIn('"document_id": "spec:alpha"', text)
        self.assertIn('"state": "draft"', text)
        self.assertIn(documents.META_START, text)
        index = (self.specs / "index.md").read_text(encoding="utf-8")
        self.assertIn("[Alpha spec](alpha.md)", index)
        catalog = self.catalog()
        self.assertIn("spec:alpha", catalog["documents"])
        self.assertEqual(catalog["documents"]["spec:alpha"]["state"], "draft")
        self.assertTrue((self.root / "work" / "lifecycle").is_dir())

    def test_create_collision_refused(self):
        code, _, err = self.cli("document", "create", "spec", "alpha", "--apply")
        self.assertEqual(code, 0, err)
        original = (self.specs / "alpha.md").read_bytes()
        code, _, err = self.cli("document", "create", "spec", "alpha", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("занят", err)
        self.assertEqual((self.specs / "alpha.md").read_bytes(), original)

    def test_create_slug_of_index_refused(self):
        code, _, err = self.cli("document", "create", "spec", "index", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("индекс", err)

    def test_create_bad_slug_refused(self):
        code, _, err = self.cli("document", "create", "spec", "Not A Slug!",
                                "--apply")
        self.assertEqual(code, 1)
        self.assertIn("slug", err)

    def test_create_unknown_kind_collection_refused(self):
        code, _, err = self.cli("document", "create", "adr", "0001-x",
                                "--apply")
        self.assertEqual(code, 1)
        self.assertIn("adr", err)

    def test_create_without_documents_section_refused(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        del project["documents"]
        path.write_text(json.dumps(project) + "\n", encoding="utf-8")
        code, _, err = self.cli("document", "create", "spec", "alpha",
                                "--apply")
        self.assertEqual(code, 1)
        self.assertIn("documents", err)

    def test_create_discussion_initial_state_open(self):
        self.configure({"discussion": {"active": "docs/discussions",
                                       "archive": "docs/archive/discussions",
                                       "index": "docs/discussions/index.md"}})
        code, _, err = self.cli("index", "--documents")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("document", "create", "discussion", "topic",
                                "--apply")
        self.assertEqual(code, 0, err)
        text = (self.root / "docs" / "discussions" / "topic.md").read_text(
            encoding="utf-8")
        self.assertIn('"state": "open"', text)


class DocumentRegisterTest(DocumentCliCase):
    def setUp(self):
        super().setUp()
        self.legacy = self.root / "docs" / "plans" / "legacy-plan.md"
        self.legacy.write_text("# Старый план\n\nТело legacy-документа.\n",
                               encoding="utf-8")
        self.meta = {"schema_version": 1, "document_id": "plan:legacy-plan",
                     "kind": "plan", "state": "accepted",
                     "created_at": "2026-01-01T00:00:00+03:00",
                     "updated_at": "2026-01-02T00:00:00+03:00",
                     "current_revision": 1, "relations": [], "tasks": [],
                     "changes": [], "materials": [], "archive": None}
        self.meta_json = json.dumps(self.meta, ensure_ascii=False)

    def test_preview_keeps_body_and_writes_nothing(self):
        before = tree_fingerprint(self.root)
        code, out, err = self.cli("document", "register",
                                  "docs/plans/legacy-plan.md",
                                  "--metadata", self.meta_json)
        self.assertEqual(code, 0, err)
        self.assertIn("Предпросмотр", out)
        self.assertEqual(tree_fingerprint(self.root), before)

    def test_apply_inserts_meta_keeps_body_updates_index(self):
        code, _, err = self.cli("document", "register",
                                "docs/plans/legacy-plan.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 0, err)
        text = self.legacy.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Старый план\n\n"
                                        + documents.META_START))
        self.assertTrue(text.endswith("Тело legacy-документа.\n"))
        index = (self.root / "docs" / "plans" / "index.md").read_text(
            encoding="utf-8")
        self.assertIn("| [Старый план](legacy-plan.md) | accepted |", index)
        self.assertNotIn("| legacy |", index)
        catalog = self.catalog()
        self.assertIn("plan:legacy-plan", catalog["documents"])
        self.assertEqual(catalog["legacy"], [])

    def test_metadata_from_file(self):
        meta_file = self.root / "docs" / "meta.json"
        meta_file.write_text(self.meta_json, encoding="utf-8")
        code, _, err = self.cli("document", "register",
                                "docs/plans/legacy-plan.md",
                                "--metadata", "docs/meta.json", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("plan:legacy-plan", self.catalog()["documents"])

    def test_register_twice_refused(self):
        code, _, err = self.cli("document", "register",
                                "docs/plans/legacy-plan.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("document", "register",
                                "docs/plans/legacy-plan.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("метаданные", err)

    def test_unknown_schema_version_not_treated_as_legacy(self):
        bad = dict(self.meta, document_id="plan:other", schema_version=99)
        other = self.root / "docs" / "plans" / "other.md"
        other.write_text("# Другой\n\nТекст.\n", encoding="utf-8")
        code, _, err = self.cli("document", "register", "docs/plans/other.md",
                                "--metadata", json.dumps(bad), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("schema_version", err)
        self.assertNotIn(documents.META_START,
                         other.read_text(encoding="utf-8"))

    def test_register_outside_collection_refused(self):
        stray = self.root / "docs" / "stray.md"
        stray.write_text("# Посторонний\n\nТекст.\n", encoding="utf-8")
        code, _, err = self.cli("document", "register", "docs/stray.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("коллекц", err)

    def test_register_collection_index_refused(self):
        code, _, err = self.cli("document", "register", "docs/plans/index.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("индекс", err)

    def test_register_duplicate_document_id_refused(self):
        code, _, err = self.cli("document", "register",
                                "docs/plans/legacy-plan.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 0, err)
        twin = self.root / "docs" / "plans" / "twin.md"
        twin.write_text("# Двойник\n\nТекст.\n", encoding="utf-8")
        code, _, err = self.cli("document", "register", "docs/plans/twin.md",
                                "--metadata", self.meta_json, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("занят", err)


class IndexDocumentsTest(DocumentCliCase):
    def test_index_all_writes_task_and_document_indexes(self):
        self.add_task("DEV-1", "ready")
        code, out, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err)
        index = (self.root / "docs" / "tasks" / "index.md").read_text(
            encoding="utf-8")
        self.assertIn("DEV-1", index)

    def test_index_documents_idempotent(self):
        code, out, err = self.cli("index", "--documents")
        self.assertEqual(code, 0, err)
        self.assertIn("актуальн", out)

    def test_index_without_markers_refused_and_untouched(self):
        index = self.specs / "index.md"
        index.write_text("# Ручной индекс\n\nне трогать\n", encoding="utf-8")
        code, _, err = self.cli("index", "--documents")
        self.assertEqual(code, 1)
        self.assertIn("playbook-index", err)
        self.assertEqual(index.read_text(encoding="utf-8"),
                         "# Ручной индекс\n\nне трогать\n")

    def test_index_splices_only_marked_region(self):
        index = self.specs / "index.md"
        index.write_text("# Ручной индекс\n\n<!-- playbook-index:start -->\n"
                         "OLD\n<!-- playbook-index:end -->\n\nХвост.\n",
                         encoding="utf-8")
        code, _, err = self.cli("index", "--documents")
        self.assertEqual(code, 0, err)
        text = index.read_text(encoding="utf-8")
        self.assertIn("Ручной индекс", text)
        self.assertIn("Хвост.", text)
        self.assertNotIn("OLD", text)

    def test_check_detects_stale_index_without_fixing(self):
        code, _, err = self.cli("document", "create", "spec", "alpha",
                                "--apply")
        self.assertEqual(code, 0, err)
        index = self.specs / "index.md"
        stale = index.read_text(encoding="utf-8").replace(
            "| [alpha](alpha.md)", "| [beta](alpha.md)")
        index.write_text(stale, encoding="utf-8")
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertEqual(index.read_text(encoding="utf-8"), stale)


class LegacyInstallTest(CliCase):
    """Установка без раздела documents: прежнее поведение сохранено."""

    def test_index_and_check_work_without_documents(self):
        self.install()
        self.add_task("DEV-1", "ready")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)
        self.assertNotIn("каталог документов", out)
        code, _, err = self.cli("index", "--documents")
        self.assertEqual(code, 1)
        self.assertIn("documents", err)


if __name__ == "__main__":
    unittest.main()
