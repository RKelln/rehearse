"""The `rehearse` command: run one cell, or a suite of them."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter

from pathlib import Path

from . import board as board_mod
from . import compare as compare_mod
from .backends import BackendError, QuotaError, is_mock_spec, make_backend
from . import background
from .case import Case, CaseError, load_case
from .scaffold import init_case
from .checks import check_run, find_case_file
from .judge import judge_run, rubric_for_run
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
              keep_work: bool = False, judge: bool = False, cells: list | None = None) -> tuple[list, bool]:
    """Run every variant x sim x repeat in turn (or the given `cells`, a list of (variant, sim, repeat)).

    A QuotaError stops the suite. So does reaching `max_usd`. The runs left are listed as 'not run'.
    `spend` collects the known cost per seat. Returns (results, stopped_early).
    """
    run_fn = run_fn or execute_run
    out = out or _say
    spend = spend if spend is not None else Spend()
    cells = cells or [(v, s, r) for v in variants for s in sims for r in range(1, repeats + 1)]
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
        if judge:
            try:
                data = _judge_one(res.path, case, None, spend, out)
            except QuotaError as e:
                out(f"stopped: {e}")
                for v2, s2, r2 in cells[i + 1:]:
                    out(f"not run: {v2} x {s2} (repeat {r2})")
                return results, True
    return results, False


def _run_continuation(case: Case, variant: str, label: str, **kw):
    """run_suite's run function for a continuation labelled "cont:<id>"."""
    return execute_run(case, variant, None, continuation=label[len("cont:"):], **kw)


def _judge_one(folder, case: Case, seat, spend: Spend, out) -> dict:
    """Judge one run, count its cost, and say what came of it."""
    data = judge_run(folder, case, seat=seat)
    spend.add_judge(data["cost_usd"])
    counts = Counter(a["verdict"] for a in data["answers"])
    line = ", ".join(f"{counts[k]} {k}" for k in ("pass", "partial", "fail", "unclear") if counts[k])
    out(f"  judge: {line}" + (f"  VOID: {data['void_reason']}" if data["void"] else "")
        + (f"  ERROR: {data['error']}" if data["error"] else ""))
    return data


def _past_judge_cost(case: Case) -> tuple[float, int] | None:
    """(mean known cost of one judging, number of past judgings) for this case, or None."""
    costs = []
    for p in sorted(Path(case.runs_dir).glob("*/judge.json")) if Path(case.runs_dir).is_dir() else []:
        try:
            c = json.loads(p.read_text()).get("cost_usd")
        except (OSError, ValueError):
            continue
        if isinstance(c, (int, float)):
            costs.append(c)
    return (sum(costs) / len(costs), len(costs)) if costs else None


def _judge_command(args) -> int:
    """rehearse judge: judge saved runs. Live seats print a plan and ask first."""
    if args.max_usd is not None and not (math.isfinite(args.max_usd) and args.max_usd > 0):
        raise CaseError("--max-usd must be a number more than 0")
    fixed = load_case(args.case) if args.case else None
    cases: dict = {}
    todo = []                                   # (folder, case)
    for folder in find_runs(args.runs):
        case = fixed
        if case is None:
            found = find_case_file(folder)
            if not found:
                raise CaseError(f"{display_path(folder)}: no case.toml above this run; pass --case")
            case = cases.setdefault(found, load_case(found))
        todo.append((folder, case))
    seats = {id(c): args.seat or c.judge_seat for _, c in todo}
    for folder, c in todo:
        if seats[id(c)] is None:
            raise CaseError("no judge seat: pass --seat or add [judge] seat = \"...\" to case.toml")
        if not rubric_for_run(c, folder):
            raise CaseError(f"case {c.name} has no rubric; add rubric = \"rubric.md\" to case.toml")
    live = any(not is_mock_spec(seats[id(c)]) for _, c in todo)
    names = ", ".join(dict.fromkeys(_seat_name(seats[id(c)]) for _, c in todo))
    _say(f"{len(todo)} runs to judge, judge seat: {names}")
    if live:
        first = _past_judge_cost(todo[0][1])
        _say(f"cost estimate: about ${first[0] * len(todo):.2f} (from {first[1]} past judge runs of this case)"
             if first else "cost estimate: no past judge runs")
    else:
        _say("cost estimate: none, the judge seat is the mock backend")
    if args.dry_run:
        for folder, _ in todo:
            _say(f"  {display_path(folder)}")
        return 0
    if live and not _confirm(len(todo), args.yes, "judgings"):
        _say("cancelled; nothing was judged")
        return 1
    spend, bad = Spend(), False
    for i, (folder, case) in enumerate(todo):
        if args.max_usd is not None and spend.total >= args.max_usd:
            _say(f"stopped: the budget of ${args.max_usd:g} is reached (spent ${spend.total:.4f})")
            for f2, _ in todo[i:]:
                _say(f"not judged: {display_path(f2)}: budget reached")
            _say(spend.line())
            return 1
        _say(display_path(folder))
        try:
            data = _judge_one(folder, case, args.seat, spend, _say)
        except QuotaError as e:
            _say(f"stopped: {e}")
            return 1
        bad = bad or bool(data["error"])
    if live:
        _say(spend.line())
    return 1 if bad else 0


