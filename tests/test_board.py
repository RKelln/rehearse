import json
import unittest

from helpers import CaseTest, build_runs
from rehearse.backends import BackendError
from rehearse.board import SIM_CHECK, cell_text, load_records, tally
from test_cli import run_main

CHECKS = '''
from rehearse.checks import Check

def asks(run):
    """Asked a question."""
    return Check.passed("q") if any("?" in t for t in run.subject_turns) else Check.failed("none")
'''
GOOD = dict(subject=("What?", "More?"), sim=("A thing.", "DONE"))
VOID = dict(subject=("What?", "More?"), sim=("As an AI I cannot.", "DONE"))


def board(path, *extra):
    code, out, err = run_main(["board", str(path), *extra])
    assert code == 0, err
    return out


class BoardTests(CaseTest):
    def test_k_of_n_and_all_marker(self):
        runs = build_runs(self.root, checks_py=CHECKS, n=2, **GOOD)
        out = board(runs)
        self.assertIn("| baseline | dana | 2 of 2 ✓all | 2 of 2 ✓all |", out)

    def test_single_run_has_no_all_marker(self):
        out = board(build_runs(self.root, checks_py=CHECKS, **GOOD))
        self.assertIn("| 1 of 1 | 1 of 1 |", out)
        self.assertNotIn("✓all", out)

    def test_failures_show_in_count_and_read_list(self):
        runs = build_runs(self.root, checks_py=CHECKS, subject=("No", "no"), sim=GOOD["sim"], n=2)
        out = board(runs)
        self.assertIn("| 0 of 2 |", out)
        self.assertIn("Read these first", out)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith("- ") and "dana-" in l), 2)

    def test_partial_counts(self):
        src = "from rehearse.checks import Check\ndef half(run):\n    return Check.partial('meh')\n"
        out = board(build_runs(self.root, checks_py=src, n=2, **GOOD))
        self.assertIn("0 of 2 (+2 partial)", out)

    def test_void_runs_are_excluded_and_noted(self):
        build_runs(self.root, checks_py=CHECKS, **GOOD)
        runs = build_runs(self.root, checks_py=CHECKS, **VOID)
        out = board(runs)
        self.assertIn("| baseline | dana | 1 of 1 +1 void | 1 of 2 |", out)   # asks leaves the void run out
        self.assertNotIn("✓all", out)
        self.assertIn("Voided runs (failed the sim check): 1", out)
        self.assertIn("void: says_it_is_an_ai", out)
        rec = load_records(str(runs))
        self.assertEqual(tally(rec, "asks")["void"], 1)
        self.assertEqual(tally(rec, SIM_CHECK), {"k": 1, "n": 2, "partial": 0, "fail": 1, "error": 0,
                                                 "unchecked": 0, "skip": 0, "void": 0, "errorrun": 0})

    def test_errors_and_skips_excluded(self):
        src = "def boom(run):\n    raise KeyError('x')\n"
        out = board(build_runs(self.root, checks_py=src, **GOOD))
        self.assertIn("| boom |", out)
        self.assertIn("| - +1 error |", out)

    def test_header_banner_footer_stops(self):
        out = board(build_runs(self.root, checks_py=CHECKS, **GOOD))
        self.assertIn("## tcase", out)
        self.assertIn("subject seat: mock, model: mock", out)
        self.assertIn("PLUMBING ONLY, NOT A RESULT", out)
        self.assertIn("Stop reasons: sim-stop 1", out)
        self.assertIn("Counts are runs, never percentages. One run is an anecdote. Read the transcripts.", out)

    def test_models_differ_warning_and_no_mock_banner(self):
        build_runs(self.root, subject_model="m1", **GOOD)
        runs = build_runs(self.root, subject_model="m2", **GOOD)
        out = board(runs)
        self.assertIn("WARNING: the seats or models differ", out)

    def test_error_run_counted(self):
        build_runs(self.root, checks_py=CHECKS, **GOOD)
        runs = build_runs(self.root, checks_py=CHECKS, subject=(BackendError("down"),), sim=GOOD["sim"])
        out = board(runs)
        self.assertIn("Stop reasons: error: down 1, sim-stop 1", out)
        self.assertIn("Runs that ended in an error: 1", out)
        self.assertIn("| baseline | dana | 1 of 1 +1 error run | 1 of 1 +1 error run |", out)
        self.assertIn("run ended: error: down", out)

    def test_groups_by_case(self):
        a = build_runs(self.root / "a", name="alpha", **GOOD)
        b = build_runs(self.root / "b", name="beta", **GOOD)
        out = board(a.parent.parent)
        self.assertIn("## alpha", out)
        self.assertIn("## beta", out)

    def test_out_file_and_scrub(self):
        runs = build_runs(self.root, sim=("mail me@example.com", "DONE"), subject=("a?", "b"))
        target = self.root / "board.md"
        out = board(runs, "--out", str(target))
        self.assertEqual(target.read_text().strip(), out.strip())
        self.assertNotIn("me@example.com", out)

    def test_missing_folder_exit_2(self):
        code, _, err = run_main(["board", str(self.root / "nope")])
        self.assertEqual(code, 2)

    def test_cell_text(self):
        z = dict(k=0, n=0, partial=0, fail=0, error=0, unchecked=0, skip=0, void=0, errorrun=0)
        self.assertEqual(cell_text(z), "-")
        self.assertEqual(cell_text({**z, "k": 3, "n": 3}), "3 of 3 ✓all")
        self.assertEqual(cell_text({**z, "k": 1, "n": 3, "partial": 1}), "1 of 3 (+1 partial)")
        self.assertEqual(cell_text({**z, "k": 2, "n": 2, "error": 1}), "2 of 2 +1 error")
        self.assertEqual(cell_text({**z, "k": 1, "n": 1, "error": 1, "unchecked": 2, "skip": 1, "void": 3, "errorrun": 4}),
                         "1 of 1 +1 error +2 unchecked +1 skip +3 void +4 error run")


