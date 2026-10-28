"""Audit probes: assertions describe observed defects, NOT acceptance tests.

Run: python3 docs/tasks/AUDIT-005-evidence/probes.py -v
Only temporary installations are mutated. Fixture construction reuses CliCase;
all project CLI calls below run the actual installed CLI in a subprocess.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from test_cli import CliCase, REPO as SOURCE

CORE = "ai-dev-playbook"
SETTINGS = CORE + "/project.json"


class Probes(CliCase):
    def cli(self, *args):
        result = subprocess.run(
            [sys.executable, "-B", str(self.root / CORE / "scripts/playbook.py"),
             "--root", str(self.root), *args], capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    def ok(self, *args):
        result = self.cli(*args)
        self.assertEqual(result[0], 0, result)
        return result

    def setup_release(self, mutate=None, steps=None, verification=None):
        self.install(verification=verification)
        return self.fixture_release(mutate=mutate,
            updates_from={"0.0.2": {"steps": steps or []}})

    def saved(self, release, *args):
        self.ok("update", "plan", str(release), "--save", *args)

    def test_A1_new_file_after_preview_is_overwritten(self):
        rel = CORE + "/rules/new.md"
        release = self.setup_release(mutate=lambda kit:
            (kit / "rules/new.md").write_text("release content"))
        self.saved(release)
        (self.root / rel).write_text("user content created AFTER preview")
        self.ok("update", "apply")
        self.assertEqual((self.root / rel).read_text(), "release content")
        print("A1: unreviewed new project file overwritten, exit=0")

    def test_A2_drift_allowed_by_plan_fails_apply(self):
        release = self.setup_release()
        rel = CORE + "/rules/git.md"
        original = (self.root / rel).read_text() + "\nlocal customization\n"
        (self.root / rel).write_text(original)
        self.saved(release)
        plan = json.loads((self.root / "work/update/plan.json").read_text())
        self.assertIn(rel, plan["drift"])
        self.assertFalse(plan["conflicts"])
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Итоговое содержимое не совпадает", err)
        self.assertEqual((self.root / rel).read_text(), original)
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")
        print("A2: no-conflict drift plan rolls back at final hash check")

    def test_A3_keep_removed_file_produces_invalid_manifest(self):
        release = self.setup_release(mutate=lambda kit: (kit / "templates/adr.md").unlink())
        rel = CORE + "/templates/adr.md"
        (self.root / rel).write_text("local ADR template")
        self.saved(release, "--resolve", rel + "=keep")
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("kept должен ссылаться", err)
        print("A3: keep for removed locally edited file makes invalid manifest and rolls back")

    def test_A4_rename_collision_loses_existing_value(self):
        steps = [{"op": "json_rename", "file": SETTINGS,
                  "key": "custom.old", "to": "custom.new"}]
        release = self.setup_release(steps=steps)
        path = self.root / SETTINGS
        data = json.loads(path.read_text())
        data["custom"] = {"old": "source value", "new": "existing user value"}
        path.write_text(json.dumps(data))
        self.saved(release)
        plan = json.loads((self.root / "work/update/plan.json").read_text())
        self.assertFalse(plan["conflicts"])
        self.ok("update", "apply")
        self.assertEqual(json.loads(path.read_text())["custom"], {"new": "source value"})
        print("A4: json_rename silently replaces existing destination value")

    def test_A4_nested_set_loses_parent_value(self):
        release = self.setup_release(steps=[{"op": "json_set", "file": SETTINGS,
            "key": "custom.child", "value": "new"}])
        path = self.root / SETTINGS
        data = json.loads(path.read_text())
        data["custom"] = "user scalar"
        path.write_text(json.dumps(data))
        self.saved(release)
        self.ok("update", "apply")
        self.assertEqual(json.loads(path.read_text())["custom"], {"child": "new"})
        print("A4: nested json_set silently discards scalar parent")

    def test_A5_schema_upgrade_succeeds_but_installed_CLI_breaks(self):
        def mutate(kit):
            path = kit / "templates/project.json"
            data = json.loads(path.read_text())
            data["schema_version"] = 2
            path.write_text(json.dumps(data))
        release = self.setup_release(mutate=mutate, steps=[{"op": "json_set",
            "file": SETTINGS, "key": "schema_version", "value": 2}])
        self.saved(release)
        self.ok("update", "apply")
        code, out, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("Неподдерживаемая схема project.json", err)
        self.assertFalse((self.root / "work/update/pending.json").exists())
        print("A5: apply exit=0, installed check fails on schema=2, pending absent")

    def interrupted(self):
        release = self.setup_release()
        self.saved(release)
        # Execute the real apply, terminate at first completed journaled write.
        # No implementation files are modified; hook lives only in child memory.
        code = """
