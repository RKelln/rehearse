"""The judge: a model reads a saved run and answers the case's rubric. Results are directional.

The rubric is a Markdown file (`rubric = "rubric.md"` in case.toml): optional frontmatter, then
numbered questions, each with optional `Pass:` and `Fail:` lines. The judge sees the transcript, the
files the subject wrote, the answer key only if the rubric asks, and the sim's mood and knowledge
page. It never sees the variant text. Every quote it gives is checked against what it was shown.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import frontmatter
from .backends import BackendError, Msg, QuotaError, make_backend
from .case import Case, CaseError
from .checks import Run

VERDICTS = ("pass", "partial", "fail", "unclear")
SIM_KEYS = ("in_character", "kept_up_mood", "used_only_knowledge")
SIM_VALUES = {"in_character": ("pass", "fail"), "kept_up_mood": ("pass", "partial", "fail"),
              "used_only_knowledge": ("pass", "partial", "fail")}
FILE_LIMIT = 30_000
RETRY = "Reply with only the JSON object."

_QUESTION = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_FIELD = re.compile(r"^\s*(Pass|Fail)\s*:\s*(.*)$", re.I)
_TRUE = ("true", "yes", "on", "1")


def parse_rubric(text: str, where: str = "rubric") -> dict:
    """Read a rubric. Returns {"questions": [{id, question, pass, fail}], "answer_key": bool, "artifacts": bool}."""
    meta, body = frontmatter.parse(text)
    flags = {}
    for key, default in (("answer_key", False), ("artifacts", True)):
        raw = str(meta.get(key, "")).strip().lower()
        if raw and raw not in _TRUE + ("false", "no", "off", "0"):
            raise CaseError(f"{where}: '{key}' in the frontmatter must be true or false")
        flags[key] = raw in _TRUE if raw else default
    questions, field = [], None
    for n, line in enumerate(body.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        q, f = _QUESTION.match(line), _FIELD.match(line)
        if f and questions:
            field = f.group(1).lower()
            questions[-1][field] = f.group(2).strip()
        elif q:
            if int(q.group(1)) != len(questions) + 1:
                raise CaseError(f"{where}: question numbers must go 1, 2, 3 ...; found {q.group(1)} "
                                f"where {len(questions) + 1} was expected (line {n})")
            questions.append({"id": len(questions) + 1, "question": q.group(2), "pass": "", "fail": ""})
            field = "question"
        elif questions:                      # a continuation of the line above
            questions[-1][field] = (questions[-1][field] + " " + line.strip()).strip()
        else:
            raise CaseError(f"{where}: line {n} is before the first numbered question: {line.strip()[:60]!r}")
    if not questions:
        raise CaseError(f"{where}: no questions found; write them as '1. question text'")
    return {"questions": questions, "answer_key": flags["answer_key"], "artifacts": flags["artifacts"]}


def _frame() -> str:
    return resources.files("rehearse").joinpath("frames", "judge.md").read_bytes().decode()


def rubric_for_run(case: Case, folder) -> dict | None:
    """The questions to ask about one saved run: the case rubric's, then the `expect` lines of the
    continuation the run snapshot holds (ids e1, e2 ...). None when there are none.

    Returns {"questions", "answer_key", "artifacts", "purpose"}; `purpose` is "" for an ordinary run.
    """
    from .case import parse_continuation
    base = case.rubric or {"questions": [], "answer_key": False, "artifacts": True}
    rubric = {**base, "questions": list(base["questions"]), "purpose": ""}
    snap = Path(folder) / "inputs" / "continuation.md"
    if snap.is_file():
        cont = parse_continuation(snap.read_bytes().decode(), "snapshot", snap)
        if not cont.rubric:
            rubric["questions"] = []          # only the continuation's own expectations are asked
        rubric["purpose"] = cont.purpose
        rubric["questions"] += [{"id": f"e{i}", "question": e, "pass": "", "fail": ""}
                                for i, e in enumerate(cont.expect, 1)]
    return rubric if rubric["questions"] else None


def _turns_text(run: Run) -> str:
    """The transcript for the judge: turns only, with the subject's tool calls inline. No variant name."""
    parts = []
    for t in run.turns:
        who = "PERSON" if t["side"] == "person" else "SUBJECT"
        written = ", written in advance, not produced by the subject" if t.get("prefilled") and who == "SUBJECT" \
            else ", written in advance" if t.get("prefilled") else ""
        block = [f"{who} (turn {t.get('n', '?')}{written}):"]
        call = t.get("call")
        if call and t["side"] == "subject":
            try:
                res = json.loads((run.folder / "raw" / call).read_text()).get("result", {})
            except (OSError, ValueError):
                res = {}
            for tc in res.get("tool_calls") or []:
                block.append(f"[tool] {tc.get('name', '?')} {tc.get('arg', '')}".rstrip())
        block.append(t["text"])
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def _artifact_text(run: Run) -> str:
    root = run.artifacts / "files"
    if not root.is_dir():
        return ""
    parts = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_bytes().decode()
        except UnicodeDecodeError:
            continue
        note = ""
        if len(text) > FILE_LIMIT:
            text, note = text[:FILE_LIMIT], "\n[file cut at 30 kB]"
        parts.append(f"--- file: {p.relative_to(root).as_posix()}\n{text}{note}")
    return "\n\n".join(parts)


