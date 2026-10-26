import json
import tempfile
import unittest
from pathlib import Path

from rehearse.case import load_case
from rehearse.checks import Run
from rehearse.importing import write_imported_run
from helpers import make_case


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.case = load_case(make_case(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **kw):
        args = dict(variant="v1", sim="dana",
                    turns=[{"side": "person", "text": "Hi"},
                           {"side": "subject", "text": "Hello", "tool_calls": [{"name": "Read", "arg": "a.md"}]},
                           {"side": "person", "text": "DONE"}],
                    artifacts={"review.md": b"# Review\n"}, meta={"stop_reason": "sim_done"},
                    source="old/run1", assessment="# mine\n", date="2026-10-26")
        args.update(kw)
        return write_imported_run(self.case, **args)

    def test_layout_and_meta(self):
        p = self.write()
        self.assertTrue(p.name.startswith("2026-10-26-"))
        meta = json.loads((p / "meta.json").read_text())
        self.assertTrue(meta["imported"])
        self.assertEqual(meta["source"], "old/run1")
        self.assertIsNone(meta["cost"]["subject"]["usd"])
        self.assertEqual(meta["turns"], {"person": 2, "subject": 1})
        self.assertFalse((p / "artifacts" / "diff.json").exists())
        self.assertEqual((p / "assessment.md").read_text(), "# mine\n")
        run = Run.load(p, self.case)
        self.assertEqual(run.artifact("review.md"), "# Review\n")
        self.assertEqual(run.subject_turns, ["Hello"])
        self.assertEqual(run.person_turns, ["Hi", "DONE"])
        self.assertTrue((p / "checks.json").is_file())

    def test_never_overwrites(self):
        a, b = self.write(), self.write()
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_dir() and b.is_dir())

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            self.write(turns=[])
        with self.assertRaises(ValueError):
            self.write(artifacts={"../x": b"no"})

    def test_scrubs(self):
        p = self.write(turns=[{"side": "person", "text": "mail me@example.com"}])
        self.assertNotIn("me@example.com", (p / "turns.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()


class ImportInputsTests(ImportTests):
    def test_inputs_snapshot_feeds_sim_check(self):
        p = self.write(turns=[{"side": "person", "text": "Hi"}, {"side": "subject", "text": "When?"},
                              {"side": "person", "text": "Tuesday at 4"}],
                       inputs={"knowledge.md": "Dana is free Tuesday at 4.\n"})
        self.assertEqual((p / "inputs" / "knowledge.md").read_text(), "Dana is free Tuesday at 4.\n")
        sim = json.loads((p / "checks.json").read_text())["sim_check"]
        item = next(i for i in sim["items"] if i["name"] == "invented_numbers")
        self.assertEqual(item["result"], "pass")

    def test_person_played_sim_is_skipped(self):
        p = self.write(meta={"seats": {"subject": "claude:opus", "sim": "human"}})
        sim = json.loads((p / "checks.json").read_text())["sim_check"]
        self.assertEqual(sim["result"], "skip")
        self.assertFalse(json.loads((p / "checks.json").read_text())["void"])

    def test_input_names_are_plain(self):
        with self.assertRaises(ValueError):
            self.write(inputs={"../knowledge.md": "x"})
