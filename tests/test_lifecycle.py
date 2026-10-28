import hashlib
import json
import unittest
from pathlib import Path

from test_document_cli import DocumentCliCase
from test_documents import doc_meta, write_doc, tree_fingerprint, local_material
from test_playbook import playbook, write_card

fops = playbook.file_operations
lifecycle = playbook.lifecycle
documents = playbook.documents


def request_json(doc_id, **action):
    return {"schema_version": 1,
            "actor": {"name": "Devin", "role": "executor",
                      "model": "SWE-2 High"},
            "basis": "Инженер подтвердил архивирование",
            "scope": {"documents": [doc_id]},
            "actions": [dict({"action": "archive_document",
                              "document": doc_id,
                              "reason": "работа завершена",
                              "successor": None}, **action)]}


def task_request(task_id, action="close_task", **fields):
    """Запрос операции над карточкой: close_task/reopen."""
    item = {"action": action, "task": task_id}
    if action == "close_task":
        item.setdefault("outcome", "done")
    if action == "reopen":
        item.setdefault("status", "in_progress")
        item.setdefault("reason", "возврат в работу")
    item.update(fields)
    return {"schema_version": 1,
            "actor": {"name": "Devin", "role": "executor",
                      "model": "SWE-2 High"},
            "basis": "Инженер подтвердил операцию",
            "scope": {"tasks": [task_id]},
            "actions": [item]}


class LifecycleCase(DocumentCliCase):
    """Временный проект: коллекции spec и plan, индексы сгенерированы."""

    def setUp(self):
        super().setUp()
        # check --ready требует зарегистрированные каталоги компонентов.
        (self.root / "tests").mkdir(exist_ok=True)

    def project_fingerprint(self):
        """Отпечаток дерева без runtime-области work/ (операции/журналы)."""
        return {rel: digest
                for rel, digest in tree_fingerprint(self.root).items()
                if not rel.startswith("work/")}

    def add_spec(self, slug, state="superseded", materials=None,
                 body=None, **meta):
        meta = {"document_id": f"spec:{slug}", "state": state,
                "materials": materials or [], **meta}
        write_doc(self.specs, f"{slug}.md", doc_meta(**meta),
                  body or f"Текст {slug}.\n")

    def add_plan(self, slug, state="completed", tasks=None, **meta):
        data = doc_meta(document_id=f"plan:{slug}", kind="plan",
                        state=state, tasks=tasks or [], **meta)
        directory = self.root / "docs" / "plans"
        write_doc(directory, f"{slug}.md", data)

    def request(self, payload, name="request.json"):
        path = self.root / "work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return f"work/{name}"

    def save_plan(self, payload):
        rel = self.request(payload)
        code, out, err = self.cli("lifecycle", "plan", rel, "--save")
        self.assertEqual(code, 0, err + out)
        marker = "План записан: "
        self.assertIn(marker, out)
        return rel, out.split(marker, 1)[1].split("\n", 1)[0]

    def archived(self, slug):
        return self.root / "docs" / "archive" / "specs" / f"{slug}.md"

    def meta_of(self, path):
        errors = []
        meta = documents.parse_meta(path.read_text(encoding="utf-8"),
                                    str(path), errors)
        self.assertEqual(errors, [])
        return meta


