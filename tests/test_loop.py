import unittest

from helpers import (CaseTest, KNOWLEDGE_SENTINEL, MOOD_SENTINEL, PERSONA_SENTINEL, VARIANT_SENTINEL)
from rehearse.backends import BackendError, QuotaError, SeatResult
from rehearse.loop import clean_sim_reply, fill_frame


def roles(messages):
    return [m.role for m in messages]


class LoopTests(CaseTest):
    def test_turn_order_and_roles(self):
        out, subj, sim, _ = self.converse(["s1", "s2"], ["p1", "DONE"], persona_opening="open")
        self.assertEqual([(t.side, t.n, t.text) for t in out.turns],
                         [("person", 0, "open"), ("subject", 1, "s1"), ("person", 1, "p1"),
                          ("subject", 2, "s2"), ("person", 2, "DONE")])
        self.assertEqual(out.stop, "sim-stop")
        # Subject sees the person as "user", itself as "assistant".
        self.assertEqual(roles(subj.calls[0][1]), ["user"])
        self.assertEqual(roles(subj.calls[1][1]), ["user", "assistant", "user"])
        self.assertEqual([m.content for m in subj.calls[1][1]], ["open", "s1", "p1"])
        # Sim sees the subject as "user", itself as "assistant".
        self.assertEqual(roles(sim.calls[0][1]), ["user"])
        self.assertEqual([m.content for m in sim.calls[1][1]], ["s1", "p1", "s2"])
        self.assertEqual(roles(sim.calls[1][1]), ["user", "assistant", "user"])
        self.assertIn("You started the chat by typing: open", sim.calls[0][0])

    def test_subject_gets_variant_as_system(self):
        _, subj, _, _ = self.converse(["s1"], ["DONE"], variant="cheerful")
        self.assertIn("cheerful instructions", subj.calls[0][0])

    def test_turn_cap(self):
        out, subj, sim, _ = self.converse(max_turns=3)
        self.assertEqual(out.stop, "turn-cap")
        self.assertEqual(out.count("subject"), 3)
        self.assertEqual(len(sim.calls), 2)       # no sim reply after the last subject turn

    def test_subject_stop_pattern(self):
        out, _, sim, _ = self.converse(["hi", "That's all for today, bye"], stop_pattern="(?i)that's all for today")
        self.assertEqual(out.stop, "subject-stop")
        self.assertEqual(out.count("subject"), 2)
        self.assertEqual(len(sim.calls), 1)

    def test_sim_stop_with_extra_text_is_flagged(self):
        out, *_ = self.converse(["a", "b"], ["x", "Thanks! done."])
        self.assertEqual(out.stop, "sim-stop")
        self.assertTrue(out.stop_with_text)
        self.assertEqual(out.turns[-1].text, "Thanks! done.")

    def test_bare_stop_word_is_not_flagged(self):
        out, *_ = self.converse(["a"], ['"Done."'])
        self.assertEqual(out.stop, "sim-stop")
        self.assertFalse(out.stop_with_text)

    def test_stop_word_at_start_of_longer_reply_does_not_stop(self):
        out, *_ = self.converse(["a", "b"], ["Done with that part, but more to say", "DONE"])
        self.assertEqual(out.turns[2].text, "Done with that part, but more to say")
        self.assertEqual(out.count("subject"), 2)

    def test_is_stop_rule(self):
        from rehearse.loop import is_stop
        cases = [("DONE", (True, False)), ("  'done!' ", (True, False)), ("ok. DONE", (True, True)),
                 ("Done with that part", (False, False)), ("undone", (False, False)),
                 ("I am done now", (False, False)), ("", (False, False))]
        for text, want in cases:
            self.assertEqual(is_stop(text, "DONE"), want, text)

    def test_every_request_starts_with_user_and_alternates(self):
        _, subj, sim, _ = self.converse(max_turns=5, persona_opening="start")
        for seat in (subj, sim):
            self.assertGreater(len(seat.calls), 1)
            for _, messages in seat.calls:
                self.assertEqual(roles(messages), [("user", "assistant")[i % 2] for i in range(len(messages))])
                self.assertEqual(messages[-1].role, "user")

    def test_unexpected_exception_is_recorded_not_lost(self):
        out, *_ = self.converse([OSError("disk gone")])
        self.assertEqual(out.stop, "error: OSError: disk gone")
        self.assertIsNone(out.quota)

    def test_keyboard_interrupt_stops_as_interrupted(self):
        out, *_ = self.converse(["a", KeyboardInterrupt()])
        self.assertEqual(out.stop, "interrupted")
        self.assertTrue(out.interrupted)
        self.assertEqual(out.count("subject"), 1)

    def test_turns_carry_their_call_file(self):
        out, *_ = self.converse(["a", "b"], ["x", "DONE"])
        self.assertEqual([t.call for t in out.turns], [None, "subject-01.json", "sim-01.json",
                                                      "subject-02.json", "sim-02.json"])

    def test_custom_stop_word(self):
        out, *_ = self.converse(["a"], ["FINISHED"], stop="FINISHED")
        self.assertEqual(out.stop, "sim-stop")

    def test_backend_error_subject(self):
        out, *_ = self.converse([BackendError("boom")])
        self.assertEqual(out.stop, "error: boom")
        self.assertIsNone(out.quota)
        self.assertEqual(out.calls[0]["result"]["error"], "boom")

    def test_seat_result_error(self):
        out, *_ = self.converse(["ok", ], [SeatResult(text="", error="bad json")])
        self.assertEqual(out.stop, "error: bad json")

    def test_quota_error_is_kept_for_the_caller(self):
        out, *_ = self.converse(["a"], [QuotaError("out of quota")])
        self.assertTrue(out.stop.startswith("error:"))
        self.assertIsInstance(out.quota, QuotaError)

    def test_empty_sim_reply_retried_once(self):
        out, _, sim, _ = self.converse(["a", "b"], ["  ", "fine", "DONE"])
        self.assertEqual(out.turns[2].text, "fine")
        self.assertEqual(out.stop, "sim-stop")
        self.assertEqual(len(out.calls), 5)

    def test_empty_sim_reply_twice(self):
        out, *_ = self.converse(["a", "b"], ["", "Dana:  "])
        self.assertIn("empty reply twice", out.stop)
        self.assertTrue(out.stop.startswith("error:"))

    def test_empty_subject_reply(self):
        out, *_ = self.converse([" "])
        self.assertTrue(out.stop.startswith("error:"))

    def test_label_stripped(self):
        out, *_ = self.converse(["a", "b"], ["Dana: not much", "Person: DONE"])
        self.assertEqual(out.turns[2].text, "not much")
        self.assertEqual(out.stop, "sim-stop")
        out, *_ = self.converse(["a"], ["User: hi"])
        self.assertEqual(out.turns[2].text, "hi")

    def test_label_strip_leaves_ordinary_colons(self):
        from rehearse.case import Persona
        p = Persona(sim="dana", name="dana", path=None, body="")
        self.assertEqual(clean_sim_reply("Note: it is fine", p), "Note: it is fine")

    def test_no_opening_is_an_error(self):
        out, subj, *_ = self.converse(["a"], opening="")
        self.assertTrue(out.stop.startswith("error: no opening"))
        self.assertEqual(subj.calls, [])

    def test_persona_opening_wins(self):
        out, subj, *_ = self.converse(["a"], ["DONE"], opening="case opening", persona_opening="persona opening")
        self.assertEqual(subj.calls[0][1][0].content, "persona opening")
        self.assertEqual(out.turns[0].text, "persona opening")

    def test_case_opening_used_otherwise(self):
        out, subj, *_ = self.converse(["a"], ["DONE"], opening="case opening")
        self.assertEqual(subj.calls[0][1][0].content, "case opening")

    def test_escalation_lines(self):
        _, _, sim, _ = self.converse(max_turns=5, escalation=("LINE-A", "LINE-B"))
        systems = [s for s, _ in sim.calls]
        self.assertEqual(len(systems), 4)
        self.assertIn("LINE-A", systems[0])
        self.assertIn("LINE-B", systems[1])
        self.assertIn("LINE-B", systems[2])      # the last line repeats
        self.assertNotIn("LINE-A", systems[2])

    def test_no_escalation_no_line(self):
        _, _, sim, _ = self.converse(max_turns=2)
        self.assertNotIn("{turn_line}", sim.calls[0][0])

    def test_frame_fill_is_single_pass(self):
        from rehearse.case import Persona
        p = Persona(sim="d", name="d", path=None, body="B {stop}", mood="M", knowledge="K {persona}")
        text = fill_frame("{persona}|{knowledge}|{stop}|{turn_line}", p, "T")
        self.assertEqual(text.strip(), "B {stop}|K {persona}|DONE|T")

    def test_isolation(self):
        out, subj, sim, _ = self.converse(["a", "b", "c"], ["x", "y", "DONE"], persona_opening="start")
        for system, messages in sim.calls:
            blob = system + " ".join(m.content for m in messages)
            self.assertNotIn(VARIANT_SENTINEL, blob)
            self.assertIn(PERSONA_SENTINEL, system)
            self.assertIn(KNOWLEDGE_SENTINEL, system)
        for system, messages in subj.calls:
            blob = system + " ".join(m.content for m in messages)
            for s in (PERSONA_SENTINEL, MOOD_SENTINEL, KNOWLEDGE_SENTINEL, "Rules:", "DONE"):
                self.assertNotIn(s, blob)
            self.assertIn(VARIANT_SENTINEL, system)


if __name__ == "__main__":
    unittest.main()
