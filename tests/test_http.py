import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from fakes import FakeServer, ok_body
from helpers import CaseTest, build_runs
from rehearse.backends import BackendError, Msg, QuotaError, SetupError, make_backend, parse_seat_spec
from rehearse.backends.keys import keys_file, read_key
from rehearse.case import CaseError, load_case
from rehearse.runs import execute_run

MSGS = [Msg("user", "hi")]


def http(url, **spec):
    return make_backend({"backend": "http", "model": "m1", "base_url": url, "key_env": "TEST_KEY",
                         **spec})


class SeatSpecTests(unittest.TestCase):
    def test_string_forms(self):
        s = parse_seat_spec("http:llama3:8b@http://localhost:11434/v1")
        self.assertEqual((s["backend"], s["model"], s["base_url"], s["key_env"]),
                         ("http", "llama3:8b", "http://localhost:11434/v1", "OPENAI_API_KEY"))
        s = parse_seat_spec("openai:gpt-x")
        self.assertEqual((s["base_url"], s["key_env"]), ("https://api.openai.com/v1", "OPENAI_API_KEY"))
        s = parse_seat_spec("openrouter:vendor/model:free")
        self.assertEqual((s["model"], s["base_url"], s["key_env"]),
                         ("vendor/model:free", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
        s = parse_seat_spec("deepseek")
        self.assertEqual((s["model"], s["base_url"], s["key_env"]),
                         ("deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY"))
        self.assertEqual(parse_seat_spec("deepseek:deepseek-reasoner")["model"], "deepseek-reasoner")
        self.assertEqual(parse_seat_spec("claude:sonnet")["backend"], "claude")
        self.assertIsNone(parse_seat_spec("claude")["model"])

    def test_table_form(self):
        s = parse_seat_spec({"backend": "http", "model": "m", "base_url": "http://x/v1", "key_env": "K",
                             "price_in_per_mtok": 1.5, "price_out_per_mtok": 3, "timeout": 30, "rpm": 10})
        self.assertEqual((s["price_in_per_mtok"], s["rpm"], s["key_env"]), (1.5, 10, "K"))
        s = parse_seat_spec({"backend": "deepseek", "price_in_per_mtok": 0.3})
        self.assertEqual((s["backend"], s["model"], s["price_in_per_mtok"]), ("http", "deepseek-chat", 0.3))
        self.assertEqual(parse_seat_spec({"backend": "claude", "model": "opus", "timeout": 60})["timeout"], 60)

    def test_bad_specs_say_why(self):
        for bad, text in (("gpt:4", "known backends are: mock, claude, http, openai, openrouter, deepseek"),
                          ("http:onlymodel", "http:MODEL@BASE_URL"),
                          ("openai", "needs a model"),
                          ({"backend": "http", "model": "m"}, "base_url"),
                          ({"backend": "http", "model": "m", "base_url": "u", "color": 1}, "unknown setting 'color'"),
                          ({"backend": "http", "model": "m", "base_url": "u", "rpm": "fast"}, "must be a number"),
                          ({"model": "m"}, "known backends")):
            with self.subTest(bad):
                with self.assertRaisesRegex(BackendError, text):
                    parse_seat_spec(bad)

    def test_describe_is_short(self):
        self.assertEqual(make_backend("deepseek").describe(), "http:deepseek-chat@api.deepseek.com")

    def test_case_accepts_table_seats(self):
        from helpers import make_case
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = make_case(Path(tmp))
            path.write_text(path.read_text().replace(
                'seat = "mock"\nopening', 'seat = { backend = "http", model = "m", base_url = "http://127.0.0.1:1/v1" }\nopening', 1))
            self.assertEqual(load_case(path).subject_seat["model"], "m")
            path.write_text(path.read_text().replace('"http", model = "m"', '"nope", model = "m"'))
            with self.assertRaisesRegex(CaseError, "known backends"):
                load_case(path)


class HttpBackendTests(CaseTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TEST_KEY", None)

    def test_request_shape_usage_and_localhost_without_key(self):
        with FakeServer([ok_body("Hi!", 11, 7, "served-model")]) as srv:
            b = http(srv.url)
            res = b.chat("SYS", [Msg("user", "a"), Msg("assistant", "b"), Msg("user", "c")])
        (req,) = srv.requests
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual([m["role"] for m in req["json"]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(req["json"]["messages"][0]["content"], "SYS")
        self.assertEqual(req["json"]["model"], "m1")
        self.assertNotIn("Authorization", req["headers"])
        self.assertEqual((res.text, res.tokens_in, res.tokens_out, res.model), ("Hi!", 11, 7, "served-model"))
        self.assertIsNone(res.cost_usd)
        self.assertIn('"prompt_tokens"', res.raw["body"])

    def test_prices_give_cost(self):
        with FakeServer([ok_body("x", 2_000_000, 1_000_000)]) as srv:
            res = http(srv.url, price_in_per_mtok=1.0, price_out_per_mtok=4.0).chat("s", MSGS)
        self.assertAlmostEqual(res.cost_usd, 6.0)

    def test_one_price_is_not_enough(self):
        with FakeServer() as srv:
            self.assertIsNone(http(srv.url, price_in_per_mtok=1.0).chat("s", MSGS).cost_usd)

    def test_key_sent_when_set(self):
        with mock.patch.dict(os.environ, {"TEST_KEY": "sk-abcdef123456"}), FakeServer() as srv:
            http(srv.url).chat("s", MSGS)
        self.assertEqual(srv.requests[0]["headers"]["Authorization"], "Bearer sk-abcdef123456")

    def test_remote_without_key_is_setup_error(self):
        b = http("https://api.invalid/v1")
        with self.assertRaisesRegex(SetupError, "TEST_KEY"):
            b.chat("s", MSGS)

    def test_retries_on_429_and_500(self):
        replies = [(429, {"Retry-After": "0"}, "slow down"), (500, {}, "oops"), ok_body("finally")]
        with FakeServer(replies) as srv:
            b = http(srv.url)
            b.backoff = 0.01
            res = b.chat("s", MSGS)
        self.assertEqual(res.text, "finally")
        self.assertEqual(len(srv.requests), 3)

    def test_gives_up_after_the_attempts(self):
        with FakeServer([(503, {"Retry-After": "0"}, "down")]) as srv:
            b = http(srv.url)
            b.attempts = 3
            with self.assertRaisesRegex(BackendError, "HTTP 503"):
                b.chat("s", MSGS)
        self.assertEqual(len(srv.requests), 3)

    def test_quota_and_key_errors(self):
        with FakeServer([(402, {}, "pay up")]) as srv:
            with self.assertRaises(QuotaError):
                http(srv.url).chat("s", MSGS)
        with FakeServer([(429, {}, "free-models-per-day exceeded")]) as srv:
            with self.assertRaises(QuotaError):
                http(srv.url).chat("s", MSGS)
        with FakeServer([(401, {}, "bad key")]) as srv:
            with self.assertRaises(SetupError):
                http(srv.url).chat("s", MSGS)

    def test_other_client_errors_do_not_retry(self):
        with FakeServer([(400, {}, "bad request")]) as srv:
            with self.assertRaisesRegex(BackendError, "HTTP 400"):
                http(srv.url).chat("s", MSGS)
        self.assertEqual(len(srv.requests), 1)

    def test_unreadable_reply_keeps_raw_body(self):
        with FakeServer([(200, {}, "<html>not json</html>")]) as srv:
            with self.assertRaises(BackendError) as cm:
                http(srv.url).chat("s", MSGS)
        self.assertEqual(cm.exception.raw["body"], "<html>not json</html>")
        with FakeServer([(200, {}, '{"choices": []}')]) as srv:
            with self.assertRaises(BackendError) as cm:
                http(srv.url).chat("s", MSGS)
        self.assertEqual(cm.exception.raw["body"], '{"choices": []}')

    def test_raw_body_reaches_the_run_folder_on_parse_failure(self):
        with FakeServer([(200, {}, "garbled body")]) as srv:
            case = self.live_case(srv.url)
            res = execute_run(case, "baseline", "dana")
        import json
        raw = json.loads((res.path / "raw" / "subject-01.json").read_text())
        self.assertEqual(raw["result"]["raw"]["body"], "garbled body")
        self.assertTrue(res.stop.startswith("error:"))

    def live_case(self, url, max_turns=2, **seat):
        import json
        from helpers import make_case
        path = make_case(self.root / "c", max_turns=max_turns)
        toml = path.read_text().replace('seat = "mock"', "seat = " + _toml_table(seat, url), 2)
        path.write_text(toml)
        return load_case(path)


def _toml_table(seat, url):
    items = {"backend": "http", "model": "m1", "base_url": url, **seat}
    return "{ " + ", ".join(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in items.items()) + " }"


class KeysTests(CaseTest):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        (self.home / ".config" / "rehearse").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TEST_KEY", None)
        self.file = keys_file()

    def write(self, text, mode=0o600):
        self.file.write_text(text)
        self.file.chmod(mode)

    def test_file_with_comments(self):
        self.write("# my keys\n\nOTHER=zzzzzzzz\nTEST_KEY = \"abc-12345678\"\n")
        self.assertEqual(read_key("TEST_KEY"), "abc-12345678")
        self.assertEqual(read_key("MISSING"), "")

    def test_environment_wins(self):
        self.write("TEST_KEY=from-file-1234\n")
        with mock.patch.dict(os.environ, {"TEST_KEY": "from-env-12345"}):
            self.assertEqual(read_key("TEST_KEY"), "from-env-12345")

    def test_refuses_readable_file(self):
        for mode in (0o640, 0o604, 0o644):
            self.write("TEST_KEY=abc-12345678\n", mode)
            with self.assertRaisesRegex(SetupError, "chmod 600 .*rehearse/keys"):
                read_key("TEST_KEY")
        self.write("TEST_KEY=abc-12345678\n", 0o600)
        self.assertEqual(read_key("TEST_KEY"), "abc-12345678")

    def test_file_not_read_when_env_has_the_key(self):
        self.write("TEST_KEY=x\n", 0o644)
        with mock.patch.dict(os.environ, {"TEST_KEY": "from-env-12345"}):
            self.assertEqual(read_key("TEST_KEY"), "from-env-12345")

    def test_key_never_reaches_a_run_folder(self):
        secret = "sk-SECRET-VALUE-9876543210"
        echo = ok_body(f"my key is {secret} and mail me@example.com")
        with mock.patch.dict(os.environ, {"TEST_KEY": secret}), FakeServer([echo]) as srv:
            from helpers import make_case
            path = make_case(self.root / "c", max_turns=2)
            seat = ('{ backend = "http", model = "m1", base_url = "%s", key_env = "TEST_KEY" }' % srv.url)
            path.write_text(path.read_text().replace('seat = "mock"', "seat = " + seat, 2))
            res = execute_run(load_case(path), "baseline", "dana")
        self.assertGreater(len(srv.requests), 0)
        files = [p for p in res.path.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 8)
        for p in files:
            self.assertNotIn(secret, p.read_text(), p.name)
        self.assertIn("[key removed]", (res.path / "transcript.md").read_text())


if __name__ == "__main__":
    unittest.main()
