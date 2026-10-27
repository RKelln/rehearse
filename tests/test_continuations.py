import json
import shutil
from pathlib import Path

from helpers import (CaseTest, KNOWLEDGE_SENTINEL, MOOD_SENTINEL, PERSONA_SENTINEL, TODAY, VARIANT_SENTINEL,
                     make_case)
from test_cli import run_main
from rehearse import board
from rehearse.backends import MockBackend
from rehearse.case import CaseError, load_case, parse_continuation
from rehearse.checks import Run
from rehearse.judge import build_request, judge_run
from rehearse.runs import execute_run

PURPOSE_SENTINEL = "PURPOSE-SENTINEL-2e7a"
EXPECT_SENTINEL = "EXPECT-SENTINEL-93cd"
CONTEXT_SENTINEL = "CONTEXT-SENTINEL-1b60"

CONT = f"""\
---
purpose: "{PURPOSE_SENTINEL} test one step"
expect:
  - "{EXPECT_SENTINEL} asks one question"
  - gives no score
---
## Person (turn 0)

Start the interview.

## Subject (turn 1)

How would you welcome a customer?

## Person (turn 1)

I would say hello.
"""
RUBRIC = "1. Was it polite?\n   Pass: yes\n"
GOOD = {"answers": [{"id": 1, "verdict": "pass", "quote": "Which part first?"},
                    {"id": "e1", "verdict": "pass", "quote": "Which part first?"},
                    {"id": "e2", "verdict": "fail", "quote": ""}],
        "sim": {"in_character": "fail", "kept_up_mood": "pass", "used_only_knowledge": "pass",
                "quote": "Start the interview."}}


def cont_case(root: Path, conts=None, subject_toml="", top="", tail="", **kw) -> Path:
    """A case with a `continuations` folder (id -> file text), extra [subject] lines and extra top-level lines."""
    path = make_case(root, **kw)
    (root / "continuations").mkdir(exist_ok=True)
    for cid, text in (conts if conts is not None else {"welcome": CONT}).items():
        (root / "continuations" / f"{cid}.md").write_text(text)
    text = path.read_text().replace('adapter = "chat"\n', 'adapter = "chat"\n' + subject_toml, 1)
    head, rest = text.split("\n", 1)
    path.write_text(head + "\n" + top + rest + "\n" + tail)
    return path


