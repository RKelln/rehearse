import unittest

from rehearse import frontmatter


class FrontmatterTests(unittest.TestCase):
    def test_flat_keys_quoted_and_not(self):
        meta, body = frontmatter.parse('---\nname: dana\nopening: "Hi, you: there"\nmood: \'a b\'\n---\n\nBody here.\n')
        self.assertEqual(meta, {"name": "dana", "opening": "Hi, you: there", "mood": "a b"})
        self.assertEqual(body, "Body here.")

    def test_comment_dropped_but_not_inside_quotes(self):
        meta, _ = frontmatter.parse('---\nknowledge: k/d.md     # the facts\nmood: "a # b"\n---\nx')
        self.assertEqual(meta["knowledge"], "k/d.md")
        self.assertEqual(meta["mood"], "a # b")

    def test_escalation_list(self):
        text = '---\nname: x\nescalation:\n  - "first"\n  - second one\n  - \'third\'\nstop: DONE\n---\nbody'
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["escalation"], ["first", "second one", "third"])
        self.assertEqual(meta["stop"], "DONE")
        self.assertEqual(body, "body")

    def test_no_frontmatter(self):
        self.assertEqual(frontmatter.parse("just text"), ({}, "just text"))

    def test_unclosed_frontmatter_is_not_frontmatter(self):
        meta, body = frontmatter.parse("---\nname: x\nno end")
        self.assertEqual(meta, {})


if __name__ == "__main__":
    unittest.main()
