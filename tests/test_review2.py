"""Tests for the step 2 review fixes."""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

from helpers import CaseTest, build_runs
from rehearse import checks as checks_mod
from rehearse.backends import BackendError
from rehearse.checks import Check, Run, check_run, load_checks, run_check
from rehearse.case import load_case
from rehearse.parse import minutes, section
from test_board import CHECKS, GOOD, board
from test_cli import run_main

ONE_CRASH = '''
from rehearse.checks import Check
def flaky(run):
    if run.meta["repeat"] == 2:
        raise RuntimeError("boom")
    return Check.passed("ok")
'''


class BoardCellTests(CaseTest):
    def test_error_on_one_of_three_is_not_all(self):
        out = board(build_runs(self.root, checks_py=ONE_CRASH, n=3, **GOOD))
        self.assertIn("| baseline | dana | 2 of 2 +1 error | 3 of 3 ✓all |", out)
        self.assertEqual(out.count("✓all"), 1)

    def test_run_without_checks_json_is_unchecked(self):
        runs = build_runs(self.root, checks_py=CHECKS, n=3, **GOOD)
        victim = sorted(runs.iterdir())[0]
        (victim / "checks.json").unlink()
        out = board(runs)
        self.assertIn("| baseline | dana | 2 of 2 +1 unchecked | 2 of 2 +1 unchecked |", out)
        self.assertNotIn("✓all", out)
        self.assertIn(f"{victim.name}: no checks.json", out)

    def test_read_first_reasons(self):
        src = ("from rehearse.checks import Check\ndef bad(run):\n    return Check.failed('x')\n"
               "def crash(run):\n    raise ValueError('v')\n")
        out = board(build_runs(self.root, checks_py=src, **GOOD))
        self.assertIn("failed: bad; check error: crash", out)

    def test_n_is_explained_under_table(self):
        out = board(build_runs(self.root, checks_py=CHECKS, **GOOD))
        self.assertLess(out.index("|---|"), out.index("Each cell is `k of n`"))

    def test_error_run_checks_are_still_saved_but_excluded(self):
        runs = build_runs(self.root, checks_py=CHECKS, subject=(BackendError("down"),), sim=GOOD["sim"])
        (folder,) = runs.iterdir()
        self.assertTrue((folder / "checks.json").exists())
        self.assertIn("| - +1 error run | - +1 error run |", board(runs))


class CompareCellTests(CaseTest):
    def pair(self, before_kw, after_kw):
        self.k = getattr(self, "k", 0) + 1
        b = build_runs(self.root / f"b{self.k}", **{"checks_py": CHECKS, **GOOD, **before_kw})
        a = build_runs(self.root / f"a{self.k}", **{"checks_py": CHECKS, **GOOD, **after_kw})
        code, out, err = run_main(["compare", str(b), str(a)])
        self.assertEqual(code, 0, err)
        return out

    def test_denominator_change_is_flagged(self):
        out = self.pair(dict(n=3), dict(n=3, subject=(BackendError("down"),)))
        self.assertIn("denominators differ", out)
        self.assertIn("+3 error run", out)
        self.assertIn("the number of runs that counted", out)
        self.assertNotIn("| up |", out)

    def test_partials_count_as_a_change(self):
        half = "from rehearse.checks import Check\ndef half(run):\n    return Check.partial('m')\n"
        fail = "from rehearse.checks import Check\ndef half(run):\n    return Check.failed('m')\n"
        out = self.pair(dict(checks_py=half), dict(checks_py=fail))
        self.assertIn("| half | 0 of 1 (+1 partial) | 0 of 1 | down |", out)
        out = self.pair(dict(checks_py=fail), dict(checks_py=half))
        self.assertIn("| up |", out)

    def test_mock_banner(self):
        self.assertIn("PLUMBING ONLY, NOT A RESULT", self.pair({}, {}))

    def test_unchecked_shown(self):
        b = build_runs(self.root / "b", checks_py=CHECKS, **GOOD)
        a = build_runs(self.root / "a", checks_py=CHECKS, **GOOD)
        (next(a.iterdir()) / "checks.json").unlink()
        code, out, _ = run_main(["compare", str(b), str(a)])
        self.assertIn("+1 unchecked", out)
        self.assertIn("denominators differ", out)


