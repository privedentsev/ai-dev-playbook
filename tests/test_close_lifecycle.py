import json
import unittest
from pathlib import Path

from test_cli import PASS_CHECK
from test_lifecycle import LifecycleCase, task_request
from test_playbook import playbook, write_card

lifecycle = playbook.lifecycle
documents = playbook.documents
document_links = playbook.document_links
fops = playbook.file_operations

CARD = ("# {ident}: test\n\nID: {ident}\nStatus: {status}\nPriority: P2\n"
        "Depends-On: {deps}\nExecutor: Devin\nModel: SWE-2 High\n"
        "Created: 2026-01-01T00:00:00Z\nClosed: {closed}\n")

TASK_META = {"schema_version": 1, "relations": [], "materials": [],
             "moves": []}


def managed_card(directory, ident, status, deps="-", meta=None,
                 closed=None, body=""):
    """Карточка с блоком метаданных (managed-режим PB-026)."""
    directory.mkdir(parents=True, exist_ok=True)
    if closed is None:
        closed = "unknown" if status in {"done", "cancelled"} else "-"
    text = CARD.format(ident=ident, status=status, deps=deps, closed=closed)
    raw = json.dumps(meta if meta is not None else dict(TASK_META),
                     ensure_ascii=False, indent=2)
    text += (f"\n{documents.META_START}\n```json\n{raw}\n```\n"
             f"{documents.META_END}\n{body}")
    (directory / f"{ident}.md").write_text(text, encoding="utf-8")


def promote_request(doc_id, change, payload="Новая редакция.\n",
                    acceptance=None):
    return {"schema_version": 1,
            "actor": {"name": "Devin", "role": "executor",
                      "model": "SWE-2 High"},
            "basis": "Аудит принял изменение",
            "scope": {"documents": [doc_id]},
            "actions": [{"action": "promote_change", "document": doc_id,
                         "change": change, "payload": payload,
                         "acceptance": acceptance}]}


def promotion_receipt(root, doc_path, action):
    """Synthetic acceptance fixture; does not assert real auditor identity."""
    meta = documents.parse_meta(doc_path.read_text(), str(doc_path), [])
    receipt = {"schema_version": 1, "document": action["document"],
               "change": action["change"], "revision": meta["current_revision"],
               "result": "accepted", "kind": "acceptance",
               "actor": {"name": "Test auditor", "role": "auditor"},
               "at": "2026-10-22T13:00:00+03:00",
               "document_sha256": fops.sha256(doc_path.read_bytes()),
               "payload_sha256": fops.sha256(action["payload"].encode())}
    path = root / "work" / (action["change"] + "-acceptance.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt))
    action["acceptance"] = path.relative_to(root).as_posix()
    return path


class CloseLifecycleCase(LifecycleCase):
    """Проект с documents, managed-карточками и настроенным verify."""

    def setUp(self):
        super().setUp()
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["verification"] = {"inputs": ["docs"],
                                   "checks": [PASS_CHECK]}
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2)
                        + "\n", encoding="utf-8")

    def tasks_dir(self):
        return self.root / "docs" / "tasks"

    def archive_dir(self):
        return self.root / "docs" / "archive" / "tasks"

    def managed_task(self, ident="DEV-1", status="in_review", deps="-",
                     meta=None, body=""):
        managed_card(self.tasks_dir(), ident, status, deps=deps, meta=meta,
                     body=body)
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def verify(self, ident="DEV-1"):
        code, out, err = self.cli("verify", ident)
        self.assertEqual(code, 0, err + out)

    def handoff(self, *args, ident="DEV-1"):
        return self.cli("lifecycle", "handoff", ident,
                        "--summary", "реализация готова", "--actor", "Devin",
                        "--model", "SWE-2 High", *args)

    def accept(self, *args, ident="DEV-1"):
        return self.cli("lifecycle", "accept", ident, "--actor", "Auditor",
                        "--model", "SWE-2 High", *args)

    def apply_request(self, payload):
        _, plan_rel = self.save_plan(payload)
        return self.cli("lifecycle", "apply", plan_rel)

    def prepare_done(self, ident="DEV-1", **kwargs):
        self.managed_task(ident, **kwargs)
        self.verify(ident)
        self.assertEqual(self.handoff(ident=ident)[0], 0)
        self.assertEqual(
            self.accept("--result", "accepted", ident=ident)[0], 0)


