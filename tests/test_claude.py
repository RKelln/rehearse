import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeClaude, chat_json, stream
from helpers import CaseTest
from rehearse.backends import BackendError, Msg, QuotaError, SetupError, make_backend
from rehearse.backends.claude import flatten


def argv_has(argv, flag, value=None):
    """Is `flag` in argv, and (if given) is it immediately followed by `value`?"""
    if flag not in argv:
        return False
    return value is None or argv[argv.index(flag) + 1] == value


class ClaudeChatTests(CaseTest):
    def backend(self, steps, spec="claude:sonnet"):
        fake = FakeClaude(self.root / "fake", steps)
        self.addCleanup(fake.__exit__)
        fake.__enter__()
        return make_backend(spec), fake

    def test_sim_call_is_isolated(self):
        b, fake = self.backend([{"stdout": chat_json("hi there")}])
        res = b.chat("SYSTEM TEXT", [Msg("user", "hello")])
        (call,) = fake.calls
        a = call["argv"]
        self.assertIn("-p", a)
        self.assertTrue(argv_has(a, "--output-format", "json"))
        self.assertTrue(argv_has(a, "--model", "sonnet"))
        self.assertNotIn("--system-prompt", a)                         # not on the command line, where ps shows it
        self.assertNotIn("SYSTEM TEXT", " ".join(a))
        sf = call["system_file"]
        self.assertEqual((sf["content"], sf["mode"], sf["dir_mode"]), ("SYSTEM TEXT", 0o600, 0o700))
        self.assertNotEqual(os.path.dirname(sf["path"]), call["cwd"])    # not in the seat's working folder
        self.assertFalse(os.path.exists(sf["path"]))                    # removed after the call
        self.assertTrue(argv_has(a, "--tools", ""))
        self.assertTrue(argv_has(a, "--setting-sources", ""))
        self.assertIn("--strict-mcp-config", a)
        self.assertIn("--no-session-persistence", a)
        self.assertNotIn("--bare", a)
        self.assertEqual(call["stdin"], "hello")
        self.assertEqual(call["listing"], [])                        # an empty folder
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())
        self.assertEqual((res.text, res.cost_usd, res.model), ("hi there", 0.01, "claude-test"))
        self.assertEqual((res.tokens_in, res.tokens_out), (10, 5))

    def test_bare_with_api_key(self):
        b, fake = self.backend([{"stdout": chat_json()}])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            b.chat("s", [Msg("user", "hello")])
        self.assertIn("--bare", fake.calls[0]["argv"])

    def test_conversation_is_flattened(self):
        text = flatten([Msg("user", "a"), Msg("assistant", "b"), Msg("user", "c")])
        self.assertEqual(text, "The conversation so far:\n\nTHEM: a\n\nYOU: b\n\nTHEM: c\n\n"
                               "Write only your next message. No label, no narration.")
        self.assertEqual(flatten([Msg("user", "solo")]), "solo")

    def test_cwd_removed_when_backend_is_gone(self):
        b, _ = self.backend([{"stdout": chat_json()}])
        cwd = b._cwd
        self.assertTrue(os.path.isdir(cwd))
        del b
        import gc
        gc.collect()
        self.assertFalse(os.path.isdir(cwd))

    def test_budget_flag(self):
        b, fake = self.backend([{"stdout": chat_json()}])
        b.max_budget_usd = 0.5
        b.chat("s", [Msg("user", "x")])
        self.assertTrue(argv_has(fake.calls[0]["argv"], "--max-budget-usd", "0.5"))

    def test_errors(self):
        b, _ = self.backend([{"stdout": chat_json("it broke", is_error=True), "exit": 1}])
        with self.assertRaisesRegex(BackendError, "it broke"):
            b.chat("s", [Msg("user", "x")])
        b, _ = self.backend([{"stdout": "not json at all", "stderr": "trace", "exit": 0}])
        with self.assertRaises(BackendError) as cm:
            b.chat("s", [Msg("user", "x")])
        self.assertEqual(cm.exception.raw["stdout"], "not json at all")     # raw kept for the run folder

    def test_quota_text_is_quota_error(self):
        b, _ = self.backend([{"stdout": "", "stderr": "Claude usage limit reached. Resets 5pm", "exit": 1}])
        with self.assertRaises(QuotaError):
            b.chat("s", [Msg("user", "x")])

    def test_missing_program_is_setup_error(self):
        with mock.patch.dict(os.environ, {"REHEARSE_CLAUDE_BIN": str(self.root / "nope")}):
            with self.assertRaisesRegex(SetupError, "REHEARSE_CLAUDE_BIN"):
                make_backend("claude").chat("s", [Msg("user", "x")])

    def test_describe_and_capabilities(self):
        b = make_backend("claude:opus")
        self.assertEqual(b.describe(), "claude:opus")
        self.assertEqual(b.capabilities, frozenset({"chat", "agent", "resume", "fork"}))


