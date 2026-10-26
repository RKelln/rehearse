"""Tests for the step 3 review fixes and the live findings."""

import http.client as httpclient
import json
import math
import os
import stat
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from fakes import FakeClaude, FakeServer, chat_json, ok_body, stream
from helpers import CaseTest, make_agent_case, make_case
from rehearse import cli
from rehearse.backends import BackendError, MockBackend, Msg, QuotaError, SetupError, make_backend
from rehearse.backends.http import HttpBackend, retry_wait
from rehearse.backends.keys import keys_file, read_key, scrub_keys
from rehearse.case import CaseError, load_case
from rehearse.checks import Run
from rehearse.cost import Spend, estimate_line
from rehearse.runs import execute_run, scrub, write_json
from rehearse.workspace import Workspace, matches
from test_cli import run_main
from test_live_cli import LiveCaseTest

MSGS = [Msg("user", "hi")]


def http(url, **spec):
    return make_backend({"backend": "http", "model": "m1", "base_url": url, **spec})


class ToolRestrictionTests(CaseTest):
    def argv(self, allowed=("Read", "Write(review.md)", "Edit(review.md)"), **options):
        with FakeClaude(self.root / "fake", [{"stdout": stream()}]) as fake:
            s = make_backend("claude:opus").open_session(self.root, allowed_tools=list(allowed), options=options)
            s.send("x")
        return fake.calls[0]["argv"]

    def test_tools_and_permission_mode_restrict(self):
        a = self.argv()
        self.assertEqual(a[a.index("--tools") + 1], "Read,Write,Edit")
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(a[a.index("--allowedTools") + 1:], ["Read", "Write(review.md)", "Edit(review.md)"])

    def test_options_override(self):
        a = self.argv(tools=["Read", "Grep"], permission_mode="plan")
        self.assertEqual(a[a.index("--tools") + 1], "Read,Grep")
        self.assertEqual(a[a.index("--permission-mode") + 1], "plan")

    def test_case_toml_options_reach_the_command(self):
        path = make_agent_case(self.root, seat='"claude"')
        path.write_text(path.read_text().replace('adapter = "agent"', 'adapter = "agent"\ntools = ["Read"]\npermission_mode = "plan"'))
        case = load_case(path)
        self.assertEqual(case.subject_options, {"tools": ["Read"], "permission_mode": "plan"})
        with FakeClaude(self.root / "fake", [{"stdout": stream()}]) as fake:
            execute_run(case, "baseline", "dana", sim_backend=MockBackend(["DONE"]))
        a = fake.calls[0]["argv"]
        self.assertEqual((a[a.index("--tools") + 1], a[a.index("--permission-mode") + 1]), ("Read", "plan"))

    def test_bad_tools_option(self):
        path = make_agent_case(self.root)
        path.write_text(path.read_text().replace('adapter = "agent"', 'adapter = "agent"\ntools = "Read"'))
        with self.assertRaisesRegex(CaseError, "tools must be a list"):
            load_case(path)


class SpendLineTests(unittest.TestCase):
    def cost(self, calls, unknown, usd):
        return {"calls": calls, "unknown_calls": unknown, "known_usd": usd}

    def test_forms(self):
        s = Spend()
        s.add({"subject": self.cost(3, 3, 0.0), "sim": self.cost(4, 1, 0.5)})
        self.assertEqual(s.line(), "spent: subject unknown, sim $0.5000 + unknown for 1 calls")
        s = Spend()
        s.add({"subject": self.cost(2, 0, 0.25), "sim": self.cost(2, 2, 0.0)}, {"subject": False, "sim": True})
        self.assertEqual(s.line(), "spent: subject $0.2500, sim mock (no cost)")
        self.assertEqual(Spend().line(), "spent: subject not called, sim not called")