class CombinedClosureTest(CloseLifecycleCase):
    """Совмещённые операции close и archive плана в одном запросе."""
    def test_combined_close_and_plan_archive(self):
        """Совмещённые close и archive плана в одном запросе"""
        self.managed_task()
        self.add_plan("block", tasks=["DEV-1"])
        self.assertEqual(self.cli('index', '--all')[0], 0)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept("--result", "accepted")[0], 0)
        req = task_request("DEV-1")
        req["scope"]["documents"] = ["plan:block"]
        req["actions"].insert(0, {"action": "archive_document", "document": "plan:block",
                                   "reason": "completed", "successor": None})
        code, out, err = self.apply_request(req)
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.archive_dir() / "DEV-1.md").is_file())
        self.assertTrue((self.root / "docs/archive/plans/block.md").is_file())

    def test_combined_close_refuses_without_acceptance(self):
        """Совмещённый close без приёмки отклоняется"""
        self.managed_task()
        self.add_plan("block", tasks=["DEV-1"])
        self.assertEqual(self.cli('index', '--all')[0], 0)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        req = task_request("DEV-1")
        req["scope"]["documents"] = ["plan:block"]
        req["actions"].append({"action": "archive_document", "document": "plan:block",
                               "reason": "completed", "successor": None})
        original = (self.tasks_dir() / "DEV-1.md").read_bytes()
        code, _, _ = self.cli("lifecycle", "plan", self.request(req))
        self.assertEqual(code, 1)
        self.assertEqual((self.tasks_dir() / "DEV-1.md").read_bytes(), original)
        self.assertTrue((self.root / "docs/plans/block.md").is_file())

    def test_combined_close_refuses_unfinished_task_outside_scope(self):
        """Совмещённый close отклоняет незавершённую задачу вне scope"""
        self.managed_task()
        self.managed_task("DEV-2")
        self.add_plan("block", tasks=["DEV-1", "DEV-2"])
        self.assertEqual(self.cli('index', '--all')[0], 0)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept("--result", "accepted")[0], 0)
        req = task_request("DEV-1")
        req["scope"]["documents"] = ["plan:block"]
        req["actions"].append({"action": "archive_document", "document": "plan:block",
                               "reason": "completed", "successor": None})
        code, _, err = self.cli("lifecycle", "plan", self.request(req))
        self.assertEqual(code, 1)
        self.assertIn("DEV-2", err)
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertTrue((self.root / "docs/plans/block.md").is_file())


class HandoffAcceptTest(CloseLifecycleCase):
    """handoff/accept: регистрация прогонов и проекций, отказы без
    verify и handoff."""
    def test_handoff_registers_runs_and_projection(self):
        """handoff регистрирует прогоны verify и проекцию задачи"""
        self.managed_task()
        self.verify()
        code, out, err = self.handoff()
        self.assertEqual(code, 0, err + out)
        meta = self.meta_of(self.tasks_dir() / "DEV-1.md")
        handoff = meta["handoff"]
        self.assertEqual(handoff["actor"]["name"], "Devin")
        self.assertEqual(len(handoff["reports"]), 1)
        run_id = handoff["reports"][0]
        material = next(item for item in meta["materials"]
                        if item["id"] == run_id)
        self.assertTrue(material["source"].startswith(
            "work/verification/DEV-1/"))
        projection = lifecycle.card_projection(
            (self.tasks_dir() / "DEV-1.md").read_text(encoding="utf-8"))
        self.assertEqual(handoff["card_projection"], projection)

    def test_handoff_without_verify_refused(self):
        """handoff без свежего verify отклоняется"""
        self.managed_task()
        code, _, err = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn("verify", err)

    def test_handoff_legacy_card_refused(self):
        """handoff legacy-карточки в managed-проекте отклоняется"""
        write_card(self.tasks_dir(), "DEV-1", "in_review")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        self.verify()
        code, _, err = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn("метаданных", err)

    def test_accept_without_handoff_refused(self):
        """accept без handoff отклоняется"""
        self.managed_task()
        code, _, err = self.accept("--result", "accepted")
        self.assertEqual(code, 1)
        self.assertIn("передачи", err)

    def test_accept_records_result_and_link(self):
        """accept записывает результат и ссылку приёмки"""
        self.managed_task()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        code, out, err = self.accept("--result", "accepted",
                                     "--note", "проверено")
        self.assertEqual(code, 0, err + out)
        meta = self.meta_of(self.tasks_dir() / "DEV-1.md")
        acceptance = meta["acceptance"]
        self.assertEqual(acceptance["result"], "accepted")
        self.assertEqual(acceptance["kind"], "acceptance")
        self.assertEqual(acceptance["role"], "auditor")
        self.assertEqual(acceptance["handoff_at"], meta["handoff"]["at"])
        self.assertEqual(acceptance["detail"], "проверено")


