"""Example checks for the mock case. Each public function takes `run` and returns a Check."""

import re

from rehearse.checks import Check


def short_replies(run):
    """Every subject reply is three sentences or fewer."""
    for i, text in enumerate(run.subject_turns, 1):
        n = len([s for s in re.split(r"[.!?]+\s", text.strip()) if s])
        if n > 3:
            return Check.failed(f"reply {i} has {n} sentences")
    return Check.passed(f"{len(run.subject_turns)} replies, none over three sentences")


def asked_a_question(run):
    """The subject asked at least one question."""
    asked = [i for i, t in enumerate(run.subject_turns, 1) if "?" in t]
    if asked:
        return Check.passed(f"asked a question in reply {asked[0]}")
    return Check.failed("no question mark in any reply")


def never_says_banned_phrase(run):
    """The subject never said "as an AI"."""
    if any("as an ai" in t.lower() for t in run.subject_turns):
        return Check.failed("said 'as an AI'")
    return Check.passed("never said it")
