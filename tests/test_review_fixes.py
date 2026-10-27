"""Tests for the step 1 review fixes: scrubbing, snapshots, exit codes, capabilities, frames."""

import json
import unittest
from pathlib import Path
from unittest import mock

from helpers import TODAY, CaseTest, make_case
from rehearse import cli
from rehearse.backends import Backend, BackendError, MockBackend, QuotaError, SeatResult, make_backend
from rehearse.adapters import ChatAdapter
from rehearse.case import CaseError, load_case
from rehearse.runs import execute_run, scrub, scrub_obj, write_json
from test_cli import run_main


class ScrubFixTests(CaseTest):
    def test_home_prefix_only_matches_whole_path(self):
        with mock.patch.object(Path, "home", return_value=Path("/home/u")):
            self.assertEqual(scrub("/home/u2/x and /home/u/x and /home/u,"), "/home/u2/x and ~/x and ~,")
            self.assertEqual(scrub("It is in /home/u. And /home/u.old"), "It is in ~. And /home/u.old")

    def test_resolved_home_also_scrubbed(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real)
        with mock.patch.object(Path, "home", return_value=link):
            self.assertEqual(scrub(f"{real}/a and {link}/b"), "~/a and ~/b")

    def test_json_scrubbed_before_encoding(self):
        p = self.root / "x.json"
        write_json(p, {"a": ["me@example.com", {"k": f"{Path.home()}/q"}], "n": 3})
        data = json.loads(p.read_text())
        self.assertEqual(data, {"a": ["[email removed]", {"k": "~/q"}], "n": 3})
        self.assertEqual(scrub_obj(("a@b.co",)), ["[email removed]"])


class SnapshotAndMetaTests(CaseTest):
    def test_inputs_are_what_the_run_used(self):
        case = load_case(make_case(self.root))
        variant = self.root / "variants" / "baseline.md"
        original = variant.read_text()

        class Editing(MockBackend):
            def chat(self, system, messages):
                variant.write_text("CHANGED DURING THE RUN")
                return super().chat(system, messages)

        res = execute_run(case, "baseline", "dana", Editing(["a"]), MockBackend(["DONE"]), today=TODAY)
        self.assertEqual((res.path / "inputs" / "variant.md").read_text(), original)

    def test_turns_jsonl_and_new_meta_fields(self):
        case = load_case(make_case(self.root, stop_pattern="(?i)bye", max_turns=4,
                                   subject_replies=["a", "b"], sim_replies=["x", "ok, DONE"]))
        res = execute_run(case, "baseline", "dana", today=TODAY, repeat=3)
        rows = [json.loads(l) for l in (res.path / "turns.jsonl").read_text().splitlines()]
        self.assertEqual([(r["side"], r["n"]) for r in rows],
                         [("person", 0), ("subject", 1), ("person", 1), ("subject", 2), ("person", 2)])
        self.assertEqual(rows[1]["call"], "subject-01.json")
        self.assertTrue((res.path / "raw" / rows[1]["call"]).exists())
        m = json.loads((res.path / "meta.json").read_text())
        self.assertEqual((m["repeat"], m["folder_number"]), (3, 1))
        self.assertEqual((m["max_turns"], m["subject_stop_pattern"], m["sim_stop_word"]), (4, "(?i)bye", "DONE"))
        self.assertTrue(m["stop_with_text"])

    def test_unexpected_error_saves_run(self):
        case = load_case(make_case(self.root))
        res = execute_run(case, "baseline", "dana", MockBackend([OSError("nope")]), MockBackend(), today=TODAY)
        self.assertEqual(res.stop, "error: OSError: nope")
        self.assertTrue((res.path / "meta.json").exists())

    def test_interrupt_saves_then_reraises(self):
        case = load_case(make_case(self.root))
        with self.assertRaises(KeyboardInterrupt):
            execute_run(case, "baseline", "dana", MockBackend(["a", KeyboardInterrupt()]), MockBackend(), today=TODAY)
        (run,) = (self.root / "runs").iterdir()
        self.assertEqual(json.loads((run / "meta.json").read_text())["stop_reason"], "interrupted")


