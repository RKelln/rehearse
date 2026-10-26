import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeClaude, stream
from helpers import CaseTest, make_agent_case, make_case
from rehearse.adapters import AgentAdapter, ChatAdapter
from rehearse.backends import BackendError, MockBackend, make_backend
from rehearse.case import CaseError, load_case
from rehearse.checks import Run
from rehearse.runs import execute_run
from rehearse.workspace import Workspace, diff_hashes, excluded, hash_tree
from test_cli import run_main


def work_dirs():
    return {p for p in Path(tempfile.gettempdir()).glob("rehearse-work-*")}


class GlobTests(unittest.TestCase):
    def test_exclude_patterns(self):
        pats = ["for-people/**", ".facilitator", "*.tmp", "docs/**/draft.md"]
        yes = ["for-people/a.md", "for-people/deep/b.md", ".facilitator/x", "a.tmp", "docs/draft.md",
               "docs/a/b/draft.md", "sub/a.tmp", "deep/.facilitator/y"]
        no = ["notes.md", "docs/final.md", "people/a.md", "for-people.md", "sub/for-people/x.md"]
        for rel in yes:
            self.assertTrue(excluded(rel, pats), rel)
        for rel in no:
            self.assertFalse(excluded(rel, pats), rel)

    def test_diff_hashes(self):
        d = diff_hashes({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "d": "4"})
        self.assertEqual(d, {"added": ["d"], "changed": ["b"], "removed": ["c"]})


class AgentRunTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.before_dirs = work_dirs()

    def new_dirs(self):
        return work_dirs() - self.before_dirs

    def test_end_to_end_diff_and_artifacts(self):
        script = ('[{ text = "Wrote it.", write = { "review.md" = "# Review\\nline\\n", "notes.md" = "# Notes\\n- Changed\\n" } }, "More?"]')
        case = load_case(make_agent_case(self.root, subject_toml=script))
        res = execute_run(case, "baseline", "dana")
        self.assertEqual(res.stop, "sim-stop")
        art = res.path / "artifacts"
        diff = json.loads((art / "diff.json").read_text())
        self.assertEqual(diff, {"added": ["review.md"], "changed": ["notes.md"], "removed": []})
        self.assertEqual((art / "files" / "review.md").read_text(), "# Review\nline\n")
        patch = (art / "diff.patch").read_text()
        self.assertIn("--- a/notes.md", patch)
        self.assertIn("-- Finish the report", patch)
        self.assertIn("+- Changed", patch)
        run = Run.load(res.path, case)
        self.assertEqual(run.diff, diff)
        self.assertEqual(run.artifact("review.md"), "# Review\nline\n")
        self.assertEqual(run.tool_calls, [{"name": "Write", "arg": "review.md"}, {"name": "Write", "arg": "notes.md"}])
        self.assertIn("Finish the report", run.scenario_file("notes.md"))
        self.assertIn("facilitator only", run.scenario_file("for-people/secret.md"))   # the original, not the copy
        self.assertIsNone(run.scenario_file("nope.md"))
        meta = json.loads((res.path / "meta.json").read_text())
        self.assertEqual(meta["adapter"], "agent")

    def test_work_copy_is_outside_the_case_and_removed(self):
        case = load_case(make_agent_case(self.root))
        backend = MockBackend(["Hi", "More?"])
        res = execute_run(case, "baseline", "dana", backend, MockBackend(["DONE"]))
        workdir = Path(backend.session_calls[0]["workdir"])
        self.assertTrue(workdir.name.startswith("rehearse-work-"))
        self.assertNotIn(self.root.resolve(), workdir.resolve().parents)
        self.assertFalse(workdir.exists())
        self.assertEqual(self.new_dirs(), set())
        self.assertEqual({s["session_id"] for s in backend.session_calls}, {"mock-session-1"})

    def test_keep_work(self):
        case = load_case(make_agent_case(self.root))
        backend = MockBackend(["Hi"])
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            execute_run(case, "baseline", "dana", backend, MockBackend(["DONE"]), keep_work=True)
        workdir = Path(backend.session_calls[0]["workdir"])
        self.addCleanup(lambda: __import__("shutil").rmtree(workdir, ignore_errors=True))
        self.assertTrue((workdir / "notes.md").exists())
        self.assertIn(str(workdir), buf.getvalue())

    def test_excluded_files_never_reach_the_work_copy(self):
        case = load_case(make_agent_case(self.root))
        ws = Workspace.create(case, "baseline")
        try:
            self.assertEqual(sorted(hash_tree(ws.path)), ["notes.md"])
            self.assertEqual(sorted(ws.scenario), ["notes.md"])
        finally:
            ws.cleanup()

    def test_overlay_is_laid_over_and_recorded(self):
        case = load_case(make_agent_case(self.root))
        ws = Workspace.create(case, "plain")
        try:
            self.assertEqual(sorted(hash_tree(ws.path)), ["CLAUDE.md", "notes.md"])
            self.assertEqual(sorted(ws.overlay), ["CLAUDE.md"])
        finally:
            ws.cleanup()
        res = execute_run(case, "plain", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))
        self.assertEqual((res.path / "inputs" / "variant" / "CLAUDE.md").read_text(), "Keep replies short.\n")
        manifest = json.loads((res.path / "inputs" / "manifest.json").read_text())
        self.assertIn("variant/CLAUDE.md", manifest)
        scenario = json.loads((res.path / "inputs" / "scenario.json").read_text())
        self.assertEqual(list(scenario["files"]), ["notes.md"])
        self.assertEqual(scenario["exclude"], ["for-people/**"])
        self.assertEqual(len(scenario["files"]["notes.md"]), 64)
        self.assertFalse((res.path / "inputs" / "variant.md").exists())
        self.assertEqual(json.loads((res.path / "artifacts" / "diff.json").read_text())["added"], [])

    def test_baseline_without_overlay_has_no_variant_inputs(self):
        case = load_case(make_agent_case(self.root))
        res = execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))
        self.assertFalse((res.path / "inputs" / "variant").exists())

    def test_agent_variant_must_be_a_folder(self):
        path = make_agent_case(self.root)
        (self.root / "afile.md").write_text("x")
        path.write_text(path.read_text().replace('plain = "variants/plain"', 'plain = "afile.md"'))
        with self.assertRaisesRegex(CaseError, "folder not found"):
            load_case(path)


class IsolationGuardTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.before = work_dirs()

    def run_case(self, scenario, exclude=("for-people/**",)):
        case = load_case(make_agent_case(self.root, scenario=scenario, exclude=exclude))
        return execute_run(case, "baseline", "dana", MockBackend(["Hi"]), MockBackend(["DONE"]))

    def test_copy_of_knowledge_page_under_another_name(self):
        with self.assertRaisesRegex(CaseError, r"harmless\.md \(knowledge page\)"):
            self.run_case({"harmless.md": "Dana has two tasks.\n"})
        self.assertFalse((self.root / "runs").exists())
        self.assertEqual(work_dirs() - self.before, set())

    def test_copy_of_persona_file(self):
        persona = (self.root / "sims" / "dana.md")
        make_agent_case(self.root)                   # writes the persona; copy its exact text into the scenario
        text = persona.read_text()
        with self.assertRaisesRegex(CaseError, "persona file"):
            self.run_case({"docs/p.md": text})

    def test_same_file_name(self):
        with self.assertRaisesRegex(CaseError, "same name"):
            self.run_case({"anywhere/dana.md": "different words"})

    def test_excluded_leak_is_fine(self):
        res = self.run_case({"notes.md": "ok", "for-people/dana.md": "Dana has two tasks.\n"})
        self.assertEqual(res.stop, "sim-stop")

    def test_message_names_the_fix(self):
        with self.assertRaisesRegex(CaseError, "Add them to \\[scenario\\] exclude"):
            self.run_case({"harmless.md": "Dana has two tasks.\n"}, exclude=())


class AdapterCapabilityTests(CaseTest):
    def test_agent_adapter_needs_agent_and_resume(self):
        http = make_backend({"backend": "http", "model": "m", "base_url": "http://127.0.0.1:1/v1"})
        with self.assertRaisesRegex(BackendError, "cannot run an agent subject"):
            AgentAdapter(http, self.root)

        class NoResume(MockBackend):
            capabilities = frozenset({"chat", "agent"})
        with self.assertRaisesRegex(BackendError, "lacks 'resume'"):
            AgentAdapter(NoResume(), self.root)

    def test_chat_adapter_works_with_http(self):
        http = make_backend({"backend": "http", "model": "m", "base_url": "http://127.0.0.1:1/v1"})
        self.assertIsInstance(ChatAdapter(http, "sys"), ChatAdapter)

    def test_open_session_on_chat_only_backend(self):
        http = make_backend({"backend": "http", "model": "m", "base_url": "http://127.0.0.1:1/v1"})
        with self.assertRaisesRegex(BackendError, "cannot run an agent subject"):
            http.open_session(self.root)

    def test_cli_reports_it_plainly(self):
        path = make_agent_case(self.root, seat='{ backend = "http", model = "m", base_url = "http://127.0.0.1:1/v1" }')
        code, _, err = run_main(["run", str(path), "--variant", "baseline", "--sim", "dana", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("cannot run an agent subject", err)

    def test_mock_session_fork_and_script_safety(self):
        b = MockBackend([{"text": "x", "write": {"../escape.md": "no"}}])
        s = b.open_session(self.root)
        with self.assertRaisesRegex(BackendError, "outside the work folder"):
            s.send("hi")
        child = s.fork()
        self.assertNotEqual(child.session_id, s.session_id)


class ClaudeAgentEndToEndTests(CaseTest):
    def test_claude_subject_runs_in_the_work_copy_with_tools_on_the_command_line(self):
        case = load_case(make_agent_case(self.root, seat='"claude:opus"'))
        steps = [{"stdout": stream("Wrote it.", cost=0.10, sid="S1", tools=[("Write", {"file_path": "review.md"})])},
                 {"stdout": stream("Anything else?", cost=0.25, sid="S1")}]
        with FakeClaude(self.root / "fake", steps) as fake:
            res = execute_run(case, "plain", "dana", sim_backend=MockBackend(["ok", "DONE"]))
        first, second = fake.calls
        self.assertEqual(first["listing"], ["CLAUDE.md", "notes.md"])           # excluded folder absent
        self.assertNotIn("--resume", first["argv"])
        self.assertIn("S1", second["argv"])
        i = first["argv"].index("--allowedTools")
        self.assertEqual(first["argv"][i + 1:], ["Read", "Write(review.md)"])
        self.assertEqual(first["stdin"], "Please write my review.")
        self.assertEqual(second["stdin"], "ok")
        meta = json.loads((res.path / "meta.json").read_text())
        self.assertAlmostEqual(meta["cost"]["subject"]["usd"], 0.25)           # 0.10, then 0.25 - 0.10 = 0.15
        self.assertEqual(meta["seats"]["subject"], "claude:opus")
        self.assertFalse(meta["mock"])
        raw = json.loads((res.path / "raw" / "subject-02.json").read_text())
        self.assertEqual(raw["result"]["extras"]["cost_basis"], "delta")
        self.assertIn("stdout", raw["result"]["raw"])
        run = Run.load(res.path, case)
        self.assertEqual(run.tool_calls, [{"name": "Write", "arg": "review.md"}])


if __name__ == "__main__":
    unittest.main()