class SymlinkTests(CaseTest):
    def test_scenario_links_are_skipped_and_listed(self):
        outside = self.root / "outside.txt"
        outside.write_text("OUTSIDE SECRET")
        (self.root / "outdir").mkdir()
        (self.root / "outdir" / "x.txt").write_text("OUTSIDE DIR")
        case = load_case(make_agent_case(self.root))
        (self.root / "scenario" / "link.txt").symlink_to(outside)
        (self.root / "scenario" / "linkdir").symlink_to(self.root / "outdir")
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))
        scenario = json.loads((res.path / "inputs" / "scenario.json").read_text())
        self.assertEqual(scenario["skipped_symlinks"], ["link.txt", "linkdir"])
        self.assertEqual(sorted(scenario["files"]), ["notes.md"])
        self.assertIn("skipped a symbolic link in the scenario: link.txt", buf.getvalue())
        for p in res.path.rglob("*"):
            if p.is_file():
                self.assertNotIn("OUTSIDE", p.read_text())

    def test_links_made_by_the_agent_are_recorded_not_followed(self):
        outside = self.root / "outside.txt"
        outside.write_text("OUTSIDE SECRET")
        (self.root / "outdir").mkdir()
        (self.root / "outdir" / "x.txt").write_text("OUTSIDE DIR")
        case = load_case(make_agent_case(self.root))
        ws = Workspace.create(case, "baseline")
        try:
            (ws.path / "sneaky.txt").symlink_to(outside)
            (ws.path / "sneakydir").symlink_to(self.root / "outdir")
            (ws.path / "real.md").write_text("fine")
            run = self.root / "run"
            run.mkdir()
            diff = ws.write_artifacts(run, scrub, write_json)
        finally:
            ws.cleanup()
        self.assertEqual(diff["added"], [
            {"path": "real.md"} if False else "real.md",
            {"path": "sneaky.txt", "symlink_to": str(outside)},
            {"path": "sneakydir", "symlink_to": str(self.root / "outdir")}])
        saved = json.loads((run / "artifacts" / "diff.json").read_text())
        self.assertEqual(saved["added"][1]["symlink_to"], str(outside))
        self.assertEqual(sorted(p.name for p in (run / "artifacts" / "files").rglob("*") if p.is_file()), ["real.md"])
        self.assertNotIn("OUTSIDE", "".join(p.read_text() for p in run.rglob("*") if p.is_file()))


