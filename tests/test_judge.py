import json
import shutil
import sys
import io
from pathlib import Path
from unittest import mock

from helpers import CaseTest, KNOWLEDGE_SENTINEL, MOOD_SENTINEL, VARIANT_SENTINEL, build_runs, make_agent_case, make_case
from test_cli import run_main
from rehearse import board, compare
from rehearse.backends import BackendError, MockBackend, QuotaError, SeatResult
from rehearse.case import CaseError, load_case
from rehearse.judge import judge_run, parse_rubric

KEY_SENTINEL = "ANSWER-KEY-SENTINEL-9b1"
RUBRIC = """\
---
answer_key: {key}
---
# Review

1. Did it ask what the person needs?
   Pass: it asks an open question
   Fail: it assumes
2. Did it write the plan?
Pass: a plan file exists
"""
GOOD = {"answers": [{"id": 1, "verdict": "pass", "quote": "What do you need?"},
                    {"id": 2, "verdict": "fail", "quote": ""}],
        "sim": {"in_character": "pass", "kept_up_mood": "pass", "used_only_knowledge": "pass", "quote": "A thing."}}


def js(d):
    return json.dumps(d)


def sim(**kw):
    return {**GOOD["sim"], **kw}


class JudgeBase(CaseTest):
    def setUp(self):
        super().setUp()
        self.build()

    def build(self, key="false", judge='[judge]\nseat = "mock"\n', **kw):
        shutil.rmtree(self.root / "runs", ignore_errors=True)
        build_runs(self.root, **kw)
        text = (self.root / "case.toml").read_text()
        (self.root / "rubric.md").write_text(RUBRIC.format(key=key))
        (self.root / "key.md").write_text(KEY_SENTINEL + "\n")
        head, rest = text.split("\n", 1)
        (self.root / "case.toml").write_text(
            head + '\nrubric = "rubric.md"\nanswer_key = "key.md"\n' + rest + "\n" + judge)
        self.case = load_case(self.root)
        self.run_dir = next(self.case.runs_dir.iterdir())

    def judge(self, *replies, **kw):
        b = MockBackend(list(replies))
        return judge_run(self.run_dir, self.case, b, **kw), b


class RubricTests(CaseTest):
    def test_parse(self):
        r = parse_rubric(RUBRIC.format(key="true"))
        self.assertTrue(r["answer_key"])
        self.assertTrue(r["artifacts"])
        self.assertEqual(r["questions"][0], {"id": 1, "question": "Did it ask what the person needs?",
                                             "pass": "it asks an open question", "fail": "it assumes"})
        self.assertEqual(r["questions"][1]["pass"], "a plan file exists")

    def test_defaults_and_flags(self):
        r = parse_rubric("---\nartifacts: false\n---\n1. Q\n")
        self.assertEqual((r["answer_key"], r["artifacts"]), (False, False))

    def test_malformed(self):
        for text in ("", "just words\n1. Q", "2. Q", "1. Q\n3. R", "---\nanswer_key: maybe\n---\n1. Q",
                     "Pass: x\n1. Q"):
            with self.assertRaises(CaseError, msg=text):
                parse_rubric(text)

    def test_case_checks_files(self):
        build_runs(self.root)
        toml = self.root / "case.toml"
        orig = toml.read_text()
        head, rest = orig.split("\n", 1)
        toml.write_text(head + '\nrubric = "nope.md"\n' + rest)
        with self.assertRaises(CaseError):
            load_case(self.root)
        (self.root / "rubric.md").write_text(RUBRIC.format(key="true"))
        toml.write_text(head + '\nrubric = "rubric.md"\n' + rest)
        with self.assertRaises(CaseError):
            load_case(self.root)            # asks for the key, case has none


class StoppedOnCueTests(JudgeBase):
    def test_reported_and_never_voids(self):
        data, b = self.judge(js({**GOOD, "sim": sim(stopped_on_cue="fail")}))
        self.assertEqual(data["sim"]["stopped_on_cue"], "fail")
        self.assertFalse(data["void"])
        self.assertEqual(json.loads((self.run_dir / "judge.json").read_text())["sim"]["stopped_on_cue"], "fail")
        self.assertIn("stopped_on_cue", b.calls[0][0])
        self.assertIn("The run ended because: sim-stop", b.calls[0][1][0].content)

    def test_values(self):
        self.assertEqual(self.judge(js({**GOOD, "sim": sim(stopped_on_cue="n/a")}))[0]["sim"]["stopped_on_cue"], "n/a")
        self.assertEqual(self.judge(js(GOOD))[0]["sim"]["stopped_on_cue"], "unclear")       # not given
        self.assertEqual(self.judge(js({**GOOD, "sim": sim(stopped_on_cue="maybe")}))[0]["sim"]["stopped_on_cue"], "unclear")


