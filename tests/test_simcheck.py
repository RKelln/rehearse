import json
import shutil
import unittest

from helpers import CaseTest, build_runs
from rehearse.checks import Run
from rehearse.simcheck import claims, known_numbers, sim_check


class SimCheckTests(CaseTest):
    def check(self, sim, subject=("What do you need?", "Anything else?", "More?"), **kw):
        runs = build_runs(self.root, subject=subject, sim=sim, **kw)
        (folder,) = list(runs.iterdir())
        return sim_check(Run.load(folder)), folder

    def items(self, res):
        return {i["name"]: i for i in res["items"]}

    def test_clean_sim_passes(self):
        res, _ = self.check(("Not much.", "DONE"))
        self.assertEqual(res["result"], "pass")
        self.assertFalse(res["void"])
        self.assertTrue(all(i["result"] == "pass" for i in res["items"]))

    def test_empty_reply_fails_and_voids(self):
        res, folder = self.check(("", ""))
        self.assertEqual(self.items(res)["empty_reply"]["result"], "fail")
        self.assertTrue(res["void"])
        self.assertIn("empty_reply", res["void_reason"])
        self.assertTrue(json.loads((folder / "checks.json").read_text())["void"])

    def test_empty_reply_once_then_ok_still_fails(self):
        res, _ = self.check(("", "Fine.", "DONE"))
        self.assertEqual(self.items(res)["empty_reply"]["result"], "fail")

    def test_says_it_is_an_ai(self):
        for phrase in ("As an AI, I can't say", "I'm an AI.", "I am a large language model and proud",
                       "I'm an AI language model, sorry", "As a chatbot, no", "I'm ChatGPT", "I am Claude",
                       "I was made by Anthropic", "I was trained on a lot", "As an AI I cannot"):
            res, _ = self.check((phrase, "DONE"), subject=("a?", "b"))
            self.assertEqual(self.items(res)["says_it_is_an_ai"]["result"], "fail", phrase)
            self.assertTrue(res["void"], phrase)
            shutil.rmtree(self.root / "runs")

    def test_ordinary_phrases_do_not_fail(self):
        for phrase in ("I'm an assistant manager", "I am an assistant professor", "As an AI enthusiast I agree",
                       "I'm not an AI expert", "I work as an AI skeptic", "my language modeling class"):
            res, _ = self.check((phrase, "DONE"), subject=("a?", "b"))
            self.assertNotEqual(self.items(res)["says_it_is_an_ai"]["result"], "fail", phrase)
            self.assertFalse(res["void"], phrase)
            shutil.rmtree(self.root / "runs")

    def test_mention_is_partial_and_plain_text_passes(self):
        res, _ = self.check(("My brother works on AI", "DONE"), subject=("a?", "b"))
        item = self.items(res)["says_it_is_an_ai"]
        self.assertEqual(item["result"], "partial")
        self.assertIn("mentions being an AI? check", item["evidence"])
        self.assertFalse(res["void"])
        shutil.rmtree(self.root / "runs")
        res, _ = self.check(("my language modeling class", "DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["says_it_is_an_ai"]["result"], "pass")

    def test_stop_word_on_first_reply(self):
        res, _ = self.check(("DONE",), subject=("a?",))
        self.assertEqual(self.items(res)["stop_on_first_reply"]["result"], "fail")
        self.assertTrue(res["void"])

    def test_stage_directions_partial(self):
        for text in ("*sighs* fine", "ok (laughs)", "[pause] sure"):
            res, _ = self.check((text, "DONE"), subject=("a?", "b"))
            self.assertEqual(self.items(res)["stage_directions"]["result"], "partial", text)
            self.assertEqual(res["result"], "partial")
            self.assertFalse(res["void"])
            shutil.rmtree(self.root / "runs")

    def test_parenthetical_that_is_not_a_stage_direction(self):
        res, _ = self.check(("Tuesday (evening) works", "DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["stage_directions"]["result"], "pass")

    def test_label_stripped_partial(self):
        res, _ = self.check(("Dana: fine", "DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["label_stripped"]["result"], "partial")
        self.assertEqual(res["result"], "partial")

    def test_stop_with_text_partial(self):
        res, _ = self.check(("ok", "thanks, DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["stop_with_text"]["result"], "partial")
        self.assertFalse(res["void"])

    def test_invented_number_flagged(self):
        res, _ = self.check(("I owe 987654 dollars", "DONE"), subject=("a?", "b"))
        item = self.items(res)["invented_numbers"]
        self.assertEqual(item["result"], "partial")
        self.assertEqual(item["evidence"], "may be invented: 987654")
        self.assertFalse(res["void"])

    def test_invented_date_flagged_but_not_if_subject_said_it_first(self):
        res, _ = self.check(("It was Oct 23", "DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["invented_numbers"]["evidence"], "may be invented: oct 23")
        shutil.rmtree(self.root / "runs")
        res, _ = self.check(("It was Oct 23", "DONE"), subject=("Was it October 23?", "b"))
        self.assertEqual(self.items(res)["invented_numbers"]["result"], "pass")

    def test_subject_numbers_only_count_if_earlier(self):
        # The subject says 777 only in its second reply; the sim used it in its first.
        res, _ = self.check(("it was 777", "DONE"), subject=("a?", "now 777"))
        self.assertEqual(self.items(res)["invented_numbers"]["result"], "partial")

    def test_numbers_from_knowledge_and_persona_are_fine(self):
        res, _ = self.check(("at 5 or 03", "DONE"), subject=("a?", "b"))   # both appear in the sentinel page
        self.assertEqual(self.items(res)["invented_numbers"]["result"], "pass")

    def test_claims_and_known(self):
        shown = [c[0] for c in claims("1,500 words on October 23rd, 4:30, 12/05")]
        self.assertEqual(shown, ["oct 23", "4:30", "1500", "12/05"])
        self.assertEqual([c[0] for c in claims("23 Oct")], ["oct 23"])
        self.assertEqual(claims("none"), [])
        self.assertTrue({"7:30", "7", "30"} <= known_numbers("at 7:30"))
        self.assertTrue({"oct 27", "27"} <= known_numbers("Oct 27"))

    def test_times_known_in_either_form(self):
        for known, said in (("Does 7:30 work?", "7 works"), ("Does 7:30 work?", "30 is fine"),
                            ("Is 5pm ok?", "17:00 is fine"), ("Is 17:00 ok?", "5pm is fine"),
                            ("How about 7:30?", "7:30 works"), ("How about 7:30?", "19:30 works"),
                            ("Is 5 pm ok?", "5:00 pm")):
            res, _ = self.check((said, "DONE"), subject=(known, "b"))
            self.assertEqual(self.items(res)["invented_numbers"]["result"], "pass", (known, said))
            shutil.rmtree(self.root / "runs")

    def test_wrong_time_is_flagged(self):
        res, _ = self.check(("6pm then", "DONE"), subject=("Is 5pm ok?", "b"))
        self.assertEqual(self.items(res)["invented_numbers"]["evidence"], "may be invented: 6pm")

    def test_date_listed_once(self):
        res, _ = self.check(("It was Oct 27", "DONE"), subject=("a?", "b"))
        self.assertEqual(self.items(res)["invented_numbers"]["evidence"], "may be invented: oct 27")

if __name__ == "__main__":
    unittest.main()