class CompareTests(CaseTest):
    def pair(self, before_kw=None, after_kw=None, base=None):
        base = base or dict(checks_py=CHECKS, **GOOD)
        self.calls = getattr(self, "calls", 0) + 1
        b = build_runs(self.root / f"before{self.calls}", **{**base, **(before_kw or {})})
        a = build_runs(self.root / f"after{self.calls}", **{**base, **(after_kw or {})})
        code, out, err = run_main(["compare", str(b), str(a)])
        self.assertEqual(code, 0, err)
        return out

    def test_identical_sets_have_no_warnings(self):
        out = self.pair()
        self.assertNotIn("Warnings", out)
        self.assertIn("| tcase | baseline | dana | asks | 1 of 1 | 1 of 1 | same |", out)
        self.assertIn("Small counts move by chance. Rerun before believing a change of one or two.", out)

    def test_change_direction(self):
        out = self.pair(before_kw=dict(subject=("No.", "No.")))
        self.assertIn("| asks | 0 of 1 | 1 of 1 | up |", out)
        out = self.pair(after_kw=dict(subject=("No.", "No.")))
        self.assertIn("| asks | 1 of 1 | 0 of 1 | down |", out)

    def warn(self, text, before_kw=None, after_kw=None):
        out = self.pair(before_kw, after_kw)
        self.assertIn("Warnings", out)
        self.assertIn(text, out)

    def test_models_warning(self):
        self.warn("seats or models differ", after_kw=dict(subject_model="m9"))

    def test_max_turns_warning(self):
        self.warn("max_turns differ", after_kw=dict(max_turns=3))

    def test_n_per_cell_warning(self):
        self.warn("number of runs differs for tcase / baseline / dana: before 1, after 2", after_kw=dict(n=2))

    def test_checks_file_warning(self):
        self.warn("checks files", after_kw=dict(checks_py=CHECKS + "\n# edited\n"))

    def test_persona_knowledge_frame_warnings(self):
        self.warn("the persona changed for tcase / dana", after_kw=dict(extra_persona=" Edited."))
        self.warn("the knowledge page changed", after_kw=dict(knowledge_extra=" Edited."))
        self.warn("the sim frame changed", after_kw=dict(
            frame="{persona} {mood} {knowledge} {stop} {opening} {turn_line} custom"))

    def test_variant_warnings(self):
        out = self.pair(base=dict(checks_py=CHECKS, variants=("baseline", "other"), **GOOD),
                        after_kw=dict(variant_extra=" Edited."))
        self.assertIn("more than one variant changed", out)
        out = self.pair(after_kw=dict(variants=("baseline", "other")))
        self.assertIn("variants in only one set: tcase / other", out)

    def test_one_variant_changed_is_fine(self):
        out = self.pair(after_kw=dict(variant_extra=" Edited."))
        self.assertNotIn("Warnings", out)

    def test_comma_separated_sets(self):
        a = build_runs(self.root / "a", checks_py=CHECKS, **GOOD)
        b = build_runs(self.root / "b", checks_py=CHECKS, **GOOD)
        code, out, _ = run_main(["compare", f"{a},{b}", str(a)])
        self.assertEqual(code, 0)
        self.assertIn("number of runs differs", out)

    def test_void_shown_in_compare(self):
        out = self.pair(after_kw=dict(sim=VOID["sim"]))
        self.assertIn("+1 void", out)
        self.assertIn("denominators differ", out)

    def test_missing_folder_exit_2(self):
        code, _, _ = run_main(["compare", str(self.root / "x"), str(self.root / "y")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
