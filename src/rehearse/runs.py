"""Run folders: create one, run a conversation into it, write everything scrubbed.

A run folder is never overwritten: a new run takes the next free number.

Parts adapted from an earlier CC0-licensed harness by the same author: `scrub()`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import __version__
from .adapters import make_adapter, require
from .workspace import Workspace
from .backends import Backend, make_backend
from .backends.keys import scrub_keys
from .case import Case
from .loop import Outcome, run_conversation

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _home_pattern() -> re.Pattern | None:
    homes = {str(Path.home())}
    try:
        homes.add(str(Path.home().resolve()))
    except OSError:
        pass
    homes = sorted((h for h in homes if len(h) > 1), key=len, reverse=True)
    if not homes:
        return None
    # Not followed by a name character, so /home/u2 is not mistaken for /home/u.
    return re.compile("(?:" + "|".join(re.escape(h) for h in homes) + r")(?![\w-]|\.\w)")


def scrub(text: str) -> str:
    """Remove what must not be saved: email addresses and the home folder path.

    A subscription CLI can put the account's email address and local paths into its output,
    and a model can type them back. Everything written to disk goes through this.
    """
    text = text or ""
    home = _home_pattern()
    if home:
        text = home.sub("~", text)
    return scrub_keys(_EMAIL.sub("[email removed]", text))


def scrub_obj(obj):
    """`scrub` applied to every string in a nested dict or list (keys included)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {scrub_obj(k): scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


def write_text(path: Path, text: str) -> str:
    """Write scrubbed text and return what was written."""
    clean = scrub(text)
    path.write_text(clean)
    return clean


def write_json(path: Path, data) -> None:
    """Write scrubbed JSON. Strings are scrubbed before encoding, so escapes cannot hide a match."""
    plain = json.loads(json.dumps(data, default=str))
    path.write_text(json.dumps(scrub_obj(plain), indent=2, ensure_ascii=False) + "\n")


def display_path(path) -> str:
    """A path for the screen: relative to the current folder if it is under it, else as given."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def find_runs(paths) -> list[Path]:
    """Run folders named by `paths`: a run folder itself, or a folder holding run folders.

    Raises FileNotFoundError, in plain words, if a path is missing or holds no runs.
    """
    found: list[Path] = []
    for p in map(Path, paths):
        if (p / "meta.json").is_file():
            here = [p]
        elif p.is_dir():
            here = sorted(m.parent for m in p.rglob("meta.json"))
        else:
            raise FileNotFoundError(f"no such folder: {p}")
        if not here:
            raise FileNotFoundError(f"no run folders found in {p}")
        found += here
    return list(dict.fromkeys(found))


def _slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "-", s).strip("-") or "x"


def make_run_dir(runs_dir: Path, case: str, variant: str, sim: str,
                 today: date | None = None) -> tuple[Path, int]:
    """Create `<date>-<case>-<variant>-<sim>-<n>` with n the first free number from 1."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{(today or date.today()).isoformat()}-{_slug(case)}-{_slug(variant)}-{_slug(sim)}"
    n = 1
    while True:
        path = runs_dir / f"{stem}-{n}"
        try:
            path.mkdir(exist_ok=False)
            return path, n
        except FileExistsError:
            n += 1


def git_commit(folder: Path) -> str | None:
    """Short commit hash of the case's repository, plus "+dirty" if tracked files changed; else None."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise OSError(r.stderr)
        return r.stdout.strip()
    try:
        commit = git("rev-parse", "--short", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no", "--", ".")
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class RunResult:
    path: Path
    stop: str
    subject_turns: int
    mock: bool
    outcome: Outcome
    cost: dict = field(default_factory=dict)     # per seat: {"usd", "calls", "unknown_calls"} for this run
    mock_seats: dict = field(default_factory=dict)


def _cost(calls: list[dict], seat: str) -> dict:
    mine = [c["result"] for c in calls if c["seat"] == seat]
    unknown = sum(1 for r in mine if r.get("cost_usd") is None)
    total = None if unknown or not mine else sum(r["cost_usd"] for r in mine)
    known = sum(r["cost_usd"] for r in mine if r.get("cost_usd") is not None)
    return {"usd": total, "calls": len(mine), "unknown_calls": unknown, "known_usd": known}


def _models(calls: list[dict], seat: str) -> list[str]:
    return sorted({c["result"]["model"] for c in calls if c["seat"] == seat and c["result"].get("model")})


def _transcript(case: Case, variant: str, sim: str, seats: dict, out: Outcome, mock: bool) -> str:
    lines = [f"# {case.name}: {variant} with {sim}", "",
             f"- Case: {case.name}", f"- Variant: {variant}", f"- Sim: {sim}",
             f"- Seats: subject {seats['subject']}, sim {seats['sim']}",
             f"- Stop reason: {out.stop}",
             f"- Subject turns: {out.count('subject')}"]
    if mock:
        lines.append("- PLUMBING ONLY, NOT A RESULT: every seat was the mock backend.")
    for t in out.turns:
        who = "Person" if t.side == "person" else "Subject"
        lines += ["", f"## {who} (turn {t.n})", "", t.text]
    return "\n".join(lines) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def execute_run(case: Case, variant: str, sim: str, subject: Backend | None = None,
                sim_backend: Backend | None = None, today: date | None = None,
                repeat: int = 1, budget_usd: float | None = None, keep_work: bool = False) -> RunResult:
    """Run one conversation and save the run folder.

    Raises QuotaError (with `run_path` set) or KeyboardInterrupt after saving, if either happened.
    Backends default to the ones the case names; tests may pass their own. `repeat` is this
    run's number within its suite (1 for a single run); the folder number is separate.
    `budget_usd` is passed on to backends that can enforce it. `keep_work` keeps an agent's work
    folder (its path is printed) instead of deleting it.
    """
    # Snapshot every input once, so what is saved is exactly what the conversation used.
    persona = case.persona(sim)
    overlay = case.variants[variant]
    variant_text = overlay.read_bytes().decode() if overlay and overlay.is_file() else ""
    frame = case.frame_text()
    subject = subject or make_backend(case.subject_seat, replies=case.mock_subject, default="Tell me more.")
    sim_backend = sim_backend or make_backend(case.sim_seat, replies=case.mock_sim, default="Okay.")
    require(sim_backend, "chat")
    for backend in (subject, sim_backend):
        backend.max_budget_usd = budget_usd
    work = None
    try:
        if case.adapter == "agent":
            work = Workspace.create(case, variant)
            work.guard(case)
        adapter = make_adapter(case.adapter, subject, variant_text,
                               workdir=work.path if work else None, allowed_tools=case.allowed_tools,
                               options=case.subject_options)
        try:
            out = run_conversation(case, adapter, sim_backend, persona, frame)
        finally:
            adapter.close()
        return _save(case, variant, sim, repeat, today, subject, sim_backend, persona, variant_text,
                     frame, out, work)
    finally:
        if work:
            if keep_work:
                print(f"kept work folder: {work.path}")
            else:
                work.cleanup()


def _save(case, variant, sim, repeat, today, subject, sim_backend, persona, variant_text, frame,
          out, work) -> RunResult:
    path, n = make_run_dir(case.runs_dir, case.name, variant, sim, today)
    seats = {"subject": subject.describe(), "sim": sim_backend.describe()}
    mock = subject.is_mock and sim_backend.is_mock

    write_text(path / "transcript.md", _transcript(case, variant, sim, seats, out, mock))
    lines = [json.dumps(scrub_obj(
        {"side": t.side, "n": t.n, "text": t.text, **({"call": t.call} if t.call else {}),
         **({"label_stripped": True} if t.label_stripped else {})}),
        ensure_ascii=False) for t in out.turns]
    (path / "turns.jsonl").write_text("\n".join(lines) + "\n")
    (path / "inputs").mkdir()
    sources = {"persona.md": persona.raw, "frame.md": frame}
    if case.adapter == "chat":
        sources["variant.md"] = variant_text
    elif work:
        for rel in sorted(work.overlay):
            sources[f"variant/{rel}"] = (case.variants[variant] / rel).read_bytes().decode("utf-8", "replace")
    if persona.knowledge_path:
        sources["knowledge.md"] = persona.knowledge_raw
    manifest = {}
    for name, text in sources.items():
        (path / "inputs" / name).parent.mkdir(parents=True, exist_ok=True)
        written = write_text(path / "inputs" / name, text)
        manifest[name] = {"source_sha256": _sha(text), "written_sha256": _sha(written)}
    write_json(path / "inputs" / "manifest.json", manifest)
    if work:
        write_json(path / "inputs" / "scenario.json",
                   {"folder": case.scenario_dir.name if case.scenario_dir else None,
                    "exclude": case.scenario_exclude, "files": work.scenario,
                    "skipped_symlinks": work.skipped_symlinks, "unmatched_excludes": work.unmatched_excludes})
        work.write_artifacts(path, scrub, write_json)
    (path / "raw").mkdir()
    for c in out.calls:
        write_json(path / "raw" / c["file"], {"request": c["request"], "result": c["result"]})

    cost = {"subject": _cost(out.calls, "subject"), "sim": _cost(out.calls, "sim")}
    meta = {
        "case": case.name, "variant": variant, "sim": sim, "repeat": repeat, "folder_number": n,
        "adapter": case.adapter, "seats": seats,
        "models": {"subject": _models(out.calls, "subject"), "sim": _models(out.calls, "sim")},
        "cost": cost,
        "turns": {"subject": out.count("subject"), "person": out.count("person")},
        "max_turns": case.max_turns,
        "subject_stop_pattern": case.stop_pattern.pattern if case.stop_pattern else None,
        "sim_stop_word": persona.stop,
        "stop_reason": out.stop, "stop_with_text": out.stop_with_text,
        "started": out.started, "ended": out.ended,
        "rehearse_version": __version__, "case_commit": git_commit(case.root), "mock": mock,
    }
    write_json(path / "meta.json", meta)
    write_text(path / "assessment.md",
               f"# Assessment: {case.name}, {variant} with {sim}\n\nRead transcript.md.\n\n"
               "## What worked\n\n## What failed\n\n## Notes\n")
    from .checks import check_run     # imported here: checks.py uses this module's writers
    check_run(path, case)
    if out.quota:
        out.quota.run_path = path
        out.quota.run_cost = cost
        raise out.quota
    if out.interrupted:
        raise KeyboardInterrupt
    return RunResult(path, out.stop, out.count("subject"), mock, out, cost,
                     {"subject": subject.is_mock, "sim": sim_backend.is_mock})
