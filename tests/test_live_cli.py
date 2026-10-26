"""Live-command behaviour (estimate, asking first, budget cap) against a local fake server."""

import io
import json
import os
import unittest
from unittest import mock

from fakes import FakeClaude, FakeServer, chat_json, ok_body
from helpers import CaseTest, make_case
from rehearse import cli
from rehearse.case import load_case
from rehearse.cost import Spend, estimate, estimate_line, is_live
from test_cli import run_main


def table(url, **extra):
    items = {"backend": "http", "model": "m1", "base_url": url, **extra}
    return "{ " + ", ".join(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in items.items()) + " }"


class Tty(io.StringIO):
    def isatty(self):
        return True


class LiveCaseTest(CaseTest):
    def live(self, url, max_turns=1, **extra):
        path = make_case(self.root, max_turns=max_turns, variants=("baseline",))
        path.write_text(path.read_text().replace('seat = "mock"', "seat = " + table(url, **extra), 2))
        return path

    def write_past_run(self, name, subject_usd, sim_usd=None, case="tcase"):
        d = self.root / "runs" / name
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"case": case, "cost": {
            "subject": {"usd": subject_usd, "calls": 1, "unknown_calls": 0},
            "sim": {"usd": sim_usd, "calls": 1, "unknown_calls": 0 if sim_usd is not None else 1}}}))


