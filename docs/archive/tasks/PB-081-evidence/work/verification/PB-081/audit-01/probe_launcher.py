"""Check freshness when a PATH launcher selects a different Python."""
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / "tests"), str(root / "ai-dev-playbook/scripts")]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_run import run_policy

case = NoGitChecksE2ECase()
case.setUp()
try:
    case.create_card("DEV-01")
    venv_dir = case.base / "alternate-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_dir)
    shim_dir = case.base / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text('#!/bin/sh\nexec python-runtime "$@"\n')
    shim.chmod(0o755)
    base_bin = case.base / "base-bin"
    base_bin.mkdir()
    (base_bin / "python-runtime").symlink_to(sys._base_executable)
    (venv_dir / "bin/python-runtime").symlink_to(venv_dir / "bin/python3")

    observed = case.base / "observed.json"
    case.write("tests/test_payments.py", f"""import json
from pathlib import Path
import sys
import unittest

class TestPay(unittest.TestCase):
    def test_ok(self):
        Path({str(observed)!r}).write_text(json.dumps(
            dict(executable=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix)))
        self.assertEqual(sys.prefix, sys.base_prefix)
""")
    policy = run_policy()
    policy["checks"] = policy["checks"][:1]
    approved = policy["checks"][0]["argv"]
    approved[0] = "python3"
    case.write("checks-policy.json", json.dumps(policy))
    inherited = os.environ["PATH"]
    default = dict(os.environ, PATH=os.pathsep.join(map(str, [shim_dir, base_bin, inherited])))
    changed = dict(os.environ, PATH=os.pathsep.join(map(str, [shim_dir, venv_dir / "bin", inherited])))
    events = []

    def call(label, argv, env):
        observed.unlink(missing_ok=True)
        run = subprocess.run(argv, cwd=case.root, env=env, text=True, capture_output=True)
        events.append(dict(label=label, rc=run.returncode, stdout=run.stdout,
                           stderr=run.stderr, runtime=json.loads(observed.read_text())
                           if observed.exists() else None))

    cli = [sys.executable, str(root / "ai-dev-playbook/scripts/playbook.py"),
           "--root", str(case.root)]
    command = cli + ["checks", "run", "DEV-01", "--scope", "project", "--mode"]
    call("setup", cli + ["setup", "apply"], default)
    call("force / base", command + ["force"], default)
    call("direct / venv", approved, changed)
    call("status / venv", cli + ["checks", "status", "DEV-01"], changed)
    call("stale / venv", command + ["stale"], changed)
    call("force / venv", command + ["force"], changed)
    print(json.dumps(events, ensure_ascii=False, indent=2))
    assert [event["rc"] for event in events] == [0, 0, 1, 0, 0, 1], events
    assert "reused" in events[4]["stdout"] and events[4]["runtime"] is None, events
    assert events[2]["runtime"] == events[5]["runtime"], events
finally:
    case.doCleanups()