class LocalKeyTests(CaseTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"), "OPENAI_API_KEY": "sk-openai-12345678"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_server_gets_no_key_by_default(self):
        with FakeServer() as srv:
            make_backend(f"http:m@{srv.url}").chat("s", MSGS)
        self.assertNotIn("Authorization", srv.requests[0]["headers"])

    def test_keys_file_not_read_for_a_local_seat(self):
        keys_file().parent.mkdir(parents=True)
        keys_file().write_text("OPENAI_API_KEY=abc\n")
        keys_file().chmod(0o644)
        with FakeServer() as srv:
            make_backend(f"http:m@{srv.url}").chat("s", MSGS)            # no SetupError about permissions
            self.assertEqual(len(srv.requests), 1)

    def test_named_key_is_sent_to_a_local_server(self):
        with mock.patch.dict(os.environ, {"MY_LOCAL_KEY": "local-key-12345"}), FakeServer() as srv:
            http(srv.url, key_env="MY_LOCAL_KEY").chat("s", MSGS)
        self.assertEqual(srv.requests[0]["headers"]["Authorization"], "Bearer local-key-12345")

    def test_unreadable_keys_file(self):
        keys_file().parent.mkdir(parents=True)
        keys_file().write_bytes(b"\xff\xfe\x00bad")
        keys_file().chmod(0o600)
        os.environ.pop("OTHER_KEY", None)
        with self.assertRaisesRegex(SetupError, "cannot read the keys file"):
            read_key("OTHER_KEY")

    def test_short_keys_are_scrubbed_too(self):
        with mock.patch.dict(os.environ, {"SHORT_KEY": "ab12"}):
            read_key("SHORT_KEY")
        self.assertEqual(scrub_keys("my ab12 key"), "my [key removed] key")


class RetryTests(CaseTest):
    def test_retry_wait(self):
        self.assertEqual(retry_wait("-5", 4), 0)
        self.assertEqual(retry_wait("3", 4), 3)
        self.assertEqual(retry_wait("999", 4), 60)
        self.assertEqual(retry_wait(None, 4), 4)
        self.assertEqual(retry_wait("nonsense", 2), 2)
        later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        self.assertTrue(20 < retry_wait(later, 4) <= 30)
        past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
        self.assertEqual(retry_wait(past, 4), 0)

    def test_negative_retry_after_does_not_crash_and_last_try_does_not_sleep(self):
        with FakeServer([(429, {"Retry-After": "-10"}, "slow")]) as srv:
            b = http(srv.url)
            b.attempts = 3
            with mock.patch("time.sleep") as sleep:
                with self.assertRaisesRegex(BackendError, "gave up after 3 tries"):
                    b.chat("s", MSGS)
        self.assertEqual(sleep.call_count, 2)               # between the tries, not after the last
        self.assertEqual(len(srv.requests), 3)

    def test_incomplete_read_and_connection_reset_are_retried(self):
        with FakeServer() as srv:
            b = http(srv.url)
            b.backoff = 0.001
            real = __import__("rehearse.backends.http", fromlist=["_OPENER"])._OPENER.open
            calls = []

            def flaky(req, timeout=None):
                calls.append(1)
                if len(calls) == 1:
                    raise httpclient.IncompleteRead(b"par")
                if len(calls) == 2:
                    raise ConnectionResetError("reset")
                return real(req, timeout=timeout)

            with mock.patch("rehearse.backends.http._OPENER.open", side_effect=flaky):
                res = b.chat("s", MSGS)
        self.assertEqual(res.text, "Hello there")
        self.assertEqual(len(calls), 3)


class HttpParsingTests(CaseTest):
    def body(self, **parts):
        return (200, {}, json.dumps(parts))

    def test_odd_but_valid_replies(self):
        msg = {"message": {"content": [{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}]}}
        with FakeServer([self.body(choices=[msg], usage={"prompt_tokens": "many", "completion_tokens": 5})]) as srv:
            res = http(srv.url).chat("s", MSGS)
        self.assertEqual((res.text, res.tokens_in, res.tokens_out), ("abcd", None, 5))

    def test_unusable_replies_keep_raw(self):
        bad = ['{"choices": [{"message": {"content": {"a": 1}}}]}', '{"choices": "no"}', '[1, 2]',
               '{"choices": [{"message": "text"}]}']
        for text in bad:
            with self.subTest(text), FakeServer([(200, {}, text)]) as srv:
                with self.assertRaises(BackendError) as cm:
                    http(srv.url).chat("s", MSGS)
                self.assertEqual(cm.exception.raw["body"], text)

    def test_truncated_reply_is_marked(self):
        full = {"choices": [{"message": {"content": "cut off"}, "finish_reason": "length"}]}
        with FakeServer([(200, {}, json.dumps(full))]) as srv:
            res = http(srv.url).chat("s", MSGS)
        self.assertEqual(res.extras, {"finish_reason": "length", "truncated": True})
        with FakeServer() as srv:
            self.assertEqual(http(srv.url).chat("s", MSGS).extras, {})

    def test_redirects_are_refused(self):
        with FakeServer([(302, {"Location": "http://127.0.0.1:1/elsewhere"}, "")]) as srv:
            with self.assertRaisesRegex(BackendError, "redirect"):
                http(srv.url).chat("s", MSGS)
        self.assertEqual(len(srv.requests), 1)

    def test_key_over_plain_http_to_a_remote_host_is_refused(self):
        with mock.patch.dict(os.environ, {"REMOTE_KEY": "remote-key-12345"}):
            b = http("http://example.invalid/v1", key_env="REMOTE_KEY")
            with self.assertRaisesRegex(SetupError, "plain http"):
                b.chat("s", MSGS)


class ClaudeParsingTests(CaseTest):
    def chat(self, steps):
        with FakeClaude(self.root / "fake", steps):
            return make_backend("claude").chat("s", MSGS)

    def test_malformed_chat_output_keeps_raw(self):
        for out in ('[1, 2, 3]', '"just a string"', 'plain text'):
            with self.subTest(out), self.assertRaises(BackendError) as cm:
                self.chat([{"stdout": out}])
            self.assertEqual(cm.exception.raw["stdout"], out)

    def test_odd_fields_are_tolerated(self):
        res = self.chat([{"stdout": json.dumps({"result": "ok", "total_cost_usd": "free", "usage": [1], "modelUsage": []})}])
        self.assertEqual((res.text, res.cost_usd, res.tokens_in), ("ok", None, None))

    def test_malformed_stream_keeps_raw(self):
        bad = json.dumps({"type": "assistant", "message": "not a dict"}) + "\n" + stream()
        with FakeClaude(self.root / "fake", [{"stdout": bad}]):
            s = make_backend("claude").open_session(self.root, allowed_tools=["Read"])
            with self.assertRaises(BackendError) as cm:
                s.send("x")
        self.assertEqual(cm.exception.raw["stdout"], bad)

    def test_a_reply_that_mentions_a_limit_is_not_a_quota_error(self):
        text = "You have reached your usage limit and the quota resets 5pm, I would say"
        res = self.chat([{"stdout": chat_json(text)}])
        self.assertEqual(res.text, text)
        with FakeClaude(self.root / "fake2", [{"stdout": stream(text)}]):
            s = make_backend("claude").open_session(self.root, allowed_tools=["Read"])
            self.assertEqual(s.send("x").text, text)

    def test_nonzero_exit_with_ordinary_text_is_not_quota(self):
        with FakeClaude(self.root / "fake", [{"stdout": chat_json("mentions quota in passing"), "stderr": "oops", "exit": 3}]):
            with self.assertRaises(BackendError) as cm:
                make_backend("claude").chat("s", MSGS)
        self.assertNotIsInstance(cm.exception, QuotaError)

    def test_error_result_with_limit_wording_is_quota(self):
        with FakeClaude(self.root / "fake", [{"stdout": chat_json("Usage limit reached", is_error=True), "exit": 1}]):
            with self.assertRaises(QuotaError):
                make_backend("claude").chat("s", MSGS)
        with FakeClaude(self.root / "fake2", [{"stdout": stream("Usage limit reached", is_error=True)}]):
            s = make_backend("claude").open_session(self.root, allowed_tools=["Read"])
            with self.assertRaises(QuotaError):
                s.send("x")


class ForkCostTests(CaseTest):
    def test_first_forked_turn_is_marked_and_decrease_is_unknown(self):
        steps = [{"stdout": stream(cost=0.10, sid="S1")}, {"stdout": stream(cost=0.30, sid="S2")},
                 {"stdout": stream(cost=0.50, sid="S2")}, {"stdout": stream(cost=0.20, sid="S2")}]
        with FakeClaude(self.root / "fake", steps):
            s = make_backend("claude").open_session(self.root, allowed_tools=["Read"])
            s.send("a")
            child = s.fork()
            r2, r3, r4 = child.send("b"), child.send("c"), child.send("d")
        self.assertEqual(r2.extras["cost_basis"], "fork-unverified")
        self.assertAlmostEqual(r2.cost_usd, 0.20)                 # inherits the parent's last total
        self.assertEqual(r3.extras["cost_basis"], "delta")
        self.assertAlmostEqual(r3.cost_usd, 0.20)
        self.assertIsNone(r4.cost_usd)
        self.assertEqual(r4.extras["cost_basis"], "unknown: total decreased")


class OrphanTests(CaseTest):
    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_timeout_kills_the_whole_process_group(self):
        with FakeClaude(self.root / "fake", [{"sleep": 30}]) as fake:
            b = make_backend({"backend": "claude", "model": None, "timeout": 1})
            with self.assertRaisesRegex(BackendError, "timed out"):
                b.chat("s", MSGS)
        pid = int((Path(str(fake.log) + ".pid")).read_text())
        for _ in range(50):
            if not self.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(pid))


