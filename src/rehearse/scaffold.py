"""`rehearse init`: write a small working case to fill in, so a new case starts from a case that loads."""

from __future__ import annotations

from pathlib import Path

from .case import CaseError

CASE_CHAT = '''name = "{name}"
rubric = "rubric.md"
runs_dir = "runs"

[subject]
adapter = "chat"                 # a prompt in a plain chat; use "agent" for an agent working in a folder
seat = "{subject_seat}"
opening = "Hello."               # the first thing the person says, unless the sim has its own opening

[variants]
current = "variants/current.md"  # one file per version of the prompt; add a file to try a change

[sims]
seat = "{sim_seat}"
dir = "sims"

[limits]
max_turns = 8

[judge]
seat = "{judge_seat}"
default = false

# Scripted replies for mock seats only; delete this once the seats are real.
[mock]
subject = ["Hi. What can I help with?", "Tell me more.", "Thanks, that helps."]
sim = ["Not much.", "I guess so.", "DONE"]
'''

CASE_AGENT = CASE_CHAT.replace('adapter = "chat"                 # a prompt in a plain chat; use "agent" for an agent working in a folder',
                               'adapter = "agent"                # an agent working in a copy of the scenario folder') \
    .replace('opening = "Hello."', 'allowed_tools = ["Read"]\nopening = "Hello."') \
    .replace('[variants]\ncurrent = "variants/current.md"  # one file per version of the prompt; add a file to try a change',
             '[scenario]\nfolder = "scenario"\n\n[variants]\n'
             'current = "variants/current"    # a folder laid over the scenario copy (put AGENTS.md or CLAUDE.md in it)')

SIM = '''---
name: person
knowledge: knowledge/person.md   # the only facts this person knows
opening: "Hello, I need some help."
mood: "Short, plain answers."
stop: DONE
---

Who this person is and the rules they follow. They do not know the answer key, the checks or the
subject's instructions.
'''

KNOWLEDGE = '''# Person: what you know

Entirely made-up material for testing.

- Replace this with the facts the simulated person can use. Anything not listed is not known to them.
'''

RUBRIC = '''---
answer_key: false
artifacts: false
---
# Rubric

1. Replace this with a question about the transcript a judge can answer from the words alone.
   Pass: what a good answer looks like
   Fail: what a bad answer looks like
'''

GITIGNORE = "runs/*/*\n!runs/*/assessment.md\n"


def init_case(folder: str | Path, name: str | None = None, adapter: str = "chat", prompt: str | Path | None = None,
              subject_seat: str = "mock", sim_seat: str = "mock", judge_seat: str = "mock") -> list[Path]:
    """Write a case into `folder` and return the files written. Refuses to touch an existing case.toml."""
    root = Path(folder)
    if (root / "case.toml").exists():
        raise CaseError(f"{root / 'case.toml'} already exists; init does not overwrite a case")
    if adapter not in ("chat", "agent"):
        raise CaseError("--adapter must be chat or agent")
    prompt_text = "You are a helpful assistant. Replace this with the prompt under test.\n"
    if prompt:
        try:
            prompt_text = Path(prompt).read_text()
        except OSError as e:
            raise CaseError(f"cannot read --prompt: {e}") from None
    files = {
        "case.toml": (CASE_AGENT if adapter == "agent" else CASE_CHAT).format(
            name=name or root.resolve().name, subject_seat=subject_seat, sim_seat=sim_seat, judge_seat=judge_seat),
        "sims/person.md": SIM, "knowledge/person.md": KNOWLEDGE, "rubric.md": RUBRIC, ".gitignore": GITIGNORE,
    }
    if adapter == "agent":
        files["variants/current/AGENTS.md"] = prompt_text
        files["scenario/.gitkeep"] = ""
    else:
        files["variants/current.md"] = prompt_text
    written = []
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written


def extract_continuation(run: str | Path, at: int, cont_id: str, purpose: str, expect: list[str],
                         to: str | Path | None = None, variant: str | None = None) -> Path:
    """Write a continuation from a saved run: every turn before subject turn `at`, then stop.

    The subject's reply to the last prefilled person turn is the step under test. `to` is the folder to write
    into (default: `continuations` next to the case.toml above the run). Refuses to overwrite a file.
    """
    import json
    from .case import parse_continuation
    from .checks import find_case_file
    from .runs import scrub
    run = Path(run)
    try:
        rows = [json.loads(ln) for ln in (run / "turns.jsonl").read_text().splitlines() if ln.strip()]
    except OSError:
        raise CaseError(f"{run} is not a run folder (no turns.jsonl)") from None
    if not any(r["side"] == "subject" and r["n"] == at for r in rows):
        have = sorted(r["n"] for r in rows if r["side"] == "subject")
        raise CaseError(f"that run has no subject turn {at}; its subject turns are {have[0]} to {have[-1]}")
    if not expect:
        raise CaseError("give at least one --expect line: what a good reply does")
    keep, seen = [], False
    for r in rows:
        if r["side"] == "subject" and r["n"] == at:
            break
        keep.append(r)
    if not keep or keep[-1]["side"] != "person":
        raise CaseError("the turns before that point must end with a person turn")
    if to is None:
        case_file = find_case_file(run)
        if case_file is None:
            raise CaseError("no case.toml above the run; pass --to FOLDER")
        to = case_file.parent / "continuations"
    path = Path(to) / f"{cont_id}.md"
    if path.exists():
        raise CaseError(f"{path} already exists; pick another --id")
    head = ["---", "purpose: " + json.dumps(purpose), "expect:"] + ["  - " + json.dumps(e) for e in expect]
    if variant:
        head.append(f"variant: {variant}")
    head.append("---")
    body = "\n\n".join(f"## {'Person' if r['side'] == 'person' else 'Subject'}\n\n{r['text'].strip()}" for r in keep)
    text = scrub("\n".join(head) + "\n" + body + "\n")
    parse_continuation(text, cont_id, path)          # fail now if the file would not load
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