class JudgeTests(JudgeBase):
    def test_request_has_transcript_not_key_or_variant(self):
        data, b = self.judge(js(GOOD))
        system, msgs = b.calls[0]
        user = msgs[0].content
        self.assertIn("What do you need?", user)
        self.assertIn("A thing.", user)
        self.assertIn(KNOWLEDGE_SENTINEL, user)
        self.assertIn(MOOD_SENTINEL, user)
        self.assertNotIn(KEY_SENTINEL, user + system)
        self.assertNotIn(VARIANT_SENTINEL, user + system)
        self.assertIn("JSON", system)
        self.assertEqual([a["verdict"] for a in data["answers"]], ["pass", "fail"] if False else ["pass", "unclear"])
        self.assertTrue(data["answers"][1]["quote_not_found"])
        self.assertTrue((self.run_dir / "raw" / "judge-01.json").is_file())
        self.assertEqual(json.loads((self.run_dir / "judge.json").read_text())["rubric_sha256"], data["rubric_sha256"])

    def test_answer_key_when_asked(self):
        self.build(key="true")
        _, b = self.judge(js(GOOD))
        self.assertIn(KEY_SENTINEL, b.calls[0][1][0].content)
        self.assertNotIn(VARIANT_SENTINEL, b.calls[0][1][0].content)

    def test_artifact_shown(self):
        d = self.root / "x"
        d.mkdir()
        make_agent_case(d)
        from rehearse.runs import execute_run
        case = load_case(d)
        res = execute_run(case, "baseline", "dana", MockBackend(case.mock_subject), MockBackend(case.mock_sim))
        (d / "rubric.md").write_text("1. Q\n")
        text = (d / "case.toml").read_text()
        (d / "case.toml").write_text('rubric = "rubric.md"\n' + text + '\n[judge]\nseat = "mock"\n')
        case = load_case(d)
        b = MockBackend([js({"answers": [{"id": 1, "verdict": "pass", "quote": "# Review"}], "sim": GOOD["sim"]})])
        data = judge_run(res.path, case, b)
        self.assertIn("# Review", b.calls[0][1][0].content)
        self.assertEqual(data["answers"][0]["verdict"], "pass")

    def test_fenced_json(self):
        data, _ = self.judge("Here:\n```json\n" + js(GOOD) + "\n```")
        self.assertIsNone(data["error"])
        self.assertEqual(data["answers"][0]["verdict"], "pass")

    def test_retry_succeeds(self):
        data, b = self.judge("not json", js(GOOD))
        self.assertIsNone(data["error"])
        self.assertEqual(len(b.calls), 2)
        self.assertIn("Reply with only the JSON object", b.calls[1][1][-1].content)
        self.assertTrue((self.run_dir / "raw" / "judge-02.json").is_file())

    def test_garbage_twice_records_error(self):
        data, b = self.judge("nope", "still nope")
        self.assertTrue(data["error"])
        self.assertEqual({a["verdict"] for a in data["answers"]}, {"unclear"})
        self.assertFalse(data["void"])
        self.assertEqual(json.loads((self.run_dir / "judge.json").read_text())["error"], data["error"])

    def test_backend_error_recorded_quota_raised(self):
        data, _ = self.judge(BackendError("boom"))
        self.assertIn("boom", data["error"])
        with self.assertRaises(QuotaError):
            self.judge(QuotaError("out"))

    def test_quote_not_found_and_missing_id(self):
        bad = {"answers": [{"id": 1, "verdict": "pass", "quote": "words nobody said"}], "sim": GOOD["sim"]}
        data, _ = self.judge(js(bad))
        self.assertEqual([a["verdict"] for a in data["answers"]], ["unclear", "unclear"])
        self.assertTrue(data["answers"][0]["quote_not_found"])
        self.assertFalse(data["answers"][1]["quote_not_found"])

    def test_quote_normalised(self):
        ok = {"answers": [{"id": 1, "verdict": "pass", "quote": "WHAT  do\nyou need?"},
                          {"id": 2, "verdict": "unclear", "quote": ""}], "sim": GOOD["sim"]}
        data, _ = self.judge(js(ok))
        self.assertEqual(data["answers"][0]["verdict"], "pass")

    def test_sim_fail_voids(self):
        data, _ = self.judge(js({**GOOD, "sim": sim(in_character="fail")}))
        self.assertTrue(data["void"])
        self.assertIn("in_character", data["void_reason"])
        text = board.board(str(self.case.runs_dir))
        self.assertIn("+1 void", text)

    def test_sim_fail_unsupported_quote_does_not_void(self):
        data, _ = self.judge(js({**GOOD, "sim": sim(used_only_knowledge="fail", quote="invented")}))
        self.assertFalse(data["void"])

    def test_history(self):
        self.judge(js(GOOD))
        self.judge(js(GOOD))
        self.assertEqual(len(list((self.run_dir / "history").glob("judge-*.json"))), 1)
        self.assertEqual(len(list((self.run_dir / "raw").glob("judge-*.json"))), 2)