class ClaudeAgentTests(CaseTest):
    def session(self, steps, tools=("Read", "Write(review.md)"), **options):
        fake = FakeClaude(self.root / "fake", steps)
        fake.__enter__()
        self.addCleanup(fake.__exit__)
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        b = make_backend("claude:opus")
        return b.open_session(work, allowed_tools=list(tools), options=options), fake, work

    def test_first_turn_then_resume(self):
        s, fake, work = self.session([{"stdout": stream(sid="S1")}, {"stdout": stream(sid="S1")}])
        r1 = s.send("first message")
        r2 = s.send("second message")
        a1, a2 = fake.calls[0]["argv"], fake.calls[1]["argv"]
        for a in (a1, a2):
            self.assertTrue(argv_has(a, "--output-format", "stream-json"))
            self.assertIn("--verbose", a)
            self.assertTrue(argv_has(a, "--model", "opus"))
            self.assertTrue(argv_has(a, "--setting-sources", "project"))
            self.assertIn("--strict-mcp-config", a)
            i = a.index("--allowedTools")
            self.assertEqual(a[i + 1:], ["Read", "Write(review.md)"])
        self.assertNotIn("--resume", a1)
        self.assertTrue(argv_has(a2, "--resume", "S1"))
        self.assertNotIn("--fork-session", a2)
        self.assertEqual((fake.calls[0]["stdin"], fake.calls[1]["stdin"]), ("first message", "second message"))
        self.assertEqual(Path(fake.calls[0]["cwd"]).resolve(), work.resolve())
        self.assertEqual((r1.session_id, r1.model, r1.text), ("S1", "claude-test", "done"))
        self.assertEqual((r1.tokens_in, r1.tokens_out), (7, 3))

    def test_setting_sources_and_timeout_options(self):
        s, fake, _ = self.session([{"stdout": stream()}], setting_sources="project,user", max_budget_usd=2)
        s.send("x")
        a = fake.calls[0]["argv"]
        self.assertTrue(argv_has(a, "--setting-sources", "project,user"))
        self.assertTrue(argv_has(a, "--max-budget-usd", "2"))

    def test_fork_adds_flag_once(self):
        s, fake, _ = self.session([{"stdout": stream(sid="S1")}, {"stdout": stream(sid="S2")},
                                   {"stdout": stream(sid="S2")}])
        s.send("one")
        child = s.fork()
        child.send("two")
        child.send("three")
        a2, a3 = fake.calls[1]["argv"], fake.calls[2]["argv"]
        self.assertTrue(argv_has(a2, "--resume", "S1"))
        self.assertIn("--fork-session", a2)
        self.assertTrue(argv_has(a3, "--resume", "S2"))
        self.assertNotIn("--fork-session", a3)
        self.assertEqual(s.session_id, "S1")

    def test_tool_calls_parsed_and_paths_shortened(self):
        work = self.root / "work"
        s, fake, _ = self.session([{"stdout": stream(tools=[
            ("Read", {"file_path": f"{work}/notes.md"}),
            ("Bash", {"command": "ls " + "x" * 300}),
            ("Skill", {"skill": "weekly-review", "description": "ignored"}),
            ("Glob", {"nothing": 1})])}])
        r = s.send("go")
        self.assertEqual(r.tool_calls[0], {"name": "Read", "arg": "./notes.md"})
        self.assertEqual(len(r.tool_calls[1]["arg"]), 200)
        self.assertEqual(r.tool_calls[2], {"name": "Skill", "arg": "weekly-review"})
        self.assertEqual(r.tool_calls[3], {"name": "Glob", "arg": None})

    def test_cumulative_cost_becomes_per_turn(self):
        s, fake, _ = self.session([{"stdout": stream(cost=0.10)}, {"stdout": stream(cost=0.25)},
                                   {"stdout": stream(cost=0.05)}])
        r1, r2, r3 = s.send("a"), s.send("b"), s.send("c")
        self.assertAlmostEqual(r1.cost_usd, 0.10)
        self.assertAlmostEqual(r2.cost_usd, 0.15)
        self.assertEqual(r2.extras["cost_basis"], "delta")
        self.assertEqual(r2.extras["cost_total_reported"], 0.25)
        self.assertIsNone(r3.cost_usd)                   # a total that goes down is not believed
        self.assertEqual(r3.extras["cost_basis"], "unknown: total decreased")

    def test_missing_cost_is_none(self):
        out = stream().replace('"total_cost_usd": 0.1, ', "")
        s, _, _ = self.session([{"stdout": out}])
        self.assertIsNone(s.send("x").cost_usd)

    def test_nonzero_exit_with_result_event_is_used(self):
        s, _, _ = self.session([{"stdout": stream("partial but fine"), "stderr": "warning", "exit": 2}])
        r = s.send("x")
        self.assertEqual(r.text, "partial but fine")
        self.assertEqual(r.extras["exit_code"], 2)
        self.assertIsNone(r.error)

    def test_nonzero_exit_without_result_event(self):
        s, _, _ = self.session([{"stdout": "", "stderr": "boom: something went wrong", "exit": 1}])
        with self.assertRaises(BackendError) as cm:
            s.send("x")
        self.assertIn("something went wrong", str(cm.exception))
        self.assertEqual(cm.exception.raw["returncode"], 1)
        self.assertNotIsInstance(cm.exception, QuotaError)

    def test_garbled_lines_are_skipped_and_raw_kept(self):
        s, _, _ = self.session([{"stdout": "garbage\n" + stream("fine")}])
        r = s.send("x")
        self.assertEqual(r.text, "fine")
        self.assertIn("garbage", r.raw["stdout"])

    def test_is_error_result_sets_error(self):
        s, _, _ = self.session([{"stdout": stream("the agent failed", is_error=True)}])
        self.assertEqual(s.send("x").error, "the agent failed")

    def test_quota_in_agent_session(self):
        s, _, _ = self.session([{"stdout": "", "stderr": "You have hit your usage limit", "exit": 1}])
        with self.assertRaises(QuotaError):
            s.send("x")

    def test_fork_before_start_is_an_error(self):
        s, _, _ = self.session([{"stdout": stream()}])
        with self.assertRaises(BackendError):
            s.fork()


if __name__ == "__main__":
    unittest.main()