def build_request(run: Run, rubric: dict, answer_key: str | None) -> tuple[str, str, str]:
    """(system, user message, evidence). `evidence` is everything quotable that the judge was shown."""
    transcript = _turns_text(run)
    files = _artifact_text(run) if rubric["artifacts"] else ""
    lines = []
    for q in rubric["questions"]:
        lines.append(f"{q['id']}. {q['question']}")
        if q["pass"]:
            lines.append(f"   Pass: {q['pass']}")
        if q["fail"]:
            lines.append(f"   Fail: {q['fail']}")
    persona = run.input("persona.md")
    mood = frontmatter.parse(persona)[0].get("mood", "") if persona else ""
    sections = [("RUBRIC", "\n".join(lines))]
    if rubric.get("purpose"):
        sections.append(("PURPOSE", "This is a focused continuation: the conversation up to the subject's "
                         "last reply was written in advance. Judge what the subject said after it.\n"
                         + rubric["purpose"]))
    sections.append(("TRANSCRIPT", transcript))
    if rubric["artifacts"]:
        sections.append(("FILES", files or "(the subject wrote no text files)"))
    if rubric["answer_key"] and answer_key is not None:
        sections.append(("ANSWER KEY", answer_key.strip()))
    if persona or run.knowledge or not run.meta.get("continuation"):
        sections.append(("PERSON", f"Mood: {mood or '(none given)'}\n\nKnowledge page:\n{run.knowledge or '(none)'}"))
    else:
        sections.append(("PERSON", "(no simulated person: the person's turns were written in advance)"))
    user = "\n\n".join(f"===== {name} =====\n{body}" for name, body in sections)
    return _frame(), user, transcript + "\n" + files


def _norm(s: str) -> str:
    s = s.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return " ".join(s.lower().replace('"', "").replace("'", "").split())


def parse_reply(text: str) -> dict | None:
    """The judge's JSON object, or None. Tolerates code fences and text around the object."""
    text = re.sub(r"```[a-zA-Z]*", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("answers"), list) else None


def _answers(rubric: dict, data: dict | None, evidence: str) -> list[dict]:
    got = {}
    for a in (data or {}).get("answers", []):
        try:
            got[str(a.get("id")).strip()] = a
        except AttributeError:
            continue
    hay, out = _norm(evidence), []
    for q in rubric["questions"]:
        a = got.get(str(q["id"])) or {}
        verdict = a.get("verdict") if a.get("verdict") in VERDICTS else "unclear"
        quote = a.get("quote") if isinstance(a.get("quote"), str) else ""
        missing = False
        if verdict != "unclear" and (not quote.strip() or _norm(quote) not in hay):
            verdict, missing = "unclear", True
        out.append({"id": q["id"], "question": q["question"], "verdict": verdict, "quote": quote,
                    "quote_not_found": missing})
    return out


def _sim(data: dict | None, evidence: str) -> dict:
    raw = (data or {}).get("sim")
    raw = raw if isinstance(raw, dict) else {}
    quote = raw.get("quote") if isinstance(raw.get("quote"), str) else ""
    sim = {k: raw.get(k) if raw.get(k) in SIM_VALUES[k] else "unclear" for k in SIM_KEYS}
    sim["quote"] = quote
    sim["quote_not_found"] = bool(quote.strip()) and _norm(quote) not in _norm(evidence)
    return sim


