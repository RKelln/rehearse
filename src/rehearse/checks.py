"""Checks: code in the case that reads a saved run and says pass, partial or fail.

A case lists its checks file in case.toml (`checks = "checks.py"`). Every public function in it
(name not starting with "_") that takes one argument, `run`, is a check. It returns a `Check`
(or True or False). Checks read saved files only; they never call a model.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

from . import __version__

RESULTS = ("pass", "partial", "fail", "skip", "error")


@dataclass
class Check:
    """One check's answer: a result and one line of evidence."""
    result: str      # "pass", "partial", "fail", "skip" or "error"
    evidence: str = ""

    @classmethod
    def passed(cls, evidence: str = "") -> "Check":
        return cls("pass", evidence)

    @classmethod
    def partial(cls, evidence: str = "") -> "Check":
        return cls("partial", evidence)

    @classmethod
    def failed(cls, evidence: str = "") -> "Check":
        return cls("fail", evidence)

    @classmethod
    def skip(cls, reason: str = "") -> "Check":
        return cls("skip", reason)


class Run:
    """A read-only view of one saved run folder, for checks."""

    def __init__(self, folder: Path, meta: dict, turns: list[dict], case=None):
        self.folder = folder
        self.meta = meta
        self.turns = turns
        self.case = case

    def copy(self) -> "Run":
        """A fresh view with its own copy of the data, so one check cannot change what another sees."""
        return Run(self.folder, copy.deepcopy(self.meta), copy.deepcopy(self.turns), self.case)

    @classmethod
    def load(cls, folder, case=None) -> "Run":
        """Read a run folder. `case` (a loaded Case) is only needed for `scenario_file`."""
        folder = Path(folder)
        try:
            meta = json.loads((folder / "meta.json").read_text())
            turns = [json.loads(l) for l in (folder / "turns.jsonl").read_text().splitlines() if l.strip()]
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"{folder} is not a readable run folder: {e}") from None
        return cls(folder, meta, turns, case)

    @property
    def subject_turns(self) -> list[str]:
        """What the subject said, in order."""
        return [t["text"] for t in self.turns if t["side"] == "subject"]

    @property
    def person_turns(self) -> list[str]:
        """What the person (the sim) said, in order, starting with the scripted opening."""
        return [t["text"] for t in self.turns if t["side"] == "person"]

    @cached_property
    def transcript(self) -> str:
        return (self.folder / "transcript.md").read_text()

    @cached_property
    def calls(self) -> list[dict]:
        """Every raw seat call as {"seat", "index", "request", "result"}, in call order."""
        out = []
        for p in sorted((self.folder / "raw").glob("*.json")):
            m = re.fullmatch(r"(\w+?)-(\d+)\.json", p.name)
            if m:
                out.append({"seat": m.group(1), "index": int(m.group(2)), **json.loads(p.read_text())})
        return sorted(out, key=lambda c: (c["index"], c["seat"] != "subject"))

    @property
    def tool_calls(self) -> list[dict]:
        """Every tool call the subject made, in order. Empty for a chat subject."""
        return [tc for c in self.calls for tc in (c["result"].get("tool_calls") or [])]

    @property
    def artifacts(self) -> Path:
        """The folder of files the subject wrote (may not exist)."""
        return self.folder / "artifacts"

    def artifact(self, name: str) -> str | None:
        """Text of a file the subject wrote, or None."""
        p = self.artifacts / name
        return p.read_text() if p.is_file() else None

    @property
    def diff(self) -> dict:
        """Files added, changed and removed in the subject's folder."""
        p = self.artifacts / "diff.json"
        if p.is_file():
            return json.loads(p.read_text())
        return {"added": [], "changed": [], "removed": []}

    def input(self, name: str) -> str:
        """A saved input ("variant.md", "persona.md", "frame.md", "knowledge.md"), or ""."""
        p = self.folder / "inputs" / name
        return p.read_text() if p.is_file() else ""

    @property
    def knowledge(self) -> str:
        return self.input("knowledge.md")

    def scenario_file(self, rel: str) -> str | None:
        """Text of a file in the case's scenario folder, or None. (Scenarios arrive in step 3.)"""
        root = getattr(self.case, "scenario_dir", None)
        if root and (Path(root) / rel).is_file():
            return (Path(root) / rel).read_text()
        return None


def load_checks(path: Path) -> list[tuple[str, object, str]]:
    """Read a checks file. Returns (name, function, description) for each check in it.

    A check is a function defined in that file, with a public name, taking one parameter named
    `run`. Helpers with other parameters and functions imported from elsewhere are ignored.
    """
    path = Path(path).resolve()
    name = "_rehearse_checks_" + hashlib.sha1(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))          # so the file can import its sibling files
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
        sys.modules.pop(name, None)
    found = []
    for fname, fn in vars(module).items():
        if fname.startswith("_") or not inspect.isfunction(fn) or fn.__module__ != name:
            continue
        if list(inspect.signature(fn).parameters) != ["run"]:
            continue
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        found.append((fname, fn, doc[0] if doc else ""))
    return found


def run_check(fn, run: Run) -> Check:
    """Call one check. A crash becomes an "error" result; a bool becomes pass or fail."""
    try:
        got = fn(run)
    except KeyboardInterrupt:
        raise
    except BaseException as e:      # includes SystemExit: a check must not stop the command
        return Check("error", f"{type(e).__name__}: {e}")
    if isinstance(got, bool):
        return Check("pass" if got else "fail", "(no evidence given)")
    if isinstance(got, Check) and got.result in RESULTS:
        return Check(got.result, " ".join(str(got.evidence).split()))
    return Check("error", f"check returned {type(got).__name__}, not a Check or a bool")


def find_case_file(folder: Path) -> Path | None:
    """Walk up from a run folder to the first case.toml."""
    for parent in Path(folder).resolve().parents:
        if (parent / "case.toml").is_file():
            return parent / "case.toml"
    return None


def check_run(folder, case=None) -> dict:
    """Run the case's checks and the sim check on a saved run; write checks.json.

    An existing checks.json is moved to history/ first, never lost. Returns what was written.
    """
    from .runs import write_json            # runs.py imports this module, so import late
    from .simcheck import sim_check

    folder = Path(folder)
    run = Run.load(folder, case)
    results, checks_file, checks_sha = [], None, None
    if case is not None and case.checks_path:
        checks_file = os.path.relpath(case.checks_path, case.root)
        checks_sha = hashlib.sha256(case.checks_path.read_bytes()).hexdigest()
        try:
            loaded = load_checks(case.checks_path)
        except KeyboardInterrupt:
            raise
        except BaseException as e:      # a broken checks file is recorded, not fatal
            loaded = []
            results.append({"name": "(load checks file)", "description": "", "result": "error",
                            "evidence": f"{type(e).__name__}: {e}"})
        for name, fn, desc in loaded:
            c = run_check(fn, run.copy())
            results.append({"name": name, "description": desc, "result": c.result, "evidence": c.evidence})
    sim = sim_check(run.copy())
    data = {"checked_at": _utc(), "rehearse_version": __version__, "checks_file": checks_file,
            "checks_sha256": checks_sha, "results": results, "sim_check": sim,
            "void": sim["void"], "void_reason": sim["void_reason"]}
    target = folder / "checks.json"
    if target.exists():
        (folder / "history").mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest, n = folder / "history" / f"checks-{stamp}.json", 1
        while dest.exists():
            n += 1
            dest = folder / "history" / f"checks-{stamp}-{n}.json"
        target.rename(dest)
    write_json(target, data)
    return data


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
