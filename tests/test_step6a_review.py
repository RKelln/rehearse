"""Board seat warning, compare hashes and pairing, importer frame."""
import json
from pathlib import Path

from helpers import CaseTest, KNOWLEDGE_SENTINEL, build_runs, make_case
from test_cli import run_main
from rehearse import board
from rehearse.case import load_case
from rehearse.importing import write_imported_run

TURNS = [{"side": "person", "text": "Hi"}, {"side": "subject", "text": "Hello"}, {"side": "person", "text": "ok"}]


def imported(case, seats, variant="baseline", sim="dana", **kw):
    return write_imported_run(case, variant=variant, sim=sim, turns=TURNS, artifacts={}, source="old/x",
                              date="2026-10-26", meta={"seats": seats, "models": {"subject": [], "sim": []},
                                                       **kw.pop("meta", {})}, **kw)


class BoardSeatTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.case = load_case(make_case(self.root))

    def text(self):
        return board.board([str(self.root / "runs")])

    def test_runs_without_a_sim_do_not_trigger_the_warning(self):
        imported(self.case, {"subject": "S", "sim": "none"}, sim="cont:a", meta={"sim_used": False})
        imported(self.case, {"subject": "S", "sim": "T"})
        text = self.text()
        self.assertNotIn("WARNING", text)
        self.assertIn("sim seat: T", text)

    def test_different_subject_or_sim_seats_warn(self):
        imported(self.case, {"subject": "S", "sim": "T"})
        imported(self.case, {"subject": "S2", "sim": "T"})
        self.assertIn("WARNING", self.text())

    def test_different_sim_seats_warn(self):
        imported(self.case, {"subject": "S", "sim": "T"})
        imported(self.case, {"subject": "S", "sim": "T2"})
        self.assertIn("WARNING", self.text())

    def test_no_sim_anywhere_says_none(self):
        imported(self.case, {"subject": "S", "sim": "none"}, sim="cont:a", meta={"sim_used": False})
        self.assertIn("sim seat: none", self.text())


class CompareHashTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.live = build_runs(self.root / "live", subject=("a",), sim=("DONE",))
        self.case = load_case(self.root / "live")
        self.know = (self.root / "live" / "knowledge" / "dana.md").read_text()
        self.persona = (self.root / "live" / "sims" / "dana.md").read_text()
        self.imp = self.root / "imp"
        self.imp.mkdir()
        self.icase = load_case(make_case(self.imp))

    def compare(self, *args):
        code, out, err = run_main(["compare", str(self.live), str(self.imp / "runs"), *args])
        self.assertEqual(code, 0, err)
        return out

    def test_imported_run_with_same_text_is_not_changed(self):
        imported(self.icase, {"subject": "mock", "sim": "mock"}, inputs={"knowledge.md": self.know, "persona.md": self.persona,
                                                                        "frame.md": self.case.frame_text()})
        out = self.compare()
        self.assertNotIn("changed", out)
        self.assertNotIn("not recorded", out)

    def test_missing_hash_is_not_recorded_not_changed(self):
        imported(self.icase, {"subject": "mock", "sim": "mock"})
        out = self.compare()
        self.assertIn("the knowledge page for tcase / dana is not recorded in after runs", out)
        self.assertIn("the persona for tcase / dana is not recorded in after runs", out)
        self.assertNotIn("changed", out)

    def test_changed_text_still_says_changed(self):
        imported(self.icase, {"subject": "mock", "sim": "mock"}, inputs={"knowledge.md": self.know + "more"})
        self.assertIn("the knowledge page changed for tcase / dana", self.compare())

    def test_importer_records_frame(self):
        p = imported(self.icase, {"subject": "mock", "sim": "mock"}, inputs={"frame.md": "FRAME {stop}"})
        manifest = json.loads((p / "inputs" / "manifest.json").read_text())
        self.assertIn("written_sha256", manifest["frame.md"])
        self.assertEqual((p / "inputs" / "frame.md").read_text(), "FRAME {stop}")


class PairTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.a = build_runs(self.root / "a", variants=("old",))
        self.b = build_runs(self.root / "b", variants=("new",))

    def test_without_pair_variants_are_in_only_one_set(self):
        code, out, _ = run_main(["compare", str(self.a), str(self.b)])
        self.assertIn("variants in only one set", out)

    def test_pair_renames_in_memory_and_warns(self):
        before = sorted(p.name for p in self.a.iterdir())
        code, out, err = run_main(["compare", str(self.a), str(self.b), "--pair", "old=new"])
        self.assertEqual(code, 0, err)
        self.assertIn("variants paired by hand: old→new", out)
        self.assertIn("| tcase | old→new | dana |", out)
        self.assertNotIn("variants in only one set", out)
        self.assertEqual(before, sorted(p.name for p in self.a.iterdir()))
        self.assertEqual(json.loads(next(self.a.glob("*/meta.json")).read_text())["variant"], "old")

    def test_bad_pairs_are_plain_errors(self):
        code, _, err = run_main(["compare", str(self.a), str(self.b), "--pair", "nope=new"])
        self.assertEqual(code, 2)
        self.assertIn("no before run has the variant 'nope'", err)
        code, _, err = run_main(["compare", str(self.a), str(self.b), "--pair", "oops"])
        self.assertEqual(code, 2)
        self.assertIn("OLD=NEW", err)
