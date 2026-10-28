"""Independent PB-021 subprocess probes; writes only temporary fixtures.

Run: python3 docs/tasks/PB-021-evidence/audit_probe.py
Output is observations, not a passing regression suite.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "ai-dev-playbook/scripts/playbook.py"


def metadata(ident="spec:alpha", **kw):
    value = dict(schema_version=1, document_id=ident, kind="spec", state="accepted",
                 created_at="2026-10-01T00:00:00+03:00",
                 updated_at="2026-10-01T00:00:00+03:00", current_revision=1,
                 relations=[], tasks=[], changes=[], materials=[], archive=None)
    value.update(kw)
    return value


def text(value):
    return "# Example\n\n<!-- playbook-meta:start -->\n```json\n" + json.dumps(value) + "\n```\n<!-- playbook-meta:end -->\n"


def material(source, **kw):
    value = dict(id="proof", role="report", source=source, storage="local",
                 required=True, immutable=True, used_by=[])
    value.update(kw)
    return value


def write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def snapshot(root):
    return {str(p.relative_to(root)): ("symlink:" + os.readlink(p) if p.is_symlink()
            else "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*"))}


def run(root, *args, bytecode=False):
    env = os.environ.copy()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    cmd = [sys.executable] + ([] if bytecode else ["-B"])
    return subprocess.run(cmd + [str(root / "ai-dev-playbook/scripts/playbook.py"),
                          "--root", str(root), *args], text=True,
                          capture_output=True, env=env, timeout=30)


def fixture(root):
    result = subprocess.run([sys.executable, "-B", str(CLI), "init", str(root), "--apply"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    path = root / "ai-dev-playbook/project.json"
    config = json.loads(path.read_text())
    config.update(name="audit-fixture", setup_status="configured",
                  components=[dict(name="app", code="src", tests=["tests"], migrations="db")],
                  documents=dict(schema_version=1, collections={"spec": dict(
                      active="docs/specs", archive="docs/archive/specs", index="docs/specs/index.md")}))
    path.write_text(json.dumps(config))
    write(root, "docs/specs/alpha.md", text(metadata()))
    write(root, "src/app.py", "# product\n")
    write(root, "tests/test_app.py", "# permanent test\n")
    write(root, "db/001.sql", "-- migration\n")


def probe(name, setup, args=("document", "list"), bytecode=False):
    with tempfile.TemporaryDirectory(prefix="pb021-audit-") as tmp:
        base = Path(tmp).resolve()
        root = base / "project"
        fixture(root)
        setup(root, base)
        before = snapshot(root)
        result = run(root, *args, bytecode=bytecode)
        after = snapshot(root)
        return dict(case=name, returncode=result.returncode,
                    stdout=result.stdout.replace(str(base), "<fixture>"),
                    stderr=result.stderr.replace(str(base), "<fixture>"),
                    added=sorted(after.keys() - before.keys()),
                    removed=sorted(before.keys() - after.keys()),
                    changed=sorted(p for p in before.keys() & after.keys() if before[p] != after[p]))


def symlink_doc(root, base):
    external = write(base, "outside.md", text(metadata("spec:outside")))
    (root / "docs/specs/outside.md").symlink_to(external)


def broad_material(root, base):
    path = root / "ai-dev-playbook/project.json"
    config = json.loads(path.read_text())
    config["components"][0].update(code="app/src", tests=["app/tests"])
    path.write_text(json.dumps(config))
    write(root, "app/src/main.py", "# product\n")
    write(root, "app/tests/test_main.py", "# permanent test\n")
    write(root, "docs/specs/alpha.md", text(metadata(materials=[material("app")])))


def migrations_material(root, base):
    write(root, "docs/specs/alpha.md", text(metadata(materials=[material("db")])))


def external_duplicate(root, base):
    shared = material("https://example.invalid/proof", storage="external", access="fixture")
    write(root, "docs/specs/alpha.md", text(metadata(materials=[shared])))
    write(root, "docs/specs/beta.md", text(metadata("spec:beta", materials=[shared])))


def malformed(root, base):
    write(root, "docs/specs/alpha.md", text(metadata(state=[])))


def partial_evidence(root, base):
    write(root, "docs/specs/alpha-evidence/tracked.log", "tracked\n")
    write(root, "docs/specs/alpha-evidence/missed.log", "untracked\n")
    write(root, "docs/specs/alpha.md", text(metadata(materials=[material(
        "docs/specs/alpha-evidence/tracked.log")])))


def material_symlink(root, base):
    write(root, "docs/specs/alpha-evidence/tracked.log", "tracked\n")
    external = write(base, "outside.log", "fixture\n")
    (root / "docs/specs/alpha-evidence/link.log").symlink_to(external)
    write(root, "docs/specs/alpha.md", text(metadata(materials=[material(
        "docs/specs/alpha-evidence")])))


def false_fence(root, base):
    # A closing fence cannot carry an info string; the complete sample is fenced.
    body = "# Example\n\n````markdown\n````still-inside\n" + text(metadata("spec:fake")) + "````\n"
    write(root, "docs/specs/alpha.md", body)


def baseline(root, base):
    write(root, "docs/specs/legacy.md", "# Legacy\n")
    write(root, "docs/specs/beta.md", text(metadata("spec:beta", relations=[
        dict(relation="based_on", target="spec:alpha")])) )


if __name__ == "__main__":
    cases = [
        probe("baseline-impact", baseline, ("document", "impact", "spec:alpha")),
        probe("baseline-materials", baseline, ("document", "materials", "spec:alpha")),
        probe("baseline-list", baseline),
        probe("document-symlink-outside-root", symlink_doc),
        probe("material-contains-component-roots", broad_material),
        probe("migration-is-material", migrations_material),
        probe("two-external-owners", external_duplicate),
        probe("malformed-state-diagnostic", malformed),
        probe("partially-registered-evidence", partial_evidence),
        probe("symlink-inside-material-directory", material_symlink),
        probe("fenced-example-registered", false_fence),
        probe("read-only-default-python", baseline, bytecode=True),
    ]
    print(json.dumps(dict(python=sys.version, platform=platform.platform(), cases=cases),
                     ensure_ascii=False, indent=2))
