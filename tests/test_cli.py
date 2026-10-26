import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import CaseTest, make_case
from rehearse import cli
from rehearse.backends import QuotaError
from rehearse.case import load_case
from rehearse.runs import execute_run

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "mock-case"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(CaseTest):
    def setUp(self):
        super().setUp()
        make_case(self.root, sims=("dana", "sam"), subject_replies=["a", "b"], sim_replies=["x", "DONE"])

    def test_dry_run_lists_grid(self):
        code, out, _ = run_main(["suite", str(self.root), "--dry-run", "--repeats", "2"])
        self.assertEqual(code, 0)
        self.assertIn("8 runs", out)
        self.assertIn("cost estimate: not available yet", out)
        self.assertIn("cheerful x sam", out)
        self.assertFalse((self.root / "runs").exists())

    def test_suite_filters(self):
        code, out, _ = run_main(["suite", str(self.root), "--variants", "cheerful", "--sims", "sam", "--repeats", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(len([l for l in out.splitlines() if "stop:" in l]), 2)
        self.assertIn("PLUMBING ONLY, NOT A RESULT", out)
        names = sorted(p.name for p in (self.root / "runs").iterdir())
        self.assertEqual(len(names), 2)
        self.assertTrue(all("cheerful-sam" in n for n in names))

    def test_full_grid(self):
        code, _, _ = run_main(["suite", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 4)

    def test_run_command(self):
        code, out, _ = run_main(["run", str(self.root / "case.toml"), "--variant", "baseline", "--sim", "dana"])
        self.assertEqual(code, 0)
        self.assertIn("stop: sim-stop  turns: 2", out)

    def test_unknown_names(self):
        code, _, err = run_main(["run", str(self.root), "--variant", "nope", "--sim", "dana"])
        self.assertEqual(code, 2)
        self.assertIn("unknown variant 'nope'", err)
        self.assertIn("baseline, cheerful", err)
        code, _, _ = run_main(["suite", str(self.root), "--sims", "zed"])
        self.assertEqual(code, 2)

    def test_bad_case_message(self):
        code, _, err = run_main(["suite", str(self.root / "missing")])
        self.assertEqual(code, 2)
        self.assertIn("error: no case.toml", err)


class SuiteQuotaTests(CaseTest):
    def test_quota_error_stops_suite(self):
        case = load_case(make_case(self.root, sims=("dana", "sam")))
        calls = []

        def fake(case, v, s, repeat=1):
            calls.append((v, s))
            if len(calls) == 2:
                raise QuotaError("out of quota")
            return execute_run(case, v, s, repeat=repeat)

        lines = []
        results, stopped = cli.run_suite(case, ["baseline", "cheerful"], ["dana", "sam"], 1,
                                         run_fn=fake, out=lines.append)
        self.assertTrue(stopped)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(results), 1)
        self.assertEqual([l for l in lines if l.startswith("not run")],
                         ["not run: baseline x sam (repeat 1)", "not run: cheerful x dana (repeat 1)",
                          "not run: cheerful x sam (repeat 1)"])


class ExampleCaseTests(unittest.TestCase):
    def test_example_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "mock-case"
            shutil.copytree(EXAMPLE, case_dir, ignore=shutil.ignore_patterns("runs"))
            toml = case_dir / "case.toml"
            toml.write_text(toml.read_text().replace('# runs_dir = "runs"', 'runs_dir = "out"'))
            code, out, _ = run_main(["suite", str(case_dir)])
            self.assertEqual(code, 0, out)
            runs = sorted((case_dir / "out").iterdir())
            self.assertEqual(len(runs), 4)
            for r in runs:
                meta = json.loads((r / "meta.json").read_text())
                self.assertEqual(meta["stop_reason"], "sim-stop")
                self.assertTrue(meta["mock"])
                checks = json.loads((r / "checks.json").read_text())
                self.assertEqual([x["name"] for x in checks["results"]],
                                 ["short_replies", "asked_a_question", "never_says_banned_phrase"])
                self.assertTrue(all(x["result"] == "pass" for x in checks["results"]))
            code, board_out, _ = run_main(["board", str(case_dir / "out")])
            self.assertEqual(code, 0)
            self.assertIn("| baseline | dana | 1 of 1 |", board_out)
            self.assertIn("PLUMBING ONLY", out)


if __name__ == "__main__":
    unittest.main()