def _void(sim: dict, sim_turns: bool = True) -> tuple[bool, str]:
    if not sim_turns:
        return False, ""      # no generated sim turns (a focused continuation): nothing to void on
    failed = [k for k in ("in_character", "used_only_knowledge") if sim[k] == "fail"]
    if not failed:
        return False, ""
    if sim["quote_not_found"] or not sim["quote"].strip():
        return False, ""      # the judge could not point at the evidence; do not void on its say-so
    return True, "judge: sim failed " + ", ".join(failed)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _archive(target: Path, prefix: str) -> None:
    if not target.exists():
        return
    (target.parent / "history").mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest, n = target.parent / "history" / f"{prefix}-{stamp}.json", 1
    while dest.exists():
        n += 1
        dest = target.parent / "history" / f"{prefix}-{stamp}-{n}.json"
    target.rename(dest)


def judge_run(folder, case: Case, backend=None, seat=None) -> dict:
    """Judge one saved run and write judge.json (the old one moves to history/). Returns what was written.

    `seat` is a seat spec (default: the case's [judge] seat). A QuotaError is raised, not recorded.
    Any other failure, or a reply that is not the JSON asked for after one retry, is recorded as `error`.
    """
    from .runs import scrub_obj, write_json
    rubric = rubric_for_run(case, folder)
    if not rubric:
        raise CaseError("this case has no rubric; add rubric = \"rubric.md\" to case.toml")
    seat = seat or case.judge_seat
    if seat is None:
        raise CaseError("no judge seat: pass --seat or add [judge] seat = \"...\" to case.toml")
    backend = backend or make_backend(seat, replies=case.mock_judge, default="Okay.")
    folder = Path(folder)
    run = Run.load(folder, case)
    key = case.answer_key_path.read_bytes().decode() if case.answer_key_path else None
    system, user, evidence = build_request(run, rubric, key)
    messages = [Msg("user", user)]
    (folder / "raw").mkdir(exist_ok=True)
    data, error, costs, model = None, None, [], None
    for attempt in (1, 2):
        n = 1 + len(list((folder / "raw").glob("judge-*.json")))
        request = {"system": system, "messages": [{"role": m.role, "content": m.content} for m in messages]}
        try:
            res = backend.chat(system, messages)
        except QuotaError:
            raise
        except BackendError as e:
            write_json(folder / "raw" / f"judge-{n:02d}.json", {"request": request, "error": str(e), "raw": e.raw})
            error = f"judge call failed: {e}"
            break
        write_json(folder / "raw" / f"judge-{n:02d}.json", {"request": request, "result": {
            "text": res.text, "cost_usd": res.cost_usd, "model": res.model, "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out, "raw": res.raw, "error": res.error}})
        costs.append(res.cost_usd)
        model = res.model or model
        data = parse_reply(res.text)
        if data is not None:
            break
        error = "the judge did not reply with the JSON object asked for"
        messages = messages + [Msg("assistant", res.text or ""), Msg("user", RETRY)]
    if data is not None:
        error = None
    answers = _answers(rubric, data, evidence)
    sim = _sim(data, evidence)
    void, reason = _void(sim, any(t["side"] == "person" and not t.get("prefilled") and t["n"] >= 1
                                  for t in run.turns) or not run.meta.get("continuation"))
    known = [c for c in costs if c is not None]
    result = {"judged_at": _utc(), "seat": backend.describe(), "model": model,
              "cost_usd": sum(known) if known and len(known) == len(costs) else None,
              "rubric_sha256": hashlib.sha256(case.rubric_path.read_bytes()).hexdigest() if case.rubric_path else None,
              "continuation_sha256": hashlib.sha256((folder / "inputs" / "continuation.md").read_bytes()).hexdigest()
              if (folder / "inputs" / "continuation.md").is_file() else None,
              "answers": answers, "sim": sim, "void": void, "void_reason": reason, "error": error}
    target = folder / "judge.json"
    _archive(target, "judge")
    write_json(target, result)
    return scrub_obj(json.loads(json.dumps(result)))
