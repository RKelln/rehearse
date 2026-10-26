"""The board: a Markdown scoreboard over saved runs. Counts of runs, never percentages."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .runs import display_path, find_runs, scrub

FOOTER = ("Counts are runs, never percentages. One run is an anecdote. Read the transcripts.")


@dataclass
class Record:
    """What the board needs to know about one saved run."""
    path: Path
    meta: dict
    checks: dict | None
    manifest: dict = field(default_factory=dict)
    judge: dict | None = None

    @property
    def case(self): return self.meta.get("case", "?")
    @property
    def variant(self): return self.meta.get("variant", "?")
    @property
    def sim(self): return self.meta.get("sim", "?")
    @property
    def void(self): return bool((self.checks and self.checks.get("void")) or (self.judge and self.judge.get("void")))
    @property
    def stop(self): return self.meta.get("stop_reason", "")
    @property
    def errored(self): return self.stop.startswith("error") or self.stop == "interrupted"

    def result(self, check: str) -> str | None:
        """This run's result for a check ("sim check" is the sim check); None if absent."""
        if check.startswith(JUDGE_PREFIX):
            for a in (self.judge or {}).get("answers", []):
                if f"{JUDGE_PREFIX}{a['id']}" == check:
                    return a["verdict"]
            return None
        if not self.checks:
            return None
        if check == SIM_CHECK:
            return self.checks["sim_check"]["result"]
        for r in self.checks.get("results", []):
            if r["name"] == check:
                return r["result"]
        return None


SIM_CHECK = "sim check"
JUDGE_PREFIX = "judge "


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_records(specs) -> list[Record]:
    """Records for run folders, or folders of run folders. Comma-separated specs are allowed."""
    paths = [p for spec in ([specs] if isinstance(specs, str) else specs) for p in spec.split(",") if p]
    out = []
    for folder in find_runs(paths):
        meta = _json(folder / "meta.json")
        if meta is None:
            continue
        out.append(Record(folder, meta, _json(folder / "checks.json"), _json(folder / "inputs" / "manifest.json") or {},
                          _json(folder / "judge.json")))
    return out


EXCLUSIONS = (("error", "error"), ("unchecked", "unchecked"), ("skip", "skip"),
              ("void", "void"), ("errorrun", "error run"), ("unclear", "unclear"), ("unjudged", "unjudged"))
N_MEANS = ("Each cell is `k of n`: k runs passed out of n runs that gave a result (pass, partial or fail). "
           "`+p partial` counts inside n. Runs left out of n are listed in the cell: `+e error` (the check "
           "crashed), `+u unchecked` (no checks.json, or no result for that check), `+s skip`, `+v void` "
           "(failed the sim check), `+x error run` (the conversation ended in an error or was interrupted).")
JUDGE_MEANS = ("In `judge N` columns, `+c unclear` is a question the judge could not decide (or whose quote "
               "was not found), and `+u unjudged` is a run with no judge.json.")


def tally(records: list[Record], check: str) -> dict:
    """Count one check's results over the runs of one cell.

    n counts runs that gave pass, partial or fail. Every other run is counted under why it is
    left out: error (the check crashed), unchecked (no checks.json, or the check is not in it),
    skip, void (failed the sim check), errorrun (the conversation ended in an error).
    The sim check counts void runs, since its failure is the void.
    """
    t = {"k": 0, "n": 0, "partial": 0, "fail": 0,
         "error": 0, "unchecked": 0, "skip": 0, "void": 0, "errorrun": 0}
    judged = check.startswith(JUDGE_PREFIX)
    if judged:
        t.update(unclear=0, unjudged=0)
    any_judge = any(r.judge for r in records)
    for r in records:
        if r.errored:
            t["errorrun"] += 1
            continue
        if judged:
            if r.void:
                t["void"] += 1
            elif r.judge is None:
                t["unjudged"] += any_judge
            else:
                res = r.result(check)
                if res in ("pass", "partial", "fail"):
                    t["n"] += 1
                    t["k" if res == "pass" else res] += 1
                else:
                    t["unclear"] += 1
            continue
        if r.checks is None:
            t["unchecked"] += 1
            continue
        if r.void and check != SIM_CHECK:
            t["void"] += 1
            continue
        res = r.result(check)
        if res in ("pass", "partial", "fail"):
            t["n"] += 1
            t["k" if res == "pass" else res] += 1
        else:
            t[res if res in ("error", "skip") else "unchecked"] += 1
    return t


def exclusions(t: dict) -> tuple:
    """The counts of left-out runs, for comparing two cells."""
    return tuple(t.get(k, 0) for k, _ in EXCLUSIONS)