class EstimateTests(LiveCaseTest):
    def test_no_past_runs(self):
        case = load_case(self.live("http://127.0.0.1:1/v1"))
        self.assertEqual(estimate_line(case, 4), "cost estimate: no past runs to estimate from")

    def test_mean_of_known_costs_times_runs_planned(self):
        case = load_case(self.live("http://127.0.0.1:1/v1"))
        self.write_past_run("a", 0.10)
        self.write_past_run("b", 0.30)
        self.write_past_run("other-case", 9.0, case="other")
        est = estimate(case, 5)
        self.assertEqual(est["based_on"], 2)
        self.assertAlmostEqual(est["seats"]["subject"], 1.0)
        self.assertIsNone(est["seats"]["sim"])
        line = estimate_line(case, 5)
        self.assertIn("from 2 past runs", line)
        self.assertIn("subject about $1.00", line)
        self.assertIn("sim unknown", line)

    def test_dry_run_prints_estimate_and_does_not_call(self):
        path = self.live("http://127.0.0.1:1/v1")
        self.write_past_run("a", 0.5)
        code, out, _ = run_main(["suite", str(path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("subject about $0.50", out)
        self.assertIn("seats: subject http:m1, sim http:m1", out)

    def test_is_live(self):
        self.assertTrue(is_live(load_case(self.live("http://127.0.0.1:1/v1"))))
        self.assertFalse(is_live(load_case(make_case(self.root / "m"))))


class ConfirmationTests(LiveCaseTest):
    def test_refuses_without_yes_or_terminal(self):
        with FakeServer() as srv:
            path = self.live(srv.url)
            code, out, err = run_main(["suite", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("Pass --yes", err)
        self.assertEqual(srv.requests, [])
        self.assertFalse((self.root / "runs").exists())

    def test_yes_runs_and_reports_spend(self):
        with FakeServer([ok_body("hi", 300_000, 300_000)]) as srv:
            path = self.live(srv.url, price_in_per_mtok=1.0, price_out_per_mtok=1.0)
            code, out, _ = run_main(["suite", str(path), "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(len(srv.requests), 1)
        self.assertIn("spent: subject $0.6000, sim not called", out)
        self.assertNotIn("PLUMBING ONLY", out)
        meta = json.loads(next((self.root / "runs").iterdir()).joinpath("meta.json").read_text())
        self.assertAlmostEqual(meta["cost"]["subject"]["usd"], 0.6)

    def test_terminal_answer_no_then_yes(self):
        with FakeServer() as srv:
            path = self.live(srv.url)
            with mock.patch("sys.stdin", Tty("n\n")):
                code, out, _ = run_main(["run", str(path), "--variant", "baseline", "--sim", "dana"])
            self.assertEqual(code, 1)
            self.assertIn("Start 1 runs? [y/N]", out)
            self.assertIn("cancelled", out)
            self.assertEqual(srv.requests, [])
            with mock.patch("sys.stdin", Tty("y\n")):
                code, out, _ = run_main(["run", str(path), "--variant", "baseline", "--sim", "dana"])
            self.assertEqual(code, 0)
            self.assertEqual(len(srv.requests), 1)

    def test_mock_runs_never_ask(self):
        path = make_case(self.root / "m")
        code, out, _ = run_main(["suite", str(path)])
        self.assertEqual(code, 0)
        self.assertNotIn("Start", out)
        self.assertNotIn("spent:", out)

    def test_dry_run_never_asks(self):
        path = self.live("http://127.0.0.1:1/v1")
        code, out, _ = run_main(["suite", str(path), "--dry-run"])
        self.assertEqual(code, 0)


class BudgetTests(LiveCaseTest):
    def test_stops_when_spent_reaches_the_cap(self):
        with FakeServer([ok_body("hi", 300_000, 300_000)]) as srv:
            path = self.live(srv.url, price_in_per_mtok=1.0, price_out_per_mtok=1.0)
            code, out, _ = run_main(["suite", str(path), "--yes", "--repeats", "3", "--max-usd", "1.0"])
        self.assertEqual(code, 1)
        self.assertEqual(len(srv.requests), 2)             # 0.6, then 1.2 >= 1.0
        self.assertIn("stopped: the budget of $1 is reached", out)
        self.assertIn("not run: baseline x dana (repeat 3): budget reached", out)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 2)

    def test_remaining_budget_and_flags_are_passed_to_each_run(self):
        case = load_case(make_case(self.root / "m"))
        seen = []

        def fake(case, v, s, **kw):
            seen.append(kw)
            return mock.Mock(path=self.root, stop="sim-stop", subject_turns=1,
                             cost={"subject": {"known_usd": 0.4, "unknown_calls": 0}}, mock_seats={})

        spend = Spend()
        cli.run_suite(case, ["baseline"], ["dana"], 3, run_fn=fake, out=lambda *_: None, max_usd=1.0,
                      spend=spend, keep_work=True)
        self.assertEqual([k["repeat"] for k in seen], [1, 2, 3])
        self.assertAlmostEqual(seen[0]["budget_usd"], 1.0)
        self.assertAlmostEqual(seen[1]["budget_usd"], 0.6)
        self.assertAlmostEqual(seen[2]["budget_usd"], 0.2)
        self.assertTrue(all(k["keep_work"] for k in seen))
        self.assertAlmostEqual(spend.total, 1.2)

    def test_unknown_cost_warns_once_when_capped(self):
        with FakeServer() as srv:                         # no prices, so the cost is unknown
            path = self.live(srv.url)
            code, out, _ = run_main(["suite", str(path), "--yes", "--repeats", "2", "--max-usd", "5"])
        self.assertEqual(out.count("warning: the subject seat does not report its cost"), 1)
        self.assertIn("spent: subject unknown, sim not called", out)

    def test_mock_seats_do_not_warn(self):
        path = make_case(self.root / "m")
        code, out, _ = run_main(["suite", str(path), "--max-usd", "5"])
        self.assertNotIn("warning", out)

    def test_budget_flag_reaches_the_claude_call(self):
        path = make_case(self.root / "c", max_turns=1, variants=("baseline",))
        path.write_text(path.read_text().replace('seat = "mock"', 'seat = "claude:sonnet"', 1))
        with FakeClaude(self.root / "fake", [{"stdout": chat_json("hello", cost=0.2)}]) as fake:
            code, out, _ = run_main(["suite", str(path), "--yes", "--max-usd", "3"])
        argv = fake.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3")
        self.assertIn("spent: subject $0.2000", out)

    def test_max_usd_must_be_positive(self):
        path = self.live("http://127.0.0.1:1/v1")
        code, _, err = run_main(["suite", str(path), "--yes", "--max-usd", "0"])
        self.assertEqual(code, 2)


class QuotaSuiteTests(CaseTest):
    def test_quota_text_from_claude_stops_the_suite(self):
        path = make_case(self.root, max_turns=2, variants=("baseline", "cheerful"))
        path.write_text(path.read_text().replace('seat = "mock"', 'seat = "claude"', 1))
        steps = [{"stdout": "", "stderr": "Claude usage limit reached. Resets 5pm", "exit": 1}]
        with FakeClaude(self.root / "fake", steps) as fake:
            code, out, _ = run_main(["suite", str(path), "--yes", "--repeats", "2"])
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("stopped:", out)
        self.assertEqual(out.count("not run:"), 3)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 1)       # the partial run was saved


class EndToEndMixedTests(CaseTest):
    def test_claude_sim_with_mock_subject(self):
        path = make_case(self.root, max_turns=3, variants=("baseline",), subject_replies=["a?", "b?", "c?"])
        text = path.read_text()
        head, sep, tail = text.partition("[sims]")
        path.write_text(head + sep + tail.replace('seat = "mock"', 'seat = "claude:haiku"', 1))
        with FakeClaude(self.root / "fake", [{"stdout": chat_json("fine", cost=0.01)},
                                             {"stdout": chat_json("DONE", cost=0.01)}]) as fake:
            code, out, _ = run_main(["suite", str(path), "--yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(fake.calls), 2)
        first = fake.calls[0]
        self.assertIn("MOOD-SENTINEL", first["system_file"]["content"])
        self.assertNotIn("VARIANT-SENTINEL", json.dumps(fake.calls))        # the sim never sees the variant
        self.assertNotIn("PLUMBING ONLY", out)


if __name__ == "__main__":
    unittest.main()
