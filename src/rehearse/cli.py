"""The `rehearse` command: run one cell, or a suite of them."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from pathlib import Path

from . import board as board_mod
from . import compare as compare_mod
from .backends import QuotaError
from .case import Case, CaseError, load_case
from .checks import check_run, find_case_file
from .runs import display_path, execute_run, find_runs, scrub

BANNER = "PLUMBING ONLY, NOT A RESULT: every seat was the mock backend."


def _say(text: str = "", err: bool = False) -> None:
    """Print, with email addresses and the home folder removed."""
    print(scrub(text), file=sys.stderr if err else sys.stdout)


def _pick(case_names: dict, wanted: str | None, what: str) -> list[str]:
    """All names, or the comma-separated ones asked for; unknown names are an error."""
    if not wanted:
        return list(case_names)
    names = [n.strip() for n in wanted.split(",") if n.strip()]
    for n in names:
        if n not in case_names:
            raise CaseError(f"unknown {what} {n!r}; this case has: {', '.join(case_names)}")
    return names


def run_suite(case: Case, variants: list[str], sims: list[str], repeats: int,
              run_fn=None, out=None) -> tuple[list, bool]:
    """Run every variant x sim x repeat in turn. A QuotaError stops the suite; the rest are 'not run'.

    Returns (results, stopped_early).
    """
    run_fn = run_fn or execute_run
    out = out or _say
    cells = [(v, s, r) for v in variants for s in sims for r in range(1, repeats + 1)]
    results = []
    for i, (v, s, r) in enumerate(cells):
        try:
            res = run_fn(case, v, s, repeat=r)
        except QuotaError as e:
            saved = getattr(e, "run_path", None)
            out(f"stopped: {e}" + (f" (partial run saved in {display_path(saved)})" if saved else ""))
            for v2, s2, r2 in cells[i + 1 if saved else i:]:
                out(f"not run: {v2} x {s2} (repeat {r2})")
            return results, True
        results.append(res)
        out(f"{display_path(res.path)}  stop: {res.stop}  turns: {res.subject_turns}")
    return results, False


def _banner(results) -> None:
    if results and all(r.mock for r in results):
        _say(BANNER)


def _saved_runs_command(args) -> int:
    """check, board and compare: they read saved runs and never call a model."""
    try:
        if args.command == "check":
            fixed = load_case(args.case) if args.case else None
            bad = False
            for folder in find_runs(args.runs):
                try:
                    case = fixed
                    if case is None and (found := find_case_file(folder)):
                        case = load_case(found)
                    data = check_run(folder, case)
                except (CaseError, ValueError, OSError, KeyError) as e:
                    _say(f"{display_path(folder)}  UNREADABLE: {e}")
                    bad = True
                    continue
                counts = Counter(r["result"] for r in data["results"])
                line = ", ".join(f"{counts[k]} {k}" for k in ("pass", "partial", "fail", "skip", "error") if counts[k])
                _say(f"{display_path(folder)}  {line or 'no case checks'}  sim check: {data['sim_check']['result']}"
                     + ("  VOID: " + data["void_reason"] if data["void"] else ""))
                bad = bad or counts["error"] > 0
            return 1 if bad else 0
        if args.command == "board":
            text = board_mod.board(args.runs, Path(args.out) if args.out else None)
        else:
            text = compare_mod.compare(args.before, args.after)
        _say(text)
        return 0
    except (CaseError, FileNotFoundError, ValueError) as e:
        _say(f"error: {e}", err=True)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rehearse", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run one variant with one sim")
    p.add_argument("case", help="a folder holding case.toml, or the path to case.toml")
    p.add_argument("--variant", required=True)
    p.add_argument("--sim", required=True)
    p.add_argument("--repeats", type=int, default=1)
    s = sub.add_parser("suite", help="run every variant with every sim")
    s.add_argument("case")
    s.add_argument("--variants", help="comma-separated; default all")
    s.add_argument("--sims", help="comma-separated; default all")
    s.add_argument("--repeats", type=int, default=1)
    s.add_argument("--dry-run", action="store_true", help="list the runs without running them")
    c = sub.add_parser("check", help="rerun the checks on saved runs (no model calls)")
    c.add_argument("runs", nargs="+", help="run folders, or folders of run folders")
    c.add_argument("--case", help="the case folder or case.toml (default: found above each run)")
    b = sub.add_parser("board", help="a Markdown scoreboard of saved runs")
    b.add_argument("runs", nargs="+")
    b.add_argument("--out", help="also write the board to this file")
    m = sub.add_parser("compare", help="what moved between two sets of runs")
    m.add_argument("before", help="a folder of runs (or several, comma-separated)")
    m.add_argument("after")
    args = parser.parse_args(argv)
    if args.command in ("check", "board", "compare"):
        return _saved_runs_command(args)

    try:
        if args.repeats < 1:
            raise CaseError("--repeats must be 1 or more")
        case = load_case(args.case)
        if args.command == "run":
            variants = _pick(case.variants, args.variant, "variant")
            sims = _pick(case.sims, args.sim, "sim")
        else:
            variants = _pick(case.variants, args.variants, "variant")
            sims = _pick(case.sims, args.sims, "sim")
        if args.command == "suite" and args.dry_run:
            for s_ in sims:
                case.persona(s_)       # fail now, not after spending, if a persona is broken
            total = len(variants) * len(sims) * args.repeats
            _say(f"{total} runs: {len(variants)} variants x {len(sims)} sims x {args.repeats} repeats")
            for v in variants:
                for s_ in sims:
                    _say(f"  {v} x {s_} (x{args.repeats})")
            _say("cost estimate: not available yet")
            return 0
        results, stopped = run_suite(case, variants, sims, args.repeats)
    except CaseError as e:
        _say(f"error: {e}", err=True)
        return 2
    except KeyboardInterrupt:
        _say("interrupted; the run in progress was saved", err=True)
        return 1
    _banner(results)
    failed = any(r.stop.startswith("error") or r.stop == "interrupted" for r in results)
    return 1 if stopped or failed else 0


if __name__ == "__main__":
    sys.exit(main())
