"""Load and check a case (`case.toml`), its personas and its sim frame."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import frontmatter
from .backends import BackendError, parse_seat_spec


class CaseError(Exception):
    """The case is wrong. The message says what to fix, in plain words."""


@dataclass
class Persona:
    """A simulated person, read from a persona file."""
    sim: str                      # the name used on the command line: the file's stem
    name: str                     # display name from the frontmatter, else the stem
    path: Path
    body: str                     # who the person is
    mood: str = ""
    opening: str = ""
    escalation: list[str] = field(default_factory=list)
    stop: str = "DONE"
    knowledge_path: Path | None = None
    knowledge: str = ""
    raw: str = ""                 # the persona file exactly as read
    knowledge_raw: str = ""       # the knowledge file exactly as read

    def turn_line(self, sim_turn: int) -> str:
        """The escalation line for the sim's reply number `sim_turn` (1 is its first reply).

        Line k is used on reply k+1; the last line repeats; no lines gives "".
        """
        if not self.escalation or sim_turn < 1:
            return ""
        return self.escalation[min(sim_turn - 1, len(self.escalation) - 1)]


@dataclass
class Case:
    name: str
    root: Path
    runs_dir: Path
    adapter: str
    subject_seat: str
    opening: str
    stop_pattern: re.Pattern | None
    variants: dict[str, Path]
    sim_seat: str
    sims: dict[str, Path]
    frame_path: Path | None
    max_turns: int
    mock_subject: list[str]
    mock_sim: list[str]
    checks_path: Path | None = None   # the case's checks.py, if it has one

    def frame_text(self) -> str:
        """The sim frame: the case's own file, else the one shipped with the tool."""
        if self.frame_path:
            return self.frame_path.read_bytes().decode()
        return resources.files("rehearse").joinpath("frames", "sim.md").read_bytes().decode()

    def persona(self, sim: str) -> Persona:
        return load_persona(self.sims[sim], self.root)


def _table(data: dict, key: str, required: bool = True) -> dict:
    value = data.get(key)
    if value is None:
        if required:
            raise CaseError(f"case.toml needs a [{key}] section")
        return {}
    if not isinstance(value, dict):
        raise CaseError(f"[{key}] in case.toml must be a section, not a single value")
    return value


def _text(table: dict, key: str, where: str, required: bool = False) -> str:
    value = table.get(key)
    if value is None:
        if required:
            raise CaseError(f"{where} needs a '{key}'")
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise CaseError(f"'{key}' in {where} must be text" + (" and not empty" if required else ""))
    return value


def _seat(table: dict, where: str) -> str:
    seat = _text(table, "seat", where, required=True)
    try:
        parse_seat_spec(seat)
    except BackendError as e:
        raise CaseError(f"{where}: {e}") from None
    return seat


def _replies(table: dict, key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CaseError(f"'{key}' in [mock] must be a list of text replies")
    return value


def load_case(path: str | Path) -> Case:
    """Read and check a case. `path` is a case.toml or the folder that holds it."""
    path = Path(path)
    if path.is_dir():
        path = path / "case.toml"
    if not path.is_file():
        raise CaseError(f"no case.toml found at {path}")
    root = path.parent.resolve()
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise CaseError(f"case.toml is not valid TOML: {e}") from None

    name = _text(data, "name", "case.toml", required=True)
    runs_dir = root / (_text(data, "runs_dir", "case.toml") or "runs")

    subject = _table(data, "subject")
    adapter = _text(subject, "adapter", "[subject]") or "chat"
    if adapter != "chat":
        raise CaseError(f"[subject] adapter {adapter!r} is not built yet; only 'chat' exists")
    subject_seat = _seat(subject, "[subject]")
    opening = _text(subject, "opening", "[subject]")
    stop_pattern = None
    pattern = _text(subject, "stop_pattern", "[subject]")
    if pattern:
        try:
            stop_pattern = re.compile(pattern)
        except re.error as e:
            raise CaseError(f"[subject] stop_pattern is not a valid regular expression: {e}") from None

    raw_variants = _table(data, "variants")
    if not raw_variants:
        raise CaseError("[variants] must list at least one variant, such as baseline = \"variants/baseline.md\"")
    variants = {}
    for vname, rel in raw_variants.items():
        if not isinstance(rel, str):
            raise CaseError(f"variant {vname!r} must be a file path in quotes")
        if not (root / rel).is_file():
            raise CaseError(f"variant {vname!r}: file not found: {rel}")
        variants[vname] = root / rel

    sims_t = _table(data, "sims")
    sim_seat = _seat(sims_t, "[sims]")
    sims_dir = root / _text(sims_t, "dir", "[sims]", required=True)
    if not sims_dir.is_dir():
        raise CaseError(f"[sims] dir not found: {sims_t['dir']}")
    sims = {p.stem: p for p in sorted(sims_dir.glob("*.md"))}
    if not sims:
        raise CaseError(f"no persona files (*.md) in {sims_t['dir']}")
    frame_rel = _text(sims_t, "frame", "[sims]")
    frame_path = None
    if frame_rel:
        frame_path = root / frame_rel
        if not frame_path.is_file():
            raise CaseError(f"[sims] frame not found: {frame_rel}")
        text = frame_path.read_text()
        missing = [p for p in ("{stop}", "{knowledge}", "{opening}") if p not in text]
        if missing:
            raise CaseError(f"[sims] frame {frame_rel} is missing {', '.join(missing)}")

    limits = _table(data, "limits", required=False)
    max_turns = limits.get("max_turns", 12)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise CaseError("[limits] max_turns must be a whole number, 1 or more")

    checks_path = None
    checks_rel = _text(data, "checks", "case.toml")
    if checks_rel:
        checks_path = root / checks_rel
        if not checks_path.is_file():
            raise CaseError(f"checks file not found: {checks_rel}")

    mock = _table(data, "mock", required=False)
    return Case(name=name, root=root, runs_dir=runs_dir, adapter=adapter, subject_seat=subject_seat,
                opening=opening, stop_pattern=stop_pattern, variants=variants, sim_seat=sim_seat,
                sims=sims, frame_path=frame_path, max_turns=max_turns,
                mock_subject=_replies(mock, "subject"), mock_sim=_replies(mock, "sim"), checks_path=checks_path)


def load_persona(path: Path, case_root: Path) -> Persona:
    """Read a persona file. Its `knowledge` path is relative to the case folder."""
    raw = path.read_bytes().decode()
    meta, body = frontmatter.parse(raw)
    escalation = meta.get("escalation", [])
    if isinstance(escalation, str):
        escalation = [escalation] if escalation else []
    persona = Persona(
        sim=path.stem, name=meta.get("name") or path.stem, path=path, body=body.strip(),
        mood=meta.get("mood", ""), opening=meta.get("opening", ""), escalation=escalation,
        stop=meta.get("stop") or "DONE", raw=raw)
    rel = meta.get("knowledge", "")
    if isinstance(rel, str) and rel:
        persona.knowledge_path = case_root / rel
        if not persona.knowledge_path.is_file():
            raise CaseError(f"persona {path.name}: knowledge file not found: {rel}")
        persona.knowledge_raw = persona.knowledge_path.read_bytes().decode()
        persona.knowledge = persona.knowledge_raw.strip()
    return persona
