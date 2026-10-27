import json
import unittest

from helpers import CaseTest, build_runs, make_agent_case
from rehearse.board import WORK_CHECK, load_records, tally
from rehearse.case import load_case
from rehearse.checks import Run
from rehearse.runs import execute_run
from rehearse.workcheck import work_folder_check
from test_cli import run_main


class WorkFolderCheckTests(CaseTest):
    def agent(self, calls, write=None):
        j = json.dumps
        parts = ['text = "Done."', "tool_calls = [" + ", ".join(
            f"{{ name = {j(c['name'])}, arg = {j(c['arg'])} }}" for c in calls) + "]"]
        if write:
            parts.append("write = { " + ", ".join(f"{j(k)} = {j(v)}" for k, v in write.items()) + " }")
        script = "[{ " + ", ".join(parts) + ' }, "More?"]'
        case = load_case(make_agent_case(self.root, subject_toml=script))
        res = execute_run(case, "baseline", "dana")
        folder = res.path
        return Run.load(folder), folder

    def items(self, res):
        return {i["name"]: i["result"] for i in res["items"]}

    def test_outside_absolute_path_fails_and_voids(self):
        run, folder = self.agent([{"name": "Read", "arg": "/etc/passwd"}, {"name": "Read", "arg": "./a.md"}])
        res = work_folder_check(run)
        self.assertEqual(self.items(res)["paths_inside_work_folder"], "fail")
        self.assertIn("/etc/passwd", res["void_reason"])
        saved = json.loads((folder / "checks.json").read_text())
        self.assertTrue(saved["void"])
        self.assertEqual(saved["work_folder_check"]["result"], "fail")
        self.assertIn("paths_inside_work_folder", saved["void_reason"])

    def test_home_path_fails(self):
        run, _ = self.agent([{"name": "write", "arg": "~/notes.md"}], write={"x.md": "x"})
        self.assertEqual(self.items(work_folder_check(run))["paths_inside_work_folder"], "fail")

    def test_relative_paths_pass(self):
        run, _ = self.agent([{"name": "Read", "arg": "./x"}, {"name": "Glob", "arg": "docs/*.md"},
                             {"name": "Read", "arg": "x"}], write={"x.md": "x"})
        res = work_folder_check(run)
        self.assertEqual(res["result"], "pass")
        self.assertFalse(res["void"])

    def test_bash_absolute_path_is_partial(self):
        run, _ = self.agent([{"name": "Bash", "arg": "cat /etc/hosts && ls ./ && curl https://a.b/c"}])
        res = work_folder_check(run)
        self.assertEqual(self.items(res)["paths_inside_work_folder"], "partial")
        self.assertFalse(res["void"])
        self.assertNotIn("a.b", res["items"][0]["evidence"])

    def test_writes_with_empty_diff_fail(self):
        run, _ = self.agent([{"name": "Write", "arg": "./review.md"}])
        res = work_folder_check(run)
        self.assertEqual(self.items(res)["wrote_something_when_tools_wrote"], "fail")
        self.assertTrue(res["void"])

    def test_chat_run_skips(self):
        runs = build_runs(self.root)
        (folder,) = list(runs.iterdir())
        res = work_folder_check(Run.load(folder))
        self.assertEqual(res["result"], "skip")
        self.assertFalse(res["void"])
        self.assertEqual(json.loads((folder / "checks.json").read_text())["work_folder_check"]["result"], "skip")

    def test_board_column_and_void_exclusion(self):
        self.agent([{"name": "Read", "arg": "./a.md"}], write={"x.md": "x"})
        _, folder = self.agent([{"name": "Read", "arg": "/etc/passwd"}], write={"x.md": "x"})
        runs = folder.parent
        code, out, _ = run_main(["board", str(runs)])
        self.assertIn("| work folder |", out)
        self.assertIn("Voided runs (failed the sim check, the work folder check): 1", out)
        self.assertIn("void: paths_inside_work_folder", out)
        t = tally(load_records(str(runs)), WORK_CHECK)
        self.assertEqual((t["k"], t["fail"], t["void"]), (1, 1, 0))

    def test_chat_board_has_no_column(self):
        code, out, _ = run_main(["board", str(build_runs(self.root))])
        self.assertNotIn("work folder |", out)


if __name__ == "__main__":
    unittest.main()
