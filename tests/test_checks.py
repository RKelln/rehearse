import json
import unittest
from pathlib import Path

from helpers import TODAY, CaseTest, build_runs, make_case
from rehearse.backends import MockBackend, SeatResult
from rehearse.case import CaseError, load_case
from rehearse.checks import Check, Run, check_run, find_case_file, load_checks
from rehearse.runs import execute_run
from test_cli import run_main

CHECKS = '''
from rehearse.checks import Check
import os

def asks(run):
    """Asked a question.

    More detail here."""
    return Check.passed("q") if any("?" in t for t in run.subject_turns) else Check.failed("none")

def truthy(run):
    return True

def falsy(run):
    return False

def boom(run):
    """Always crashes."""
    raise ValueError("bad thing")

def half(run):
    return Check.partial("meh")

def skipped(run):
    return Check.skip("not applicable")

def bad_return(run):
    return 5

def _private(run):
    return Check.failed("never run")

def two_args(run, extra):
    return True
'''


def by_name(data):
    return {r["name"]: r for r in data["results"]}


class CheckResultTests(CaseTest):
    def setUp(self):
        super().setUp()
        runs = build_runs(self.root, checks_py=CHECKS)
        (self.folder,) = list(runs.iterdir())
        self.data = json.loads((self.folder / "checks.json").read_text())
        self.r = by_name(self.data)

    def test_which_functions_are_checks(self):
        self.assertEqual(set(self.r), {"asks", "truthy", "falsy", "boom", "half", "skipped", "bad_return"})

    def test_each_result_path(self):
        self.assertEqual(self.r["asks"]["result"], "pass")
        self.assertEqual(self.r["asks"]["evidence"], "q")
        self.assertEqual(self.r["asks"]["description"], "Asked a question.")
        self.assertEqual((self.r["truthy"]["result"], self.r["truthy"]["evidence"]), ("pass", "(no evidence given)"))
        self.assertEqual(self.r["falsy"]["result"], "fail")
        self.assertEqual((self.r["boom"]["result"], self.r["boom"]["evidence"]), ("error", "ValueError: bad thing"))
        self.assertEqual(self.r["half"]["result"], "partial")
        self.assertEqual((self.r["skipped"]["result"], self.r["skipped"]["evidence"]), ("skip", "not applicable"))
        self.assertEqual(self.r["bad_return"]["result"], "error")

    def test_schema(self):
        d = self.data
        for k in ("checked_at", "rehearse_version", "checks_file", "checks_sha256", "results",
                  "sim_check", "void", "void_reason"):
            self.assertIn(k, d)
        self.assertEqual(d["checks_file"], "checks.py")
        self.assertEqual(len(d["checks_sha256"]), 64)
        self.assertFalse(d["void"])

    def test_constructors(self):
        self.assertEqual(Check.passed("a").result, "pass")
        self.assertEqual(Check.partial("a").result, "partial")
        self.assertEqual(Check.failed("a").result, "fail")
        self.assertEqual(Check.skip("a"), Check("skip", "a"))

    def test_rerun_keeps_history(self):
        case = load_case(self.root)
        first = (self.folder / "checks.json").read_text()
        check_run(self.folder, case)
        check_run(self.folder, case)
        history = sorted((self.folder / "history").iterdir())
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].read_text(), first)
        self.assertTrue((self.folder / "checks.json").exists())

    def test_syntax_error_in_checks_file_is_recorded(self):
        (self.root / "checks.py").write_text("def broken(:\n")
        data = check_run(self.folder, load_case(self.root))
        self.assertEqual(data["results"][0]["result"], "error")

    def test_checks_file_missing_is_case_error(self):
        (self.root / "checks.py").unlink()
        with self.assertRaisesRegex(CaseError, "checks file not found"):
            load_case(self.root)

    def test_no_checks_file_still_runs_sim_check(self):
        folder = next(build_runs(self.root / "other", checks_py=None).iterdir())
        data = json.loads((folder / "checks.json").read_text())
        self.assertEqual((data["results"], data["checks_file"]), ([], None))
        self.assertEqual(data["sim_check"]["result"], "pass")

    def test_checks_json_is_scrubbed(self):
        src = 'from rehearse.checks import Check\ndef leak(run):\n    return Check.failed("me@example.com at %s/x" % __import__("pathlib").Path.home())\n'
        folder = next(build_runs(self.root / "leak", checks_py=src).iterdir())
        text = (folder / "checks.json").read_text()
        self.assertNotIn("me@example.com", text)
        self.assertNotIn(str(Path.home()) + "/", text)
        self.assertIn("[email removed]", text)


