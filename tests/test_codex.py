import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeCodex, codex_events
from helpers import CaseTest, make_agent_case
from rehearse.backends import BackendError, MockBackend, QuotaError, SetupError, make_backend, parse_seat_spec
from rehearse.backends.codex import sandbox_for
from rehearse.case import load_case
from rehearse.runs import execute_run
from rehearse.workcheck import FILE_TOOLS, SHELL_TOOLS, WRITE_TOOLS

AUTH = '{"tokens": {"access_token": "AUTH-SECRET-9f8e7d6c5b4a"}}'
EXCLUSIONS = ["sandbox_workspace_write.exclude_slash_tmp=true", "sandbox_workspace_write.exclude_tmpdir_env_var=true"]


def configs(argv):
    """The values given to -c, in order."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]


class CodexCase(CaseTest):
    def setUp(self):
        super().setUp()
        login = self.root / "login"
        login.mkdir()
        (login / "auth.json").write_text(AUTH)
        (login / "auth.json").chmod(0o600)
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(login)})
        env.start()
        self.addCleanup(env.stop)

    def session(self, steps, tools=("Read", "Write(note.md)"), spec="codex", files=None, tag="", **options):
        fake = FakeCodex(self.root / ("fake" + tag), steps)
        fake.__enter__()
        self.addCleanup(fake.__exit__)
        work = self.root / ("work" + tag)
        work.mkdir(exist_ok=True)
        for rel, text in (files or {}).items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        s = make_backend(spec).open_session(work, allowed_tools=None if tools is None else list(tools), options=options)
        self.addCleanup(s.close)
        return s, fake, work

    def send(self, s, message="go"):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            r = s.send(message)
        return r, err.getvalue()


class SpecTests(unittest.TestCase):
    def test_seat_specs(self):
        self.assertIsNone(parse_seat_spec("codex")["model"])
        self.assertEqual(parse_seat_spec("codex:gpt-5")["model"], "gpt-5")
        s = parse_seat_spec({"backend": "codex", "model": "m", "sandbox": "read-only", "config": ["a=1"], "timeout": 5})
        self.assertEqual((s["sandbox"], s["config"]), ("read-only", ["a=1"]))
        with self.assertRaisesRegex(BackendError, "list of strings"):
            parse_seat_spec({"backend": "codex", "config": "a=1"})
        with self.assertRaisesRegex(BackendError, "sandbox must be one of"):
            make_backend({"backend": "codex", "sandbox": "nope"})

    def test_describe_and_capabilities(self):
        self.assertEqual(make_backend("codex:gpt-5").describe(), "codex:gpt-5")
        self.assertEqual(make_backend("codex").describe(), "codex")
        self.assertEqual(make_backend("codex").capabilities, frozenset({"agent", "resume", "fork"}))

    def test_sandbox_choice(self):
        self.assertEqual(sandbox_for(["Read", "Glob"]), "read-only")
        self.assertEqual(sandbox_for(None), "read-only")
        for tool in ("Write(a.md)", "Edit", "MultiEdit", "Bash(ls *)"):
            self.assertEqual(sandbox_for(["Read", tool]), "workspace-write")

    def test_workcheck_knows_codex_tool_names(self):
        self.assertIn("shell", SHELL_TOOLS)
        self.assertLessEqual({"write", "edit", "delete"}, FILE_TOOLS)
        self.assertLessEqual({"write", "edit", "delete"}, WRITE_TOOLS)


class SessionTests(CodexCase):
    def test_first_turn_resume_and_fork_flags(self):
        s, fake, work = self.session([{"stdout": codex_events(tid="T1")}, {"stdout": codex_events(tid="T1")},
                                      {"stdout": codex_events(tid="T2")}, {"stdout": codex_events(tid="T2")}],
                                     spec="codex:gpt-test")
        r1, _ = self.send(s, "first")
        self.send(s, "-dash start")
        child = s.fork()
        r3, _ = self.send(child, "forked")
        self.send(child, "again")
        a = [c["argv"] for c in fake.calls]
        common = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        self.assertEqual(a[0][:2], ["exec", "--json"])
        self.assertEqual(a[0][:7], ["exec"] + common + ["-C", str(work)])
        # resume and fork refuse -C (verified live); they run in the work folder with PWD set
        for argv in a[1:]:
            self.assertNotIn("-C", argv)
        self.assertEqual(a[1][:2], ["exec", "resume"])
        self.assertEqual(a[2][:2], ["exec", "fork"])
        self.assertEqual(a[3][:2], ["exec", "resume"])
        for argv in a:
            self.assertIn("-m", argv)
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-test")
            for flag in common:
                self.assertIn(flag, argv)
            self.assertNotIn("--sandbox", argv)
        # the thread id goes just before the prompt; `--` only when the prompt starts with a dash
        self.assertEqual(a[0][-1], "first")
        self.assertNotIn("T1", a[0])
        self.assertNotIn("--", a[0])
        self.assertEqual(a[1][-3:], ["T1", "--", "-dash start"])
        self.assertEqual(a[2][-2:], ["T1", "forked"])
        self.assertEqual(a[3][-2:], ["T2", "again"])
        self.assertEqual((s.session_id, child.session_id), ("T1", "T2"))
        self.assertEqual((r1.session_id, r3.session_id), ("T1", "T2"))
        self.assertEqual(Path(fake.calls[0]["cwd"]).resolve(), work.resolve())
        for call in fake.calls:
            self.assertEqual(Path(call["cwd"]).resolve(), work.resolve())
            self.assertEqual(Path(call["env"]["PWD"]).resolve(), work.resolve())

    def test_exclusions_always_and_sandbox_choice(self):
        s, fake, _ = self.session([{"stdout": codex_events()}], tools=("Read", "Write(note.md)"))
        self.send(s)
        c = configs(fake.calls[0]["argv"])
        self.assertIn('sandbox_mode="workspace-write"', c)
        for item in EXCLUSIONS:
            self.assertIn(item, c)
        s2, fake2, _ = self.session([{"stdout": codex_events()}], tools=("Read", "Glob"), tag="2")
        r, _ = self.send(s2)
        c2 = configs(fake2.calls[0]["argv"])
        self.assertIn('sandbox_mode="read-only"', c2)
        for item in EXCLUSIONS:
            self.assertIn(item, c2)
        self.assertEqual(r.extras["tool_limits"], "approximate: sandbox read-only")
        s3, fake3, _ = self.session([{"stdout": codex_events()}], tools=("Read",), tag="3", sandbox="workspace-write")
        self.send(s3)
        self.assertIn('sandbox_mode="workspace-write"', configs(fake3.calls[0]["argv"]))

    def test_extra_config_from_the_seat(self):
        spec = {"backend": "codex", "config": ['model_reasoning_effort="low"']}
        s, fake, _ = self.session([{"stdout": codex_events()}], spec=spec)
        self.send(s)
        self.assertIn('model_reasoning_effort="low"', configs(fake.calls[0]["argv"]))
        self.assertNotIn("-m", fake.calls[0]["argv"])

    def test_stdin_is_closed(self):
        s, fake, _ = self.session([{"stdout": codex_events()}])
        self.send(s)
        self.assertEqual(fake.calls[0]["stdin"], "")
        self.assertEqual(fake.calls[0]["stdin_target"], "/dev/null")

    def test_reply_is_last_message_and_preamble_kept(self):
        s, _, _ = self.session([{"stdout": codex_events(["Let me look.", "Reading now.", "All done."])}])
        r, _ = self.send(s)
        self.assertEqual(r.text, "All done.")
        self.assertEqual(r.extras["preamble"], ["Let me look.", "Reading now."])
        s2, _, _ = self.session([{"stdout": codex_events(["Only."])}], tag="2")
        self.assertNotIn("preamble", self.send(s2)[0].extras)

    def test_tool_calls_mapping(self):
        work = self.root / "work"
        items = [
            {"type": "command_execution", "command": "/bin/bash -lc 'cat list.md'", "aggregated_output": "x",
             "exit_code": 0, "status": "completed"},
            {"type": "command_execution", "command": f"/bin/bash -lc \"ls '{work}/sub'\"", "exit_code": 0},
            {"type": "command_execution", "command": "echo plain", "exit_code": 0},
            {"type": "file_change", "status": "completed", "changes": [
                {"path": f"{work}/note.md", "kind": "add"}, {"path": f"{work}/a/b.md", "kind": "update"},
                {"path": f"{work}/old.md", "kind": "delete"}, {"path": "/elsewhere/x", "kind": "add"}]},
            {"type": "web_search", "query": "q"},
            {"type": "reasoning", "text": "hmm"},
        ]
        s, _, _ = self.session([{"stdout": codex_events(["ok"], items=items)}])
        r, _ = self.send(s)
        self.assertEqual(r.tool_calls, [
            {"name": "shell", "arg": "cat list.md"},
            {"name": "shell", "arg": "ls './sub'"},
            {"name": "shell", "arg": "echo plain"},
            {"name": "write", "arg": "./note.md"},
            {"name": "edit", "arg": "./a/b.md"},
            {"name": "delete", "arg": "./old.md"},
            {"name": "write", "arg": "/elsewhere/x"},
            {"name": "web_search", "arg": ""},
            {"name": "reasoning", "arg": ""},
        ])

    def test_tokens_cost_and_basis(self):
        s, _, _ = self.session([{"stdout": codex_events(usage=(100, 60, 5, 20, 7))},
                                {"stdout": codex_events(usage=(50, 0, 0, 3, 1))}])
        r1, _ = self.send(s)
        self.assertEqual((r1.tokens_in, r1.tokens_out), (100, 27))     # input already includes cached
        self.assertIsNone(r1.cost_usd)
        self.assertEqual(r1.extras["cost_basis"], "subscription (not metered)")
        r2, _ = self.send(s)
        self.assertEqual((r2.tokens_in, r2.tokens_out), (50, 4))        # per turn, not cumulative

    def test_no_usage_is_unknown_not_zero(self):
        out = "\n".join([json.dumps({"type": "thread.started", "thread_id": "T"}),
                         json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})])
        s, _, _ = self.session([{"stdout": out + "\n"}])
        r, _ = self.send(s)
        self.assertEqual((r.tokens_in, r.tokens_out, r.cost_usd), (None, None, None))

    def test_api_key_seat_computes_cost_and_skips_the_login(self):
        spec = {"backend": "codex", "model": "gpt-x", "key_env": "MY_CODEX_KEY", "price_in_per_mtok": 2.0,
                "price_out_per_mtok": 10.0}
        with mock.patch.dict(os.environ, {"MY_CODEX_KEY": "sk-test-KEY-0123456789", "CODEX_HOME": str(self.root / "none")}):
            s, fake, _ = self.session([{"stdout": codex_events(usage=(1000, 0, 0, 400, 100))}], spec=spec)
            r, _ = self.send(s)
        self.assertAlmostEqual(r.cost_usd, (1000 * 2.0 + 500 * 10.0) / 1e6)
        self.assertEqual(fake.calls[0]["env"]["OPENAI_API_KEY"], "sk-test-KEY-0123456789")
        self.assertEqual(fake.calls[0]["home_listing"], [])
        self.assertNotIn("sk-test-KEY", json.dumps(r.extras))
        with mock.patch.dict(os.environ, {"MY_CODEX_KEY": ""}):
            with mock.patch("rehearse.backends.keys.keys_file", return_value=self.root / "nokeys"):
                with self.assertRaisesRegex(SetupError, "MY_CODEX_KEY"):
                    make_backend(spec).open_session(self.root)

    def test_subscription_session_drops_an_inherited_api_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-inherited-123456"}):
            s, fake, _ = self.session([{"stdout": codex_events()}])
            self.send(s)
        self.assertNotIn("OPENAI_API_KEY", fake.calls[0]["env"])

    def test_model_from_flag_and_from_session_file(self):
        s, _, _ = self.session([{"stdout": codex_events(tid="T9")}], spec="codex:gpt-flag")
        self.assertEqual(self.send(s)[0].model, "gpt-flag")
        session = {"2026/10/rollout-2026-T9.jsonl": '{"type":"session_meta","payload":{"model":"gpt-from-file"}}\n'}
        s2, _, _ = self.session([{"stdout": codex_events(tid="T9"), "session": session}], tag="2")
        self.assertEqual(self.send(s2)[0].model, "gpt-from-file")
        by_content = {"2026/10/other.jsonl": '{"id":"T8","model":"gpt-content"}\n'}
        s3, _, _ = self.session([{"stdout": codex_events(tid="T8"), "session": by_content}], tag="3")
        self.assertEqual(self.send(s3)[0].model, "gpt-content")
        s4, _, _ = self.session([{"stdout": codex_events(tid="T7")}], tag="4")
        self.assertIsNone(self.send(s4)[0].model)

    def test_turn_failed_and_error_events(self):
        out = json.dumps({"type": "thread.started", "thread_id": "T"}) + "\n" + \
            json.dumps({"type": "turn.failed", "error": {"message": "the model broke"}}) + "\n"
        s, _, _ = self.session([{"stdout": out, "exit": 1}])
        with self.assertRaisesRegex(BackendError, "the model broke") as cm:
            self.send(s)
        self.assertNotIsInstance(cm.exception, QuotaError)
        self.assertIn("thread.started", cm.exception.raw["stdout"])
        out = json.dumps({"type": "error", "message": "boom"}) + "\n"
        s, _, _ = self.session([{"stdout": out}], tag="2")
        with self.assertRaisesRegex(BackendError, "boom"):
            self.send(s)

    def test_quota_wording_in_failure_is_quota_error(self):
        out = json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}) + "\n" + \
            json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}) + "\n"
        s, _, _ = self.session([{"stdout": out, "exit": 1}])
        with self.assertRaises(QuotaError):
            self.send(s)
        s, _, _ = self.session([{"stdout": "", "stderr": "rate limit reached", "exit": 1}], tag="2")
        with self.assertRaises(QuotaError):
            self.send(s)
        s, _, _ = self.session([{"stdout": codex_events(["the usage limit and quota were discussed"])}], tag="3")
        self.assertIn("quota", self.send(s)[0].text)       # a reply that mentions it is fine

    def test_nonzero_exit_with_events_is_used(self):
        s, _, _ = self.session([{"stdout": codex_events(["partial"]), "stderr": "warning", "exit": 3}])
        r, _ = self.send(s)
        self.assertEqual((r.text, r.extras["exit_code"]), ("partial", 3))

    def test_nonzero_exit_without_events(self):
        s, _, _ = self.session([{"stdout": "", "stderr": "it broke", "exit": 1}])
        with self.assertRaisesRegex(BackendError, "it broke") as cm:
            self.send(s)
        self.assertEqual(cm.exception.raw["returncode"], 1)

    def test_malformed_line_keeps_raw(self):
        s, _, _ = self.session([{"stdout": codex_events() + "garbage line\n"}])
        with self.assertRaises(BackendError) as cm:
            self.send(s)
        self.assertIn("garbage line", cm.exception.raw["stdout"])

    def test_missing_program_and_missing_login(self):
        s, _, _ = self.session([{"stdout": codex_events()}])
        with mock.patch.dict(os.environ, {"REHEARSE_CODEX_BIN": str(self.root / "nope")}):
            with self.assertRaisesRegex(SetupError, "REHEARSE_CODEX_BIN"):
                self.send(s)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "empty")}):
            with self.assertRaisesRegex(SetupError, "codex login"):
                make_backend("codex").open_session(self.root)

    def test_login_in_home_folder_is_used_without_codex_home(self):
        home = self.root / "fakehome"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text("HOMEAUTH")
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        env["HOME"] = str(home)
        with mock.patch.dict(os.environ, env, clear=True):
            s, fake, _ = self.session([{"stdout": codex_events()}])
            self.send(s)
        self.assertEqual(fake.calls[0]["auth_content"], "HOMEAUTH")

    def test_timeout_kills_the_process_group(self):
        s, fake, _ = self.session([{"sleep": 30}], timeout=1)
        started = time.time()
        with self.assertRaisesRegex(BackendError, "timed out"):
            self.send(s)
        self.assertLess(time.time() - started, 15)
        pid = int(Path(str(fake.log) + ".pid").read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_private_home_is_private_shared_by_forks_and_removed(self):
        s, fake, _ = self.session([{"stdout": codex_events(tid="T1")}, {"stdout": codex_events(tid="T2")}])
        self.send(s)
        child = s.fork()
        self.send(child)
        c0, c1 = fake.calls
        home = c0["env"]["CODEX_HOME"]
        self.assertEqual(c1["env"]["CODEX_HOME"], home)
        self.assertNotEqual(Path(home).resolve(), (self.root / "login").resolve())
        self.assertEqual((c0["home_mode"], c0["auth_mode"]), (0o700, 0o600))
        self.assertEqual(c0["home_listing"], ["auth.json"])
        self.assertEqual(c0["auth_content"], AUTH)
        s.close()
        self.assertTrue(os.path.exists(home))          # the fork still needs it
        child.close()
        self.assertFalse(os.path.exists(home))
        self.assertTrue((self.root / "login" / "auth.json").exists())    # the real login is untouched

    def test_fork_before_start_is_an_error(self):
        s, _, _ = self.session([{"stdout": codex_events()}])
        with self.assertRaises(BackendError):
            s.fork()

    def test_claude_md_fallback_and_skills(self):
        s, fake, _ = self.session([{"stdout": codex_events()}],
                                  files={"CLAUDE.md": "be brief", ".claude/skills/weekly/SKILL.md": "x",
                                         ".agents/skills/other/SKILL.md": "y"})
        r, _ = self.send(s)
        self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', configs(fake.calls[0]["argv"]))
        self.assertEqual(r.extras["instructions_added"], ["CLAUDE.md"])
        self.assertEqual(sorted(r.extras["skills_present"]), ["other", "weekly"])
        s2, fake2, _ = self.session([{"stdout": codex_events()}], files={"CLAUDE.md": "a", "AGENTS.md": "b"}, tag="2")
        r2, _ = self.send(s2)
        self.assertFalse([c for c in configs(fake2.calls[0]["argv"]) if "project_doc" in c])
        self.assertNotIn("instructions_added", r2.extras)
        self.assertNotIn("skills_present", r2.extras)

    def test_warning_printed_once_per_run(self):
        s, _, _ = self.session([{"stdout": codex_events(tid="T1")}, {"stdout": codex_events(tid="T1")},
                                {"stdout": codex_events(tid="T2")}], tools=("Read", "Write(review.md)"))
        _, err1 = self.send(s)
        _, err2 = self.send(s)
        _, err3 = self.send(s.fork())
        self.assertIn("codex cannot limit writes to review.md; it may write anywhere in its work folder", err1)
        self.assertIn("work-folder check", err1)
        self.assertEqual((err2, err3), ("", ""))
        s2, _, _ = self.session([{"stdout": codex_events()}], tools=("Read",), tag="2")
        self.assertIn("codex cannot limit", self.send(s2)[1])


class EndToEndTests(CodexCase):
    def test_agent_run_through_the_adapter(self):
        case = load_case(make_agent_case(self.root, seat='"codex"', allowed_tools=("Read", "Write(review.md)")))
        steps = [{"stdout": codex_events(["Looking.", "Wrote it."], tid="T1", items=[
                      {"type": "file_change", "status": "completed",
                       "changes": [{"path": "review.md", "kind": "add"}]}]),
                  "write": {"review.md": "# Review\n\nDone.\n"}},
                 {"stdout": codex_events(["Anything else?"], tid="T1")}]
        stderr = io.StringIO()
        with FakeCodex(self.root / "fakeb", steps) as fake, contextlib.redirect_stderr(stderr):
            res = execute_run(case, "plain", "dana", sim_backend=MockBackend(["ok", "DONE"]))
        first, second = fake.calls
        self.assertEqual(second["argv"][:2], ["exec", "resume"])
        self.assertEqual(first["argv"][-1], "Please write my review.")
        self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', configs(first["argv"]))    # variant adds CLAUDE.md
        self.assertEqual(stderr.getvalue().count("codex cannot limit"), 1)
        meta = json.loads((res.path / "meta.json").read_text())
        self.assertEqual(meta["seats"]["subject"], "codex")
        self.assertFalse(meta["mock"])
        diff = json.loads((res.path / "artifacts" / "diff.json").read_text())
        self.assertIn("review.md", json.dumps(diff))
        self.assertTrue((res.path / "artifacts" / "files" / "review.md").is_file())
        raw = json.loads((res.path / "raw" / "subject-01.json").read_text())
        self.assertIsNone(raw["result"]["cost_usd"])
        # the login file's content is in no file of the run
        for path in res.path.rglob("*"):
            if path.is_file():
                self.assertNotIn("AUTH-SECRET", path.read_text(errors="replace"), str(path))
        self.assertFalse(os.path.exists(first["env"]["CODEX_HOME"]))
        self.assertFalse(Path(first["cwd"]).exists())


if __name__ == "__main__":
    unittest.main()