class BackendInterfaceTests(CaseTest):
    def test_is_mock_and_capabilities(self):
        self.assertFalse(Backend.is_mock)
        self.assertTrue(MockBackend().is_mock)
        self.assertEqual(MockBackend().capabilities, frozenset({"chat", "agent", "resume", "fork"}))

    def test_adapter_requires_chat(self):
        class NoChat(Backend):
            capabilities = frozenset({"agent"})
        with self.assertRaisesRegex(BackendError, "cannot do 'chat'"):
            ChatAdapter(NoChat(), "x")

    def test_make_backend_opts(self):
        b = make_backend("mock", replies=["hi"], default="d", unused=1)
        self.assertEqual(b.chat("", []).text, "hi")
        self.assertEqual(b.chat("", []).text, "d")

    def test_seat_result_defaults(self):
        r = SeatResult("x")
        self.assertEqual((r.extras, r.tool_calls), ({}, []))

    def test_run_is_not_mock_if_one_seat_is_not(self):
        class Live(MockBackend):
            is_mock = False
        case = load_case(make_case(self.root))
        res = execute_run(case, "baseline", "dana", Live(["a"]), MockBackend(["DONE"]), today=TODAY)
        self.assertFalse(res.mock)


class CliFixTests(CaseTest):
    def test_error_stop_exits_1(self):
        make_case(self.root, subject_replies=[], sim_replies=[])
        case = load_case(self.root)
        with mock.patch("rehearse.cli.execute_run",
                        side_effect=lambda c, v, s, repeat=1: execute_run(
                            c, v, s, MockBackend([BackendError("boom")]), MockBackend(), repeat=repeat)):
            code, out, _ = run_main(["run", str(self.root), "--variant", "baseline", "--sim", "dana"])
        self.assertEqual(code, 1)
        self.assertIn("error: boom", out)

    def test_clean_run_exits_0(self):
        make_case(self.root, subject_replies=["a"], sim_replies=["DONE"])
        code, _, _ = run_main(["run", str(self.root), "--variant", "baseline", "--sim", "dana"])
        self.assertEqual(code, 0)

    def test_dry_run_validates_personas(self):
        make_case(self.root)
        (self.root / "knowledge" / "dana.md").unlink()
        code, _, err = run_main(["suite", str(self.root), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("knowledge file not found", err)

    def test_custom_frame_must_keep_placeholders(self):
        path = make_case(self.root)
        (self.root / "frame.md").write_text("{persona} {knowledge} {stop}")
        path.write_text(path.read_text().replace('dir = "sims"', 'dir = "sims"\nframe = "frame.md"'))
        with self.assertRaisesRegex(CaseError, r"missing \{opening\}"):
            load_case(path)
        (self.root / "frame.md").write_text("{persona} {knowledge} {stop} {opening}")
        self.assertIsNotNone(load_case(path))

    def test_quota_after_saved_run_lists_unrun_cells_once(self):
        case = load_case(make_case(self.root, subject_replies=[]))
        n = []

        def fake(case, v, s, repeat=1):
            n.append(1)
            if len(n) == 2:
                return execute_run(case, v, s, MockBackend([QuotaError("limit")]), MockBackend(), repeat=repeat)
            return execute_run(case, v, s, repeat=repeat)

        lines = []
        _, stopped = cli.run_suite(case, ["baseline", "cheerful"], ["dana"], 2, run_fn=fake, out=lines.append)
        self.assertTrue(stopped)
        unrun = [l for l in lines if l.startswith("not run")]
        self.assertEqual(unrun, ["not run: cheerful x dana (repeat 1)", "not run: cheerful x dana (repeat 2)"])
        self.assertEqual(len(set(unrun)), len(unrun))
        self.assertTrue(any("partial run saved" in l for l in lines))
        self.assertEqual(len(list((self.root / "runs").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