class ManagedCloseTest(CloseLifecycleCase):
    """close управляемых карточек: обязательны handoff и accept,
    свежесть входов и карточки."""
    def prepare_done(self, ident="DEV-1", **kwargs):
        self.managed_task(ident, **kwargs)
        self.verify(ident)
        self.assertEqual(self.handoff(ident=ident)[0], 0)
        self.assertEqual(
            self.accept("--result", "accepted", ident=ident)[0], 0)

    def test_close_done_managed_card(self):
        """close done управляемой карточки после handoff и accept"""
        self.prepare_done()
        code, out, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err + out)
        archived = self.archive_dir() / "DEV-1.md"
        self.assertTrue(archived.is_file())
        text = archived.read_text(encoding="utf-8")
        self.assertIn("Status: done", text)
        meta = self.meta_of(archived)
        self.assertEqual(meta["handoff"]["actor"]["name"], "Devin")
        companions = list((self.archive_dir() / "DEV-1-evidence")
                          .glob("close-*.json"))
        self.assertEqual(len(companions), 1)
        body = json.loads(companions[0].read_text(encoding="utf-8"))
        self.assertEqual(body["type"], "lifecycle-close")
        self.assertEqual(body["task"], "DEV-1")
        self.assertEqual(body["outcome"], "done")
        self.assertEqual(body["policy"], "managed")
        self.assertIsNotNone(body["handoff"])
        self.assertIsNotNone(body["acceptance"])
        # Прогон verify перенесён как материал.
        moved = list((self.archive_dir() / "DEV-1-evidence").rglob(
            "report.json"))
        self.assertEqual(len(moved), 1)

    def test_close_done_without_handoff_refused(self):
        """close done без handoff отклоняется"""
        self.managed_task()
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("передачи", err)
        self.assertTrue((self.tasks_dir() / "DEV-1.md").exists())

    def test_close_done_without_acceptance_refused(self):
        """close done без приёмки отклоняется"""
        self.managed_task()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("приёмки", err)

    def test_close_done_card_changed_after_handoff_refused(self):
        """Изменение карточки после handoff блокирует close"""
        self.prepare_done(body="## Цель\n\nИсходная цель.\n")
        card = self.tasks_dir() / "DEV-1.md"
        card.write_text(card.read_text(encoding="utf-8")
                        .replace("Исходная цель", "Другая цель"),
                        encoding="utf-8")
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("изменилась после передачи", err)

    def test_close_done_stale_inputs_refused(self):
        """Устаревшие входы блокируют close done"""
        self.prepare_done()
        # Вход verify (docs/) изменился после прогона — отчёт несвежий.
        (self.root / "docs" / "notes.md").write_text("заметка\n",
                                                     encoding="utf-8")
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("свежего успешного прогона", err)

    def test_close_done_in_progress_refused(self):
        """close done из in_progress отклоняется"""
        self.managed_task(status="in_progress")
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("in_review", err)

    def test_close_cancelled_with_acceptance(self):
        """close cancelled с зафиксированной приёмкой"""
        self.managed_task(status="in_progress")
        self.assertEqual(
            self.accept("--result", "cancelled", "--kind", "acceptance")[0],
            0)
        code, out, err = self.cli("close", "DEV-1", "--status", "cancelled",
                                  "--reason", "неактуально")
        self.assertEqual(code, 0, err + out)
        text = (self.archive_dir() / "DEV-1.md").read_text(encoding="utf-8")
        self.assertIn("Status: cancelled", text)

    def test_close_cancelled_without_reason_refused(self):
        """close cancelled без причины отклоняется"""
        self.managed_task(status="in_progress")
        self.assertEqual(
            self.accept("--result", "cancelled", "--kind", "acceptance")[0],
            0)
        code, _, err = self.cli("close", "DEV-1", "--status", "cancelled")
        self.assertEqual(code, 1)
        self.assertIn("--reason", err)

    def test_close_cancelled_without_acceptance_refused(self):
        """close cancelled без приёмки отклоняется"""
        self.managed_task(status="in_progress")
        code, _, err = self.cli("close", "DEV-1", "--status", "cancelled",
                                "--reason", "неактуально")
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)

    def test_diagnostic_acceptance_not_enough(self):
        """Диагностическая приёмка не достаточна для close"""
        self.managed_task()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(
            self.accept("--result", "accepted", "--kind", "diagnostic")[0],
            0)
        code, _, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("diagnostic", err)

    def test_close_dry_run_writes_nothing(self):
        """close --dry-run не пишет изменений"""
        self.prepare_done()
        before = self.project_fingerprint()
        code, out, err = self.cli("close", "DEV-1", "--dry-run")
        self.assertEqual(code, 0, err + out)
        self.assertIn("План", out)
        self.assertIn("close_task DEV-1", out)
        self.assertEqual(self.project_fingerprint(), before)

    def test_close_legacy_card_in_managed_project(self):
        """Карточка без метаданных в проекте с documents — legacy-политика."""
        write_card(self.tasks_dir(), "DEV-1", "in_review",
                   executor="Devin", model="SWE-2 High")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        evidence = self.tasks_dir() / "DEV-1-evidence"
        evidence.mkdir()
        (evidence / "note.md").write_text("заметка\n", encoding="utf-8")
        code, out, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.archive_dir() / "DEV-1.md").is_file())
        self.assertTrue((self.archive_dir() / "DEV-1-evidence"
                         / "note.md").is_file())


