import doctest
import unittest

from rehearse import parse
from rehearse.parse import column, minutes, quoted, section, tables, weekday

DOC = """# Weekly review

## 1. Summary
All good.

## 5) Next Steps!
Do X.
### Detail
More.

## Other
Y
"""


class SectionTests(unittest.TestCase):
    def test_finds_ignoring_case_numbers_punctuation(self):
        self.assertEqual(section(DOC, "next steps"), "Do X.\n### Detail\nMore.")
        self.assertEqual(section(DOC, "SUMMARY"), "All good.")

    def test_any_title_and_missing(self):
        self.assertEqual(section(DOC, "nothing", "other"), "Y")
        self.assertIsNone(section(DOC, "absent"))

    def test_runs_to_same_or_higher_level(self):
        self.assertEqual(section(DOC, "detail"), "More.")
        self.assertTrue(section(DOC, "weekly review").endswith("Y"))


class TableTests(unittest.TestCase):
    def test_basic_and_normalised_headers(self):
        md = "| **Day**  | Planned   Minutes |\n|:--|--:|\n| Tue | 30 |\n| Wed | 45 |\n"
        (t,) = tables(md)
        self.assertEqual(t, [{"day": "Tue", "planned minutes": "30"}, {"day": "Wed", "planned minutes": "45"}])

    def test_no_outer_pipes(self):
        (t,) = tables("Day | Min\n--- | ---\nTue | 30\n")
        self.assertEqual(t, [{"day": "Tue", "min": "30"}])

    def test_missing_header(self):
        (t,) = tables("| Tue | 30 |\n| Wed | 45 |\n")
        self.assertEqual(t, [{"1": "Tue", "2": "30"}, {"1": "Wed", "2": "45"}])
        (t,) = tables("|---|---|\n| Tue | 30 |\n")
        self.assertEqual(t, [{"1": "Tue", "2": "30"}])

    def test_uneven_rows(self):
        (t,) = tables("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n")
        self.assertEqual(t, [{"a": "1"}, {"a": "1", "b": "2", "3": "3"}])

    def test_two_tables_and_prose_between(self):
        md = "| a |\n|---|\n| 1 |\n\nTotal: 90 minutes.\n\n| b | c |\n|---|---|\n| 2 | 3 |\n"
        self.assertEqual(tables(md), [[{"a": "1"}], [{"b": "2", "c": "3"}]])

    def test_day_off_row(self):
        md = "| Day | Task | Minutes |\n|---|---|---|\n| Mon | Report | 60 |\n| Fri | Day off ... | 0 |\n"
        (t,) = tables(md)
        self.assertEqual(minutes(column(t[1], "min")), 0)
        self.assertEqual(column(t[1], "task"), "Day off ...")

    def test_no_tables(self):
        self.assertEqual(tables("just words | with a pipe? no"), [[{"1": "just words", "2": "with a pipe? no"}]])
        self.assertEqual(tables("nothing here"), [])

    def test_column_order_differs(self):
        a, b = tables("| Minutes | Day |\n|--|--|\n| 30 | Tue |\n\n| Day | Minutes |\n|--|--|\n| Tue | 30 |\n")
        self.assertEqual(column(a[0], "day"), column(b[0], "day"))
        self.assertEqual(column(a[0], "minutes"), "30")
        self.assertIsNone(column(a[0], "nope"))
        self.assertEqual(column(a[0], "nope", "DAY"), "Tue")


class WeekdayTests(unittest.TestCase):
    def test_forms(self):
        cases = {"Tue 27 (evening)": "Tue", "Tuesday": "Tue", "Tues": "Tue", "Thu Oct 29": "Thu",
                 "sat": "Sat", "Saturday morning leak": "Sat", "Wednesday": "Wed", "Thurs": "Thu",
                 "Mon-Fri": "Mon", "Sunday": "Sun", "friday night": "Fri"}
        for text, want in cases.items():
            self.assertEqual(weekday(text), want, text)

    def test_no_false_matches(self):
        for text in ("next month", "Monica called", "monday-ish".replace("monday", "monitor"), "", "Sunny", "Satellite"):
            self.assertIsNone(weekday(text), text)

    def test_first_one_wins(self):
        self.assertEqual(weekday("move it from Fri to Mon"), "Fri")


class MinutesTests(unittest.TestCase):
    def test_forms(self):
        cases = {"90": 90, "90 min": 90, "1.5 h": 90, "1h 30m": 90, "2 hours": 120, "45 minutes": 45,
                 "—": None, "": None, "1 hr 15 min": 75, "**30**": 30, "30 mins": 30, "about nothing": None,
                 "Tue 27": None, "0": 0}
        for text, want in cases.items():
            self.assertEqual(minutes(text), want, text)


class QuotedTests(unittest.TestCase):
    def test_all_kinds_in_order(self):
        md = 'Say "straight", then “curly” and **bold** done. "" nothing'
        self.assertEqual(quoted(md), ["straight", "curly", "bold"])

    def test_none(self):
        self.assertEqual(quoted("plain"), [])


class DocTests(unittest.TestCase):
    def test_docstring_examples(self):
        self.assertEqual(doctest.testmod(parse).failed, 0)


if __name__ == "__main__":
    unittest.main()