class BoardTests(JudgeBase):
    def test_columns_header_unjudged(self):
        self.build(n=2)
        runs = sorted(self.case.runs_dir.iterdir())
        judge_run(runs[0], self.case, MockBackend([js(GOOD)]))
        text = board.board(str(self.case.runs_dir))
        self.assertIn("Judge results are one model's opinion (directional). Judge: mock", text)
        row = next(l for l in text.splitlines() if l.startswith("| baseline"))
        self.assertIn("1 of 1 +1 unjudged", row)        # judge 1
        self.assertIn("+1 unclear +1 unjudged", row)     # judge 2 (quote missing)
        self.assertIn("| judge 1 |", text)

    def test_no_judge_no_columns(self):
        text = board.board(str(self.case.runs_dir))
        self.assertNotIn("Judge results", text)
        self.assertNotIn("| judge", text)
        self.assertNotIn("unjudged", text.replace("`+u unjudged`", ""))

    def test_compare_has_judge_columns(self):
        judge_run(self.run_dir, self.case, MockBackend([js(GOOD)]))
        text = compare.compare(str(self.case.runs_dir), str(self.case.runs_dir))
        self.assertIn("| judge 1 |", text)


class CliTests(JudgeBase):
    def test_judge_on_mock(self):
        self.build(judge='[judge]\nseat = "mock"\n')
        text = (self.root / "case.toml").read_text()
        (self.root / "case.toml").write_text(text.replace("[mock]", f"[mock]\njudge = [{json.dumps(js(GOOD))}]"))
        code, out, _ = run_main(["judge", str(self.case.runs_dir)])
        self.assertEqual(code, 0, out)
        self.assertIn("judge: 1 pass, 1 unclear", out)
        self.assertTrue((self.run_dir / "judge.json").is_file())

    def test_dry_run_calls_nothing(self):
        code, out, _ = run_main(["judge", str(self.case.runs_dir), "--dry-run", "--seat", "claude:sonnet"])
        self.assertEqual(code, 0)
        self.assertIn("no past judge runs", out)
        self.assertFalse((self.run_dir / "judge.json").exists())

    def test_live_refused_without_tty(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            code, _, err = run_main(["judge", str(self.case.runs_dir), "--seat", "claude:sonnet"])
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertFalse((self.run_dir / "judge.json").exists())

    def test_max_usd_stops(self):
        self.build(n=3)
        replies = [SeatResult(js(GOOD), cost_usd=0.5, model="m")] * 3
        fake = MockBackend(replies)
        with mock.patch("rehearse.judge.make_backend", return_value=fake):
            code, out, _ = run_main(["judge", str(self.case.runs_dir), "--seat", "claude:sonnet",
                                     "--yes", "--max-usd", "0.4"])
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("budget", out)
        self.assertIn("spent: ", out)

    def test_run_with_judge_flag(self):
        self.build()
        text = (self.root / "case.toml").read_text()
        (self.root / "case.toml").write_text(text.replace("[mock]", f"[mock]\njudge = [{json.dumps(js(GOOD))}]"))
        code, out, _ = run_main(["run", str(self.root), "--variant", "baseline", "--sim", "dana", "--judge"])
        self.assertEqual(code, 0, out)
        self.assertIn("judge: ", out)
        code, out, _ = run_main(["run", str(self.root), "--variant", "baseline", "--sim", "dana", "--no-judge"])
        self.assertNotIn("judge: ", out)