class ArchiveApplyTest(LifecycleCase):
    def test_plan_preview_writes_nothing(self):
        self.add_spec("alpha")
        rel = self.request(request_json("spec:alpha"))
        before = tree_fingerprint(self.root)
        code, out, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 0, err)
        self.assertIn("docs/specs/alpha.md → docs/archive/specs/alpha.md", out)
        self.assertIn("без записи", out)
        self.assertEqual(tree_fingerprint(self.root), before)

    def test_apply_moves_document_materials_and_rewrites_links(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "report.md").write_text("Отчёт\n", encoding="utf-8")
        report_hash = hashlib.sha256("Отчёт\n".encode()).hexdigest()
        (evidence / "data.csv").write_bytes(b"a,b\n1,2\n")
        self.add_spec("alpha", materials=[
            local_material("m-report", "docs/specs/alpha-evidence"),
            {"id": "m-ext", "role": "other",
             "source": "https://example.com/spec", "storage": "external",
             "required": True, "immutable": False, "used_by": [],
             "access": "публичный доступ"}])
        self.add_spec("beta", state="accepted",
                      body="См. [alpha](alpha.md) и "
                           "[отчёт](alpha-evidence/report.md).\n")
        _, plan_rel = self.save_plan(
            request_json("spec:alpha", confirmed_materials=["m-ext"]))
        code, out, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err + out)
        archived = self.archived("alpha")
        self.assertTrue(archived.is_file())
        self.assertFalse((self.specs / "alpha.md").exists())
        moved_evidence = self.root / "docs" / "archive" / "specs" / \
            "alpha-evidence"
        self.assertFalse(evidence.exists())
        self.assertEqual(hashlib.sha256(
            (moved_evidence / "report.md").read_bytes()).hexdigest(),
            report_hash)
        self.assertEqual((moved_evidence / "data.csv").read_bytes(),
                         b"a,b\n1,2\n")
        # Сопровождающий JSON-доказательство внутри evidence-каталога.
        companions = list(moved_evidence.glob("archive-lc-*.json"))
        self.assertEqual(len(companions), 1)
        companion = json.loads(companions[0].read_text(encoding="utf-8"))
        self.assertEqual(companion["document"], "spec:alpha")
        self.assertEqual(companion["basis"],
                         "Инженер подтвердил архивирование")
        # Метаданные: archive, moves, обновлённые источники материалов.
        meta = self.meta_of(archived)
        archive = meta["archive"]
        self.assertEqual(archive["original_path"], "docs/specs/alpha.md")
        self.assertEqual(archive["reason"], "работа завершена")
        self.assertIsNone(archive["successor"])
        self.assertEqual(archive["operation_id"], companion["plan_id"])
        move_map = {item["from"]: item["to"] for item in meta["moves"]}
        self.assertEqual(move_map["docs/specs/alpha.md"],
                         "docs/archive/specs/alpha.md")
        self.assertEqual(move_map["docs/specs/alpha-evidence"],
                         "docs/archive/specs/alpha-evidence")
        sources = {item["id"]: item["source"]
                   for item in meta["materials"]}
        self.assertEqual(sources["m-report"],
                         "docs/archive/specs/alpha-evidence")
        companion_id = f"lifecycle-{companion['plan_id']}"
        self.assertEqual(sources[companion_id],
                         str(companions[0].relative_to(self.root)))
        registered = {item["id"]: item for item in meta["materials"]}
        self.assertTrue(registered[companion_id]["immutable"])
        self.assertTrue(registered[companion_id]["required"])
        # Входящие ссылки beta переписаны, beta не перенесена.
        beta = (self.specs / "beta.md").read_text(encoding="utf-8")
        self.assertIn("(../archive/specs/alpha.md)", beta)
        self.assertIn("(../archive/specs/alpha-evidence/report.md)", beta)
        # Индекс коллекции: alpha в архивной секции с причиной.
        index = (self.specs / "index.md").read_text(encoding="utf-8")
        self.assertIn("[alpha.md](../archive/specs/alpha.md)", index)
        self.assertIn("работа завершена", index)
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_apply_twice_is_already_completed(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err)
        after_first = tree_fingerprint(self.root)
        code, out, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err)
        self.assertIn("уже применён", out)
        self.assertEqual(tree_fingerprint(self.root), after_first)
        archived = list((self.root / "docs" / "archive" / "specs")
                        .glob("alpha*.md"))
        self.assertEqual(len(archived), 1)

    def test_apply_two_documents_in_one_request(self):
        self.add_spec("alpha")
        self.add_spec("beta", document_id="spec:beta")
        payload = {"schema_version": 1,
                   "actor": {"name": "Devin", "role": "executor"},
                   "basis": "этап закрыт",
                   "scope": {"documents": ["spec:alpha", "spec:beta"]},
                   "actions": [
                       {"action": "archive_document", "document": "spec:alpha",
                        "reason": "a", "successor": None},
                       {"action": "archive_document", "document": "spec:beta",
                        "reason": "b", "successor": None}]}
        _, plan_rel = self.save_plan(payload)
        code, out, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err + out)
        self.assertTrue(self.archived("alpha").is_file())
        self.assertTrue(self.archived("beta").is_file())
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_archive_plan_document_with_done_tasks(self):
        write_card(self.root / "docs" / "archive" / "tasks", "DEV-9",
                   "done", closed="2026-02-01T00:00:00Z")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        self.add_plan("release", tasks=["DEV-9"])
        _, plan_rel = self.save_plan(request_json("plan:release"))
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "docs" / "archive" / "plans"
                         / "release.md").is_file())


