"""PB-044: misplaced task evidence and recoverable historical migration."""
import json

from test_close_lifecycle import CloseLifecycleCase, managed_card
from test_documents import local_material
from test_lifecycle import task_request
from test_playbook import playbook, write_card

lifecycle = playbook.lifecycle
fops = playbook.file_operations


class MaterialRepairTest(CloseLifecycleCase):
    def fixture(self, ident="DEV-1", status="done"):
        write_card(self.archive_dir(), ident, status, executor="Devin",
                   model="SWE-2 High", closed="2026-10-02T07:15:25+03:00")
        self.assertEqual(self.cli("index")[0], 0)
        src = self.tasks_dir() / f"{ident}-evidence"
        src.mkdir()
        (src / "nested").mkdir()
        (src / "empty").mkdir()
        # Historical link must resolve after the move without rewriting bytes.
        (src / "report.md").write_text("[card](../../archive/tasks/" + ident + ".md)\n")
        (src / "nested" / ".hidden").write_bytes(b"\x00\xff\r\n")
        return src

    def payload(self, *ids):
        req = task_request(ids[0], action="repair_task_materials", reason="старый close")
        req["scope"]["tasks"] = list(ids)
        req["actions"] = [dict(req["actions"][0], task=i) for i in ids]
        return req

    def test_check_rejects_archived_leftovers_with_path(self):
        self.fixture()
        before = self.project_fingerprint()
        code, _, err = self.cli("check", "--ready")
        self.assertEqual(code, 1)
        self.assertIn("DEV-1", err)
        self.assertIn("docs/tasks/DEV-1-evidence", err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_check_rejects_active_card_with_archive_evidence(self):
        self.managed_task()
        (self.archive_dir() / "DEV-1-evidence").mkdir(parents=True)
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("docs/archive/tasks/DEV-1-evidence", err)

    def test_check_rejects_both_locations_and_empty_leftover(self):
        self.fixture()
        (self.archive_dir() / "DEV-1-evidence").mkdir(parents=True)
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("противоположной стороне", err)

    def test_preview_is_read_only(self):
        self.fixture()
        rel = self.request(self.payload("DEV-1"))
        before = self.project_fingerprint()
        code, out, err = self.cli("lifecycle", "plan", rel)
        self.assertEqual(code, 0, err)
        self.assertIn("repair_task_materials", out)
        self.assertEqual(self.project_fingerprint(), before)

    def test_apply_preserves_bytes_dates_links_and_repeat(self):
        src = self.fixture()
        original = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
        card = self.archive_dir() / "DEV-1.md"
        with card.open("a") as stream:
            stream.write("\n[report](../../tasks/DEV-1-evidence/report.md)\n")
        _, rel = self.save_plan(self.payload("DEV-1"))
        code, out, err = self.cli("lifecycle", "apply", rel)
        self.assertEqual(code, 0, err + out)
        dst = self.archive_dir() / src.name
        self.assertFalse(src.exists())
        self.assertTrue((dst / "empty").is_dir())
        for name, content in original.items():
            self.assertEqual((dst / name).read_bytes(), content)
        text = card.read_text()
        self.assertIn("Status: done", text)
        self.assertIn("Closed: 2026-10-02T07:15:25+03:00", text)
        self.assertIn("[report](DEV-1-evidence/report.md)", text)
        meta = self.meta_of(card)
        self.assertTrue(meta["materials"][0]["immutable"])
        self.assertNotIn("acceptance", meta)
        receipt = json.loads(next(dst.glob("repair-*.json")).read_text())
        self.assertEqual(len(receipt["files"]), len(original))
        self.assertEqual(self.cli("check", "--ready")[0], 0)
        before = self.project_fingerprint()
        self.assertEqual(self.cli("lifecycle", "apply", rel)[0], 0)
        self.assertEqual(self.project_fingerprint(), before)
        # Registered immutable evidence remains usable through normal reopen.
        code, out, err = self.apply_request(task_request("DEV-1", action="reopen"))
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.tasks_dir() / src.name / "report.md").is_file())
        self.assertEqual(self.cli("check", "--ready")[0], 0)

    def test_batch_repair_and_cancelled_status(self):
        self.fixture()
        self.fixture("DEV-2", "cancelled")
        _, rel = self.save_plan(self.payload("DEV-1", "DEV-2"))
        code, out, err = self.cli("lifecycle", "apply", rel)
        self.assertEqual(code, 0, err + out)
        self.assertIn("Status: cancelled", (self.archive_dir() / "DEV-2.md").read_text())
        self.assertEqual(self.cli("check", "--ready")[0], 0)

    def test_unselected_leftover_and_missing_source_refused(self):
        self.fixture()
        self.fixture("DEV-2")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("DEV-2", err)
        import shutil
        shutil.rmtree(self.tasks_dir() / "DEV-2-evidence")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-2")))
        self.assertEqual(code, 1)
        self.assertIn("DEV-1", err)  # cannot hide another task's violation
        shutil.rmtree(self.tasks_dir() / "DEV-1-evidence")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-2")))
        self.assertEqual(code, 1)
        self.assertIn("нет каталога", err)

    def test_stale_plan_after_new_material_refused(self):
        src = self.fixture()
        _, rel = self.save_plan(self.payload("DEV-1"))
        (src / "new.log").write_text("late evidence")
        before = self.project_fingerprint()
        code, _, err = self.cli("lifecycle", "apply", rel)
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_collision_refused_without_writes(self):
        self.fixture()
        (self.archive_dir() / "DEV-1-evidence").mkdir()
        before = self.project_fingerprint()
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("занято", err)
        self.assertEqual(self.project_fingerprint(), before)

    def test_symlink_refused(self):
        src = self.fixture()
        (src / "link").symlink_to(self.archive_dir() / "DEV-1.md")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("ссылка", err)
        self.assertTrue(src.exists())

    def test_managed_or_active_task_refused(self):
        self.fixture()
        managed_card(self.archive_dir(), "DEV-1", "done")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("legacy", err)
        (self.archive_dir() / "DEV-1.md").unlink()
        write_card(self.tasks_dir(), "DEV-1", "in_review")
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("архивной", err)

    def test_existing_foreign_ownership_refused(self):
        src = self.fixture()
        self.add_spec("alpha", materials=[local_material("other", "docs/tasks/DEV-1-evidence")])
        code, _, err = self.cli("lifecycle", "plan", self.request(self.payload("DEV-1")))
        self.assertEqual(code, 1)
        self.assertIn("принадлежат", err)
        self.assertTrue(src.is_dir())

    def test_final_failure_rolls_back_complete_tree(self):
        self.fixture()
        _, rel = self.save_plan(self.payload("DEV-1"))
        before = self.project_fingerprint()
        ctx = playbook._lifecycle_ctx(None)
        def broken(root, spec):
            spec = dict(spec)
            spec["final_checks"] = [lambda _: playbook.require(False, "repair rollback probe")]
            return fops.run_operation(root, spec, playbook.safe_path, playbook.require)
        ctx["run_operation"] = broken
        with self.assertRaisesRegex(ValueError, "repair rollback probe"):
            lifecycle.apply_command(self.root, rel, ctx, playbook.safe_path, playbook.require)
        self.assertEqual(self.project_fingerprint(), before)
        self.assertIsNone(fops.pending_info(self.root))