import os, signal, sys
sys.path.insert(0, sys.argv[1] + '/ai-dev-playbook/scripts')
import playbook, updating
original = updating._journal
def hook(op, event, **fields):
    original(op, event, **fields)
    if event == 'write':
        os.kill(os.getpid(), signal.SIGKILL)
updating._journal = hook
sys.exit(playbook.main(['--root', sys.argv[1], 'update', 'apply']))
"""
        before = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob("*") if p.is_file()
                  and "work" not in p.relative_to(self.root).parts
                  and "__pycache__" not in p.parts}
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.root)],
                               capture_output=True, text=True)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        marker = self.root / "work/update/pending.json"
        self.assertTrue(marker.is_file())
        op = self.root / json.loads(marker.read_text())["op_dir"]
        self.assertNotEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")
        return op, marker, before

    def test_control_SIGKILL_restore_full_hash_match(self):
        op, marker, before = self.interrupted()
        self.assertEqual(self.cli("check")[0], 1)
        self.ok("update", "restore")
        for rel, digest in before.items():
            self.assertEqual(hashlib.sha256((self.root / rel).read_bytes()).hexdigest(), digest, rel)
        self.ok("check", "--ready")
        print("CONTROL: real SIGKILL after first write; CLI restore returns all preimage hashes")

    def test_A6_missing_backup_is_reported_as_success(self):
        op, marker, before = self.interrupted()
        (op / "backup" / CORE / "VERSION").unlink()
        self.ok("update", "restore")
        self.assertFalse(marker.exists())
        self.assertNotEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")
        self.assertEqual(self.cli("check")[0], 1)
        print("A6: missing VERSION backup skipped, restore exit=0 and marker removed")

    def test_A6_standalone_missing_index_is_success(self):
        op, marker, before = self.interrupted()
        (op / "backup_index.json").unlink()
        result = subprocess.run([sys.executable, "-B", str(op / "restore.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")
        print("A6: standalone restore without backup_index removes pending and exits 0")

    def test_A7_live_edit_during_staging_is_overwritten(self):
        # Trusted configured check models an editor changing the original while
        # potentially long staged checks run. Only this temporary root is edited.
        def mutate(kit):
            path = kit / "rules/git.md"
            path.write_text(path.read_text() + "\nrelease change\n")
        target = self.root / CORE / "rules/git.md"
        verification = {"inputs": [CORE], "checks": [{"id": "editor",
            "argv": [sys.executable, "-c", "from pathlib import Path; Path(" +
                     repr(str(target)) + ").write_text('user edit DURING staged checks')"],
            "timeout_seconds": 10}]}
        release = self.setup_release(mutate=mutate, verification=verification)
        self.saved(release)
        self.ok("update", "apply")
        self.assertIn("release change", target.read_text())
        self.assertNotIn("user edit DURING", target.read_text())
        print("A7: original edited during staging; no second fingerprint check; edit overwritten")

    def test_A8_manifest_reserved_setting_path_is_deleted(self):
        release = self.setup_release()
        path = self.root / CORE / "manifest.json"
        data = json.loads(path.read_text())
        resources = self.root / CORE / "resources.json"
        data["files"][CORE + "/resources.json"] = "sha256:" + hashlib.sha256(resources.read_bytes()).hexdigest()
        path.write_text(json.dumps(data))
        self.ok("check", "--ready")
        self.saved(release)
        self.ok("update", "apply")
        self.assertFalse(resources.exists())
        self.assertEqual(self.cli("check")[0], 1)
        print("A8: corrupt manifest accepted; resources.json becomes managed removal and is deleted")


if __name__ == "__main__":
    unittest.main()