class ArchiveRefusalTest(LifecycleCase):
    """Каждый отказ — до записи: дерево неизменно."""

    def assert_refused(self, payload, fragment):
        self.add_spec("alpha")
        rel = self.request(payload)
        before = self.project_fingerprint()
        code, _, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 1)
        self.assertIn(fragment, err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_non_final_state_refused(self):
        self.add_spec("alpha", state="accepted")
        rel = self.request(request_json("spec:alpha"))
        before = tree_fingerprint(self.root)
        code, _, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 1)
        self.assertIn("не финальное", err)
        self.assertEqual(tree_fingerprint(self.root), before)

    def test_legacy_document_refused(self):
        write_doc(self.specs, "alpha.md", None)
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("register", err)

    def test_already_archived_refused(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err)
        # Повторный запрос на тот же документ — отказ на этапе plan.
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha"),
                                             "request-2.json"))
        self.assertEqual(code, 1)
        self.assertIn("уже в архиве", err)

    def test_destination_collision_refused(self):
        self.add_spec("alpha")
        blocker = self.archived("alpha")
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("# Чужой\n", encoding="utf-8")
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("Назначение занято", err)

    def test_unconfirmed_external_required_refused(self):
        self.add_spec("alpha", materials=[
            {"id": "m-ext", "role": "other", "source": "https://ex.com/x",
             "storage": "external", "required": True, "immutable": False,
             "used_by": [], "access": "ключ в хранилище"}])
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("m-ext", err)
        # Подтверждённый external — план строится.
        code, out, err = self.cli(
            "lifecycle", "plan",
            self.request(request_json("spec:alpha",
                                      confirmed_materials=["m-ext"]),
                         "request-2.json"))
        self.assertEqual(code, 0, err)

    def test_unknown_confirmed_material_refused(self):
        self.assert_refused(request_json("spec:alpha",
                                         confirmed_materials=["nope"]),
                            "неизвестные id")

    def test_unaccounted_evidence_refused(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "stray.bin").write_bytes(b"x")
        self.assert_refused(request_json("spec:alpha"), "неучтённые")

    def test_work_area_material_refused(self):
        live = self.root / "work" / "lifecycle" / "data"
        live.mkdir(parents=True)
        (live / "x.bin").write_bytes(b"x")
        self.add_spec("alpha", materials=[local_material(
            "m-live", "work/lifecycle/data", required=False)])
        rel = self.request(request_json("spec:alpha"))
        before = self.project_fingerprint()
        code, _, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 1)
        self.assertIn("work/", err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_missing_material_refused(self):
        self.add_spec("alpha", materials=[local_material(
            "m-1", "docs/specs/alpha-evidence/missing.md",
            required=False)])
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("отсутствует", err)

    def test_scope_mismatch_refused(self):
        payload = request_json("spec:alpha")
        payload["scope"]["documents"].append("spec:beta")
        self.assert_refused(payload, "не совпадает")

    def test_close_task_unknown_task_refused(self):
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(task_request("DEV-9")))
        self.assertEqual(code, 1)
        self.assertIn("Нет задачи", err)

    def test_scope_tasks_mismatch_refused(self):
        payload = task_request("DEV-9")
        payload["scope"]["tasks"].append("DEV-10")
        self.assert_refused(payload, "не совпадает")

    def test_unknown_action_refused(self):
        self.add_spec("alpha")
        payload = request_json("spec:alpha")
        payload["actions"][0]["action"] = "explode"
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(payload))
        self.assertEqual(code, 1)
        self.assertIn("неизвестное действие", err)

    def test_successor_missing_refused(self):
        self.assert_refused(request_json("spec:alpha",
                                         successor="spec:ghost"),
                            "successor")

    def test_successor_within_same_operation_refused(self):
        self.add_spec("alpha")
        self.add_spec("beta", document_id="spec:beta")
        payload = {"schema_version": 1,
                   "actor": {"name": "Devin", "role": "executor"},
                   "basis": "b", "scope": {"documents": ["spec:alpha",
                                                       "spec:beta"]},
                   "actions": [
                       {"action": "archive_document", "document": "spec:alpha",
                        "reason": "r", "successor": "spec:beta"},
                       {"action": "archive_document", "document": "spec:beta",
                        "reason": "r", "successor": None}]}
        rel = self.request(payload)
        code, _, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 1)
        self.assertIn("преемник", err)

    def test_plan_document_with_open_tasks_refused(self):
        write_card(self.root / "docs" / "tasks", "DEV-3", "in_progress")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        self.add_plan("release", tasks=["DEV-3"])
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("plan:release")))
        self.assertEqual(code, 1)
        self.assertIn("незаверш", err)

    def test_bad_request_shapes_refused(self):
        self.add_spec("alpha")
        for bad in ("{", "[1]", json.dumps({"schema_version": 2}),
                    json.dumps({"schema_version": 1, "actor": {},
                                "basis": "b",
                                "scope": {"documents": ["spec:alpha"]},
                                "actions": []})):
            path = self.root / "work" / "bad.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(bad, encoding="utf-8")
            code, _, err = self.cli("lifecycle", "plan", "work/bad.json")
            self.assertEqual(code, 1, bad)

    def test_wikilink_conflict_refused(self):
        self.add_spec("alpha")
        self.add_spec("beta", document_id="spec:beta", state="accepted",
                      body="См. [[alpha]].\n")
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("ручного разбора", err)

    def test_no_documents_section_refused(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        del project["documents"]
        path.write_text(json.dumps(project) + "\n", encoding="utf-8")
        write_doc(self.specs, "alpha.md", None)
        code, _, err = self.cli("lifecycle", "plan",
                                self.request(request_json("spec:alpha")))
        self.assertEqual(code, 1)
        self.assertIn("documents", err)


class StalePlanTest(LifecycleCase):
    def test_input_change_after_plan_refused(self):
        self.add_spec("alpha")
        self.add_spec("beta", document_id="spec:beta", state="accepted",
                      body="См. [alpha](alpha.md).\n")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        beta = self.specs / "beta.md"
        beta.write_text(beta.read_text(encoding="utf-8") + "новая строка\n",
                        encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertTrue((self.specs / "alpha.md").is_file())
        self.assertFalse(self.archived("alpha").exists())

    def test_catalog_change_after_plan_refused(self):
        # Новый документ коллекции, появившийся после планирования, меняет
        # состав каталога и входов — сохранённый план устаревает.
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        self.add_spec("gamma", document_id="spec:gamma", state="accepted",
                      body="См. [alpha](alpha.md).\n")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertTrue((self.specs / "alpha.md").is_file())

    def test_new_evidence_file_after_plan_refused(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("Отчёт\n", encoding="utf-8")
        self.add_spec("alpha", materials=[
            local_material("m-1", "docs/specs/alpha-evidence")])
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        (evidence / "late.bin").write_bytes(b"late")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertTrue((self.specs / "alpha.md").is_file())
        self.assertTrue((evidence / "late.bin").is_file())

    def test_tampered_plan_refused(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        plan_path = self.root / plan_rel
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["moves"]["docs/specs/alpha.md"] = "docs/archive/specs/evil.md"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertTrue((self.specs / "alpha.md").is_file())
        self.assertFalse((self.root / "docs" / "archive" / "specs" / "evil.md")
                         .exists())

    def test_tampered_request_in_plan_refused(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        plan_path = self.root / plan_rel
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["request"]["actions"][0]["reason"] = "подменено"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)

    def test_plan_from_other_root_refused(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        plan_path = self.root / plan_rel
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["root"] = "/tmp/other-project"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("другого корня", err)


class InterruptRestoreTest(LifecycleCase):
    def test_crash_mid_apply_then_restore(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("Отчёт\n", encoding="utf-8")
        self.add_spec("alpha", materials=[
            local_material("m-1", "docs/specs/alpha-evidence")])
        # Свежий индекс до прерывания: восстановление возвращает это состояние.
        code, _, err = self.cli("index", "--documents")
        self.assertEqual(code, 0, err)
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        before = self.project_fingerprint()
        original = fops._atomic_write
        calls = {"count": 0}

        def crash(target, content, op_id):
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt()
            return original(target, content, op_id)

        fops._atomic_write = crash
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.cli("lifecycle", "apply", plan_rel)
        finally:
            fops._atomic_write = original
        # Маркер остался, обычные команды заблокированы.
        pending = fops.pending_info(self.root)
        self.assertIsNotNone(pending)
        code, _, err = self.cli("index")
        self.assertEqual(code, 1)
        self.assertIn("Незаверш", err)
        code, out, err = self.cli("lifecycle", "status")
        self.assertEqual(code, 0, err)
        self.assertIn(pending["op_id"], out)
        # Восстановление возвращает исходное дерево.
        code, out, err = self.cli("lifecycle", "restore")
        self.assertEqual(code, 0, err + out)
        self.assertIsNone(fops.pending_info(self.root))
        self.assertEqual(self.project_fingerprint(), before)
        self.assertTrue((self.specs / "alpha.md").is_file())
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err)

    def test_restore_without_pending_refused(self):
        code, _, err = self.cli("lifecycle", "restore")
        self.assertEqual(code, 1)
        self.assertIn("Нет незаверш", err)

    def test_apply_failure_restores_preimage(self):
        evidence = self.specs / "alpha-evidence"
        evidence.mkdir(parents=True)
        (evidence / "r.md").write_text("Отчёт\n", encoding="utf-8")
        self.add_spec("alpha", materials=[
            local_material("m-1", "docs/specs/alpha-evidence")])
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        # Отказ финальной проверки → полный откат силами engine; staged
        # проверки остаются настоящими.
        before = self.project_fingerprint()
        original_run = fops.run_operation

        def run_broken(root, spec):
            spec = dict(spec)
            spec["final_checks"] = [lambda root_: playbook.require(
                False, "final check boom")]
            return original_run(root, spec, playbook.safe_path,
                                playbook.require)

        ctx = {
            "config": playbook.config,
            "read_tasks": playbook.read_tasks,
            "scan_files": playbook._scan_markdown,
            "core_prefix": playbook.CORE,
            "collection_index_write": playbook._collection_index_write,
            "check_links": playbook.check_links,
            "version": lambda root: "0.0.2",
            "run_operation": run_broken,
        }
        with self.assertRaisesRegex(ValueError, "final check boom"):
            lifecycle.apply_command(self.root, plan_rel, ctx,
                                    playbook.safe_path, playbook.require)
        self.assertIsNone(fops.pending_info(self.root))
        self.assertEqual(self.project_fingerprint(), before)


class StatusTest(LifecycleCase):
    def test_status_lists_saved_plans(self):
        self.add_spec("alpha")
        _, plan_rel = self.save_plan(request_json("spec:alpha"))
        code, out, err = self.cli("lifecycle", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("Незавершённых операций", out)
        self.assertIn(Path(plan_rel).name, out)


if __name__ == "__main__":
    unittest.main()
