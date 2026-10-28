"""Independent acceptance matrix for the final A2/A5 fixes."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ai-dev-playbook/scripts"))
import document_links as links


class AcceptanceMatrix(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.old = self.root / "docs/a.md"
        self.new = self.root / "archive/a.md"
        self.old.parent.mkdir()
        self.b = self.old.parent / "b.md"
        self.b.write_text("# Target\n")

    def test_A2_backslash_parity_rewrite_and_check(self):
        for count in range(5):
            for delimiter in ("`", "``"):
                with self.subTest(backslashes=count, delimiter=delimiter):
                    text = "\\" * count + delimiter + "[example](missing.md)" + delimiter + " [real](b.md)\n"
                    actual = links.rewrite(text, self.old, self.new, {self.old: self.new})["text"]
                    expected = text.replace("(b.md)", "(../docs/b.md)")
                    if count % 2:
                        expected = expected.replace("(missing.md)", "(../docs/missing.md)")
                    self.assertEqual(actual, expected)
                    errors = links.check_text(text, "a.md", self.old.parent, self.root)
                    self.assertEqual(len(errors), count % 2)

    def test_A5_wikilink_forms_and_move_directions(self):
        forms = ["b", "b.md", "b#section", "b.md#section", "b?raw=1#section|Alias"]
        cases = [("target", {self.b: self.new.parent / "b.md"}, True),
                 ("source", {self.old: self.new}, True),
                 ("both", {self.old: self.new, self.b: self.new.parent / "b.md"}, False),
                 ("directory", {self.old.parent: self.new.parent}, False),
                 ("neither", {}, False)]
        for form in forms:
            self.old.write_text(f"[[{form}]]\n")
            for name, moves, expected in cases:
                with self.subTest(form=form, move=name):
                    plan = links.plan_rewrites([self.old], moves, self.root)
                    self.assertEqual(bool(plan["conflicts"]), expected, plan)
                    self.assertFalse(plan["updates"])

    def test_A5_missing_and_ambiguous_candidates(self):
        self.old.write_text("[[missing]]\n")
        self.assertFalse(links.plan_rewrites([self.old], {self.old: self.new}, self.root)["conflicts"])
        (self.old.parent / "b").write_text("Second candidate\n")
        self.old.write_text("[[b#section|Alias]]\n")
        plan = links.plan_rewrites([self.old], {self.b: self.new.parent / "b.md"}, self.root)
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["kind"], "unsupported")
        self.assertFalse(plan["updates"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