class RunViewTests(CaseTest):
    def test_load_from_mock_run(self):
        case = load_case(make_case(self.root, persona_opening="Hi there"))
        sr = SeatResult("Sure?", tool_calls=[{"name": "read", "arg": "a.md"}])
        res = execute_run(case, "baseline", "dana", MockBackend([sr, "b"]), MockBackend(["x", "DONE"]), today=TODAY)
        run = Run.load(res.path, case)
        self.assertEqual(run.meta["variant"], "baseline")
        self.assertEqual(run.subject_turns, ["Sure?", "b"])
        self.assertEqual(run.person_turns, ["Hi there", "x", "DONE"])
        self.assertIn("## Subject (turn 1)", run.transcript)
        self.assertEqual(run.tool_calls, [{"name": "read", "arg": "a.md"}])
        self.assertIn("KNOWLEDGE-SENTINEL", run.knowledge)
        self.assertEqual(run.artifacts, res.path / "artifacts")
        self.assertIsNone(run.artifact("review.md"))
        self.assertEqual(run.diff, {"added": [], "changed": [], "removed": []})
        self.assertIsNone(run.scenario_file("week.md"))
        self.assertEqual([c["seat"] for c in run.calls], ["subject", "sim", "subject", "sim"])

    def test_artifacts_and_diff_read_when_present(self):
        case = load_case(make_case(self.root))
        res = execute_run(case, "baseline", "dana", MockBackend(["a"]), MockBackend(["DONE"]), today=TODAY)
        (res.path / "artifacts").mkdir()
        (res.path / "artifacts" / "review.md").write_text("hello")
        (res.path / "artifacts" / "diff.json").write_text('{"added": ["review.md"], "changed": [], "removed": []}')
        run = Run.load(res.path)
        self.assertEqual(run.artifact("review.md"), "hello")
        self.assertEqual(run.diff["added"], ["review.md"])

    def test_not_a_run_folder(self):
        with self.assertRaises(ValueError):
            Run.load(self.root)

    def test_find_case_file_walks_up(self):
        runs = build_runs(self.root)
        folder = next(runs.iterdir())
        self.assertEqual(find_case_file(folder), (self.root / "case.toml").resolve())
        self.assertIsNone(find_case_file(Path("/")))

    def test_load_checks_lists_descriptions(self):
        (self.root / "c.py").write_text(CHECKS)
        found = {n: d for n, _, d in load_checks(self.root / "c.py")}
        self.assertEqual(found["boom"], "Always crashes.")
        self.assertEqual(found["truthy"], "")


class CheckCommandTests(CaseTest):
    def test_check_command_reruns_and_exits(self):
        runs = build_runs(self.root, checks_py=CHECKS, n=2)
        code, out, _ = run_main(["check", str(runs)])
        self.assertEqual(code, 1)                 # the 'boom' check errors
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertIn("2 error", out)
        for folder in runs.iterdir():
            self.assertTrue((folder / "history").is_dir())

    def test_clean_checks_exit_0_and_case_flag(self):
        runs = build_runs(self.root, checks_py="from rehearse.checks import Check\ndef ok(run):\n    return Check.passed('y')\n")
        code, out, _ = run_main(["check", str(next(runs.iterdir())), "--case", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("1 pass", out)

    def test_missing_path_exits_2(self):
        code, _, err = run_main(["check", str(self.root / "nope")])
        self.assertEqual(code, 2)
        self.assertIn("no such folder", err)

    def test_folder_without_runs_exits_2(self):
        (self.root / "empty").mkdir()
        code, _, err = run_main(["check", str(self.root / "empty")])
        self.assertEqual(code, 2)


class AutomaticChecksTests(CaseTest):
    def test_every_run_gets_checks_json(self):
        runs = build_runs(self.root, checks_py=CHECKS, n=2)
        for f in runs.iterdir():
            self.assertTrue((f / "checks.json").exists())


if __name__ == "__main__":
    unittest.main()