class FormatTests(CaseTest):
    def parse(self, text):
        return parse_continuation(text, "c", Path("c.md"))

    def err(self, text):
        with self.assertRaises(CaseError) as cm:
            self.parse(text)
        return str(cm.exception)

    def test_parses_all_fields(self):
        c = self.parse(CONT.replace("---\n##", "variant: baseline\nsim: dana\nmax_turns: 3\n---\n##"))
        self.assertEqual([s for s, _ in c.turns], ["person", "subject", "person"])
        self.assertEqual(c.turns[1][1], "How would you welcome a customer?")
        self.assertEqual(len(c.expect), 2)
        self.assertEqual((c.variant, c.sim, c.max_turns), ("baseline", "dana", 3))

    def test_turn_numbers_and_headings_are_optional(self):
        c = self.parse(CONT.replace(" (turn 0)", "").replace(" (turn 1)", ""))
        self.assertEqual(len(c.turns), 3)

    def test_other_headings_stay_in_the_text(self):
        c = self.parse(CONT.replace("I would say hello.", "## Plan\n\nI would say hello."))
        self.assertIn("## Plan", c.turns[2][1])

    def test_errors(self):
        self.assertIn("first section must be", self.err(CONT.replace("## Person (turn 0)", "## Subject (turn 0)")))
        self.assertIn("last section must be", self.err(CONT + "\n## Subject (turn 2)\n\nHello\n"))
        self.assertIn("both from the person", self.err(CONT + "\n## Person\n\nAgain\n"))
        self.assertIn("is empty", self.err(CONT.replace("I would say hello.", "")))
        self.assertIn("needs a 'purpose'", self.err(CONT.replace(f'purpose: "{PURPOSE_SENTINEL} test one step"\n', "")))
        self.assertIn("needs 'expect'", self.err(CONT.replace("expect:", "variant:")))
        self.assertIn("unknown setting", self.err(CONT.replace("---\n##", "colour: red\n---\n##")))
        self.assertIn("text before the first", self.err("---\npurpose: p\nexpect:\n  - e\n---\nJust text\n"))
        self.assertIn("no '## Person'", self.err("---\npurpose: p\nexpect:\n  - e\n---\n"))
        self.assertIn("max_turns needs a 'sim'", self.err(CONT.replace("---\n##", "max_turns: 2\n---\n##")))
        self.assertIn("whole number", self.err(CONT.replace("---\n##", "sim: dana\nmax_turns: x\n---\n##")))

    def test_case_errors_name_unknown_variant_and_sim(self):
        cont_case(self.root, {"c": CONT.replace("---\n##", "variant: nope\n---\n##")})
        with self.assertRaisesRegex(CaseError, "unknown variant 'nope'"):
            load_case(self.root)
        cont_case(self.root, {"c": CONT.replace("---\n##", "sim: nobody\n---\n##")})
        with self.assertRaisesRegex(CaseError, "unknown sim 'nobody'"):
            load_case(self.root)

    def test_default_folder_and_explicit_setting(self):
        cont_case(self.root)
        self.assertEqual(list(load_case(self.root).continuations), ["welcome"])
        shutil.move(self.root / "continuations", self.root / "cont2")
        self.assertEqual(load_case(self.root).continuations, {})
        cont_case(self.root, top='continuations = "missing"\n')
        with self.assertRaisesRegex(CaseError, "folder not found"):
            load_case(self.root)


