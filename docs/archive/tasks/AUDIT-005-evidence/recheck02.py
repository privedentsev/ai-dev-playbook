"""Second independent recheck. Last two probes assert remaining defects."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from probes import Probes, CORE, SETTINGS
from recheck01 import Recheck
from test_cli import CliCase


class Recheck2(CliCase):
    cli = Probes.cli
    ok = Probes.ok
    saved = Probes.saved
    setup_release = Probes.setup_release
    interrupted = Probes.interrupted
    slow_apply = Recheck.slow_apply
    argv = Recheck.argv
    test_A1 = Recheck.test_A1_new_and_collision_paths_preserved
    test_A7_editor = Recheck.test_A7_external_editor_preserved_and_lock_released

    def test_A2_drift_and_second_plan(self):
        release = self.setup_release()
        path = self.root / CORE / "rules/git.md"
        content = path.read_text() + "\nlocal customization\n"
        path.write_text(content)
        self.saved(release)
        self.ok("update", "apply")
        self.assertEqual(path.read_text(), content)
        self.ok("check", "--ready")
        self.saved(release)
        plan = json.loads((self.root / "work/update/plan.json").read_text())
        self.assertTrue(plan["empty"])
        self.assertIn(CORE + "/rules/git.md", plan["drift"])

    def test_A3_keep_removed_file(self):
        release = self.setup_release(mutate=lambda kit: (kit / "templates/adr.md").unlink())
        rel = CORE + "/templates/adr.md"
        (self.root / rel).write_text("custom")
        self.saved(release, "--resolve", rel + "=keep")
        self.ok("update", "apply")
        self.assertEqual((self.root / rel).read_text(), "custom")
        self.assertNotIn(rel, self.manifest()["files"])
        self.assertNotIn(rel, self.manifest().get("kept", []))
        self.ok("check", "--ready")
        self.saved(release)
        self.assertTrue(json.loads((self.root / "work/update/plan.json").read_text())["empty"])

    def test_A4_collisions_rejected_without_changes(self):
        self.install()
        path = self.root / SETTINGS
        for custom, step in [
            ({"old":"a", "new":"b"}, {"op":"json_rename", "key":"custom.old", "to":"custom.new"}),
            ("scalar", {"op":"json_set", "key":"custom.child", "value":1}),
            (None, {"op":"json_set", "key":"custom.child", "value":1}),
            ({"old":1}, {"op":"json_rename", "key":"custom", "to":"custom.child"})]:
            data = json.loads(path.read_text()); data["custom"] = custom
            path.write_text(json.dumps(data)); before = path.read_bytes()
            step["file"] = SETTINGS
            release = self.fixture_release(updates_from={"0.0.2":{"steps":[step]}})
            with self.subTest(step=step):
                self.assertEqual(self.cli("update", "plan", str(release), "--save")[0], 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.root / "work/update/plan.json").exists())

    def test_A5_incompatible_cli_rejected(self):
        def mutate(kit):
            path = kit / "templates/project.json"
            data = json.loads(path.read_text()); data["schema_version"] = 2
            path.write_text(json.dumps(data))
        release = self.setup_release(mutate=mutate, steps=[{"op":"json_set", "file":SETTINGS,
            "key":"schema_version", "value":2}])
        before = (self.root / SETTINGS).read_bytes()
        self.saved(release)
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Целевой CLI", err)
        self.assertEqual((self.root / SETTINGS).read_bytes(), before)
        self.assertFalse((self.root / "work/update/pending.json").exists())
        self.ok("check", "--ready")

    def test_A6_missing_and_corrupt_backup_refused_then_restored(self):
        op, marker, before = self.interrupted()
        path = op / "backup" / CORE / "VERSION"
        content = path.read_bytes()
        for case in ("missing", "corrupt"):
            if case == "missing": path.unlink()
            else: path.write_text("corrupt")
            for runner in ("cli", "standalone"):
                with self.subTest(case=case, runner=runner):
                    if runner == "cli": code = self.cli("update", "restore")[0]
                    else:
                        code = subprocess.run([sys.executable, "-B", str(op / "restore.py")],
                            capture_output=True).returncode
                    self.assertEqual(code, 1)
                    self.assertTrue(marker.exists())
            path.write_bytes(content)
        self.ok("update", "restore")
        for rel, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / rel).read_bytes()).hexdigest(), digest, rel)
        self.ok("check", "--ready")

    def test_A7_live_restore_refused(self):
        proc, gate, release = self.slow_apply()
        marker = self.root / "work/update/pending.json"
        before = marker.read_bytes()
        self.assertEqual(self.cli("update", "restore")[0], 1)
        self.assertEqual(self.cli("update", "apply")[0], 1)
        self.assertEqual(self.cli("check")[0], 1)
        self.assertEqual(marker.read_bytes(), before)
        gate.touch()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.ok("check", "--ready")

    def test_A8_reserved_paths_rejected(self):
        self.install()
        manifest_path = self.root / CORE / "manifest.json"
        original = manifest_path.read_bytes()
        for rel in (SETTINGS, CORE + "/resources.json", CORE + "/manifest.json"):
            for recalc in (False, True):
                data = json.loads(original)
                data["files"][rel] = "sha256:" + hashlib.sha256((self.root / rel).read_bytes()).hexdigest()
                if recalc:
                    data["release_id"] = "sha256:" + hashlib.sha256(json.dumps(data["files"], sort_keys=True).encode()).hexdigest()
                manifest_path.write_text(json.dumps(data))
                with self.subTest(rel=rel, recalc=recalc):
                    self.assertEqual(self.cli("check")[0], 1)
                    self.assertTrue((self.root / rel).exists())
                manifest_path.write_bytes(original)

    def test_remaining_A6_incomplete_index_false_success(self):
        op, marker, before = self.interrupted()
        path = op / "backup_index.json"
        data = json.loads(path.read_text())
        data["backed"].remove(CORE + "/VERSION")
        path.write_text(json.dumps(data))
        self.ok("update", "restore")
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.3")
        self.assertEqual(json.loads((self.root / SETTINGS).read_text())["playbook_version"], "0.0.2")
        self.assertEqual(self.cli("check")[0], 1)
        print("A6 REMAINS: VERSION omitted from backed (still in hashes/backup), restore=0, mixed state, pending removed")

    def test_remaining_A7_standalone_ignores_pending_owner(self):
        op, marker, before = self.interrupted()
        self.ok("update", "restore")
        self.ok("update", "apply")  # valid 0.0.2 -> 0.0.3, old op remains on disk
        release = self.fixture_release(version="0.0.4", updates_from={"0.0.3":{"steps":[]}})
        self.saved(release)
        child_code = """
import os, signal, sys
sys.path.insert(0, sys.argv[1] + '/ai-dev-playbook/scripts')
import playbook, updating
original = updating._journal
def hook(op, event, **fields):
    original(op, event, **fields)
    if event == 'write': os.kill(os.getpid(), signal.SIGKILL)
updating._journal = hook
sys.exit(playbook.main(['--root', sys.argv[1], 'update', 'apply']))
"""
        child = subprocess.run([sys.executable, "-B", "-c", child_code, str(self.root)],
                               capture_output=True)
        self.assertEqual(child.returncode, -9)
        self.assertNotEqual(json.loads(marker.read_text())["op_id"], op.name)
        result = subprocess.run([sys.executable, "-B", str(op / "restore.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")
        print("A7 REMAINS: old standalone restore rolls interrupted 0.0.3->0.0.4 back to 0.0.2 and removes newer pending, exit=0")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Recheck2)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
