"""Shared test helpers: build a small case in a temp folder and run it on mock seats."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from rehearse.backends import MockBackend
from rehearse.case import load_case
from rehearse.loop import run_conversation
from rehearse.adapters import make_adapter

VARIANT_SENTINEL = "VARIANT-SENTINEL-71c2"
PERSONA_SENTINEL = "PERSONA-SENTINEL-4d9e"
MOOD_SENTINEL = "MOOD-SENTINEL-88ab"
KNOWLEDGE_SENTINEL = "KNOWLEDGE-SENTINEL-5f03"


def q(s):
    """A TOML/YAML-safe quoted string."""
    return json.dumps(s)


def make_case(root: Path, *, opening="Hello.", persona_opening="", stop_pattern="", max_turns=6,
              escalation=(), stop="DONE", variants=("baseline", "cheerful"), sims=("dana",),
              subject_replies=(), sim_replies=(), extra_persona="", checks_py=None, name="tcase", frame=None,
              knowledge_extra="", variant_extra="") -> Path:
    """Write a case into `root` and return the path of its case.toml."""
    (root / "variants").mkdir(parents=True, exist_ok=True)
    (root / "sims").mkdir(exist_ok=True)
    (root / "knowledge").mkdir(exist_ok=True)
    toml = [f"name = {q(name)}", "runs_dir = \"runs\"", "[subject]", "adapter = \"chat\"",
            "seat = \"mock\"", f"opening = {q(opening)}"]
    if checks_py is not None:
        (root / "checks.py").write_text(checks_py)
        toml.insert(1, 'checks = "checks.py"')
    if stop_pattern:
        toml.append(f"stop_pattern = {q(stop_pattern)}")
    toml += ["[variants]"]
    for v in variants:
        (root / "variants" / f"{v}.md").write_text(f"{VARIANT_SENTINEL} {v} instructions.{variant_extra}\n")
        toml.append(f"{v} = {q(f'variants/{v}.md')}")
    toml += ["[sims]", "seat = \"mock\"", "dir = \"sims\"",
             *([f'frame = "frame.md"'] if frame else []), "[limits]", f"max_turns = {max_turns}",
             "[mock]", f"subject = {json.dumps([r for r in subject_replies if isinstance(r, str)])}",
             f"sim = {json.dumps([r for r in sim_replies if isinstance(r, str)])}"]
    for s in sims:
        (root / "knowledge" / f"{s}.md").write_text(f"{KNOWLEDGE_SENTINEL} {s} facts {{braces}}.{knowledge_extra}\n")
        fm = ["---", f"name: {s}", f"knowledge: knowledge/{s}.md", f"mood: {q(MOOD_SENTINEL)}",
              f"stop: {stop}"]
        if persona_opening:
            fm.append(f"opening: {q(persona_opening)}")
        if escalation:
            fm.append("escalation:")
            fm += [f"  - {q(e)}" for e in escalation]
        fm.append("---")
        (root / "sims" / f"{s}.md").write_text("\n".join(fm) + f"\n\n{PERSONA_SENTINEL} {s}.{extra_persona}\n")
    if frame:
        (root / "frame.md").write_text(frame)
    (root / "case.toml").write_text("\n".join(toml) + "\n")
    return root / "case.toml"


class CaseTest(unittest.TestCase):
    """Gives each test a temp folder in self.root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def converse(self, subject_replies=(), sim_replies=(), variant="baseline", sim="dana", **case_kw):
        """Run one conversation with scripted mocks. Returns (outcome, subject mock, sim mock, case)."""
        path = make_case(self.root, subject_replies=subject_replies, sim_replies=sim_replies, **case_kw)
        case = load_case(path)
        subj = MockBackend(subject_replies, default="Go on.")
        simb = MockBackend(sim_replies, default="Okay.")
        persona = case.persona(sim)
        adapter = make_adapter(case.adapter, subj, case.variants[variant].read_text())
        out = run_conversation(case, adapter, simb, persona, case.frame_text())
        return out, subj, simb, case


TODAY = date(2026, 10, 26)


def build_runs(root: Path, *, subject=("What do you need?", "Tell me more?"), sim=("A thing.", "DONE"),
               n=1, variants=("baseline",), sims=("dana",), subject_model=None, **case_kw) -> Path:
    """Make a case in `root` and run every variant x sim n times on mock seats. Returns its runs folder."""
    from rehearse.backends import SeatResult
    path = make_case(root, variants=variants, sims=sims, **case_kw)
    case = load_case(path)
    from rehearse.runs import execute_run
    for v in variants:
        for s in sims:
            for i in range(n):
                replies = [SeatResult(r, model=subject_model) if subject_model else r for r in subject]
                subj = MockBackend(replies, default="Go on?")
                if subject_model:
                    subj.is_mock = False
                execute_run(case, v, s, subj, MockBackend(list(sim), default="Okay."), today=TODAY, repeat=i + 1)
    return case.runs_dir
