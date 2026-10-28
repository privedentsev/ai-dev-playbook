"""PB-022 repeat audit: remaining A1/A2/A4 cases and A3 positive control.

Assertions describe expected behavior. All fixtures are temporary.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ai-dev-playbook/scripts"))
import document_links as links


class Reaudit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.old = self.root / "docs/a.md"
        self.new = self.root / "docs/archive/a.md"
        self.old.parent.mkdir()

    def rewrite(self, text):
        return links.rewrite(text, self.old, self.new, {self.old: self.new})["text"]

    def test_A1_angle_query_fragment_stays_inside_token(self):
        text = '[x](<a b.md?raw=1#sec> "title")\n'
        self.assertEqual(self.rewrite(text), '[x](<../a b.md?raw=1#sec> "title")\n')

    def test_A1_close_preserves_clickable_angle_link(self):
        cli = [sys.executable, str(ROOT / "ai-dev-playbook/scripts/playbook.py")]
        def run(*args):
            p = subprocess.run(cli + list(args), text=True, capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p
        run("init", str(self.root), "--apply")
        card = self.root / "docs/tasks/DEV-1.md"
        card.write_text("# Task\n\nID: DEV-1\nStatus: in_review\nPriority: P2\n"
                        "Depends-On: -\nCreated: 2026-01-01T00:00:00Z\nClosed: -\n")
        (self.root / "docs/a b.md").write_text("# Target\n")
        readme = self.root / "README.md"
        text = "[target](<docs/a b.md?raw=1#sec>)\n"
        readme.write_text(text)
        run("--root", str(self.root), "index")
        run("--root", str(self.root), "check")
        run("--root", str(self.root), "close", "DEV-1")
        run("--root", str(self.root), "check")
        self.assertEqual(readme.read_text(), text,
                         "close AND check succeed despite destroying link syntax")

    def test_A2_unmatched_backtick_does_not_disable_later_code_span(self):
        text = "stray ` then ``[example](b.md)``\n"
        self.assertEqual(self.rewrite(text), text)

    def test_A2_escaped_backtick_does_not_hide_real_link(self):
        text = "\\` literal [real](b.md) `\n"
        self.assertEqual(self.rewrite(text), "\\` literal [real](../b.md) `\n")

    def test_A4_image_and_enclosing_link_both_relocated(self):
        self.assertEqual(self.rewrite("[![i](i.png)](b.md)\n"),
                         "[![i](../i.png)](../b.md)\n")

    def test_A4_missing_image_inside_link_is_checked(self):
        (self.old.parent / "b.md").write_text("target\n")
        errors = links.check_text("[![i](missing.png)](b.md)\n", "a.md",
                                  self.old.parent, self.root)
        self.assertTrue(errors, "Broken image silently passes check")

    def test_A3_controls_source_target_managed_and_frozen(self):
        b = self.old.parent / "b.md"
        b.write_text("target\n")
        for text in ['<a href="b.md">x</a>\n', "<a href=b.md>x</a>\n", "[[b.md]]\n"]:
            with self.subTest(text=text):
                self.old.write_text(text)
                source_only = links.plan_rewrites([self.old], {self.old: self.new}, self.root)
                self.assertEqual(source_only["conflicts"][0]["kind"], "unsupported")
                target_only = links.plan_rewrites([self.old], {b: self.new.parent / "b.md"}, self.root)
                self.assertEqual(target_only["conflicts"][0]["kind"], "unsupported")
        self.old.write_text("[b](b.md)\n")
        moves = {b: self.new.parent / "b.md"}
        core = links.plan_rewrites([self.old], moves, self.root, managed=[self.old.parent])
        self.assertEqual(core["conflicts"][0]["kind"], "managed-core")
        self.assertFalse(core["updates"])
        frozen = links.plan_rewrites([self.old], moves, self.root, frozen=[self.old])
        self.assertFalse(frozen["updates"])
        self.assertEqual(frozen["frozen"], [self.old])

    def test_A5_joint_move_wikilink_does_not_conflict(self):
        b = self.old.parent / "b.md"
        b.write_text("target\n")
        self.old.write_text("[[b.md]]\n")
        together = {self.old: self.new, b: self.new.parent / "b.md"}
        self.assertEqual(links.plan_rewrites([self.old], together, self.root)["conflicts"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
