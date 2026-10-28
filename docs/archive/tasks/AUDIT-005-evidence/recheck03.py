"""Acceptance recheck: all tests assert required safe behavior, not defects."""
import hashlib
import json
import subprocess
import sys
import unittest
from recheck02 import Recheck2, CORE


class Recheck3(Recheck2):
    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()
                and "work" not in p.relative_to(self.root).parts
                and "__pycache__" not in p.parts}

    def test_remaining_A6_incomplete_index_false_success(self):
        op, marker, before = self.interrupted()
        path = op / "backup_index.json"
        original = path.read_bytes()
        marker_before = marker.read_bytes()
        interrupted = self.snapshot()
        variants = []
        for mode in ("omit", "empty", "duplicate", "overlap", "hashes", "unlisted"):
            data = json.loads(original)
            if mode == "omit": data["backed"].remove(CORE + "/VERSION")
            if mode == "empty": data["backed"] = []
            if mode == "duplicate": data["backed"].append(data["backed"][0])
            if mode == "overlap": data["added"].append(data["backed"][0])
            if mode == "hashes": data["hashes"].pop(CORE + "/VERSION")
            if mode == "unlisted":
                data["backed"].remove(CORE + "/VERSION")
                data["hashes"].pop(CORE + "/VERSION")
            variants.append((mode, data))
        for mode, data in variants:
            path.write_text(json.dumps(data))
            for runner in ("cli", "standalone"):
                with self.subTest(mode=mode, runner=runner):
                    if runner == "cli": code, out, err = self.cli("update", "restore")
                    else:
                        result = subprocess.run([sys.executable, "-B", str(op / "restore.py")],
                                                capture_output=True, text=True)
                        code, out, err = result.returncode, result.stdout, result.stderr
                    self.assertEqual(code, 1, out + err)
                    self.assertEqual(marker.read_bytes(), marker_before)
                    self.assertEqual(self.snapshot(), interrupted)
        path.write_bytes(original)
        self.ok("update", "restore")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(marker.exists())
        self.ok("check", "--ready")

    def test_remaining_A7_standalone_ignores_pending_owner(self):
        old, marker, before = self.interrupted()
        self.ok("update", "restore")
        self.ok("update", "apply")
        release = self.fixture_release(version="0.0.4", updates_from={"0.0.3":{"steps":[]}})
        self.saved(release)
        before_next = self.snapshot()
        code = """
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
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.root)],
                               capture_output=True)
        self.assertEqual(child.returncode, -9)
        marker_before = marker.read_bytes()
        current = self.root / json.loads(marker_before)["op_dir"]
        interrupted = self.snapshot()
        result = subprocess.run([sys.executable, "-B", str(old / "restore.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(marker.read_bytes(), marker_before)
        self.assertEqual(self.snapshot(), interrupted)
        result = subprocess.run([sys.executable, "-B", str(current / "restore.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.snapshot(), before_next)
        self.assertEqual((self.root / CORE / "VERSION").read_text().strip(), "0.0.3")
        self.ok("check", "--ready")
        # Explicit absence of pending never authorizes an old rollback.
        result = subprocess.run([sys.executable, "-B", str(old / "restore.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.snapshot(), before_next)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Recheck3)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
