"""The `rehearse` command: run one cell, or a suite of them."""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter

from pathlib import Path

from . import board as board_mod
from . import compare as compare_mod
from .backends import BackendError, QuotaError
from .case import Case, CaseError, load_case
from .checks import check_run, find_case_file
from .cost import Spend, estimate_line, is_live, seat_is_mock
from .runs import display_path, execute_run, find_runs, scrub

BANNER = "PLUMBING ONLY, NOT A RESULT: every seat was the mock backend."


def _say(text: str = "", err: bool = False) -> None:
    """Print, with email addresses and the home folder removed."""
    print(scrub(text), file=sys.stderr if err else sys.stdout)


def _seat_name(seat) -> str:
    """A seat as the case wrote it, short, for the plan."""
    return seat if isinstance(seat, str) else str(seat.get("backend")) + (f":{seat['model']}" if seat.get("model") else "")


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
              run_fn=None, out=None, max_usd: float | None = None, spend: Spend | None = None,
              keep_work: bool = False) -> tuple[list, bool]:
    """Run every variant x sim x repeat in turn.

    A QuotaError stops the suite. So does reaching `max_usd`. The runs left are listed as 'not run'.
    `spend` collects the known cost per seat. Returns (results, stopped_early).
    """
    run_fn = run_fn or execute_run
    out = out or _say
    spend = spend if spend is not None else Spend()
    cells = [(v, s, r) for v in variants for s in sims for r in range(1, repeats + 1)]
    results, warned = [], set()
    for i, (v, s, r) in enumerate(cells):
        if max_usd is not None and spend.total >= max_usd:
            out(f"stopped: the budget of ${max_usd:g} is reached (spent ${spend.total:.4f})")
            for v2, s2, r2 in cells[i:]:
                out(f"not run: {v2} x {s2} (repeat {r2}): budget reached")
            return results, True
        kw = {"repeat": r}
        if keep_work:
            kw["keep_work"] = True
        if max_usd is not None:
            kw["budget_usd"] = max_usd - spend.total
        try:
            res = run_fn(case, v, s, **kw)
        except QuotaError as e:
            saved = getattr(e, "run_path", None)
            spend.add(getattr(e, "run_cost", None) or {})       # the partial run still cost something
            out(f"stopped: {e}" + (f" (partial run saved in {display_path(saved)})" if saved else ""))
            for v2, s2, r2 in cells[i + 1 if saved else i:]:
                out(f"not run: {v2} x {s2} (repeat {r2})")
            return results, True
        results.append(res)
        cost = getattr(res, "cost", None) or {}
        spend.add(cost, getattr(res, "mock_seats", None))
        for seat in ("subject", "sim"):
            if (max_usd is not None and seat not in warned and cost.get(seat, {}).get("unknown_calls")
                    and not (getattr(res, "mock_seats", None) or {}).get(seat)):
                warned.add(seat)
                out(f"warning: the {seat} seat does not report its cost, so --max-usd cannot count it")
        out(f"{display_path(res.path)}  stop: {res.stop}  turns: {res.subject_turns}")
    return results, False


def _confirm(total: int, yes: bool) -> bool:
    """Ask before a live command. Without a terminal and without --yes, refuse."""
    if yes:
        return True
    if not sys.stdin.isatty():
        raise CaseError("this would call a live model. Pass --yes to start without being asked "
                        "(there is no terminal to ask on)")
    sys.stdout.write(f"Start {total} runs? [y/N] ")
    sys.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


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


def _live_flags(parser) -> None:
    parser.add_argument("--yes", action="store_true", help="do not ask before a live run")
    parser.add_argument("--max-usd", type=float, metavar="X", help="stop starting new runs once X dollars are spent")
    parser.add_argument("--keep-work", action="store_true", help="keep an agent's work folder after the run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rehearse", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run one variant with one sim")
    p.add_argument("case", help="a folder holding case.toml, or the path to case.toml")
    p.add_argument("--variant", required=True)
    p.add_argument("--sim", required=True)
    p.add_argument("--repeats", type=int, default=1)
    _live_flags(p)
    s = sub.add_parser("suite", help="run every variant with every sim")
    s.add_argument("case")
    s.add_argument("--variants", help="comma-separated; default all")
    s.add_argument("--sims", help="comma-separated; default all")
    s.add_argument("--repeats", type=int, default=1)
    s.add_argument("--dry-run", action="store_true", help="list the runs and the cost estimate; run nothing")
    _live_flags(s)
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
        if getattr(args, "max_usd", None) is not None and not (math.isfinite(args.max_usd) and args.max_usd > 0):
            raise CaseError("--max-usd must be a number more than 0")
        case = load_case(args.case)
        if args.command == "run":
            variants = _pick(case.variants, args.variant, "variant")
            sims = _pick(case.sims, args.sim, "sim")
        else:
            variants = _pick(case.variants, args.variants, "variant")
            sims = _pick(case.sims, args.sims, "sim")
        for s_ in sims:
            case.persona(s_)       # fail now, not after spending, if a persona is broken
        total = len(variants) * len(sims) * args.repeats
        _say(f"{total} runs: {len(variants)} variants x {len(sims)} sims x {args.repeats} repeats")
        for v in variants:
            for s_ in sims:
                _say(f"  {v} x {s_} (x{args.repeats})")
        _say(f"seats: subject {_seat_name(case.subject_seat)}, sim {_seat_name(case.sim_seat)}")
        _say(estimate_line(case, total))
        if args.command == "suite" and args.dry_run:
            return 0
        live = is_live(case)
        if live:
            if not _confirm(total, args.yes):
                _say("cancelled; nothing was run")
                return 1
        spend = Spend()
        results, stopped = run_suite(case, variants, sims, args.repeats, max_usd=args.max_usd,
                                     spend=spend, keep_work=args.keep_work)
        if live:
            _say(spend.line())
    except (CaseError, BackendError) as e:      # a quota error never gets here: run_suite handles it
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
