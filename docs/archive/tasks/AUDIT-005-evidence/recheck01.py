"""Independent acceptance and active-restore probe; temporary projects only."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from probes import Probes, CORE, SETTINGS
from test_cli import CliCase


class Recheck(CliCase):
    cli = Probes.cli
    ok = Probes.ok
    saved = Probes.saved
    setup_release = Probes.setup_release

    def test_A1_new_and_collision_paths_preserved(self):
        rel = CORE + "/rules/new.md"
        release = self.setup_release(mutate=lambda kit:
            (kit / "rules/new.md").write_text("release"))
        self.saved(release)
        target = self.root / rel
        target.write_text("new user file")
        self.assertEqual(self.cli("update", "apply")[0], 1)
        self.assertEqual(target.read_text(), "new user file")
        for choice in ("keep", "release"):
            for action in ("edit", "delete"):
                with self.subTest(choice=choice, action=action):
                    target.write_text("collision before plan")
                    self.saved(release, "--resolve", rel + "=" + choice)
                    if action == "edit":
                        target.write_text("changed after plan")
                    else:
                        target.unlink()
                    code, out, err = self.cli("update", "apply")
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("устарел", err)
                    self.assertEqual(target.exists(), action == "edit")
                    if target.exists():
                        self.assertEqual(target.read_text(), "changed after plan")
        self.assertFalse((self.root / "work/update/pending.json").exists())
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")

    def slow_apply(self):
        marker = self.root / "work/check-started"
        gate = self.root / "work/check-continue"
        script = ("from pathlib import Path; import time; "
                  "Path(" + repr(str(marker)) + ").touch(); "
                  "gate=Path(" + repr(str(gate)) + "); "
                  "\nfor i in range(200):\n"
                  " if gate.exists(): break\n time.sleep(0.05)\n"
                  "else: raise RuntimeError('gate timeout')\n")
        release = self.setup_release(verification={"inputs": ["docs"], "checks": [
            {"id": "gate", "argv": [sys.executable, "-c", script], "timeout_seconds": 15}]})
        self.saved(release)
        proc = subprocess.Popen(self.argv("update", "apply"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        self.addCleanup(cleanup)
        end = time.monotonic() + 8
        while not marker.exists() and proc.poll() is None and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        return proc, gate, release

    def argv(self, *args):
        return [sys.executable, "-B", str(self.root / CORE / "scripts/playbook.py"),
                "--root", str(self.root), *args]

    def test_A7_external_editor_preserved_and_lock_released(self):
        proc, gate, release = self.slow_apply()
        target = self.root / CORE / "rules/git.md"
        edit = subprocess.run([sys.executable, "-c",
            "from pathlib import Path; Path(" + repr(str(target)) + ").write_text('external edit')"])
        self.assertEqual(edit.returncode, 0)
        gate.touch()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn("устарел", err)
        self.assertEqual(target.read_text(), "external edit")
        self.assertFalse((self.root / "work/update/pending.json").exists())
        journals = list((self.root / "work/update").glob("*/journal.jsonl"))
        self.assertFalse(any('"event": "done"' in p.read_text() for p in journals))
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.2")

    def test_A7_second_apply_blocked(self):
        proc, gate, release = self.slow_apply()
        code, out, err = self.cli("update", "apply")
        self.assertEqual(code, 1, out + err)
        self.assertIn("Незаверш", err)
        gate.touch()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out + err)
        self.ok("check", "--ready")

    def test_active_restore_unlocks_running_apply(self):
        # Observed-defect probe, not a passing acceptance assertion.
        proc, gate, release = self.slow_apply()
        self.assertIsNone(proc.poll())
        self.ok("update", "restore")
        self.assertFalse((self.root / "work/update/pending.json").exists())
        self.assertIsNone(proc.poll())
        self.ok("check", "--ready")
        second = subprocess.Popen(self.argv("update", "apply"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            end = time.monotonic() + 8
            while time.monotonic() < end and second.poll() is None:
                journals = list((self.root / "work/update").glob("*/journal.jsonl"))
                if sum('"event": "staged"' in p.read_text() for p in journals) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(sum('"event": "staged"' in p.read_text() for p in journals), 2)
            self.assertIsNone(proc.poll())
            self.assertIsNone(second.poll())
            gate.touch()
            second_out, second_err = second.communicate(timeout=15)
            out, err = proc.communicate(timeout=15)
        finally:
            if second.poll() is None:
                second.kill()
                second.communicate()
        journals = list((self.root / "work/update").glob("*/journal.jsonl"))
        starts = sum('"event": "start"' in p.read_text() for p in journals)
        self.assertEqual(starts, 2)
        print("ACTIVE RESTORE: first process remained alive, pending removed, ordinary check allowed; "
              "second apply entered; exits:", proc.returncode, second.returncode)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Recheck)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
