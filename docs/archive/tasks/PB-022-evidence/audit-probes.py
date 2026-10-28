"""Independent PB-022 audit probes. Run from any cwd; fixtures stay in temp dirs.

Assertions describe the required behavior; failures reproduce audit findings.
This is audit evidence, not a change to the permanent product test suite.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ai-dev-playbook/scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import document_links as links
import documents
from test_documents import CatalogCase, doc_meta, write_doc


class AuditProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "docs/a.md"
        self.source.parent.mkdir()
        self.target = self.root / "docs/archive/a.md"

    def cli(self, *args):
        p = subprocess.run([sys.executable, str(ROOT / "ai-dev-playbook/scripts/playbook.py"),
                            "--root", str(self.root), *args], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_A1_encoded_destination_keeps_identity(self):
        for token, name in [("a%23b.md", "a#b.md"), ("a%3Fb.md", "a?b.md"),
                            ("a%2520b.md", "a%20b.md"), ("./a%3Ab.md", "a:b.md")]:
            with self.subTest(token=token):
                expected = self.source.parent / name
                expected.write_text("target\n")
                text = links.rewrite(f"[x]({token})\n", self.source, self.source, {})["text"]
                result = urlsplit(links.scan(text)[0]["target"])
                actual = (self.source.parent / unquote(result.path)).resolve()
                self.assertEqual((result.scheme, actual), ("", expected), text)

    def test_A1_close_preserves_unrelated_links(self):
        # Real CLI subprocesses, including init, check before and after close.
        p = subprocess.run([sys.executable, str(ROOT / "ai-dev-playbook/scripts/playbook.py"),
                            "init", str(self.root), "--apply"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        task = self.root / "docs/tasks/DEV-1.md"
        task.write_text("# Task\n\nID: DEV-1\nStatus: in_review\nPriority: P2\n"
                        "Depends-On: -\nCreated: 2026-01-01T00:00:00Z\nClosed: -\n")
        (self.root / "docs/a#b.md").write_text("# Target\n")
        readme = self.root / "README.md"
        readme.write_text("[unrelated](docs/a%23b.md)\n")
        self.assertEqual(self.cli("index")[0], 0)
        self.assertEqual(self.cli("check")[0], 0)
        self.assertEqual(self.cli("close", "DEV-1")[0], 0)
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, f"After close: {readme.read_text()!r}; {err}")

    def test_A1_generated_index_destination_keeps_identity(self):
        path = self.source.parent / "a#b.md"
        path.write_text("# Target\n")
        token = documents._index_link(self.root, self.source.parent, path)
        actual = (self.source.parent / unquote(urlsplit(token).path)).resolve()
        self.assertEqual(actual, path, token)

    def test_A2_code_examples_preserved(self):
        for text in ["`example\n[x](b.md)\nend`\n", "    [x](b.md)\n"]:
            with self.subTest(text=text):
                result = links.rewrite(text, self.source, self.target,
                                       {self.source: self.target})
                self.assertEqual(result["text"], text)

    def test_A3_moved_source_unsupported_outgoing_conflict(self):
        (self.source.parent / "b.md").write_text("target\n")
        for text in ['<a href="b.md">x</a>\n', '[[b.md]]\n']:
            with self.subTest(text=text):
                self.source.write_text(text)
                plan = links.plan_rewrites([self.source], {self.source: self.target}, self.root)
                self.assertTrue(plan["conflicts"], repr(plan))

    def test_A3_unquoted_html_affected_target_conflict(self):
        b = self.source.parent / "b.md"
        b.write_text("target\n")
        self.source.write_text("<a href=b.md>x</a>\n")
        plan = links.plan_rewrites([self.source], {b: self.target.parent / "b.md"}, self.root)
        self.assertTrue(plan["conflicts"], repr(plan))

    def test_A4_nested_inline_label_relocated(self):
        text = "[a [b] c](b.md)\n"
        (self.source.parent / "b.md").write_text("target\n")
        result = links.rewrite(text, self.source, self.target, {self.source: self.target})
        self.assertEqual(result["text"], "[a [b] c](../b.md)\n")

    def test_A4_nested_inline_label_checked(self):
        errors = links.check_text("[a [b] c](missing.md)\n", "a.md",
                                  self.source.parent, self.root)
        self.assertTrue(errors, "Missing target is silently ignored")

    def test_supported_full_map_and_immutable_positive(self):
        b = self.source.parent / "b.md"
        new_b = self.target.parent / "b.md"
        image = self.source.parent / "img.png"
        image.write_bytes(b"image")
        self.source.write_text("[b](b.md?raw=1#s) [self](a.md) ![image](img.png)\n[x]: b.md\n[x][x]\n")
        b.write_text("[a](a.md)\n")
        moves = {self.source: self.target, b: new_b}
        plan = links.plan_rewrites([self.source, b], moves, self.root)
        self.assertFalse(plan["conflicts"])
        self.target.parent.mkdir()
        for old, new in moves.items():
            new.write_text(plan["updates"].get(old, {}).get("updated", old.read_text()))
            old.unlink()
        for path in (self.target, new_b):
            self.assertEqual(links.check_text(path.read_text(), str(path), path.parent, self.root), [])
        immutable = "[a](a.md) [b](b.md)\n"
        self.assertEqual(links.check_text(immutable, "evidence", self.source.parent, self.root, moves), [])

    def test_index_manual_regions_and_determinism_positive(self):
        case = CatalogCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.configure()
        write_doc(case.specs, "alpha.md", doc_meta())
        catalog = case.catalog()
        region = documents.index_region(catalog, "spec")
        original = "# Manual\r\nTop\n" + links.INDEX_START + "\nold\n" + links.INDEX_END + "\nTail\r\n"
        new = documents.splice_index(original, region)
        self.assertEqual(new.split(links.INDEX_START)[0], original.split(links.INDEX_START)[0])
        self.assertEqual(new.split(links.INDEX_END)[1], original.split(links.INDEX_END)[1])
        self.assertEqual(documents.splice_index(new, region), new)
        self.assertIn("## Актуальные", new)
        self.assertIn("## Архив", new)


if __name__ == "__main__":
    unittest.main(verbosity=2)