class SuiteSpendTests(CaseTest):
    def test_partial_run_cost_counts_when_quota_stops(self):
        case = load_case(make_case(self.root))

        def fake(case, v, s, **kw):
            e = QuotaError("out")
            e.run_path = self.root
            e.run_cost = {"subject": {"known_usd": 0.7, "calls": 1, "unknown_calls": 0}}
            raise e

        spend = Spend()
        cli.run_suite(case, ["baseline"], ["dana"], 2, run_fn=fake, out=lambda *_: None, spend=spend)
        self.assertAlmostEqual(spend.total, 0.7)

    def test_end_to_end_quota_after_a_paid_turn(self):
        path = make_case(self.root, max_turns=4, variants=("baseline",))
        path.write_text(path.read_text().replace('seat = "mock"', 'seat = "claude"', 1))
        steps = [{"stdout": chat_json("first reply", cost=0.2)}, {"stdout": "", "stderr": "usage limit reached", "exit": 1}]
        with FakeClaude(self.root / "fake", steps):
            code, out, _ = run_main(["suite", str(path), "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("spent: subject $0.2000", out)


class ExcludeTests(CaseTest):
    def test_gitignore_like_matching(self):
        yes = {"for-people/": ["for-people/a.md", "for-people/x/y.md"], "./for-people/**": ["for-people/a.md"],
               "*.tmp": ["a.tmp", "d/e/a.tmp"], "secret.md": ["secret.md", "a/b/secret.md"],
               "notes/": ["notes/a.md"], "docs/*.md": ["docs/a.md"]}
        no = {"for-people/": ["x/for-people-not.md", "for-people.md"], "docs/*.md": ["docs/sub/a.md", "x/docs/a.md"],
              "notes/": ["notes"]}
        for pat, rels in yes.items():
            for rel in rels:
                self.assertTrue(matches(rel, [pat]), (pat, rel))
        for pat, rels in no.items():
            for rel in rels:
                self.assertFalse(matches(rel, [pat]), (pat, rel))

    def test_unmatched_patterns_warn_and_are_recorded(self):
        case = load_case(make_agent_case(self.root, exclude=("for-people/**", "ghost/", "*.nothing")))
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))
        self.assertIn("the exclude pattern 'ghost/' matched nothing", buf.getvalue())
        self.assertNotIn("for-people/**' matched nothing", buf.getvalue())
        scenario = json.loads((res.path / "inputs" / "scenario.json").read_text())
        self.assertEqual(scenario["unmatched_excludes"], ["ghost/", "*.nothing"])