class ReopenTest(CloseLifecycleCase):
    """reopen: возврат карточки, снятие приёмки, блокировки активных и
    зависимых задач."""
    def archived_done(self, ident="DEV-1", deps="-", managed=True):
        """Закрытая done-карточка в архиве (напрямую, минуя операции)."""
        write_card(self.archive_dir(), ident, "done", deps=deps,
                   executor="Devin", model="SWE-2 High")
        if managed:
            card = self.archive_dir() / f"{ident}.md"
            raw = json.dumps(dict(TASK_META), ensure_ascii=False, indent=2)
            card.write_text(
                card.read_text(encoding="utf-8")
                + f"\n{documents.META_START}\n```json\n{raw}\n```\n"
                + f"{documents.META_END}\n", encoding="utf-8")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def test_reopen_returns_card_and_clears_acceptance(self):
        """reopen возвращает карточку и снимает приёмку"""
        self.archived_done()
        code, out, err = self.apply_request(task_request(
            "DEV-1", action="reopen", status="in_progress",
            reason="находки аудита"))
        self.assertEqual(code, 0, err + out)
        card = self.tasks_dir() / "DEV-1.md"
        self.assertTrue(card.is_file())
        self.assertFalse((self.archive_dir() / "DEV-1.md").exists())
        text = card.read_text(encoding="utf-8")
        self.assertIn("Status: in_progress", text)
        self.assertIn("Closed: -", text)
        self.assertIn("## Возврат", text)
        self.assertIn("находки аудита", text)
        meta = self.meta_of(card)
        self.assertNotIn("handoff", meta)
        self.assertNotIn("acceptance", meta)
        companions = list((self.tasks_dir() / "DEV-1-evidence")
                          .glob("reopen-*.json"))
        self.assertEqual(len(companions), 1)
        body = json.loads(companions[0].read_text(encoding="utf-8"))
        self.assertEqual(body["type"], "lifecycle-reopen")
        self.assertEqual(body["previous_status"], "done")
        index = (self.tasks_dir() / "index.md").read_text(encoding="utf-8")
        self.assertIn("(DEV-1.md)", index.split("## Архив")[0])

    def test_reopen_blocks_dependents(self):
        """reopen блокирует зависимые задачи"""
        self.archived_done("DEV-1")
        self.archived_done("DEV-2", deps="DEV-1")
        code, out, err = self.apply_request(task_request(
            "DEV-2", action="reopen", status="in_progress",
            reason="переоткрытие"))
        self.assertEqual(code, 0, err + out)
        # DEV-1 осталась done — её зависимый DEV-2 вернулся: граф не
        # нарушен, т.к. DEV-1 не зависит от DEV-2.

    def test_reopen_with_done_dependent_refused(self):
        """reopen при done-зависимой отклоняется"""
        self.archived_done("DEV-1")
        self.archived_done("DEV-2", deps="DEV-1")
        before = self.project_fingerprint()
        code, _, err = self.cli(
            "lifecycle", "plan",
            self.request(task_request("DEV-1", action="reopen",
                                      status="in_progress",
                                      reason="возврат")))
        self.assertEqual(code, 1)
        self.assertIn("недопустимый граф", err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_reopen_active_dependent_blocked(self):
        """Активная зависимая блокирует reopen"""
        self.archived_done("DEV-1")
        write_card(self.tasks_dir(), "DEV-2", "in_progress", deps="DEV-1",
                   executor="Devin", model="SWE-2 High")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, out, err = self.apply_request(task_request(
            "DEV-1", action="reopen", status="in_progress",
            reason="исправление"))
        self.assertEqual(code, 0, err + out)
        dep_text = (self.tasks_dir() / "DEV-2.md").read_text(
            encoding="utf-8")
        self.assertIn("Status: blocked", dep_text)
        self.assertIn("## Блокировка", dep_text)

    def test_reopen_combined_with_dependent(self):
        """Совмещённый reopen зависимой карточки"""
        self.archived_done("DEV-1")
        self.archived_done("DEV-2", deps="DEV-1")
        payload = task_request("DEV-1", action="reopen",
                               status="in_progress", reason="возврат")
        payload["actions"].append({"action": "reopen", "task": "DEV-2",
                                   "status": "blocked",
                                   "reason": "возврат"})
        payload["scope"]["tasks"].append("DEV-2")
        code, out, err = self.apply_request(payload)
        self.assertEqual(code, 0, err + out)
        self.assertIn("Status: in_progress",
                      (self.tasks_dir() / "DEV-1.md")
                      .read_text(encoding="utf-8"))
        self.assertIn("Status: blocked",
                      (self.tasks_dir() / "DEV-2.md")
                      .read_text(encoding="utf-8"))

    def test_reopen_active_card_refused(self):
        """reopen активной карточки отклоняется"""
        write_card(self.tasks_dir(), "DEV-1", "in_progress")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli(
            "lifecycle", "plan",
            self.request(task_request("DEV-1", action="reopen",
                                      status="ready", reason="r")))
        self.assertEqual(code, 1)
        self.assertIn("не в архиве", err)

    def test_reopen_legacy_card(self):
        """reopen legacy-карточки в архиве"""
        self.archived_done(managed=False)
        evidence = self.archive_dir() / "DEV-1-evidence"
        evidence.mkdir()
        (evidence / "note.md").write_text("x\n", encoding="utf-8")
        code, out, err = self.apply_request(task_request(
            "DEV-1", action="reopen", status="ready", reason="возврат"))
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertTrue((self.tasks_dir() / "DEV-1-evidence"
                         / "note.md").is_file())


class PromoteChangeTest(CloseLifecycleCase):
    """promote изменений: ревизии и bump, квитанции приёмки, отказы
    по состояниям."""
    def spec_with_change(self, state="implemented"):
        basis = {"type": "engineer_approval", "who": "engineer",
                 "at": "2026-01-02T00:00:00Z", "detail": "утверждено",
                 "source": "review"}
        self.add_spec("alpha", state="accepted",
                      changes=[{"id": "chg-1", "state": state,
                                "section": "Область", "basis": basis,
                                "tasks": [], "acceptance": None}],
                      body="Прежняя редакция текста.\n")

    def test_promote_writes_revision_and_bumps(self):
        """promote изменений пишет ревизию и bump"""
        self.spec_with_change()
        request = promote_request("spec:alpha", "chg-1")
        promotion_receipt(self.root, self.specs / "alpha.md", request["actions"][0])
        code, out, err = self.apply_request(request)
        self.assertEqual(code, 0, err + out)
        text = (self.specs / "alpha.md").read_text(encoding="utf-8")
        self.assertIn("Новая редакция.", text)
        self.assertNotIn("Прежняя редакция текста.",
                         text.split(documents.META_END)[-1])
        meta = self.meta_of(self.specs / "alpha.md")
        self.assertEqual(meta["current_revision"], 2)
        change = meta["changes"][0]
        self.assertEqual(change["state"], "accepted")
        snapshot = self.specs / "alpha-evidence" / "revision-1.md"
        self.assertTrue(snapshot.is_file())
        self.assertIn("Прежняя редакция текста.",
                      snapshot.read_text(encoding="utf-8"))
        companions = list((self.specs / "alpha-evidence")
                          .glob("promote-*.json"))
        self.assertEqual(len(companions), 1)
        body = json.loads(companions[0].read_text(encoding="utf-8"))
        self.assertEqual(body["type"], "lifecycle-promote")
        self.assertEqual((body["revision_from"], body["revision_to"]),
                         (1, 2))

    def test_promotion_rejects_missing_wrong_and_stale_acceptance(self):
        """promote отклоняет отсутствующую, чужую и устаревшую приёмку"""
        self.spec_with_change()
        req = promote_request("spec:alpha", "chg-1")
        original = (self.specs / "alpha.md").read_bytes()
        code, _, _ = self.cli("lifecycle", "plan", self.request(req))
        self.assertEqual(code, 1)
        for key, value in [("result", "cancelled"), ("kind", "diagnostic"),
                           ("document", "spec:other"), ("change", "other"),
                           ("revision", 99), ("payload_sha256", "wrong"),
                           ("document_sha256", "wrong"), ("actor", {}), ("at", "invalid")]:
            with self.subTest(key=key):
                path = promotion_receipt(self.root, self.specs / "alpha.md", req["actions"][0])
                receipt = json.loads(path.read_text())
                receipt[key] = value
                path.write_text(json.dumps(receipt))
                code, _, _ = self.cli("lifecycle", "plan", self.request(req))
                self.assertEqual(code, 1)
                self.assertEqual((self.specs / "alpha.md").read_bytes(), original)

    def test_receipt_changed_after_plan_refuses_apply(self):
        """Изменённая после плана квитанция отклоняет apply"""
        self.spec_with_change()
        req = promote_request("spec:alpha", "chg-1")
        path = promotion_receipt(self.root, self.specs / "alpha.md", req["actions"][0])
        _, plan = self.save_plan(req)
        path.write_text(path.read_text() + " ")
        code, _, _ = self.cli("lifecycle", "apply", plan)
        self.assertEqual(code, 1)
        self.assertEqual(self.meta_of(self.specs / "alpha.md")["current_revision"], 1)

    def test_promote_not_implemented_refused(self):
        """promote нереализованного изменения отклоняется"""
        self.spec_with_change(state="approved")
        code, _, err = self.cli(
            "lifecycle", "plan",
            self.request(promote_request("spec:alpha", "chg-1")))
        self.assertEqual(code, 1)
        self.assertIn("implemented", err)

    def test_promote_unknown_change_refused(self):
        """promote неизвестного изменения отклоняется"""
        self.add_spec("alpha", state="accepted")
        code, _, err = self.cli(
            "lifecycle", "plan",
            self.request(promote_request("spec:alpha", "chg-9")))
        self.assertEqual(code, 1)
        self.assertIn("chg-9", err)

    def test_promote_archived_refused(self):
        """promote архивированного изменения отклоняется"""
        self.spec_with_change()
        # Архивирование требует финальное состояние коллекции.
        card = self.specs / "alpha.md"
        meta = self.meta_of(card)
        meta["state"] = "superseded"
        raw = json.dumps(meta, ensure_ascii=False, indent=2)
        text = card.read_text(encoding="utf-8")
        positions = documents._meta_positions(text.splitlines())
        start, end = positions
        lines = text.split("\n")
        card.write_text("\n".join(
            lines[:start] + [documents.META_START, "```json",
                             *raw.splitlines(), "```",
                             documents.META_END] + lines[end + 1:]),
            encoding="utf-8")
        _, plan_rel = self.save_plan(
            {"schema_version": 1,
             "actor": {"name": "Devin", "role": "executor"},
             "basis": "b", "scope": {"documents": ["spec:alpha"]},
             "actions": [{"action": "archive_document",
                          "document": "spec:alpha", "reason": "готово",
                          "successor": None}]})
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 0, err)
        code, _, err = self.cli(
            "lifecycle", "plan",
            self.request(promote_request("spec:alpha", "chg-1"),
                         "request-2.json"))
        self.assertEqual(code, 1)
        self.assertIn("архиве", err)


