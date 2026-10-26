import unittest

from helpers import CaseTest, make_case
from rehearse.backends import BackendError, make_backend, parse_seat_spec
from rehearse.case import CaseError, load_case


class CaseTests(CaseTest):
    def load_with(self, old, new):
        path = make_case(self.root)
        path.write_text(path.read_text().replace(old, new))
        return load_case(path)

    def test_loads_folder_or_file(self):
        make_case(self.root, sims=("dana", "sam"))
        a = load_case(self.root)
        b = load_case(self.root / "case.toml")
        self.assertEqual(a.name, "tcase")
        self.assertEqual(list(a.sims), ["dana", "sam"])
        self.assertEqual(a.variants.keys(), b.variants.keys())

    def test_missing_case(self):
        with self.assertRaisesRegex(CaseError, "no case.toml"):
            load_case(self.root)

    def test_errors_are_plain(self):
        cases = [
            ('name = "tcase"', "", "needs a 'name'"),
            ('adapter = "chat"', 'adapter = "bogus"', "not built yet"),
            ('seat = "mock"\nopening', 'seat = "nope"\nopening', "known backends are: mock"),
            ('max_turns = 6', 'max_turns = 0', "max_turns"),
            ('[variants]', '[variants]\nghost = "variants/ghost.md"', "file not found"),
            ('dir = "sims"', 'dir = "nowhere"', "dir not found"),
            ('[limits]', '[frame]\n[limits]\n[sims.x]', None),
        ]
        for old, new, msg in cases[:-1]:
            with self.subTest(msg):
                with self.assertRaisesRegex(CaseError, msg):
                    self.load_with(old, new)

    def test_bad_regex_and_bad_toml(self):
        with self.assertRaisesRegex(CaseError, "stop_pattern"):
            path = make_case(self.root, stop_pattern="(")
            load_case(path)
        (self.root / "case.toml").write_text("name = ")
        with self.assertRaisesRegex(CaseError, "not valid TOML"):
            load_case(self.root)

    def test_missing_sections(self):
        (self.root / "case.toml").write_text('name = "x"\n')
        with self.assertRaisesRegex(CaseError, r"\[subject\]"):
            load_case(self.root)

    def test_persona_knowledge_missing(self):
        make_case(self.root)
        (self.root / "knowledge" / "dana.md").unlink()
        case = load_case(self.root)
        with self.assertRaisesRegex(CaseError, "knowledge file not found"):
            case.persona("dana")

    def test_shipped_frame_has_placeholders(self):
        frame = load_case(make_case(self.root)).frame_text()
        for p in ("{persona}", "{mood}", "{knowledge}", "{stop}", "{turn_line}"):
            self.assertIn(p, frame)

    def test_seat_spec(self):
        self.assertEqual(parse_seat_spec("mock")["backend"], "mock")
        self.assertEqual(parse_seat_spec("mock:x")["model"], "x")
        with self.assertRaisesRegex(BackendError, "known backends are: mock, claude, http"):
            parse_seat_spec("gpt:4")
        self.assertTrue(make_backend("mock").describe().startswith("mock"))


if __name__ == "__main__":
    unittest.main()
