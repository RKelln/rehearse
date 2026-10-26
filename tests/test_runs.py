import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from helpers import TODAY, CaseTest, make_case
from rehearse import __version__
from rehearse.backends import BackendError, MockBackend, QuotaError, SeatResult
from rehearse.case import load_case
from rehearse.runs import execute_run, git_commit, make_run_dir, scrub


class ScrubTests(unittest.TestCase):
    def test_email_and_home(self):
        home = str(Path.home())
        out = scrub(f"me@example.com wrote {home}/x/y and a.b+c@d.co.uk")
        self.assertEqual(out, "[email removed] wrote ~/x/y and [email removed]")


class RunFolderTests(CaseTest):
    def test_naming_and_never_overwrite(self):
        runs = self.root / "runs"
        a, na = make_run_dir(runs, "case", "v", "s", TODAY)
        b, nb = make_run_dir(runs, "case", "v", "s", TODAY)
        self.assertEqual((a.name, b.name), ("2026-10-26-case-v-s-1", "2026-10-26-case-v-s-2"))
        self.assertEqual((na, nb), (1, 2))

    def test_existing_folder_untouched(self):
        runs = self.root / "runs"
        old = runs / "2026-10-26-case-v-s-1"
        old.mkdir(parents=True)
        (old / "keep.txt").write_text("precious")
        new, n = make_run_dir(runs, "case", "v", "s", TODAY)
        self.assertEqual(n, 2)
        self.assertEqual((old / "keep.txt").read_text(), "precious")
        self.assertEqual(list(old.iterdir()), [old / "keep.txt"])

    def test_two_runs_same_day(self):
        case = load_case(make_case(self.root, subject_replies=["a"], sim_replies=["DONE"]))
        r1 = execute_run(case, "baseline", "dana", today=TODAY)
        r2 = execute_run(case, "baseline", "dana", today=TODAY)
        self.assertTrue(r1.path.name.endswith("-1"))
        self.assertTrue(r2.path.name.endswith("-2"))
        self.assertTrue((r1.path / "meta.json").exists())


class SavedRunTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.home = str(Path.home())
        leak = f"mail me at leak@example.com from {self.home}/secret"
        self.case = load_case(make_case(self.root, subject_replies=[leak, "b"],
                                        sim_replies=[leak, "DONE"], persona_opening=f"hi {leak}"))
        self.res = execute_run(self.case, "cheerful", "dana", today=TODAY)
        self.path = self.res.path

    def test_files_exist(self):
        names = {p.name for p in self.path.iterdir()}
        self.assertEqual(names, {"transcript.md", "turns.jsonl", "checks.json", "inputs", "raw", "meta.json", "assessment.md"})
        self.assertEqual(sorted(p.name for p in (self.path / "raw").iterdir()),
                         ["sim-01.json", "sim-02.json", "subject-01.json", "subject-02.json"])
        self.assertEqual(sorted(p.name for p in (self.path / "inputs").iterdir()),
                         ["frame.md", "knowledge.md", "manifest.json", "persona.md", "variant.md"])

    def test_everything_scrubbed(self):
        files = [p for p in self.path.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 10)
        for p in files:
            text = p.read_text()
            self.assertNotIn("leak@example.com", text, p.name)
            self.assertNotIn(self.home, text, p.name)
        self.assertIn("[email removed]", (self.path / "transcript.md").read_text())
        self.assertIn("~/secret", (self.path / "raw" / "subject-01.json").read_text())

    def test_raw_has_request_and_result(self):
        raw = json.loads((self.path / "raw" / "sim-01.json").read_text())
        self.assertIn("system", raw["request"])
        self.assertEqual(raw["request"]["messages"][0]["role"], "user")
        self.assertIn("text", raw["result"])

    def test_manifest_hashes(self):
        m = json.loads((self.path / "inputs" / "manifest.json").read_text())
        self.assertEqual(set(m), {"variant.md", "persona.md", "frame.md", "knowledge.md"})
        for name, entry in m.items():
            data = (self.path / "inputs" / name).read_bytes()
            self.assertEqual(entry["written_sha256"], hashlib.sha256(data).hexdigest())
            self.assertIn("source_sha256", entry)
        src = (self.root / "variants" / "cheerful.md").read_bytes()
        self.assertEqual(m["variant.md"]["source_sha256"], hashlib.sha256(src).hexdigest())

    def test_meta(self):
        m = json.loads((self.path / "meta.json").read_text())
        self.assertEqual((m["case"], m["variant"], m["sim"], m["repeat"]), ("tcase", "cheerful", "dana", 1))
        self.assertEqual(m["folder_number"], 1)
        self.assertEqual(m["seats"], {"subject": "mock", "sim": "mock"})
        self.assertEqual(m["stop_reason"], "sim-stop")
        self.assertEqual(m["turns"], {"subject": 2, "person": 2})
        self.assertTrue(m["mock"])
        self.assertEqual(m["rehearse_version"], __version__)
        self.assertIsNone(m["cost"]["subject"]["usd"])
        self.assertEqual(m["cost"]["subject"]["unknown_calls"], 2)
        self.assertEqual(m["models"]["sim"], ["mock"])
        for k in ("started", "ended", "case_commit"):
            self.assertIn(k, m)

    def test_assessment_stub(self):
        text = (self.path / "assessment.md").read_text()
        self.assertIn("Read transcript.md.", text)
        for h in ("## What worked", "## What failed", "## Notes"):
            self.assertIn(h, text)

    def test_transcript(self):
        text = (self.path / "transcript.md").read_text()
        self.assertIn("Stop reason: sim-stop", text)
        self.assertLess(text.index("## Person (turn 0)"), text.index("## Subject (turn 1)"))
        self.assertLess(text.index("## Subject (turn 1)"), text.index("## Person (turn 1)"))
        self.assertIn("PLUMBING ONLY", text)


class MetaDetailTests(CaseTest):
    def test_cost_known_and_not_mock(self):
        case = load_case(make_case(self.root))
        subj = MockBackend([SeatResult("a", cost_usd=0.5, model="m1"), SeatResult("b", cost_usd=0.25, model="m1")])
        subj.describe = lambda: "claude:x"
        subj.is_mock = False
        res = execute_run(case, "baseline", "dana", subj, MockBackend(["hi", "DONE"]), today=TODAY)
        m = json.loads((res.path / "meta.json").read_text())
        self.assertEqual(m["cost"]["subject"], {"usd": 0.75, "calls": 2, "unknown_calls": 0})
        self.assertIsNone(m["cost"]["sim"]["usd"])
        self.assertFalse(m["mock"])
        self.assertEqual(m["models"]["subject"], ["m1"])
        self.assertNotIn("PLUMBING", (res.path / "transcript.md").read_text())

    def test_git_commit_failure_is_none(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(git_commit(self.root))

    def test_error_run_is_saved(self):
        case = load_case(make_case(self.root))
        res = execute_run(case, "baseline", "dana", MockBackend([BackendError("boom")]), MockBackend(), today=TODAY)
        self.assertEqual(res.stop, "error: boom")
        self.assertTrue((res.path / "raw" / "subject-01.json").exists())

    def test_quota_error_saved_then_raised(self):
        case = load_case(make_case(self.root))
        with self.assertRaises(QuotaError):
            execute_run(case, "baseline", "dana", MockBackend([QuotaError("limit")]), MockBackend(), today=TODAY)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