class LoaderTests(CaseTest):
    def write(self, text, name="c.py"):
        p = self.root / name
        p.write_text(text)
        return p

    def test_selection_rule(self):
        (self.root / "helper_mod.py").write_text("def shared(run):\n    return True\nVALUE = 3\n")
        p = self.write('''
from helper_mod import shared, VALUE
from os.path import basename
from rehearse.checks import Check
def total(text):
    return 1
def two(run, extra):
    return 1
def wrong_name(r):
    return True
def real(run):
    """Real one."""
    return VALUE == 3
def _hidden(run):
    return True
''')
        names = [n for n, _, _ in load_checks(p)]
        self.assertEqual(names, ["real"])
        self.assertEqual(run_check(load_checks(p)[0][1], None), Check("pass", "(no evidence given)"))

    def test_sys_modules_and_path_restored(self):
        before_path, before_mods = list(sys.path), set(sys.modules)
        load_checks(self.write("def a(run):\n    return True\n"))
        self.assertEqual(sys.path, before_path)
        self.assertEqual(set(sys.modules), before_mods)

    def test_dataclass_in_checks_file_loads(self):
        p = self.write("from dataclasses import dataclass\n@dataclass\nclass P:\n    x: int = 1\n"
                       "def a(run):\n    return P().x == 1\n")
        self.assertEqual([n for n, _, _ in load_checks(p)], ["a"])

    def test_system_exit_in_check_is_error(self):
        p = self.write("import sys\ndef quits(run):\n    sys.exit(3)\n")
        res = run_check(load_checks(p)[0][1], None)
        self.assertEqual(res.result, "error")
        self.assertIn("SystemExit", res.evidence)

    def test_keyboard_interrupt_is_not_swallowed(self):
        def stop(run):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_check(stop, None)

    def test_exit_during_import_is_recorded(self):
        runs = build_runs(self.root, checks_py="import sys\nsys.exit(2)\n", **GOOD)
        (folder,) = runs.iterdir()
        data = check_run(folder, load_case(self.root))
        self.assertEqual(data["results"][0]["result"], "error")

    def test_each_check_gets_its_own_turns(self):
        src = ('def first(run):\n    run.turns.clear()\n    run.meta["x"] = 1\n    return True\n'
               'def second(run):\n    return len(run.turns) > 0 and "x" not in run.meta\n')
        runs = build_runs(self.root, checks_py=src, **GOOD)
        (folder,) = runs.iterdir()
        data = check_run(folder, load_case(self.root))
        self.assertEqual({r["name"]: r["result"] for r in data["results"]}, {"first": "pass", "second": "pass"})
        self.assertTrue(len(Run.load(folder).turns) > 0)


class CliOutputTests(CaseTest):
    def test_output_is_scrubbed_and_relative(self):
        runs = build_runs(self.root, **GOOD)
        old = os.getcwd()
        os.chdir(self.root)
        try:
            code, out, _ = run_main(["check", "runs"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("runs/"), out)
        self.assertNotIn(str(self.root), out)

    def test_printed_text_has_no_email_or_home(self):
        runs = build_runs(self.root, **GOOD)
        (folder,) = runs.iterdir()
        code, out, err = run_main(["check", str(folder), "--case", str(self.root)])
        self.assertNotIn(str(Path.home()) + "/", out + err)
        code, out, err = run_main(["board", str(Path.home() / "me@example.com-nowhere")])
        self.assertEqual(code, 2)
        self.assertNotIn("me@example.com", err)
        self.assertIn("[email removed]", err)

    def test_check_continues_past_unreadable_run(self):
        runs = build_runs(self.root, checks_py=CHECKS, n=3, **GOOD)
        folders = sorted(runs.iterdir())
        (folders[0] / "turns.jsonl").unlink()
        code, out, _ = run_main(["check", str(runs)])
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("UNREADABLE", lines[0])
        self.assertIn("1 pass", lines[1])
        self.assertIn("1 pass", lines[2])


class ParseNitTests(unittest.TestCase):
    def test_minutes_leftmost_and_range(self):
        self.assertEqual(minutes("30 min (of 2 hours)"), 30)
        self.assertEqual(minutes("2 hours (30 min of it)"), 120)
        self.assertEqual(minutes("5-10 min"), 10)
        self.assertEqual(minutes("1-2 hours"), 120)
        self.assertEqual(minutes("5\u201310 minutes"), 10)
        self.assertEqual(minutes("1h 30m"), 90)

    def test_section_prefers_start_of_heading(self):
        md = "## Not the next steps\nWRONG\n## Next steps\nRIGHT\n"
        self.assertEqual(section(md, "next steps"), "RIGHT")
        self.assertEqual(section("## Not the next steps\nONLY\n", "next steps"), "ONLY")
        self.assertEqual(section("## 3. Next steps today\nOK\n## Other\n", "next steps"), "OK")


class DocsTests(unittest.TestCase):
    def test_docs_state_the_test_command_and_checks_doc_exists(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("README.md", "AGENTS.md", "docs/DESIGN.md"):
            self.assertIn("uv run python -m unittest discover -s tests", (root / name).read_text(), name)
        self.assertIn("docs/CHECKS.md", (root / "README.md").read_text())
        self.assertTrue((root / "docs" / "CHECKS.md").is_file())


if __name__ == "__main__":
    unittest.main()