class RunTests(CaseTest):
    def go(self, subject_replies=("Which part first?",), sim_replies=(), conts=None, variant="baseline", **kw):
        path = cont_case(self.root, conts, subject_replies=subject_replies, sim_replies=sim_replies, **kw)
        case = load_case(path)
        subj, simb = MockBackend(list(subject_replies), default="Go on."), MockBackend(list(sim_replies), default="Okay.")
        res = execute_run(case, variant, None, subj, simb, today=TODAY, continuation="welcome")
        return res, subj, simb, case

    def test_messages_and_one_reply(self):
        res, subj, simb, _ = self.go()
        system, messages = subj.calls[0]
        self.assertEqual([(m.role, m.content) for m in messages],
                         [("user", "Start the interview."), ("assistant", "How would you welcome a customer?"),
                          ("user", "I would say hello.")])
        self.assertEqual(len(subj.calls), 1)
        self.assertEqual(simb.calls, [])
        self.assertEqual(res.stop, "continuation-done")
        meta = json.loads((res.path / "meta.json").read_text())
        self.assertEqual((meta["continuation"], meta["prefilled_turns"], meta["prefill_mode"], meta["sim"]),
                         ("welcome", 3, "messages", "cont:welcome"))
        self.assertEqual(len(meta["prefill_sha256"]), 64)
        self.assertIs(meta["sim_used"], False)
        self.assertTrue(res.path.name.startswith("2026-10-26-tcase-baseline-cont-welcome-"))
        rows = [json.loads(l) for l in (res.path / "turns.jsonl").read_text().splitlines()]
        self.assertEqual([bool(r.get("prefilled")) for r in rows], [True, True, True, False])
        self.assertEqual((res.path / "inputs" / "continuation.md").read_text(), CONT)
        self.assertIn("prefilled", (res.path / "transcript.md").read_text())
        run = Run.load(res.path)
        self.assertEqual(run.new_subject_turns, ["Which part first?"])
        self.assertEqual(len(run.subject_turns), 2)

    def test_purpose_and_expect_never_reach_a_seat(self):
        res, subj, simb, _ = self.go(sim_replies=["Fine.", "DONE"],
                                     conts={"welcome": CONT.replace("---\n##", "sim: dana\n---\n##")})
        seen = [system + "".join(m.content for m in msgs) for system, msgs in subj.calls + simb.calls]
        self.assertTrue(simb.calls)
        for text in seen:
            for sentinel in (PURPOSE_SENTINEL, EXPECT_SENTINEL, "gives no score"):
                self.assertNotIn(sentinel, text)
        raw = "".join(p.read_text() for p in (res.path / "raw").glob("*.json"))
        self.assertNotIn(PURPOSE_SENTINEL, raw)

    def test_sim_continues_after_the_prefill(self):
        res, subj, simb, _ = self.go(subject_replies=["s-new1", "s-new2", "s-new3"], sim_replies=["p-new1", "DONE"],
                                     conts={"welcome": CONT.replace("---\n##", "sim: dana\nmax_turns: 5\n---\n##")})
        self.assertEqual(res.stop, "sim-stop")
        self.assertEqual(res.subject_turns, 2)           # new replies only
        self.assertEqual([m.content for m in subj.calls[1][1]][-3:], ["I would say hello.", "s-new1", "p-new1"])
        sim_msgs = simb.calls[0][1]
        self.assertEqual([(m.role, m.content) for m in sim_msgs[:3]],
                         [("user", "How would you welcome a customer?"), ("assistant", "I would say hello."),
                          ("user", "s-new1")])
        self.assertIn("You started the chat by typing: Start the interview.", simb.calls[0][0])
        run = Run.load(res.path, None)
        self.assertEqual(json.loads((res.path / "meta.json").read_text())["persona"], "dana")
        self.assertTrue((res.path / "inputs" / "persona.md").is_file())
        self.assertEqual(run.new_subject_turns, ["s-new1", "s-new2"])

    def test_max_turns_counts_new_replies_only(self):
        res, *_ = self.go(subject_replies=["a", "b", "c"], sim_replies=["x", "y"],
                          conts={"welcome": CONT.replace("---\n##", "sim: dana\nmax_turns: 2\n---\n##")})
        self.assertEqual((res.stop, res.subject_turns), ("turn-cap", 2))

    def test_sim_check_skips_without_sim_turns(self):
        res, *_ = self.go()
        checks = json.loads((res.path / "checks.json").read_text())
        self.assertEqual(checks["sim_check"]["result"], "skip")
        self.assertIn("no sim turns: a focused continuation", checks["sim_check"]["items"][0]["evidence"])
        self.assertFalse(checks["void"])

    def test_sim_check_ignores_prefilled_person_turns(self):
        conts = {"welcome": CONT.replace("I would say hello.", "I am an AI language model.")
                 .replace("---\n##", "sim: dana\n---\n##")}
        res, *_ = self.go(sim_replies=["Fine thanks.", "DONE"], conts=conts, subject_replies=["a", "b"])
        checks = json.loads((res.path / "checks.json").read_text())
        self.assertEqual(checks["sim_check"]["result"], "pass")

    def test_case_checks_still_run(self):
        res, *_ = self.go(checks_py="from rehearse.checks import Check\n"
                                    "def asked(run):\n    return Check.passed(run.new_subject_turns[0])\n")
        checks = json.loads((res.path / "checks.json").read_text())
        self.assertEqual(checks["results"][0]["evidence"], "Which part first?")

    def test_context_is_appended_and_snapshotted(self):
        path = cont_case(self.root, subject_toml='context = "context.md"\n')
        (self.root / "context.md").write_text(f"{CONTEXT_SENTINEL} public profile\n")
        case = load_case(path)
        subj = MockBackend(["ok"])
        res = execute_run(case, "baseline", None, subj, MockBackend(), today=TODAY, continuation="welcome")
        system = subj.calls[0][0]
        self.assertTrue(system.startswith(VARIANT_SENTINEL))
        self.assertIn("## Context\n\n" + CONTEXT_SENTINEL, system)
        self.assertEqual((res.path / "inputs" / "context.md").read_text(), f"{CONTEXT_SENTINEL} public profile\n")
        self.assertNotIn(CONTEXT_SENTINEL, (res.path / "inputs" / "variant.md").read_text())

    def test_context_needs_an_existing_file_and_a_chat_subject(self):
        cont_case(self.root, subject_toml='context = "nope.md"\n')
        with self.assertRaisesRegex(CaseError, "context file not found"):
            load_case(self.root)

    def test_max_prompt_chars_refusal(self):
        cont_case(self.root, subject_toml="max_prompt_chars = 20\n")
        code, out, err = run_main(["suite", str(self.root), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("over the limit of 20", err)
        code, out, err = run_main(["continue", str(self.root), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("over the limit of 20", err)
        with self.assertRaises(CaseError):
            execute_run(load_case(self.root), "baseline", None, MockBackend(), MockBackend(), continuation="welcome")

    def test_max_prompt_chars_allows_a_short_variant(self):
        cont_case(self.root, subject_toml="max_prompt_chars = 500\n")
        code, out, err = run_main(["continue", str(self.root), "--dry-run"])
        self.assertEqual(code, 0, err)


class CommandTests(CaseTest):
    def test_dry_run_lists_continuations(self):
        cont_case(self.root, {"welcome": CONT, "second": CONT.replace("---\n##", "variant: cheerful\n---\n##")})
        code, out, err = run_main(["continue", str(self.root), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("3 runs", out)
        self.assertIn("baseline x cont:welcome", out)
        self.assertIn("cheerful x cont:second", out)
        self.assertNotIn("baseline x cont:second", out)
        self.assertFalse((self.root / "runs").exists())
        code, out, err = run_main(["continue", str(self.root), "--dry-run", "--only", "second", "--repeats", "2"])
        self.assertIn("2 runs", out)

    def test_continue_runs_on_mock_and_board_groups_by_cont(self):
        cont_case(self.root, subject_replies=["Which part first?"])
        code, out, err = run_main(["continue", str(self.root), "--variants", "baseline"])
        self.assertEqual(code, 0, err)
        self.assertIn("stop: continuation-done", out)
        text = board.board([str(self.root / "runs")])
        self.assertIn("| baseline | cont:welcome |", text)
        self.assertIn("sim seat: none", text)
        self.assertNotIn("WARNING", text)

    def test_unknown_only_and_no_continuations(self):
        cont_case(self.root)
        code, out, err = run_main(["continue", str(self.root), "--only", "zzz", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("unknown continuation 'zzz'", err)
        shutil.rmtree(self.root / "continuations")
        code, out, err = run_main(["continue", str(self.root), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("no continuations", err)


GOOD = {"answers": [{"id": 1, "verdict": "pass", "quote": "Which part first?"},
                    {"id": "e1", "verdict": "pass", "quote": "Which part first?"},
                    {"id": "e2", "verdict": "fail", "quote": "Which part first?"}],
        "sim": {"in_character": "fail", "kept_up_mood": "pass", "used_only_knowledge": "pass",
                "quote": "Start the interview."}}
E_ONLY = {"answers": GOOD["answers"][1:], "sim": GOOD["sim"]}


class JudgeTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.build()

    def build(self, conts=None, judge_replies=()):
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        path = cont_case(self.root, conts, top='rubric = "rubric.md"\n', tail='[judge]\nseat = "mock"\n',
                         subject_replies=["Which part first?"], sim_replies=["Fine.", "DONE"])
        (self.root / "rubric.md").write_text(RUBRIC)
        text = path.read_text().replace("[mock]\n", f"[mock]\njudge = {json.dumps(list(judge_replies))}\n", 1)
        path.write_text(text)
        self.case = load_case(self.root)
        self.res = execute_run(self.case, "baseline", None, MockBackend(["Which part first?", "x", "y"]),
                               MockBackend(["Fine.", "DONE"]), today=TODAY, continuation="welcome")

    def request(self):
        from rehearse.judge import rubric_for_run
        run = Run.load(self.res.path, self.case)
        return build_request(run, rubric_for_run(self.case, self.res.path), None)

    def test_one_reply_continuation_asks_only_its_expectations(self):
        system, user, evidence = self.request()
        self.assertNotIn("Was it polite?", user)
        self.assertIn(f"e1. {EXPECT_SENTINEL} asks one question", user)
        self.assertIn("e2. gives no score", user)
        self.assertIn(PURPOSE_SENTINEL, user)
        self.assertIn("written in advance", user)

    def test_rubric_true_adds_the_case_questions_and_sim_defaults_to_true(self):
        self.build({"welcome": CONT.replace("---\n##", "rubric: true\n---\n##")})
        self.assertIn("1. Was it polite?", self.request()[1])
        self.build({"welcome": CONT.replace("---\n##", "sim: dana\n---\n##")})
        self.assertIn("1. Was it polite?", self.request()[1])
        self.build({"welcome": CONT.replace("---\n##", "sim: dana\nrubric: false\n---\n##")})
        self.assertNotIn("Was it polite?", self.request()[1])
        with self.assertRaisesRegex(CaseError, "rubric' must be true or false"):
            parse_continuation(CONT.replace("---\n##", "rubric: maybe\n---\n##"), "c", Path("c.md"))

    def test_judge_never_sees_variant_persona_knowledge_or_context(self):
        (self.root / "context.md").write_text(CONTEXT_SENTINEL + "\n")
        text = (self.root / "case.toml").read_text().replace('adapter = "chat"\n', 'adapter = "chat"\ncontext = "context.md"\n', 1)
        (self.root / "case.toml").write_text(text)
        self.case = load_case(self.root)
        self.res = execute_run(self.case, "baseline", None, MockBackend(["Which part first?"]), MockBackend(),
                               today=TODAY, continuation="welcome")
        system, user, evidence = self.request()
        for sentinel in (VARIANT_SENTINEL, KNOWLEDGE_SENTINEL, PERSONA_SENTINEL, CONTEXT_SENTINEL):
            self.assertNotIn(sentinel, system + user)

    def test_judge_board_columns_blank_where_not_asked(self):
        data = judge_run(self.res.path, self.case, MockBackend([json.dumps(E_ONLY)]))
        self.assertEqual([(a["id"], a["verdict"]) for a in data["answers"]], [("e1", "pass"), ("e2", "fail")])
        self.assertFalse(data["void"])        # the judge's sim part cannot void a run with no sim turns
        text = board.board([str(self.root / "runs")])
        main_header = next(l for l in text.splitlines() if l.startswith("| variant | sim |"))
        self.assertNotIn("judge e1", main_header)
        self.assertIn("### Expectations of cont:welcome", text)
        self.assertIn("| variant | judge e1 | judge e2 |", text)
        self.assertIn("| baseline | 1 of 1 | 0 of 1 |", text)      # a real fail with a found quote
        self.assertNotIn("unclear", text.split("### Expectations")[1].split("Read these first")[0].replace("`+c unclear`", ""))

    def test_run_not_asked_a_judge_id_counts_nothing(self):
        from rehearse.board import load_records, tally, cell_for
        self.res2 = execute_run(self.case, "baseline", "dana", MockBackend(["a", "b"]), MockBackend(["x", "DONE"]),
                                today=TODAY)
        judge_run(self.res.path, self.case, MockBackend([json.dumps(E_ONLY)]))
        judge_run(self.res2.path, self.case, MockBackend([json.dumps({"answers": [
            {"id": 1, "verdict": "pass", "quote": "a"}], "sim": GOOD["sim"]})]))
        recs = load_records([str(self.root / "runs")])
        cont = [r for r in recs if r.sim == "cont:welcome"]
        normal = [r for r in recs if r.sim == "dana"]
        t = tally(normal, "judge e1")
        self.assertEqual((t["n"], t["unclear"]), (0, 0))
        self.assertEqual(cell_for(normal, "judge e1"), "")
        self.assertEqual(cell_for(cont, "judge 1"), "")
        self.assertEqual(cell_for(normal, "judge 1"), "1 of 1")

    def test_judge_works_without_a_case_rubric(self):
        text = (self.root / "case.toml").read_text().replace('rubric = "rubric.md"\n', "")
        (self.root / "case.toml").write_text(text)
        case = load_case(self.root)
        data = judge_run(self.res.path, case, MockBackend([json.dumps(E_ONLY)]))
        self.assertEqual([a["id"] for a in data["answers"]], ["e1", "e2"])

    def test_continue_with_judge_flag_on_mock(self):
        self.build(judge_replies=[json.dumps(E_ONLY)])
        code, out, err = run_main(["continue", str(self.root), "--judge", "--variants", "baseline"])
        self.assertEqual(code, 0, err)
        self.assertIn("judge:", out)
        runs = sorted(p for p in (self.root / "runs").iterdir() if (p / "judge.json").is_file())
        self.assertEqual(len(runs), 1)
        data = json.loads((runs[0] / "judge.json").read_text())
        self.assertEqual([a["id"] for a in data["answers"]], ["e1", "e2"])
        self.assertEqual(data["answers"][1]["verdict"], "fail")
        self.assertEqual(data["answers"][0]["question"].split()[0], EXPECT_SENTINEL)


class SimTests(CaseTest):
    def test_context_never_reaches_the_sim(self):
        path = cont_case(self.root, {"welcome": CONT.replace("---\n##", "sim: dana\n---\n##")},
                         subject_toml='context = "context.md"\n', sim_replies=["Fine.", "DONE"],
                         subject_replies=["a", "b"])
        (self.root / "context.md").write_text(CONTEXT_SENTINEL + "\n")
        case = load_case(path)
        simb = MockBackend(["Fine.", "DONE"])
        execute_run(case, "baseline", None, MockBackend(["a", "b"]), simb, today=TODAY, continuation="welcome")
        self.assertTrue(simb.calls)
        for system, msgs in simb.calls:
            self.assertNotIn(CONTEXT_SENTINEL, system + "".join(m.content for m in msgs))

    def test_escalation_counts_generated_sim_replies_after_a_prefill(self):
        path = cont_case(self.root, {"welcome": CONT.replace("---\n##", "sim: dana\nmax_turns: 3\n---\n##")},
                         escalation=["LINE-ONE", "LINE-TWO"], subject_replies=["a", "b", "c"],
                         sim_replies=["x", "y"])
        case = load_case(path)
        simb = MockBackend(["x", "y"])
        execute_run(case, "baseline", None, MockBackend(["a", "b", "c"]), simb, today=TODAY, continuation="welcome")
        self.assertIn("LINE-ONE", simb.calls[0][0])
        self.assertNotIn("LINE-TWO", simb.calls[0][0])
        self.assertIn("LINE-TWO", simb.calls[1][0])

    def test_escalation_unchanged_for_an_ordinary_run(self):
        out, subj, sim, _ = self.converse(["a", "b", "c"], ["x", "y"], escalation=["LINE-ONE", "LINE-TWO"], max_turns=3)
        self.assertIn("LINE-ONE", sim.calls[0][0])
        self.assertIn("LINE-TWO", sim.calls[1][0])

    def test_max_prompt_chars_needs_a_chat_subject(self):
        from helpers import make_agent_case
        path = make_agent_case(self.root)
        text = path.read_text().replace('adapter = "agent"\n', 'adapter = "agent"\nmax_prompt_chars = 100\n', 1)
        path.write_text(text)
        with self.assertRaisesRegex(CaseError, "max_prompt_chars needs a chat subject"):
            load_case(path)