def cell_text(t: dict) -> str:
    """"k of n (+p partial)", then each exclusion; " ✓all" only when nothing at all is left out."""
    text = f"{t['k']} of {t['n']}" if t["n"] else "-"
    if t["partial"]:
        text += f" (+{t['partial']} partial)"
    left_out = [f"+{t[k]} {label}" for k, label in EXCLUSIONS if t.get(k)]
    if left_out:
        text += " " + " ".join(left_out)
    elif t["n"] >= 2 and t["k"] == t["n"]:
        text += " ✓all"
    return text


def _names(records: list[Record]) -> list[str]:
    seen = []
    for r in records:
        for x in (r.checks or {}).get("results", []):
            if x["name"] not in seen:
                seen.append(x["name"])
    return seen


def _judge_names(records: list[Record]) -> list[str]:
    seen = []
    for r in records:
        for a in (r.judge or {}).get("answers", []):
            if f"{JUDGE_PREFIX}{a['id']}" not in seen:
                seen.append(f"{JUDGE_PREFIX}{a['id']}")
    return seen


def _why_read(r: Record) -> list[str]:
    """Reasons a run should be read first."""
    why = []
    if r.errored:
        why.append(f"run ended: {r.stop}")
    if r.judge and r.judge.get("error"):
        why.append(f"judge error: {r.judge['error']}")
    if r.checks is None:
        return why + (["void: " + r.judge.get("void_reason", "")] if r.void else []) + ["no checks.json"]
    results = r.checks.get("results", [])
    for label, kind in (("failed", "fail"), ("check error", "error")):
        names = [x["name"] for x in results if x["result"] == kind]
        if names:
            why.append(f"{label}: {', '.join(names)}")
    if r.checks.get("void"):
        why.append(f"void: {r.checks.get('void_reason', '')}")
    elif r.void:
        why.append(f"void: {r.judge.get('void_reason', '')}")
    return why


def render(records: list[Record]) -> str:
    """The board as Markdown, one section per case."""
    lines = ["# Board", ""]
    for case in dict.fromkeys(r.case for r in records):
        rs = [r for r in records if r.case == case]
        lines += [f"## {case}", "", f"Runs: {len(rs)}", ""]
        combos = Counter((json.dumps(r.meta.get("seats")), json.dumps(r.meta.get("models"))) for r in rs)
        if len(combos) == 1:
            seats, models = rs[0].meta.get("seats", {}), rs[0].meta.get("models", {})
            for seat in ("subject", "sim"):
                lines.append(f"- {seat} seat: {seats.get(seat, '?')}, model: {', '.join(models.get(seat, [])) or 'unknown'}")
        else:
            lines.append("- **WARNING: the seats or models differ between these runs. Do not compare them.**")
            for (seats, models), n in combos.items():
                lines.append(f"  - {n} runs: seats {seats}, models {models}")
        if all(r.meta.get("mock") for r in rs):
            lines.append("- **PLUMBING ONLY, NOT A RESULT: every seat was the mock backend.**")
        stops = Counter(r.stop for r in rs)
        lines.append("- Stop reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(stops.items())))
        errors = sum(r.errored for r in rs)
        lines.append(f"- Runs that ended in an error: {errors}")
        lines.append(f"- Voided runs (failed the sim check{' or the judge' + chr(39) + 's sim part' if any(r.judge for r in rs) else ''}): {sum(r.void for r in rs)}")
        lines.append(f"- Runs with no checks.json: {sum(r.checks is None for r in rs)}")
        judge_names = _judge_names(rs)
        if judge_names:
            seats = list(dict.fromkeys(f"{j['seat']} ({j['model'] or 'unknown model'})"
                                       for j in (r.judge for r in rs) if j))
            lines.append("- Judge results are one model's opinion (directional). Judge: " + ", ".join(seats))
            first = next(r.judge for r in rs if r.judge)
            lines += [f"  - judge {a['id']}: {a['question']}" for a in first["answers"]]
        names = _names(rs) + [SIM_CHECK] + judge_names
        lines += ["", "| variant | sim | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
        cells = dict.fromkeys((r.variant, r.sim) for r in rs)
        for variant, sim in cells:
            cell = [r for r in rs if (r.variant, r.sim) == (variant, sim)]
            lines.append(f"| {variant} | {sim} | " + " | ".join(cell_text(tally(cell, n)) for n in names) + " |")
        lines += ["", N_MEANS + (" " + JUDGE_MEANS if judge_names else ""), "", "Read these first (a failed check, a check error, no checks.json, a void, or a run error):"]
        reasons = [(r, _why_read(r)) for r in rs]
        lines += [f"- {display_path(r.path)}: {'; '.join(w)}" for r, w in reasons if w] or ["- none"]
        lines.append("")
    lines += [FOOTER, ""]
    return "\n".join(lines)


def board(specs, out: Path | None = None) -> str:
    """Build the board for the given runs. Writes it to `out` too, if given. Returns the text."""
    text = scrub(render(load_records(specs)))
    if out:
        Path(out).write_text(text)
    return text
