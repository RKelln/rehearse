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
class Continuation:
    """A focused continuation: a prefilled transcript, then one subject reply (or a short conversation)."""
    id: str                       # the file's stem
    path: Path
    purpose: str                  # for the judge only; never sent to a seat
    expect: list[str]             # plain-language expectations; become judge questions e1, e2 ...
    turns: list[tuple[str, str]]  # (side, text) with side "person" or "subject"; first and last are "person"
    variant: str | None = None    # limit to one variant
    sim: str | None = None        # a persona to continue with after the prefill; None = one reply only
    max_turns: int | None = None  # with a sim: cap on generated subject replies (default: the case's)
    rubric: bool = False          # also ask the case rubric's questions (default: only with a sim)
    raw: str = ""                 # the file exactly as read


@dataclass
class Case:
    name: str
    root: Path
    runs_dir: Path
    adapter: str
    subject_seat: str | dict
    opening: str
    stop_pattern: re.Pattern | None
    variants: dict[str, Path | None]      # a file (chat) or a folder to overlay (agent); None = no overlay
    sim_seat: str | dict
    sims: dict[str, Path]
    frame_path: Path | None
    max_turns: int
    mock_subject: list[str]
    mock_sim: list[str]
    checks_path: Path | None = None   # the case's checks.py, if it has one
    allowed_tools: list[str] | None = None   # agent subject: tools allowed on the command line
    scenario_dir: Path | None = None         # agent subject: the folder copied for each run
    scenario_exclude: list[str] = field(default_factory=list)   # globs, relative to the scenario folder
    subject_options: dict = field(default_factory=dict)         # agent: "tools", "permission_mode", "permission"
    rubric_path: Path | None = None          # the case's rubric.md, if it has one
    rubric: dict | None = None               # that file, parsed (see judge.parse_rubric)
    answer_key_path: Path | None = None
    judge_seat: str | dict | None = None
    judge_default: bool = False              # judge every run right after its checks
    mock_judge: list[str] = field(default_factory=list)
    continuations: dict[str, Continuation] = field(default_factory=dict)
    context_path: Path | None = None         # chat subject: text appended to every variant's system prompt
    context_raw: str = ""
    max_prompt_chars: int | None = None      # refuse a variant longer than this
    stop_after_artifact: str | None = None   # agent subject: a path in the work folder; see loop.py
    turns_after_artifact: int | None = None  # optional cap: subject turns allowed after it first appears

    def frame_text(self) -> str:
        """The sim frame: the case's own file, else the one shipped with the tool."""
        if self.frame_path:
            return self.frame_path.read_bytes().decode()
        return resources.files("rehearse").joinpath("frames", "sim.md").read_bytes().decode()

    def system_text(self, variant_text: str) -> str:
        """The subject's system prompt: the variant, then the case's context under a "Context" heading."""
        if not self.context_raw.strip():
            return variant_text
        return variant_text.rstrip("\n") + "\n\n## Context\n\n" + self.context_raw.strip() + "\n"

    def check_prompt_limit(self, variants) -> None:
        """Refuse, in plain words, a variant longer than `max_prompt_chars` (the variant file alone;
        the shared context is not counted, since it is test material, not the prompt under test)."""
        if not self.max_prompt_chars:
            return
        for v in variants:
            path = self.variants.get(v)
            if path and path.is_file():
                n = len(path.read_bytes().decode())
                if n > self.max_prompt_chars:
                    raise CaseError(f"variant {v!r} is {n} characters, over the limit of "
                                    f"{self.max_prompt_chars} (max_prompt_chars in [subject])")

    def persona(self, sim: str) -> Persona:
        return load_persona(self.sims[sim], self.root)


_SECTION = re.compile(r"^##\s+(Person|Subject)\s*(?:\(\s*(?:turn\s*)?\d+\s*(?:,\s*prefilled\s*)?\))?\s*$", re.I)
_CONT_KEYS = ("purpose", "expect", "variant", "sim", "max_turns", "rubric")