class CollectionIndexRetentionTest(CloseLifecycleCase):
    """Удержание индексов коллекций при close/reopen и проверках
    планов."""
    def setup_done_with_plan(self, ident="DEV-1", plan_slug="block"):
        self.managed_task(ident)
        self.add_plan(plan_slug, state="draft", tasks=[ident])
        code, out, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err + out)
        self.verify(ident)
        self.assertEqual(self.handoff(ident=ident)[0], 0)
        self.assertEqual(
            self.accept("--result", "accepted", ident=ident)[0], 0)

    def test_close_task_updates_active_plan_index_and_dry_run(self):
        """close задачи обновляет индекс активного плана, dry-run безопасен"""
        self.setup_done_with_plan("DEV-1")

        plan_index = self.root / "docs" / "plans" / "index.md"
        self.assertTrue(plan_index.is_file())
        self.assertIn("[DEV-1](../tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))

        # 1. Dry-run: предпросмотр показывает diff и не изменяет файлы проекта
        before = self.project_fingerprint()
        code, out, err = self.cli("close", "DEV-1", "--dry-run")
        self.assertEqual(code, 0, err + out)
        self.assertIn("docs/plans/index.md", out)
        self.assertIn("docs/archive/tasks/DEV-1.md", out)
        self.assertEqual(self.project_fingerprint(), before)
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertFalse((self.archive_dir() / "DEV-1.md").exists())
        self.assertIn("[DEV-1](../tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))

        # 2. Штатное закрытие: карточка в архиве, ссылки в индексе плана обновлены
        code, out, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.archive_dir() / "DEV-1.md").is_file())
        self.assertFalse((self.tasks_dir() / "DEV-1.md").exists())

        plan_text = plan_index.read_text(encoding="utf-8")
        self.assertIn("[DEV-1](../archive/tasks/DEV-1.md)", plan_text)
        self.assertNotIn("[DEV-1](../tasks/DEV-1.md)", plan_text)

        # 3. check --ready проходит
        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err + out)

        # 4. index --all не возвращает старые ссылки
        code, out, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err + out)
        self.assertIn("[DEV-1](../archive/tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))
        self.assertNotIn("[DEV-1](../tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))

    def test_reopen_restores_plan_index_and_blocks_active_dependent(self):
        """reopen восстанавливает индекс плана и блокирует активную зависимую"""
        self.setup_done_with_plan("DEV-1")
        self.assertEqual(self.cli("close", "DEV-1")[0], 0)

        plan_index = self.root / "docs" / "plans" / "index.md"
        self.assertIn("[DEV-1](../archive/tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))

        # Создаём активную зависимую задачу DEV-2 в статусе in_progress
        write_card(self.tasks_dir(), "DEV-2", "in_progress", deps="DEV-1",
                   executor="Devin", model="SWE-2 High")
        self.assertEqual(self.cli("index")[0], 0)

        # Reopen DEV-1
        reopen_req = task_request("DEV-1", action="reopen", status="in_progress",
                                  reason="доработка")
        code, out, err = self.apply_request(reopen_req)
        self.assertEqual(code, 0, err + out)

        # Карточка возвращена
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertFalse((self.archive_dir() / "DEV-1.md").exists())

        # Индекс плана указывает на активный путь
        plan_text = plan_index.read_text(encoding="utf-8")
        self.assertIn("[DEV-1](../tasks/DEV-1.md)", plan_text)
        self.assertNotIn("[DEV-1](../archive/tasks/DEV-1.md)", plan_text)

        # Активный зависимый заблокирован
        dep_text = (self.tasks_dir() / "DEV-2.md").read_text(encoding="utf-8")
        self.assertIn("Status: blocked", dep_text)
        self.assertIn("## Блокировка", dep_text)

        # check --ready проходит
        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err + out)

        # index --all сохраняет активные ссылки
        code, out, err = self.cli("index", "--all")
        self.assertEqual(code, 0, err + out)
        self.assertIn("[DEV-1](../tasks/DEV-1.md)", plan_index.read_text(encoding="utf-8"))

    def test_manual_links_across_collections_including_none_generator(self):
        """Ручные ссылки через коллекции, включая none-генератор"""
        self.managed_task("DEV-1")
        self.add_spec("alpha", state="draft")
        self.assertEqual(self.cli("index", "--all")[0], 0)

        spec_index = self.root / "docs" / "specs" / "index.md"
        orig_spec = spec_index.read_text(encoding="utf-8")
        # Добавляем ручной текст с ссылкой вне области генерации до verify
        spec_index.write_text("# Спецификации\n\nРучная ссылка: [DEV-1](../tasks/DEV-1.md).\n\n"
                              + orig_spec, encoding="utf-8")

        self.verify("DEV-1")
        self.assertEqual(self.handoff(ident="DEV-1")[0], 0)
        self.assertEqual(
            self.accept("--result", "accepted", ident="DEV-1")[0], 0)

        # Несвязанный индекс (adr)
        adr_index = self.root / "docs" / "adr" / "index.md"
        adr_before = adr_index.read_text(encoding="utf-8")

        # close DEV-1: _collection_index_write для spec вернёт None (генерация spec не менялась)
        code, out, err = self.cli("close", "DEV-1")
        self.assertEqual(code, 0, err + out)

        # Ручная ссылка в spec_index переписана в архив
        self.assertIn("Ручная ссылка: [DEV-1](../archive/tasks/DEV-1.md).",
                      spec_index.read_text(encoding="utf-8"))
        # Маркеры и область сохранены
        self.assertIn(document_links.INDEX_START, spec_index.read_text(encoding="utf-8"))

        # Несвязанный индекс adr остался нетронутым
        self.assertEqual(adr_index.read_text(encoding="utf-8"), adr_before)

        # check --ready проходит
        code, out, err = self.cli("check", "--ready")
        self.assertEqual(code, 0, err + out)

        # Reopen DEV-1: ручная ссылка возвращается к активному пути
        reopen_req = task_request("DEV-1", action="reopen", status="in_progress",
                                  reason="возврат")
        code, out, err = self.apply_request(reopen_req)
        self.assertEqual(code, 0, err + out)
        self.assertIn("Ручная ссылка: [DEV-1](../tasks/DEV-1.md).",
                      spec_index.read_text(encoding="utf-8"))
        self.assertEqual(adr_index.read_text(encoding="utf-8"), adr_before)

    def test_stale_plan_rejects_modified_collection_index(self):
        """Устаревший план отклоняет изменённый индекс коллекции"""
        self.setup_done_with_plan("DEV-1")

        # 1. Изменение индекса на диске после verify/plan делает входы несвежими
        _, plan_rel = self.save_plan(task_request("DEV-1"))
        plan_index = self.root / "docs" / "plans" / "index.md"
        plan_index.write_text(plan_index.read_text(encoding="utf-8") + "\n<!-- tamper -->\n",
                              encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel)
        self.assertEqual(code, 1)
        self.assertIn("изменились", err)

        # Карточка не перенесена частично
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertFalse((self.archive_dir() / "DEV-1.md").exists())

        # 2. Подмена хеша записи индекса в файле плана отвергается как подменённый план
        code, _, _ = self.cli("index", "--all")
        self.verify("DEV-1")
        self.assertEqual(self.handoff(ident="DEV-1")[0], 0)
        self.assertEqual(self.accept("--result", "accepted", ident="DEV-1")[0], 0)
        rel = self.request(task_request("DEV-1"), name="request-2.json")
        code, out, err = self.cli("lifecycle", "plan", rel, "--save")
        self.assertEqual(code, 0, err + out)
        plan_rel2 = out.split("План записан: ", 1)[1].split("\n", 1)[0]
        plan_path = self.root / plan_rel2
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_data["writes"]["docs/plans/index.md"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        plan_path.write_text(json.dumps(plan_data, ensure_ascii=False, indent=2), encoding="utf-8")
        code, _, err = self.cli("lifecycle", "apply", plan_rel2)
        self.assertEqual(code, 1)
        self.assertIn("План устарел или подменён", err)
        self.assertTrue((self.tasks_dir() / "DEV-1.md").is_file())
        self.assertFalse((self.archive_dir() / "DEV-1.md").exists())

    def test_staged_check_failure_restores_preimage(self):
        """Сбой staged-проверки восстанавливает пред-образ"""
        self.setup_done_with_plan("DEV-1")
        _, plan_rel = self.save_plan(task_request("DEV-1"))

        before = self.project_fingerprint()
        original_run = fops.run_operation

        def run_broken(root, spec):
            spec = dict(spec)
            spec["final_checks"] = [lambda root_: playbook.require(
                False, "final check failure")]
            return original_run(root, spec, playbook.safe_path,
                                playbook.require)

        ctx = dict(playbook._lifecycle_ctx(None))
        ctx["run_operation"] = run_broken
        with self.assertRaisesRegex(ValueError, "final check failure"):
            lifecycle.apply_command(self.root, plan_rel, ctx,
                                    playbook.safe_path, playbook.require)
        self.assertIsNone(fops.pending_info(self.root))
        self.assertEqual(self.project_fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
