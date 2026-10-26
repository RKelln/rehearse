"""Write a run that happened somewhere else as a normal run folder.

The folder has the same layout as a live run, so `check`, `board` and `compare` work on it. What
is not known is saved as null, never guessed. A run is never overwritten: it takes the next free number.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as _date
from pathlib import Path

from . import __version__
from .case import Case
from .runs import make_run_dir, scrub, write_json, write_text


def _unknown_cost() -> dict:
    return {"usd": None, "calls": 0, "unknown_calls": 0, "known_usd": 0.0}


def write_imported_run(case: Case, *, variant: str, sim: str, turns: list[dict],
                       artifacts: dict[str, bytes], meta: dict, source: str,
                       assessment: str | None = None, date: str,
                       inputs: dict[str, str] | None = None) -> Path:
    """Save an outside run into the case's runs folder and return its path.

    `turns`: dicts with `side` ("person" or "subject") and `text`, optionally `tool_calls`
    (a list of {"name", "arg"}). They are numbered here: the person's first turn is 0, the
    subject's first is 1. `artifacts`: file path in the work folder to bytes. `meta`: anything
    known about the run (`seats`, `models`, `cost` per seat, `stop_reason`, and so on); it
    overrides the defaults. `source` is a path relative to the case folder. No diff.json is
    written, so checks about files written skip.
    """
    if not turns:
        raise ValueError("an imported run needs at least one turn")
    day = _date.fromisoformat(date)
    path, n = make_run_dir(case.runs_dir, case.name, variant, sim, day)
    counts = {"person": 0, "subject": 0}
    rows = []
    for t in turns:
        side = t["side"]
        if side not in counts:
            raise ValueError(f"turn side must be 'person' or 'subject', not {side!r}")
        k = counts[side] + (0 if side == "person" else 1)
        counts[side] += 1
        row = {"side": side, "n": k, "text": t["text"]}
        if t.get("tool_calls"):
            row["tool_calls"] = t["tool_calls"]
        rows.append(row)

    seats = meta.get("seats") or {"subject": None, "sim": None}
    lines = [f"# {case.name}: {variant} with {sim}", "",
             f"- Case: {case.name}", f"- Variant: {variant}", f"- Sim: {sim}",
             f"- IMPORTED from {source}; seats and costs may be unknown",
             f"- Subject turns: {counts['subject']}"]
    for r in rows:
        who = "Person" if r["side"] == "person" else "Subject"
        lines += ["", f"## {who} (turn {r['n']})", "", r["text"]]
        for tc in r.get("tool_calls", []):
            lines.append(f"\n- tool `{tc.get('name')}`: `{tc.get('arg')}`")
    write_text(path / "transcript.md", "\n".join(lines) + "\n")
    (path / "turns.jsonl").write_text(
        "\n".join(json.dumps(scrub_row(r), ensure_ascii=False) for r in rows) + "\n")

    if inputs:
        # Snapshots of what the old run used, such as "knowledge.md" and "persona.md", so the sim
        # check can tell given facts from invented ones.
        (path / "inputs").mkdir(exist_ok=True)
        manifest = {}
        for name, text in inputs.items():
            if "/" in name or name.startswith("."):
                raise ValueError(f"input name must be a plain file name: {name}")
            write_text(path / "inputs" / name, text)
            manifest[name] = {"written_sha256": hashlib.sha256(
                (path / "inputs" / name).read_bytes()).hexdigest()}
        write_json(path / "inputs" / "manifest.json", manifest)

    if artifacts:
        for name, data in artifacts.items():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"artifact path must stay inside the run: {name}")
            target = path / "artifacts" / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(scrub(data.decode()))
            except UnicodeDecodeError:
                target.write_bytes(data)

    base = {
        "case": case.name, "variant": variant, "sim": sim, "repeat": 1, "folder_number": n,
        "adapter": case.adapter, "seats": seats, "models": {"subject": [], "sim": []},
        "cost": {"subject": _unknown_cost(), "sim": _unknown_cost()},
        "turns": dict(counts), "max_turns": None, "stop_reason": "unknown",
        "started": None, "ended": None, "rehearse_version": __version__,
        "case_commit": None, "mock": False,
    }
    base.update(meta)
    base.update({"imported": True, "source": source, "turns": dict(counts), "folder_number": n,
                 "variant": variant, "sim": sim, "case": case.name, "date": date})
    write_json(path / "meta.json", base)
    if assessment is not None:
        write_text(path / "assessment.md", assessment)
    from .checks import check_run
    check_run(path, case)
    return path


def scrub_row(row: dict) -> dict:
    from .runs import scrub_obj
    return scrub_obj(row)