def parse_continuation(raw: str, cont_id: str, path: Path) -> Continuation:
    """Read a continuation file: frontmatter, then `## Person` / `## Subject` sections."""
    where = f"continuation {cont_id}"
    meta, body = frontmatter.parse(raw)
    for key in meta:
        if key not in _CONT_KEYS:
            raise CaseError(f"{where}: unknown setting {key!r}; the settings are: {', '.join(_CONT_KEYS)}")
    purpose = meta.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise CaseError(f"{where} needs a 'purpose' in its frontmatter")
    expect = meta.get("expect")
    if isinstance(expect, str) and expect.strip():
        expect = [expect]
    if not (isinstance(expect, list) and expect and all(isinstance(e, str) and e.strip() for e in expect)):
        raise CaseError(f"{where} needs 'expect' in its frontmatter: a list of lines such as '  - asks one question'")
    variant = meta.get("variant") or None
    sim = meta.get("sim") or None
    rubric_flag = str(meta.get("rubric", "")).strip().lower()
    if rubric_flag and rubric_flag not in ("true", "yes", "on", "false", "no", "off"):
        raise CaseError(f"{where}: 'rubric' must be true or false")
    use_rubric = rubric_flag in ("true", "yes", "on") if rubric_flag else bool(sim)
    max_turns = None
    if "max_turns" in meta:
        try:
            max_turns = int(meta["max_turns"])
        except (TypeError, ValueError):
            max_turns = 0
        if max_turns < 1:
            raise CaseError(f"{where}: max_turns must be a whole number, 1 or more")
        if not sim:
            raise CaseError(f"{where}: max_turns needs a 'sim' (without one the subject gives exactly one reply)")
    turns: list[tuple[str, str]] = []
    for line in body.splitlines():
        m = _SECTION.match(line)
        if m:
            turns.append(("person" if m.group(1).lower() == "person" else "subject", ""))
        elif turns:
            turns[-1] = (turns[-1][0], turns[-1][1] + line + "\n")
        elif line.strip():
            raise CaseError(f"{where}: text before the first '## Person' section: {line.strip()[:60]!r}")
    turns = [(side, text.strip()) for side, text in turns]
    if not turns:
        raise CaseError(f"{where} has no '## Person' / '## Subject' sections")
    for i, (side, text) in enumerate(turns, 1):
        if not text:
            raise CaseError(f"{where}: section {i} ({side}) is empty")
    if turns[0][0] != "person":
        raise CaseError(f"{where}: the first section must be '## Person'")
    if turns[-1][0] != "person":
        raise CaseError(f"{where}: the last section must be '## Person' (the subject replies next)")
    for i in range(1, len(turns)):
        if turns[i][0] == turns[i - 1][0]:
            raise CaseError(f"{where}: sections {i} and {i + 1} are both from the {turns[i][0]}; "
                            "the two sides must take turns (join them into one section)")
    return Continuation(id=cont_id, path=path, purpose=purpose.strip(), expect=[e.strip() for e in expect],
                        turns=turns, variant=variant, sim=sim, max_turns=max_turns, rubric=use_rubric, raw=raw)


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


def _seat(table: dict, where: str):
    seat = table.get("seat")
    if isinstance(seat, dict):
        pass
    elif not isinstance(seat, str) or not seat.strip():
        raise CaseError(f"{where} needs a 'seat', such as \"mock\" or {{ backend = \"http\", ... }}")
    try:
        parse_seat_spec(seat)
    except BackendError as e:
        raise CaseError(f"{where}: {e}") from None
    return seat