class GuardScopeTests(CaseTest):
    def test_work_folder_inside_the_case_is_refused(self):
        case = load_case(make_agent_case(self.root))
        inside = self.root / "tmp"
        inside.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(inside)}), mock.patch("tempfile.tempdir", str(inside)):
            with self.assertRaisesRegex(CaseError, "inside the case folder"):
                Workspace.create(case, "baseline")
        self.assertEqual(list(inside.iterdir()), [])                       # and it cleaned up

    def test_other_sims_files_are_guarded_too(self):
        path = make_agent_case(self.root)
        (self.root / "sims" / "sam.md").write_text("---\nname: sam\nknowledge: knowledge/sam.md\n---\n\nSam.\n")
        (self.root / "knowledge" / "sam.md").write_text("Sam owns a boat called Zephyr.\n")
        (self.root / "scenario" / "boat.txt").write_text("Sam owns a boat called Zephyr.\n")
        case = load_case(path)
        with self.assertRaisesRegex(CaseError, r"boat\.txt \(knowledge page\)"):
            execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))

    def test_frame_copy_is_guarded(self):
        case = load_case(make_agent_case(self.root))
        (self.root / "scenario" / "frame-copy.md").write_text(case.frame_text())
        with self.assertRaisesRegex(CaseError, r"frame-copy\.md \(sim frame\)"):
            execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))


class EstimateAndCapTests(LiveCaseTest):
    def test_at_least_when_a_seat_is_unknown(self):
        case = load_case(self.live("http://127.0.0.1:1/v1"))
        self.write_past_run("a", 0.10)
        self.write_past_run("b", 0.30)
        line = estimate_line(case, 5)
        self.assertIn("total at least $1.00 (plus unknown for sim)", line)

    def test_max_usd_must_be_a_positive_finite_number(self):
        path = self.live("http://127.0.0.1:1/v1")
        for bad in ("0", "-1", "nan", "inf"):
            code, _, err = run_main(["suite", str(path), "--dry-run", "--max-usd", bad])
            self.assertEqual(code, 2, bad)
            self.assertIn("--max-usd", err)