def _confirm(total: int, yes: bool, what: str = "runs") -> bool:
    """Ask before a live command. Without a terminal and without --yes, refuse."""
    if yes:
        return True
    if not sys.stdin.isatty():
        raise CaseError("this would call a live model. Pass --yes to start without being asked "
                        "(there is no terminal to ask on)")
    sys.stdout.write(f"Start {total} {what}? [y/N] ")
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
            pairs = {}
            for spec in args.pair:
                old, sep, new = spec.partition("=")
                if not (sep and old.strip() and new.strip()):
                    raise ValueError(f"--pair needs OLD=NEW, got {spec!r}")
                pairs[old.strip()] = new.strip()
            text = compare_mod.compare(args.before, args.after, pairs)
        _say(text)
        return 0
    except (CaseError, FileNotFoundError, ValueError) as e:
        _say(f"error: {e}", err=True)
        return 2


def _live_flags(parser, keep_work: bool = True) -> None:
    parser.add_argument("--yes", action="store_true", help="do not ask before a live run")
    parser.add_argument("--max-usd", type=float, metavar="X", help="stop starting new runs once X dollars are spent")
    if keep_work:
        parser.add_argument("--keep-work", action="store_true", help="keep an agent's work folder after the run")
    parser.add_argument("--judge", action="store_true", help="judge each run right after its checks")
    parser.add_argument("--no-judge", action="store_true", help="do not judge (when the case judges by default)")
    parser.add_argument("--background", action="store_true",
                        help="check the case, then run detached with --yes (ask the owner first); see `rehearse status`")


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
    i = sub.add_parser("init", help="write a small working case (on mock seats) to fill in")
    i.add_argument("folder")
    i.add_argument("--name")
    i.add_argument("--adapter", choices=["chat", "agent"], default="chat")
    i.add_argument("--prompt", help="a file holding the prompt under test")
    i.add_argument("--subject-seat", default="mock")
    i.add_argument("--sim-seat", default="mock")
    i.add_argument("--judge-seat", default="mock")
    st = sub.add_parser("status", help="background runs started with --background, and their logs")
    st.add_argument("case")
    k = sub.add_parser("continue", help="run the case's focused continuations (prefilled transcripts)")
    k.add_argument("case")
    k.add_argument("--only", help="comma-separated continuation ids; default all")
    k.add_argument("--variants", help="comma-separated; default all")
    k.add_argument("--repeats", type=int, default=1)
    k.add_argument("--dry-run", action="store_true", help="list the runs and the cost estimate; run nothing")
    _live_flags(k, keep_work=False)
    c = sub.add_parser("check", help="rerun the checks on saved runs (no model calls)")
    c.add_argument("runs", nargs="+", help="run folders, or folders of run folders")
    c.add_argument("--case", help="the case folder or case.toml (default: found above each run)")
    j = sub.add_parser("judge", help="judge saved runs with the case's rubric (a model call)")
    j.add_argument("runs", nargs="+", help="run folders, or folders of run folders")
    j.add_argument("--case", help="the case folder or case.toml (default: found above each run)")
    j.add_argument("--seat", help="the judge seat, backend[:model] (default: [judge] seat in case.toml)")
    j.add_argument("--dry-run", action="store_true", help="list the runs, the seat and the estimate; call nothing")
    j.add_argument("--yes", action="store_true", help="do not ask before a live judging")
    j.add_argument("--max-usd", type=float, metavar="X", help="stop judging once X dollars are spent")
    b = sub.add_parser("board", help="a Markdown scoreboard of saved runs")
    b.add_argument("runs", nargs="+")
    b.add_argument("--out", help="also write the board to this file")
    m = sub.add_parser("compare", help="what moved between two sets of runs")
    m.add_argument("before", help="a folder of runs (or several, comma-separated)")
    m.add_argument("after")
    m.add_argument("--pair", action="append", default=[], metavar="OLD=NEW",
                   help="compare the before variant OLD with the after variant NEW (repeatable)")
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            files = init_case(args.folder, args.name, args.adapter, args.prompt,
                              args.subject_seat, args.sim_seat, args.judge_seat)
        except CaseError as e:
            _say(f"error: {e}", err=True)
            return 2
        for f in files:
            _say(f"wrote {display_path(f)}")
        _say(f"next: edit the sim, knowledge and rubric, then `rehearse suite {args.folder} --dry-run` "
             "(see docs/NEW-CASE.md)")
        return 0
    if args.command == "status":
        try:
            lines = background.status(load_case(args.case).runs_dir)
        except CaseError as e:
            _say(f"error: {e}", err=True)
            return 2
        _say("\n".join(lines) or "no background runs")
        return 0
    if args.command in ("check", "board", "compare"):
        return _saved_runs_command(args)
    if args.command == "judge":
        try:
            return _judge_command(args)
        except (CaseError, BackendError, FileNotFoundError, ValueError) as e:
            _say(f"error: {e}", err=True)
            return 2
        except KeyboardInterrupt:
            _say("interrupted", err=True)
            return 1

    try:
        if args.repeats < 1:
            raise CaseError("--repeats must be 1 or more")
        if getattr(args, "max_usd", None) is not None and not (math.isfinite(args.max_usd) and args.max_usd > 0):
            raise CaseError("--max-usd must be a number more than 0")
        case = load_case(args.case)
        if getattr(args, "background", False):
            raw = list(sys.argv[1:] if argv is None else argv)
            rest = [a for a in raw if a not in ("--background", "--yes")] + ["--yes"]
            log = background.start(rest, case.runs_dir)
            _say(f"started in the background; follow it with `rehearse status {args.case}`\nlog: {display_path(log)}")
            return 0
        cells = None
        if args.command == "continue":
            if not case.continuations:
                raise CaseError("this case has no continuations: add a continuations folder "
                                "(see docs/CHECKS.md, Focused continuations)")
            variants = _pick(case.variants, args.variants, "variant")
            ids = _pick(case.continuations, args.only, "continuation")
            for cid in ids:
                if case.continuations[cid].sim:
                    case.persona(case.continuations[cid].sim)
            sims = [f"cont:{cid}" for cid in ids]
            cells = [(v, f"cont:{cid}", r) for v in variants for cid in ids
                     if case.continuations[cid].variant in (None, v) for r in range(1, args.repeats + 1)]
            if not cells:
                raise CaseError("no runs: the chosen continuations are limited to variants that were not chosen")
            total = len(cells)
            case.check_prompt_limit(variants)
            _say(f"{total} runs: {len(ids)} continuations x variants ({', '.join(variants)}) x {args.repeats} repeats")
            for v, s_, r in cells[::args.repeats]:
                cont = case.continuations[s_[5:]]
                mode = f"then sim {cont.sim}" if cont.sim else "one reply"
                _say(f"  {v} x {s_} (x{args.repeats}): {sum(1 for t in cont.turns)} prefilled turns, {mode}")
            sim_used = any(case.continuations[s_[5:]].sim for _, s_, _ in cells)
            _say(f"seats: subject {_seat_name(case.subject_seat)}, sim "
                 + (_seat_name(case.sim_seat) if sim_used else "none (no continuation names a sim)"))
        else:
            sim_used = True
            if args.command == "run":
                variants = _pick(case.variants, args.variant, "variant")
                sims = _pick(case.sims, args.sim, "sim")
            else:
                variants = _pick(case.variants, args.variants, "variant")
                sims = _pick(case.sims, args.sims, "sim")
            for s_ in sims:
                case.persona(s_)       # fail now, not after spending, if a persona is broken
            case.check_prompt_limit(variants)
            total = len(variants) * len(sims) * args.repeats
            _say(f"{total} runs: {len(variants)} variants x {len(sims)} sims x {args.repeats} repeats")
            for v in variants:
                for s_ in sims:
                    _say(f"  {v} x {s_} (x{args.repeats})")
            _say(f"seats: subject {_seat_name(case.subject_seat)}, sim {_seat_name(case.sim_seat)}")
        _say(estimate_line(case, total, args.command == "continue", sim_used))
        if args.command in ("suite", "continue") and args.dry_run:
            return 0
        if args.judge and args.no_judge:
            raise CaseError("use --judge or --no-judge, not both")
        judging = args.judge or (case.judge_default and not args.no_judge)
        if judging and not ((case.rubric or args.command == "continue") and case.judge_seat):
            raise CaseError("judging needs a rubric and a [judge] seat in case.toml")
        live = is_live(case, sim_used) or (judging and not is_mock_spec(case.judge_seat))
        if judging:
            _say(f"judge seat: {_seat_name(case.judge_seat)}")
        if live:
            if not _confirm(total, args.yes):
                _say("cancelled; nothing was run")
                return 1
        spend = Spend()
        results, stopped = run_suite(case, variants, sims, args.repeats, max_usd=args.max_usd,
                                     spend=spend, keep_work=getattr(args, "keep_work", False), judge=judging,
                                     cells=cells, run_fn=_run_continuation if cells else None)
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
