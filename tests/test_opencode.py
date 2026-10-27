import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeOpencode, oc_events
from helpers import CaseTest, build_runs, make_agent_case
from rehearse.backends import BackendError, QuotaError, SetupError, make_backend, parse_seat_spec
from rehearse.backends.opencode import map_permissions
from rehearse.case import load_case
from rehearse.runs import execute_run
from test_cli import run_main

KEY = "sk-test-SECRET-0123456789"


def argv_has(argv, flag, value=None):
    if flag not in argv:
        return False
    return value is None or argv[argv.index(flag) + 1] == value


class OpencodeCase(CaseTest):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)

    def session(self, steps, tools=("Read", "Write(note.md)"), spec="opencode:deepseek/deepseek-chat", files=None,
                tag="", **options):
        fake = FakeOpencode(self.root / ("fake" + tag), steps)
        fake.__enter__()
        self.addCleanup(fake.__exit__)
        work = self.root / ("work" + tag)
        work.mkdir(exist_ok=True)
        for rel, text in (files or {}).items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        s = make_backend(spec).open_session(work, allowed_tools=list(tools), options=options)
        self.addCleanup(s.close)
        return s, fake, work


class SpecTests(unittest.TestCase):
    def test_string_and_table_specs(self):
        s = parse_seat_spec("opencode:deepseek/deepseek-chat")
        self.assertEqual((s["backend"], s["model"]), ("opencode", "deepseek/deepseek-chat"))
        s = parse_seat_spec({"backend": "opencode", "model": "mine/m1", "price_in_per_mtok": 1, "timeout": 30,
                             "provider": {"base_url": "http://127.0.0.1:1/v1", "models": ["m1", "m2"]}})
        self.assertEqual(s["provider"]["models"], ["m1", "m2"])
        with self.assertRaisesRegex(BackendError, "must be a table"):
            parse_seat_spec({"backend": "opencode", "model": "a/b", "provider": "x"})

    def test_bad_model_and_provider_settings(self):
        with self.assertRaisesRegex(BackendError, "PROVIDER/MODEL"):
            make_backend("opencode:nomodel")
        with self.assertRaisesRegex(BackendError, "unknown setting 'url'"):
            make_backend({"backend": "opencode", "model": "a/b", "provider": {"url": "x"}})

    def test_describe_and_capabilities(self):
        b = make_backend("opencode:deepseek/deepseek-chat")
        self.assertEqual(b.describe(), "opencode:deepseek/deepseek-chat")
        self.assertEqual(b.capabilities, frozenset({"agent", "resume", "fork"}))
        with self.assertRaisesRegex(BackendError, "lacks 'chat'|cannot"):
            from rehearse.adapters import ChatAdapter
            ChatAdapter(b, "x")

    def test_permission_mapping(self):
        self.assertEqual(map_permissions(["Read", "Write(note.md)"]),
                         {"*": "deny", "read": "allow", "edit": {"*": "deny", "note.md": "allow"}})
        self.assertEqual(map_permissions(["Glob", "Grep", "Edit", "Bash(git status *)", "Bash(ls:*)", "Task"]),
                         {"*": "deny", "glob": "allow", "grep": "allow", "edit": "allow",
                          "bash": {"*": "deny", "git status *": "allow", "ls *": "allow"}})
        self.assertEqual(map_permissions(["Write(a.md)", "Edit(b.md)"])["edit"],
                         {"*": "deny", "a.md": "allow", "b.md": "allow"})
        self.assertEqual(map_permissions(["Read"], has_skills=True)["skill"], "allow")
        self.assertEqual(map_permissions(None), {"*": "deny"})