class ArtifactTests(CaseTest):
    def make(self):
        case = load_case(make_agent_case(self.root, scenario={"notes.md": "# Notes\n", "run.sh": "echo\n", "bin.dat": "x"}))
        os.chmod(self.root / "scenario" / "run.sh", 0o644)
        ws = Workspace.create(case, "baseline")
        self.addCleanup(ws.cleanup)
        run = self.root / "run"
        run.mkdir()
        return ws, run

    def test_mode_change_is_recorded(self):
        ws, run = self.make()
        os.chmod(ws.path / "run.sh", 0o755)
        diff = ws.write_artifacts(run, scrub, write_json)
        self.assertEqual(diff["changed"], [{"path": "run.sh", "mode": "0644 -> 0755"}])

    def test_added_text_files_are_in_the_patch_and_copied_byte_for_byte(self):
        ws, run = self.make()
        (ws.path / "review.md").write_bytes(b"line one\r\nline two\r\n")
        (ws.path / "notes.md").write_text("# Notes\nmore\n")
        ws.write_artifacts(run, scrub, write_json)
        self.assertEqual((run / "artifacts" / "files" / "review.md").read_bytes(), b"line one\r\nline two\r\n")
        patch = (run / "artifacts" / "diff.patch").read_text()
        self.assertIn("--- /dev/null", patch)
        self.assertIn("+++ b/review.md", patch)
        self.assertIn("+line one", patch)
        self.assertIn("--- a/notes.md", patch)

    def test_private_text_is_scrubbed_and_flagged(self):
        ws, run = self.make()
        (ws.path / "review.md").write_text(f"mail me@example.com from {Path.home()}/x\n")
        (ws.path / "clean.md").write_text("nothing private\n")
        diff = ws.write_artifacts(run, scrub, write_json)
        entries = {(e["path"] if isinstance(e, dict) else e): e for e in diff["added"]}
        self.assertEqual(entries["review.md"], {"path": "review.md", "scrubbed": True})
        self.assertEqual(entries["clean.md"], "clean.md")
        saved = (run / "artifacts" / "files" / "review.md").read_text()
        self.assertEqual(saved, "mail [email removed] from ~/x\n")
        self.assertNotIn("me@example.com", (run / "artifacts" / "diff.patch").read_text())

    def test_binary_with_private_data_is_withheld(self):
        ws, run = self.make()
        (ws.path / "dump.bin").write_bytes(b"\xff\xfe\x00" + str(Path.home()).encode() + b"\x00")
        (ws.path / "ok.bin").write_bytes(b"\xff\xfe\x00 fine")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-binary-key-1234"}):
            (ws.path / "key.bin").write_bytes(b"\xff\x00sk-binary-key-1234")
            diff = ws.write_artifacts(run, scrub, write_json)
        by = {(e["path"] if isinstance(e, dict) else e): e for e in diff["added"]}
        self.assertEqual(by["dump.bin"], {"path": "dump.bin", "withheld": "contains private data"})
        self.assertEqual(by["key.bin"], {"path": "key.bin", "withheld": "contains private data"})
        self.assertEqual(by["ok.bin"], "ok.bin")
        self.assertFalse((run / "artifacts" / "files" / "dump.bin").exists())
        self.assertTrue((run / "artifacts" / "files" / "ok.bin").exists())

    def test_run_artifact_bytes_and_binary(self):
        case = load_case(make_agent_case(self.root))
        res = execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))
        (res.path / "artifacts" / "files").mkdir(exist_ok=True)
        (res.path / "artifacts" / "files" / "pic.bin").write_bytes(b"\xff\xfe\x00")
        run = Run.load(res.path)
        self.assertIsNone(run.artifact("pic.bin"))
        self.assertEqual(run.artifact_bytes("pic.bin"), b"\xff\xfe\x00")
        self.assertIsNone(run.artifact_bytes("none.bin"))


if __name__ == "__main__":
    unittest.main()
