"""PB-081: real Python launchers and runtime-bound CLI evidence."""
import json
import os
import subprocess
import sys
import venv

from test_checks_e2e import NoGitChecksE2ECase, ROOT
from test_checks_run import run_policy
import checks_results


class InterpreterCliTest(NoGitChecksE2ECase):
    def exercise(self, executable):
        self.create_card("DEV-01")
        venv_dir = self.base / "approved-venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_dir)
        launch = venv_dir / "bin" / "python3"
        observed = self.base / "observed.json"
        self.write("tests/test_payments.py", f"""
import json
import sys
import unittest
from pathlib import Path

class TestPay(unittest.TestCase):
    def test_ok(self):
        Path({str(observed)!r}).write_text(json.dumps(
            [sys.executable, sys.prefix, sys.base_prefix]))
        self.assertEqual(sys.prefix, sys.base_prefix)
""")
        policy = run_policy()
        policy["checks"] = policy["checks"][:1]
        approved = policy["checks"][0]["argv"]
        approved[0] = str(launch) if executable is None else executable
        self.write("checks-policy.json", json.dumps(policy))
        env = dict(os.environ, PATH=str(launch.parent) + os.pathsep + os.environ["PATH"])

        def cli(*args):
            return subprocess.run(
                [sys.executable, str(ROOT / "ai-dev-playbook/scripts/playbook.py"),
                 "--root", str(self.root), *args],
                env=env, text=True, capture_output=True)

        def run(mode):
            return cli("checks", "run", "DEV-01", "--scope", "project", "--mode", mode)

        setup = cli("setup", "apply")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        direct = subprocess.run(approved, cwd=self.root, env=env, capture_output=True)
        self.assertEqual(direct.returncode, 1, direct.stderr)
        expected = json.loads(observed.read_text())
        self.assertNotEqual(expected[1], expected[2])
        observed.unlink()
        result = run("force")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(observed.read_text()), expected)
        record, = checks_results.load_results(self.root)[0]
        self.assertEqual(record["status"], "failed")
        report = json.loads((self.root / record["evidence"]["adapter_report"]).read_text())
        self.assertEqual(report["argv"][0], approved[0])
        self.assertEqual(report["runtime"]["prefix"], expected[1])

        if executable is None:
            launch.unlink()
            for inaccessible in (False, True):
                with self.subTest(inaccessible=inaccessible):
                    if inaccessible:
                        launch.write_text("#!/bin/sh\nexit 0\n")
                        launch.chmod(0o644)
                    observed.unlink(missing_ok=True)
                    result = run("force")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("интерпретатор отсутствует или недоступен", result.stdout)
                    self.assertFalse(observed.exists())
        else:
            # Same policy and executable name, different PATH target.
            base_bin = self.base / "base-bin"
            base_bin.mkdir()
            (base_bin / executable).symlink_to(sys._base_executable)
            env["PATH"] = str(base_bin) + os.pathsep + os.environ["PATH"]
            result = run("stale")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = run("stale")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("reused", result.stdout)
            env["PATH"] = str(launch.parent) + os.pathsep + env["PATH"]
            result = run("stale")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(json.loads(observed.read_text()), expected)

    def test_absolute_venv_and_unavailable_interpreter(self):
        self.exercise(None)

    def test_python_from_path(self):
        self.exercise("python")

    def test_python3_from_path(self):
        self.exercise("python3")

    def test_unchanged_launcher_dispatching_to_different_runtime_is_blocked(self):
        self.create_card("DEV-01")
        venv_dir = self.base / "alternate-venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_dir)
        base_bin = self.base / "base-bin"
        base_bin.mkdir()
        (base_bin / "python3").symlink_to(sys._base_executable)
        (venv_dir / "bin/python-runtime").symlink_to(venv_dir / "bin/python3")
        (base_bin / "python-runtime").symlink_to(base_bin / "python3")
        shim_dir = self.base / "shims"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_text('#!/bin/sh\nexec python-runtime "$@"\n')
        shim.chmod(0o755)
        observed = self.base / "observed"
        self.write("tests/test_payments.py", f"""
import sys
import unittest
from pathlib import Path

class TestPay(unittest.TestCase):
    def test_ok(self):
        Path({str(observed)!r}).write_text(sys.prefix)
        self.assertEqual(sys.prefix, sys.base_prefix)
""")
        policy = run_policy()
        policy["checks"] = policy["checks"][:1]
        approved = policy["checks"][0]["argv"]
        approved[0] = "python3"
        self.write("checks-policy.json", json.dumps(policy))
        inherited = os.environ["PATH"]

        def cli(path, *args):
            return subprocess.run(
                [sys.executable, str(ROOT / "ai-dev-playbook/scripts/playbook.py"),
                 "--root", str(self.root), *args],
                env=dict(os.environ, PATH=path), text=True, capture_output=True)

        def run(path, mode):
            return cli(path, "checks", "run", "DEV-01", "--scope", "project",
                       "--mode", mode)

        direct_path = os.pathsep.join((str(base_bin), inherited))
        setup = cli(direct_path, "setup", "apply")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        for mode in ("force", "stale"):
            result = run(direct_path, mode)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reused", result.stdout)

        for directory, expected_rc in ((base_bin, 0), (venv_dir / "bin", 1)):
            path = os.pathsep.join((str(shim_dir), str(directory), inherited))
            with self.subTest(runtime=directory):
                direct = subprocess.run(approved, cwd=self.root, capture_output=True,
                                        env=dict(os.environ, PATH=path))
                self.assertEqual(direct.returncode, expected_rc, direct.stderr)
                expected_prefix = sys.base_prefix if expected_rc == 0 else str(venv_dir)
                self.assertEqual(observed.read_text(), expected_prefix)
                observed.unlink()
                status = cli(path, "checks", "status", "DEV-01")
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn("Покрытие обязательств области задачи: 0/1", status.stdout)
                gate = cli(path, "checks", "release-ready", "DEV-01")
                self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
                for mode in ("stale", "force"):
                    result = run(path, mode)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("неподдерживаемый launcher", result.stdout)
                    self.assertNotIn("reused", result.stdout)
                    self.assertFalse(observed.exists())