class SessionTests(OpencodeCase):
    def test_first_turn_resume_and_environment(self):
        s, fake, work = self.session([{"stdout": oc_events(["Hello.", "Second part."], sid="ses_1")},
                                      {"stdout": oc_events(["Again"], sid="ses_1")}])
        r1 = s.send("-starts with a dash")
        r2 = s.send("second")
        a1, a2 = fake.calls[0]["argv"], fake.calls[1]["argv"]
        self.assertEqual(a1[:6], ["run", "--standalone", "--format", "json", "-m", "deepseek/deepseek-chat"])
        self.assertEqual(a1[-2:], ["--", "-starts with a dash"])
        self.assertNotIn("-s", a1)
        self.assertTrue(argv_has(a2, "-s", "ses_1"))
        self.assertNotIn("--fork", a2)
        self.assertEqual(a2[-2:], ["--", "second"])
        self.assertEqual(Path(fake.calls[0]["cwd"]).resolve(), work.resolve())
        self.assertEqual(fake.calls[0]["stdin"], "")
        self.assertEqual((r1.text, r1.session_id, r1.model), ("Hello.\n\nSecond part.", "ses_1", "deepseek/deepseek-chat"))
        self.assertEqual(r2.text, "Again")
        # the environment: a fresh empty HOME, one config variable, the key only in the environment
        env = fake.calls[0]["env"]
        self.assertEqual(fake.calls[0]["home_listing"], [])
        self.assertEqual(fake.calls[0]["home_mode"], 0o700)
        self.assertNotEqual(Path(env["HOME"]).resolve(), Path.home().resolve())
        self.assertEqual(fake.calls[1]["env"]["HOME"], env["HOME"])
        self.assertEqual(env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        # opencode finds its project folder from PWD: an inherited PWD would send it elsewhere
        self.assertEqual(Path(env["PWD"]).resolve(), work.resolve())
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], {"*": "deny", "read": "allow", "edit": {"*": "deny", "note.md": "allow"}})
        self.assertEqual(config["provider"]["deepseek"]["options"],
                         {"baseURL": "https://api.deepseek.com/v1", "apiKey": "{env:DEEPSEEK_API_KEY}"})
        self.assertEqual(config["provider"]["deepseek"]["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(config["provider"]["deepseek"]["models"], {"deepseek-chat": {}})
        self.assertNotIn("instructions", config)
        self.assertEqual(env["DEEPSEEK_API_KEY"], KEY)
        self.assertNotIn(KEY, env["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn(KEY, " ".join(a1))
        self.assertNotIn(KEY, json.dumps(r1.extras))
        home = env["HOME"]
        s.close()
        self.assertFalse(os.path.exists(home))               # removed with the session

    def test_xdg_variables_are_not_passed_on(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/somewhere"}):
            s, fake, _ = self.session([{"stdout": oc_events()}])
            s.send("x")
        self.assertNotIn("XDG_CONFIG_HOME", fake.calls[0]["env"])

    def test_fork_adds_flag_once_and_tracks_new_id(self):
        s, fake, _ = self.session([{"stdout": oc_events(sid="S1")}, {"stdout": oc_events(sid="S2")},
                                   {"stdout": oc_events(sid="S2")}])
        s.send("one")
        child = s.fork()
        child.send("two")
        child.send("three")
        a2, a3 = fake.calls[1]["argv"], fake.calls[2]["argv"]
        self.assertTrue(argv_has(a2, "-s", "S1"))
        self.assertIn("--fork", a2)
        self.assertTrue(argv_has(a3, "-s", "S2"))
        self.assertNotIn("--fork", a3)
        self.assertEqual((s.session_id, child.session_id), ("S1", "S2"))
        self.assertEqual(fake.calls[1]["env"]["HOME"], fake.calls[0]["env"]["HOME"])   # the parent's session lives there
        home = fake.calls[0]["env"]["HOME"]
        s.close()
        self.assertTrue(os.path.exists(home))                # the fork still needs it
        child.close()
        self.assertFalse(os.path.exists(home))

    def test_fork_before_start_is_an_error(self):
        s, _, _ = self.session([{"stdout": oc_events()}])
        with self.assertRaises(BackendError):
            s.fork()

    def test_tokens_summed_and_cost_unknown_without_prices(self):
        out = oc_events(["a"], tokens=(10, 4, 3, 2, 1)) + oc_events(["b"], tokens=(5, 6, 0, 0, 0))
        s, _, _ = self.session([{"stdout": out}])
        r = s.send("x")
        self.assertEqual((r.tokens_in, r.tokens_out), (10 + 2 + 1 + 5, 4 + 3 + 6))
        self.assertIsNone(r.cost_usd)

    def test_cost_computed_with_prices_and_reported_cost_wins(self):
        spec = {"backend": "opencode", "model": "deepseek/deepseek-chat", "price_in_per_mtok": 2.0,
                "price_out_per_mtok": 10.0}
        s, _, _ = self.session([{"stdout": oc_events(tokens=(1000, 500, 0, 0, 0))},
                                {"stdout": oc_events(cost=0.5)}], spec=spec)
        r1 = s.send("x")
        self.assertAlmostEqual(r1.cost_usd, (1000 * 2.0 + 500 * 10.0) / 1e6)
        r2 = s.send("y")
        self.assertEqual(r2.cost_usd, 0.5)
        self.assertEqual(r2.extras["cost_basis"], "reported")

    def test_no_token_numbers_is_unknown_not_zero(self):
        out = json.dumps({"type": "text", "sessionID": "S", "part": {"text": "hi"}}) + "\n"
        s, _, _ = self.session([{"stdout": out}], spec={"backend": "opencode", "model": "deepseek/m",
                                                        "price_in_per_mtok": 1, "price_out_per_mtok": 1})
        r = s.send("x")
        self.assertEqual((r.tokens_in, r.tokens_out, r.cost_usd), (None, None, None))

    def test_reply_is_text_after_last_tool_and_calls_are_parsed(self):
        work = self.root / "work"
        s, _, _ = self.session([{"stdout": oc_events(["final answer"], tools=[
            ("read", {"filePath": f"{work}/data.md"}),
            ("write", {"path": f"{work}/note.md", "content": "hello"}),
            ("bash", {"command": "ls " + "x" * 300}),
            ("skill", {"name": "weekly-review"}),
            ("glob", {"nothing": 1}),
            ("skill", {"id": "weekly-review"})])}])     # the real opencode skill tool sends "id"
        r = s.send("go")
        self.assertEqual(r.text, "final answer")                     # not "thinking aloud" from before the tools
        self.assertEqual(r.tool_calls[0], {"name": "read", "arg": "./data.md"})
        self.assertEqual(r.tool_calls[1], {"name": "write", "arg": "./note.md"})
        self.assertEqual(len(r.tool_calls[2]["arg"]), 200)
        self.assertEqual(r.tool_calls[3], {"name": "skill", "arg": "weekly-review"})
        self.assertEqual(r.tool_calls[4], {"name": "glob", "arg": None})
        self.assertEqual(r.tool_calls[5], {"name": "skill", "arg": "weekly-review"})

    def test_text_before_tools_is_used_when_nothing_follows(self):
        lines = [json.dumps({"type": "text", "sessionID": "S", "part": {"text": "only text"}}),
                 json.dumps({"type": "tool_use", "sessionID": "S", "part": {"tool": "read", "state": {"input": {}}}}),
                 json.dumps({"type": "mystery", "sessionID": "S", "part": {}})]
        s, _, _ = self.session([{"stdout": "\n".join(lines) + "\n"}])
        self.assertEqual(s.send("x").text, "only text")

    def test_malformed_line_keeps_raw(self):
        s, _, _ = self.session([{"stdout": oc_events() + "garbage line\n", "stderr": "warn"}])
        with self.assertRaises(BackendError) as cm:
            s.send("x")
        self.assertIn("garbage line", cm.exception.raw["stdout"])
        self.assertEqual(cm.exception.raw["stderr"], "warn")

    def test_nonzero_exit_with_events_is_used(self):
        s, _, _ = self.session([{"stdout": oc_events(["partial"]), "stderr": "warning", "exit": 3}])
        r = s.send("x")
        self.assertEqual((r.text, r.extras["exit_code"]), ("partial", 3))

    def test_nonzero_exit_without_events(self):
        s, _, _ = self.session([{"stdout": "", "stderr": "boom: it broke", "exit": 1}])
        with self.assertRaises(BackendError) as cm:
            s.send("x")
        self.assertIn("it broke", str(cm.exception))
        self.assertNotIsInstance(cm.exception, QuotaError)
        self.assertEqual(cm.exception.raw["returncode"], 1)

    def test_quota_only_from_stderr_or_error_event(self):
        s, _, _ = self.session([{"stdout": "", "stderr": "You hit a rate limit", "exit": 1}])
        with self.assertRaises(QuotaError):
            s.send("x")
        s, _, _ = self.session([{"stdout": oc_events(["we discussed the usage limit and quota"])}])
        self.assertIn("quota", s.send("x").text)             # a reply that mentions it is fine

    def test_error_event_sets_error(self):
        out = oc_events(["partial"]) + json.dumps({"type": "error", "sessionID": "ses_1",
                                                  "error": {"message": "provider failed"}}) + "\n"
        s, _, _ = self.session([{"stdout": out}])
        self.assertIn("provider failed", s.send("x").error)

    def test_missing_program_is_setup_error(self):
        s, _, _ = self.session([{"stdout": oc_events()}])
        with mock.patch.dict(os.environ, {"REHEARSE_OPENCODE_BIN": str(self.root / "nope")}):
            with self.assertRaisesRegex(SetupError, "REHEARSE_OPENCODE_BIN"):
                s.send("x")

    def test_missing_key_is_setup_error(self):
        s, _, _ = self.session([{"stdout": oc_events()}])
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with mock.patch("rehearse.backends.keys.keys_file", return_value=self.root / "nokeys"):
                with self.assertRaisesRegex(SetupError, "DEEPSEEK_API_KEY"):
                    s.send("x")

    def test_timeout_kills_the_process_group(self):
        s, fake, _ = self.session([{"sleep": 30}], timeout=1)
        started = time.time()
        with self.assertRaisesRegex(BackendError, "timed out"):
            s.send("x")
        self.assertLess(time.time() - started, 15)
        pid = int(Path(str(fake.log) + ".pid").read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_instructions_added_for_claude_md_only(self):
        s, fake, _ = self.session([{"stdout": oc_events()}], files={"CLAUDE.md": "be brief"})
        r = s.send("x")
        config = json.loads(fake.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["instructions"], ["CLAUDE.md"])
        self.assertEqual(r.extras["instructions_added"], ["CLAUDE.md"])
        s2, fake2, _ = self.session([{"stdout": oc_events()}], files={"CLAUDE.md": "a", "AGENTS.md": "b"}, tag="2")
        r2 = s2.send("x")
        self.assertNotIn("instructions", json.loads(fake2.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"]))
        self.assertNotIn("instructions_added", r2.extras)

    def test_skills_folder_allows_skill_and_raw_permission_overrides(self):
        s, fake, _ = self.session([{"stdout": oc_events()}], files={".claude/skills/x/SKILL.md": "---\nname: x\n---\n"})
        s.send("x")
        perm = json.loads(fake.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"])["permission"]
        self.assertEqual(perm["skill"], "allow")
        s2, fake2, _ = self.session([{"stdout": oc_events()}], permission={"*": "deny", "bash": "allow"}, tag="2")
        s2.send("x")
        self.assertEqual(json.loads(fake2.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"])["permission"],
                         {"*": "deny", "bash": "allow"})

    def test_custom_provider_table(self):
        spec = {"backend": "opencode", "model": "mine/m1",
                "provider": {"base_url": "http://127.0.0.1:9/v1", "models": ["m2"], "npm": "@x/y"}}
        s, fake, _ = self.session([{"stdout": oc_events()}], spec=spec)
        s.send("x")
        env = fake.calls[0]["env"]
        prov = json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["mine"]
        self.assertEqual(prov, {"npm": "@x/y", "options": {"baseURL": "http://127.0.0.1:9/v1"},
                                "models": {"m1": {}, "m2": {}}})

    def test_known_provider_gets_no_provider_block(self):
        s, fake, _ = self.session([{"stdout": oc_events()}], spec="opencode:anthropic/claude-x")
        s.send("x")
        self.assertNotIn("provider", json.loads(fake.calls[0]["env"]["OPENCODE_CONFIG_CONTENT"]))


class EndToEndTests(OpencodeCase):
    def test_agent_run_through_the_adapter(self):
        case = load_case(make_agent_case(self.root, seat='"opencode:deepseek/deepseek-chat"',
                                         allowed_tools=("Read", "Write(review.md)")))
        steps = [{"stdout": oc_events(["Wrote it."], sid="S1", tools=[("write", {"filePath": "review.md"})]),
                  "write": {"review.md": "# Review\n\nKey is not here.\n"}},
                 {"stdout": oc_events(["Anything else?"], sid="S1")}]
        from rehearse.backends import MockBackend
        with FakeOpencode(self.root / "fake", steps) as fake:
            res = execute_run(case, "plain", "dana", sim_backend=MockBackend(["ok", "DONE"]))
        first, second = fake.calls
        self.assertEqual(first["home_listing"], [])
        self.assertTrue(argv_has(second["argv"], "-s", "S1"))
        self.assertEqual(first["argv"][-1], "Please write my review.")
        config = json.loads(first["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["instructions"], ["CLAUDE.md"])        # the variant lays down a CLAUDE.md
        meta = json.loads((res.path / "meta.json").read_text())
        self.assertEqual(meta["seats"]["subject"], "opencode:deepseek/deepseek-chat")
        self.assertFalse(meta["mock"])
        diff = json.loads((res.path / "artifacts" / "diff.json").read_text())
        self.assertIn("review.md", json.dumps(diff))
        self.assertTrue((res.path / "artifacts" / "files" / "review.md").is_file())
        raw = json.loads((res.path / "raw" / "subject-01.json").read_text())
        self.assertIn("stdout", raw["result"]["raw"])
        self.assertIsNone(raw["result"]["cost_usd"])
        # the key is in no file of the run
        for path in res.path.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors="replace"), str(path))
        # and the work folder and private home are gone
        self.assertFalse(os.path.exists(first["env"]["HOME"]))
        self.assertFalse(Path(first["cwd"]).exists())

    def test_key_echoed_in_output_is_scrubbed(self):
        case = load_case(make_agent_case(self.root, seat='"opencode:deepseek/deepseek-chat"'))
        from rehearse.backends import MockBackend
        steps = [{"stdout": oc_events([f"my key is {KEY}"]), "stderr": f"oops {KEY}"}]
        with FakeOpencode(self.root / "fake", steps):
            res = execute_run(case, "baseline", "dana", sim_backend=MockBackend(["ok", "DONE"]))
        for path in res.path.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors="replace"), str(path))


class SeatWarningTests(CaseTest):
    def sets(self, before, after):
        b = build_runs(self.root / "b", subject_model="claude-sonnet-4-5", **dict(checks_py=None))
        a = build_runs(self.root / "a", subject_model="claude-sonnet-4-5", **dict(checks_py=None))
        for runs, (seat, model) in ((b, before), (a, after)):
            for meta in runs.glob("*/meta.json"):
                data = json.loads(meta.read_text())
                data["seats"]["subject"] = seat
                data["models"]["subject"] = [model]
                meta.write_text(json.dumps(data))
        code, out, err = run_main(["compare", str(b), str(a)])
        self.assertEqual(code, 0, err)
        return out

    def test_different_seat_strings_same_model_do_not_warn(self):
        out = self.sets(("claude:sonnet", "claude-sonnet-4-5"), ("claude", "claude-sonnet-4-5"))
        self.assertNotIn("seats or models", out)

    def test_different_models_still_warn(self):
        out = self.sets(("claude:sonnet", "claude-sonnet-4-5"), ("claude:opus", "claude-opus-4-1"))
        self.assertIn("subject seats or models differ", out)

    def test_alias_is_named(self):
        out = self.sets(("claude:sonnet", "sonnet"), ("claude:claude-sonnet-4-5", "claude-sonnet-4-5"))
        self.assertIn("model recorded as alias 'sonnet' in before runs", out)
        self.assertNotIn("subject seats or models differ", out)


if __name__ == "__main__":
    unittest.main()
