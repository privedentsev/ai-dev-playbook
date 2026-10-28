import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_cli import CliCase
from test_playbook import playbook, write_card

documents = playbook.documents

META_START = documents.META_START
META_END = documents.META_END


def doc_meta(**overrides):
    meta = {"schema_version": 1, "document_id": "spec:alpha", "kind": "spec",
            "state": "accepted", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z", "current_revision": 1,
            "relations": [], "tasks": [], "changes": [], "materials": [],
            "archive": None}
    meta.update(overrides)
    return meta


def doc_text(name, meta=None, body="Текст документа.\n"):
    text = f"# {name}\n\n"
    if meta is not None:
        raw = meta if isinstance(meta, str) else \
            json.dumps(meta, ensure_ascii=False, indent=2)
        text += f"{META_START}\n```json\n{raw}\n```\n{META_END}\n\n"
    return text + body


def write_doc(directory, name, meta=None, body="Текст документа.\n"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(doc_text(name, meta, body), encoding="utf-8")


def tree_fingerprint(root):
    """Отпечаток дерева: относительные пути и хеши содержимого."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = \
                hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class MetaParseTest(unittest.TestCase):
    def parse(self, text):
        errors = []
        meta = documents.parse_meta(text, "doc.md", errors)
        return meta, errors

    def test_valid_block(self):
        meta, errors = self.parse(doc_text("a", {"schema_version": 1}))
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(errors, [])

    def test_no_block(self):
        meta, errors = self.parse("# Документ\n\nБез метаданных.\n")
        self.assertIsNone(meta)
        self.assertEqual(errors, [])

    def test_example_inside_outer_fence_ignored(self):
        sample = doc_text("Пример", {"schema_version": 1})
        text = f"# Инструкция\n\n````markdown\n{sample}````\n\nТекст.\n"
        meta, errors = self.parse(text)
        self.assertIsNone(meta)
        self.assertEqual(errors, [])

    def test_fence_close_with_suffix_is_not_a_close(self):
        sample = doc_text("Пример", {"schema_version": 1})
        text = f"# Инструкция\n\n````markdown\n````still-inside\n{sample}````\n"
        meta, errors = self.parse(text)
        self.assertIsNone(meta)
        self.assertEqual(errors, [])

    def test_duplicate_json_key_rejected(self):
        raw = '{"schema_version": 1, "kind": "spec", "kind": "design"}'
        meta, errors = self.parse(doc_text("a", raw))
        self.assertIsNone(meta)
        self.assertIn("повторный JSON-ключ 'kind'", errors[0])

    def test_second_meta_block_rejected(self):
        text = doc_text("a", {"schema_version": 1}) + "\n" + \
            doc_text("b", {"schema_version": 1})
        meta, errors = self.parse(text)
        self.assertIsNone(meta)
        self.assertIn("повторный маркер", errors[0])

    def test_unclosed_block_rejected(self):
        text = f"# a\n\n{META_START}\n```json\n{{}}\n```\n"
        meta, errors = self.parse(text)
        self.assertIsNone(meta)
        self.assertIn(META_END, errors[0])

    def test_end_without_start_rejected(self):
        meta, errors = self.parse(f"# a\n\n{META_END}\n")
        self.assertIsNone(meta)
        self.assertTrue(errors)

    def test_invalid_json_rejected(self):
        meta, errors = self.parse(doc_text("a", "{не json"))
        self.assertIsNone(meta)
        self.assertIn("метаданные", errors[0])

    def test_non_object_json_rejected(self):
        meta, errors = self.parse(doc_text("a", "[1, 2]"))
        self.assertIsNone(meta)
        self.assertIn("JSON-объектом", errors[0])


class CatalogCase(CliCase):
    def setUp(self):
        super().setUp()
        self.install()
        self.specs = self.root / "docs" / "specs"
        self.specs_archive = self.root / "docs" / "archive" / "specs"

    def configure(self, collections=None):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        if collections is None:
            collections = {"spec": {"active": "docs/specs",
                                    "archive": "docs/archive/specs",
                                    "index": "docs/specs/index.md"}}
        project["documents"] = {"schema_version": 1, "collections": collections}
        if not project["components"]:
            project["components"] = [{"name": "playbook",
                                      "code": "ai-dev-playbook",
                                      "tests": ["tests"], "migrations": None}]
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def catalog(self):
        data, paths = playbook.config(self.root)
        records = playbook.read_tasks(self.root, paths)
        return documents.load_catalog(self.root, data, paths, records,
                                      playbook.safe_path, playbook.require)

    def assert_catalog_error(self, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            self.catalog()


class CollectionsConfigTest(CatalogCase):
    def test_absent_section_means_unconfigured(self):
        self.assertIsNone(self.catalog())

    def test_unknown_schema_version_rejected(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["documents"] = {"schema_version": 99, "collections": {}}
        path.write_text(json.dumps(project) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            self.catalog()

    def test_unknown_kind_rejected(self):
        self.configure({"memo": {"active": "docs/memos",
                                 "archive": "docs/archive/memos",
                                 "index": "docs/memos/index.md"}})
        with self.assertRaisesRegex(ValueError, "kind"):
            self.catalog()

    def test_collection_outside_root_rejected(self):
        self.configure({"spec": {"active": "../specs",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/specs/index.md"}})
        with self.assertRaisesRegex(ValueError, "путь|корень"):
            self.catalog()

    def test_collection_outside_docs_rejected(self):
        self.configure({"spec": {"active": "specs",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/specs/index.md"}})
        with self.assertRaisesRegex(ValueError, "внутри docs"):
            self.catalog()

    def test_overlapping_collections_rejected(self):
        self.configure({"spec": {"active": "docs/specs",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/specs/index.md"},
                        "design": {"active": "docs/specs/nested",
                                   "archive": "docs/archive/designs",
                                   "index": "docs/specs/nested/index.md"}})
        with self.assertRaisesRegex(ValueError, "пересечение"):
            self.catalog()

    def test_collection_inside_tasks_rejected(self):
        self.configure({"spec": {"active": "docs/tasks/specs",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/tasks/specs/index.md"}})
        with self.assertRaisesRegex(ValueError, "пересечение"):
            self.catalog()

    def test_symlink_in_collection_path_rejected(self):
        outside = self.root.parent / "outside-specs"
        outside.mkdir(exist_ok=True)
        (self.root / "docs" / "linked").symlink_to(outside,
                                                   target_is_directory=True)
        self.configure({"spec": {"active": "docs/linked",
                                 "archive": "docs/archive/specs",
                                 "index": "docs/linked/index.md"}})
        with self.assertRaisesRegex(ValueError, "сыл"):
            self.catalog()


class DocumentValidationTest(CatalogCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_registered_document_in_catalog(self):
        write_doc(self.specs, "alpha.md", doc_meta())
        catalog = self.catalog()
        entry = catalog["documents"]["spec:alpha"]
        self.assertEqual(entry["state"], "accepted")
        self.assertFalse(entry["archived"])

    def test_missing_fields_rejected(self):
        write_doc(self.specs, "alpha.md", {"schema_version": 1})
        self.assert_catalog_error("нет обязательных полей")

    def test_unknown_field_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta(title="Дубль заголовка"))
        self.assert_catalog_error("неизвестные поля")

    def test_unknown_meta_schema_rejected_not_legacy(self):
        write_doc(self.specs, "alpha.md", doc_meta(schema_version=99))
        self.assert_catalog_error("schema_version")

    def test_document_id_kind_mismatch_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta(document_id="design:alpha"))
        self.assert_catalog_error("document_id")

    def test_duplicate_document_id_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta())
        write_doc(self.specs, "beta.md", doc_meta())
        self.assert_catalog_error("повторный document_id")

    def test_duplicate_document_id_casefold_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta())
        write_doc(self.specs, "beta.md", doc_meta(document_id="SPEC:alpha"))
        self.assert_catalog_error("document_id|регистра")

    def test_legacy_listed_not_registered(self):
        write_doc(self.specs, "alpha.md", doc_meta())
        write_doc(self.specs, "old-note.md", None)
        catalog = self.catalog()
        self.assertEqual(len(catalog["legacy"]), 1)
        self.assertEqual(catalog["legacy"][0]["path"].name, "old-note.md")
        self.assertNotIn("old-note", catalog["documents"])

    def test_invalid_state_for_kind_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta(state="completed"))
        self.assert_catalog_error("state")

    def test_updated_before_created_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(updated_at="2020-01-01T00:00:00Z"))
        self.assert_catalog_error("updated_at")

    def test_relation_target_must_exist(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(relations=[{"relation": "based_on",
                                       "target": "spec:missing"}]))
        self.assert_catalog_error("нет такого документа")

    def test_relation_to_self_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(relations=[{"relation": "based_on",
                                       "target": "spec:alpha"}]))
        self.assert_catalog_error("сам документ")

    def test_replaces_cycle_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(relations=[{"relation": "replaces",
                                       "target": "spec:beta"}]))
        write_doc(self.specs, "beta.md",
                  doc_meta(document_id="spec:beta",
                           relations=[{"relation": "replaces",
                                       "target": "spec:alpha"}]))
        self.assert_catalog_error("цикл replaces")

    def test_unknown_task_reference_rejected(self):
        write_doc(self.specs, "alpha.md", doc_meta(tasks=["DEV-77"]))
        self.assert_catalog_error("DEV-77")

    def test_archive_location_consistency(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(archive={"at": "2026-01-03T00:00:00Z",
                                    "reason": "заменён", "successor": None,
                                    "operation_id": "op-1",
                                    "original_path": "docs/specs/alpha.md"}))
        self.assert_catalog_error("archive задан")

    def test_archived_doc_in_active_dir_needs_archive_field(self):
        write_doc(self.specs_archive, "alpha.md", doc_meta(state="superseded"))
        self.assert_catalog_error("архивном каталоге")

    def test_archived_doc_with_terminal_state_accepted(self):
        write_doc(self.specs_archive, "alpha.md",
                  doc_meta(state="superseded",
                           archive={"at": "2026-01-03T00:00:00Z",
                                    "reason": "заменён", "successor": None,
                                    "operation_id": "op-1",
                                    "original_path": "docs/specs/alpha.md"}))
        entry = self.catalog()["documents"]["spec:alpha"]
        self.assertTrue(entry["archived"])

    def test_archived_active_state_rejected(self):
        write_doc(self.specs_archive, "alpha.md",
                  doc_meta(state="accepted",
                           archive={"at": "2026-01-03T00:00:00Z",
                                    "reason": "x", "successor": None,
                                    "operation_id": "op-1",
                                    "original_path": "docs/specs/alpha.md"}))
        self.assert_catalog_error("нефинальным state")

    def test_meta_block_outside_collections_rejected(self):
        write_doc(self.root / "docs", "stray.md", doc_meta())
        self.assert_catalog_error("вне зарегистрированной коллекции")

    def test_revision_and_planned_changes_distinct(self):
        change = {"id": "ch-1", "state": "implemented", "section": "§2",
                  "basis": {"type": "engineer_approval", "who": "инженер",
                            "at": "2026-01-02T00:00:00Z",
                            "detail": "разрешено сообщением",
                            "source": "сессия PB-021"},
                  "tasks": ["DEV-1"], "acceptance": None}
        self.add_task("DEV-1", "in_progress")
        write_doc(self.specs, "alpha.md",
                  doc_meta(current_revision=2, changes=[change]))
        entry = self.catalog()["documents"]["spec:alpha"]
        self.assertEqual(entry["revision"], 2)
        self.assertEqual(entry["changes"][0]["state"], "implemented")

    def test_change_unknown_task_rejected(self):
        change = {"id": "ch-1", "state": "draft", "section": "§2",
                  "basis": {"type": "authorized_scope", "who": "Devin",
                            "at": "2026-01-02T00:00:00Z", "detail": "x",
                            "source": "y"},
                  "tasks": ["DEV-99"], "acceptance": None}
        write_doc(self.specs, "alpha.md", doc_meta(changes=[change]))
        self.assert_catalog_error("DEV-99")


class TaskMetaTest(CatalogCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def task_with_meta(self, meta):
        write_card(self.root / "docs" / "tasks", "DEV-1", "in_progress")
        card = self.root / "docs" / "tasks" / "DEV-1.md"
        raw = json.dumps(meta, ensure_ascii=False, indent=2)
        card.write_text(card.read_text(encoding="utf-8")
                        + f"\n{META_START}\n```json\n{raw}\n```\n{META_END}\n",
                        encoding="utf-8")

    def test_task_supplement_accepted(self):
        self.task_with_meta({"schema_version": 1, "relations": [],
                             "materials": []})
        catalog = self.catalog()
        self.assertIn("DEV-1", catalog["task_meta"])

    def test_header_fields_in_meta_rejected(self):
        self.task_with_meta({"schema_version": 1, "document_id": "spec:x",
                             "state": "accepted"})
        self.assert_catalog_error("неизвестные поля")

    def test_task_relation_to_document(self):
        write_doc(self.specs, "alpha.md", doc_meta())
        self.task_with_meta({"schema_version": 1,
                             "relations": [{"relation": "based_on",
                                            "target": "spec:alpha"}]})
        catalog = self.catalog()
        incoming = catalog["incoming"]["spec:alpha"]
        self.assertEqual(incoming[0]["source"], "DEV-1")

    def test_task_relation_to_missing_document_rejected(self):
        self.task_with_meta({"schema_version": 1,
                             "relations": [{"relation": "based_on",
                                            "target": "spec:none"}]})
        self.assert_catalog_error("нет такого документа")


def local_material(material_id="m-1", source="docs/specs/alpha-evidence/r.md",
                   required=True, used_by=None):
    return {"id": material_id, "role": "report", "source": source,
            "storage": "local", "required": required, "immutable": True,
            "used_by": used_by or []}


class MaterialsTest(CatalogCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_single_owner_accepted(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("report\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material()]))
        catalog = self.catalog()
        self.assertEqual(len(catalog["owners"]["spec:alpha"]), 1)

    def test_duplicate_ownership_rejected(self):
        shared = self.specs / "shared.md"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("x\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material("m-1", "docs/specs/shared.md")]))
        write_doc(self.specs, "beta.md",
                  doc_meta(document_id="spec:beta",
                           materials=[local_material("m-2", "docs/specs/shared.md")]))
        self.assert_catalog_error("уже принадлежит")

    def test_owner_dirs_overlap_rejected(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("x\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(
                      "m-1", "docs/specs/alpha-evidence")]))
        write_doc(self.specs, "beta.md",
                  doc_meta(document_id="spec:beta",
                           materials=[local_material(
                               "m-2", "docs/specs/alpha-evidence/r.md")]))
        self.assert_catalog_error("пересечение каталогов")

    def test_missing_required_material_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material()]))
        self.assert_catalog_error("отсутствует")

    def test_missing_optional_material_allowed(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(required=False)]))
        self.assertIn("spec:alpha", self.catalog()["documents"])

    def test_component_code_not_material(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(
                      source="ai-dev-playbook/scripts/playbook.py",
                      required=False)]))
        self.assert_catalog_error("компонента")

    def test_unknown_consumer_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(used_by=["DEV-404"],
                                                     required=False)]))
        self.assert_catalog_error("DEV-404")

    def test_external_material_needs_access(self):
        material = {"id": "m-1", "role": "report",
                    "source": "https://example.com/r.pdf", "storage": "external",
                    "required": False, "immutable": True, "used_by": []}
        write_doc(self.specs, "alpha.md", doc_meta(materials=[material]))
        self.assert_catalog_error("access")

    def test_material_path_escape_rejected(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(source="../../etc/x")]))
        self.assert_catalog_error("материал")

    def test_untracked_evidence_dir_is_candidate(self):
        (self.specs / "alpha-evidence").mkdir(parents=True)
        write_doc(self.specs, "alpha.md", doc_meta())
        candidates = self.catalog()["candidates"]
        self.assertTrue(any(c["owner"] == "spec:alpha" for c in candidates))


class AuditRegressionTest(CatalogCase):
    """Регрессия находок аудита PB-021 (A1–A6)."""

    def setUp(self):
        super().setUp()
        self.configure()

    def _set_components(self, components):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["components"] = components
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def _outside(self, name):
        """Каталог вне корня проекта для внешних файлов фикстуры."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve() / name

    # A1: найденные пути проверяются до чтения
    def test_symlinked_document_rejected_before_read(self):
        self.specs.mkdir(parents=True, exist_ok=True)
        outside = self._outside("outside.md")
        outside.write_text(
            doc_text("outside", doc_meta(document_id="spec:outside")),
            encoding="utf-8")
        (self.specs / "outside.md").symlink_to(outside)
        self.assert_catalog_error("символическая ссылка")

    def test_symlinked_dir_in_collection_rejected(self):
        self.specs.mkdir(parents=True, exist_ok=True)
        outside = self._outside("outside-dir")
        outside.mkdir()
        (outside / "secret.md").write_text("secret\n", encoding="utf-8")
        (self.specs / "linked").symlink_to(outside, target_is_directory=True)
        self.assert_catalog_error("символическая ссылка")

    def test_symlink_inside_material_dir_rejected(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "tracked.log").write_text("tracked\n", encoding="utf-8")
        outside = self._outside("outside.log")
        outside.write_text("outside\n", encoding="utf-8")
        (evidence / "link.log").symlink_to(outside)
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(
                      source="docs/specs/alpha-evidence")]))
        self.assert_catalog_error("символическая ссылка")

    # A2: границы компонентов проверяются в обе стороны, migrations — тоже
    def test_material_containing_component_root_rejected(self):
        self._set_components([{"name": "app", "code": "app/src",
                               "tests": ["app/tests"], "migrations": None}])
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(source="app",
                                                     required=False)]))
        self.assert_catalog_error("компонента")

    def test_migrations_dir_rejected_as_material(self):
        self._set_components([{"name": "app", "code": "app/src",
                               "tests": ["app/tests"], "migrations": "app/db"}])
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(source="app/db",
                                                     required=False)]))
        self.assert_catalog_error("компонента")

    # A3: external-материал не может иметь двух владельцев
    def test_external_material_shared_between_owners_rejected(self):
        shared = {"id": "m-1", "role": "report",
                  "source": "https://example.com/proof", "storage": "external",
                  "required": False, "immutable": True, "used_by": [],
                  "access": "fixture"}
        write_doc(self.specs, "alpha.md", doc_meta(materials=[shared]))
        write_doc(self.specs, "beta.md",
                  doc_meta(document_id="spec:beta",
                           materials=[{**shared, "id": "m-2"}]))
        self.assert_catalog_error("уже принадлежит")

    # A4: полный состав evidence и коллекций — не только *.md
    def test_partially_registered_evidence_listed(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "tracked.log").write_text("tracked\n", encoding="utf-8")
        (evidence / "missed.log").write_text("untracked\n", encoding="utf-8")
        (evidence / ".hidden").write_text("hidden\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md",
                  doc_meta(materials=[local_material(
                      source="docs/specs/alpha-evidence/tracked.log")]))
        candidates = self.catalog()["candidates"]
        paths = {c["path"] for c in candidates}
        self.assertIn("docs/specs/alpha-evidence/missed.log", paths)
        self.assertIn("docs/specs/alpha-evidence/.hidden", paths)
        self.assertNotIn("docs/specs/alpha-evidence/tracked.log", paths)
        owner = {c["path"]: c["owner"] for c in candidates}
        self.assertEqual(owner["docs/specs/alpha-evidence/missed.log"],
                         "spec:alpha")

    def test_nested_non_markdown_file_is_candidate(self):
        bundle = self.specs / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "x.txt").write_text("data\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md", doc_meta())
        candidates = self.catalog()["candidates"]
        self.assertTrue(any(c["path"] == "docs/specs/bundle/x.txt"
                            for c in candidates))

    # A5: строка с суффиксом не закрывает fence
    def test_false_fence_close_keeps_example_inside(self):
        sample = doc_text("Пример", doc_meta(document_id="spec:fake"))
        self.specs.mkdir(parents=True, exist_ok=True)
        (self.specs / "alpha.md").write_text(
            f"# Описание\n\n````markdown\n````still-inside\n{sample}````\n",
            encoding="utf-8")
        catalog = self.catalog()
        self.assertNotIn("spec:fake", catalog["documents"])
        self.assertEqual(catalog["legacy"][0]["path"].name, "alpha.md")

    # A6: адресные ошибки вместо TypeError — документ и поле
    def test_malformed_state_reported_with_document_and_field(self):
        write_doc(self.specs, "alpha.md", doc_meta(state=[]))
        self.assert_catalog_error("поле 'state'")

    def test_malformed_relation_type_reported(self):
        write_doc(self.specs, "alpha.md",
                  doc_meta(relations=[{"relation": ["based_on"],
                                       "target": "spec:alpha"}]))
        self.assert_catalog_error("поле 'relations'")

    def test_malformed_change_state_reported(self):
        change = {"id": "ch-1", "state": [], "section": "§1",
                  "basis": {"type": "engineer_approval", "who": "w",
                            "at": "2026-01-01T00:00:00Z", "detail": "d",
                            "source": "s"},
                  "tasks": [], "acceptance": None}
        write_doc(self.specs, "alpha.md", doc_meta(changes=[change]))
        self.assert_catalog_error("поле 'changes'")

    def test_malformed_material_fields_reported(self):
        material = local_material(required=False)
        material["role"] = {}
        write_doc(self.specs, "alpha.md", doc_meta(materials=[material]))
        self.assert_catalog_error("поле 'materials'")

    def test_malformed_used_by_reported(self):
        material = local_material(required=False, used_by=[["DEV-1"]])
        write_doc(self.specs, "alpha.md", doc_meta(materials=[material]))
        self.assert_catalog_error("used_by")

    def test_malformed_material_id_reported(self):
        material = local_material(required=False)
        material["id"] = []
        write_doc(self.specs, "alpha.md", doc_meta(materials=[material]))
        self.assert_catalog_error(r"alpha\.md: поле 'materials'")

    def test_malformed_change_id_reported(self):
        change = {"id": {}, "state": "draft", "section": "§1",
                  "basis": {"type": "authorized_scope", "who": "w",
                            "at": "2026-01-01T00:00:00Z", "detail": "d",
                            "source": "s"},
                  "tasks": [], "acceptance": None}
        write_doc(self.specs, "alpha.md", doc_meta(changes=[change]))
        self.assert_catalog_error(r"alpha\.md: поле 'changes'")

    def test_malformed_external_uri_reported(self):
        material = {"id": "m-1", "role": "report", "source": "https://[",
                    "storage": "external", "required": False, "immutable": True,
                    "used_by": [], "access": "fixture"}
        write_doc(self.specs, "alpha.md", doc_meta(materials=[material]))
        self.assert_catalog_error(r"alpha\.md: поле 'materials'")

    def test_malformed_state_subprocess_diagnostic(self):
        write_doc(self.specs, "alpha.md", doc_meta(state=[]))
        script = self.root / "ai-dev-playbook" / "scripts" / "playbook.py"
        result = subprocess.run(
            [sys.executable, "-B", str(script), "--root", str(self.root),
             "document", "list"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("alpha.md", result.stderr)
        self.assertIn("state", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class DocumentCommandsTest(CatalogCase):
    def setUp(self):
        super().setUp()
        self.configure()
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("report\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md",
                  doc_meta(relations=[{"relation": "based_on",
                                       "target": "spec:beta"}],
                           materials=[local_material()]))
        write_doc(self.specs, "beta.md", doc_meta(document_id="spec:beta"))
        self.add_task("DEV-1", "in_progress")

    def test_impact_shows_relations_and_materials(self):
        code, out, err = self.cli("document", "impact", "spec:alpha")
        self.assertEqual(code, 0, err)
        self.assertIn("based_on → spec:beta", out)
        self.assertIn("ревизия 1", out)
        self.assertIn("m-1", out)

    def test_impact_shows_reverse_relations(self):
        code, out, err = self.cli("document", "impact", "spec:beta")
        self.assertEqual(code, 0, err)
        self.assertIn("spec:alpha → based_on", out)

    def test_impact_unknown_document_rejected(self):
        code, _, err = self.cli("document", "impact", "spec:none")
        self.assertEqual(code, 1)
        self.assertIn("spec:none", err)

    def test_materials_for_document(self):
        code, out, err = self.cli("document", "materials", "spec:alpha")
        self.assertEqual(code, 0, err)
        self.assertIn("m-1", out)
        self.assertIn("на месте", out)

    def test_materials_for_task_owner(self):
        code, out, err = self.cli("document", "materials", "DEV-1")
        self.assertEqual(code, 0, err)
        self.assertIn("DEV-1", out)

    def test_list_shows_legacy(self):
        write_doc(self.specs, "old.md", None)
        code, out, err = self.cli("document", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("spec:alpha", out)
        self.assertIn("legacy", out)
        self.assertIn("old.md", out)

    def test_read_only_commands_do_not_change_tree(self):
        before = tree_fingerprint(self.root)
        for argv in (("document", "impact", "spec:alpha"),
                     ("document", "materials", "spec:alpha"),
                     ("document", "list")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 0, err)
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_commands_require_documents_section(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        del project["documents"]
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        code, _, err = self.cli("document", "list")
        self.assertEqual(code, 1)
        self.assertIn("documents", err)

    def test_check_validates_catalog(self):
        write_doc(self.specs, "gamma.md", doc_meta(document_id="spec:gamma",
                                                   tasks=["DEV-404"]))
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("DEV-404", err)


if __name__ == "__main__":
    unittest.main()