def _replies(table: dict, key: str) -> list:
    """Scripted replies: text, or a table `{ text = ..., write = { "file" = "content" } }`."""
    value = table.get(key, [])
    ok = isinstance(value, list) and all(
        isinstance(v, str) or (isinstance(v, dict) and isinstance(v.get("text"), str)
                               and isinstance(v.get("write", {}), dict)) for v in value)
    if not ok:
        raise CaseError(f"'{key}' in [mock] must be a list of text replies "
                        "(or tables with a 'text' and optionally a 'write' table)")
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
    if adapter not in ("chat", "agent"):
        raise CaseError(f"[subject] adapter {adapter!r} is not built yet; the adapters are 'chat' and 'agent'")
    allowed_tools = subject.get("allowed_tools")
    if allowed_tools is not None and not (isinstance(allowed_tools, list)
                                          and all(isinstance(t, str) for t in allowed_tools)):
        raise CaseError("[subject] allowed_tools must be a list of text, such as [\"Read\", \"Write(review.md)\"]")
    subject_seat = _seat(subject, "[subject]")
    opening = _text(subject, "opening", "[subject]")
    stop_pattern = None
    pattern = _text(subject, "stop_pattern", "[subject]")
    if pattern:
        try:
            stop_pattern = re.compile(pattern)
        except re.error as e:
            raise CaseError(f"[subject] stop_pattern is not a valid regular expression: {e}") from None

    context_path, context_raw = None, ""
    context_rel = _text(subject, "context", "[subject]")
    if context_rel:
        if adapter != "chat":
            raise CaseError("[subject] context needs a chat subject (an agent has no system prompt to add it to)")
        context_path = root / context_rel
        if not context_path.is_file():
            raise CaseError(f"[subject] context file not found: {context_rel}")
        context_raw = context_path.read_bytes().decode()
    max_prompt_chars = subject.get("max_prompt_chars")
    if max_prompt_chars is not None and (isinstance(max_prompt_chars, bool) or not isinstance(max_prompt_chars, int)
                                         or max_prompt_chars < 1):
        raise CaseError("[subject] max_prompt_chars must be a whole number, 1 or more")
    if max_prompt_chars is not None and adapter != "chat":
        raise CaseError("[subject] max_prompt_chars needs a chat subject (an agent's variants are folders, not one prompt)")

    options = {}
    if "tools" in subject:
        if not (isinstance(subject["tools"], list) and all(isinstance(t, str) for t in subject["tools"])):
            raise CaseError("[subject] tools must be a list of tool names, such as [\"Read\", \"Write\"]")
        options["tools"] = subject["tools"]
    if "permission_mode" in subject:
        options["permission_mode"] = _text(subject, "permission_mode", "[subject]")

    if "permission" in subject:
        if not isinstance(subject["permission"], dict):
            raise CaseError("[subject] permission must be a table (opencode permissions)")
        options["permission"] = subject["permission"]

    raw_variants = _table(data, "variants")
    if not raw_variants:
        raise CaseError("[variants] must list at least one variant, such as baseline = \"variants/baseline.md\"")
    variants = {}
    for vname, rel in raw_variants.items():
        if not isinstance(rel, str):
            raise CaseError(f"variant {vname!r} must be a path in quotes")
        if adapter == "agent":
            if rel and not (root / rel).is_dir():
                raise CaseError(f"variant {vname!r}: folder not found: {rel} "
                                "(for an agent subject a variant is a folder, or \"\" for none)")
            variants[vname] = root / rel if rel else None
        else:
            if not (root / rel).is_file():
                raise CaseError(f"variant {vname!r}: file not found: {rel}")
            variants[vname] = root / rel

    scenario = _table(data, "scenario", required=False)
    scenario_dir, exclude = None, scenario.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        raise CaseError("[scenario] exclude must be a list of glob patterns in quotes")
    if scenario.get("folder"):
        scenario_dir = root / _text(scenario, "folder", "[scenario]")
        if not scenario_dir.is_dir():
            raise CaseError(f"[scenario] folder not found: {scenario['folder']}")

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
    stop_after = limits.get("stop_after_artifact")
    if stop_after is not None:
        if adapter != "agent":
            raise CaseError("[limits] stop_after_artifact needs an agent subject")
        if (not isinstance(stop_after, str) or not stop_after.strip() or Path(stop_after).is_absolute()
                or ".." in Path(stop_after).parts):
            raise CaseError("[limits] stop_after_artifact must be a path inside the work folder, such as \"review.md\"")
    turns_after = limits.get("turns_after_artifact")
    if turns_after is not None and (isinstance(turns_after, bool) or not isinstance(turns_after, int) or turns_after < 0):
        raise CaseError("[limits] turns_after_artifact must be a whole number, 0 or more")

    checks_path = None
    checks_rel = _text(data, "checks", "case.toml")
    if checks_rel:
        checks_path = root / checks_rel
        if not checks_path.is_file():
            raise CaseError(f"checks file not found: {checks_rel}")

    rubric_path = rubric = answer_key_path = judge_seat = None
    rubric_rel = _text(data, "rubric", "case.toml")
    if rubric_rel:
        from .judge import parse_rubric       # judge.py imports this module
        rubric_path = root / rubric_rel
        if not rubric_path.is_file():
            raise CaseError(f"rubric file not found: {rubric_rel}")
        rubric = parse_rubric(rubric_path.read_bytes().decode(), rubric_rel)
    key_rel = _text(data, "answer_key", "case.toml")
    if key_rel:
        answer_key_path = root / key_rel
        if not answer_key_path.is_file():
            raise CaseError(f"answer key not found: {key_rel}")
    if rubric and rubric["answer_key"] and not answer_key_path:
        raise CaseError("the rubric asks to show the answer key to the judge, but case.toml has no answer_key")
    judge = _table(data, "judge", required=False)
    for key in judge:
        if key not in ("seat", "default"):
            raise CaseError(f"unknown setting {key!r} in [judge]; the settings are: seat, default")
    judge_default = judge.get("default", False)
    if not isinstance(judge_default, bool):
        raise CaseError("[judge] default must be true or false")
    if judge:
        judge_seat = _seat(judge, "[judge]")
    if judge_default and not (rubric and judge_seat):
        raise CaseError("[judge] default = true needs a rubric and a judge seat")

    continuations: dict[str, Continuation] = {}
    cont_rel = _text(data, "continuations", "case.toml") or ("continuations" if (root / "continuations").is_dir() else "")
    if cont_rel:
        cont_dir = root / cont_rel
        if not cont_dir.is_dir():
            raise CaseError(f"continuations folder not found: {cont_rel}")
        if adapter != "chat":
            raise CaseError("continuations need a chat subject")
        for p in sorted(cont_dir.glob("*.md")):
            cont = parse_continuation(p.read_bytes().decode(), p.stem, p)
            if cont.variant and cont.variant not in variants:
                raise CaseError(f"continuation {cont.id}: unknown variant {cont.variant!r}; "
                                f"this case has: {', '.join(variants)}")
            if cont.sim and cont.sim not in sims:
                raise CaseError(f"continuation {cont.id}: unknown sim {cont.sim!r}; "
                                f"this case has: {', '.join(sims)}")
            continuations[cont.id] = cont

    mock = _table(data, "mock", required=False)
    return Case(name=name, root=root, runs_dir=runs_dir, adapter=adapter, subject_seat=subject_seat,
                opening=opening, stop_pattern=stop_pattern, variants=variants, sim_seat=sim_seat,
                sims=sims, frame_path=frame_path, max_turns=max_turns,
                mock_subject=_replies(mock, "subject"), mock_sim=_replies(mock, "sim"), mock_judge=_replies(mock, "judge"),
                rubric_path=rubric_path, rubric=rubric, answer_key_path=answer_key_path,
                judge_seat=judge_seat, judge_default=judge_default, checks_path=checks_path,
                allowed_tools=allowed_tools, scenario_dir=scenario_dir, scenario_exclude=exclude,
                subject_options=options, continuations=continuations, context_path=context_path,
                context_raw=context_raw, max_prompt_chars=max_prompt_chars,
                stop_after_artifact=stop_after, turns_after_artifact=turns_after)


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
