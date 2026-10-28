"""Third PB-022 audit: A1/A4 controls and remaining A2/A5 boundaries."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ai-dev-playbook/scripts"))
import document_links as links


class Reaudit02(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.old = self.root / "docs/a.md"
        self.new = self.root / "docs/archive/a.md"
        self.old.parent.mkdir()
        self.b = self.old.parent / "b.md"
        self.b.write_text("# Target\n")

    def rewrite(self, text):
        return links.rewrite(text, self.old, self.new, {self.old: self.new})["text"]

    def test_A1_angle_suffix_matrix_positive(self):
        for suffix in ["?raw=1", "#sec", "?raw=1#sec"]:
            for template in ['[x](<b.md%s> "title")\n', '[x]: <b.md%s> "title"\n[x][x]\n']:
                with self.subTest(suffix=suffix, template=template):
                    text = template % suffix
                    result = self.rewrite(text)
                    self.assertEqual(result, text.replace("<b.md", "<../b.md"))
                    targets = [item["target"] for item in links.scan(result)
                               if item["kind"] in ("inline", "refdef")]
                    self.assertEqual(targets, [f"<../b.md{suffix}>"])

    def test_A4_nested_inline_and_reference_images_positive(self):
        for text in ["[![one](i.png) ![two](j.png)](b.md)\n",
                     "[![one][image]](b.md)\n[image]: i.png\n"]:
            with self.subTest(text=text):
                result = self.rewrite(text)
                self.assertIn("(../b.md)", result)
                expected = text.replace("(b.md)", "(../b.md)")
                for image in ("i.png", "j.png"):
                    expected = expected.replace(image, "../" + image)
                self.assertEqual(result, expected)
                # Missing image targets must still be checked within nested content.
                errors = links.check_text(text, "a.md", self.old.parent, self.root)
                self.assertTrue(any("i.png" in error for error in errors))

    def test_A2_even_backslashes_before_code_span(self):
        # Two backslashes render a literal backslash, leaving the backtick active.
        text = "\\\\`[example](b.md)`\n"
        self.assertEqual(self.rewrite(text), text)

    def test_A5_extensionless_joint_move_has_no_false_conflict(self):
        self.old.write_text("[[b]]\n")
        moves = {self.old: self.new, self.b: self.new.parent / "b.md"}
        plan = links.plan_rewrites([self.old], moves, self.root)
        self.assertFalse(plan["conflicts"], plan["conflicts"])

    def test_A5_extensionless_fragment_detects_moved_target(self):
        self.old.write_text("[[b#section]]\n")
        moves = {self.b: self.new.parent / "b.md"}
        plan = links.plan_rewrites([self.old], moves, self.root)
        self.assertTrue(plan["conflicts"],
                        "b.md moved but unsupported wikilink is silently accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
