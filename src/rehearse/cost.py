"""Cost: an estimate from past runs of the same case, and a running total for a budget cap."""

from __future__ import annotations

import json
from pathlib import Path

from .backends import is_mock_spec
from .case import Case

SEATS = ("subject", "sim")


def past_runs(case: Case, continuation: bool | None = None) -> list[dict]:
    """The meta.json of every saved run of this case in its runs folder.

    `continuation` True keeps only focused continuations, False only the other runs, None all.
    """
    out = []
    for meta in sorted(Path(case.runs_dir).glob("*/meta.json")) if Path(case.runs_dir).is_dir() else []:
        try:
            data = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if data.get("case") == case.name and (continuation is None or bool(data.get("continuation")) == continuation):
            out.append(data)
    return out


def seat_is_mock(case: Case, seat: str) -> bool:
    return is_mock_spec(case.subject_seat if seat == "subject" else case.sim_seat)


def is_live(case: Case, sim_used: bool = True) -> bool:
    """A command is live when any seat it uses is not the mock backend."""
    return not all(seat_is_mock(case, s) for s in SEATS if sim_used or s != "sim")


def estimate(case: Case, planned: int, continuation: bool | None = None, sim_used: bool = True) -> dict:
    """Mean known cost per run, per seat, times the runs planned.

    Returns {"based_on": number of past runs, "seats": {seat: usd, or None if unknown or mock},
    "total": usd or None}.
    """
    runs = past_runs(case, continuation)
    seats = {}
    for seat in SEATS:
        if seat == "sim" and not sim_used:
            seats[seat] = None
            continue
        known = [m["cost"][seat]["known_usd"] if "known_usd" in m["cost"][seat] else m["cost"][seat]["usd"]
                 for m in runs if m.get("cost", {}).get(seat, {}).get("usd") is not None]
        seats[seat] = sum(known) / len(known) * planned if known else None
    total = sum(v for v in seats.values() if v is not None) if any(v is not None for v in seats.values()) else None
    return {"based_on": len(runs), "seats": seats, "total": total}


def estimate_line(case: Case, planned: int, continuation: bool | None = None, sim_used: bool = True) -> str:
    """One plain line for the screen. `sim_used` False: no run here calls the sim seat."""
    if not is_live(case, sim_used):
        return "cost estimate: none, every seat is the mock backend"
    est = estimate(case, planned, continuation, sim_used)
    if not est["based_on"]:
        return "cost estimate: no past runs to estimate from"
    parts, unknown = [], []
    for seat in SEATS:
        if seat == "sim" and not sim_used:
            parts.append("sim none (not used)")
        elif seat_is_mock(case, seat):
            parts.append(f"{seat} none (mock)")
        elif est["seats"][seat] is None:
            parts.append(f"{seat} unknown")
            unknown.append(seat)
        else:
            parts.append(f"{seat} about ${est['seats'][seat]:.2f}")
    total = ""
    if est["total"] is not None:
        total = (f"; total at least ${est['total']:.2f} (plus unknown for {', '.join(unknown)})" if unknown
                 else f"; total about ${est['total']:.2f}")
    return (f"cost estimate (from {est['based_on']} past runs of this case, {planned} runs planned): "
            + ", ".join(parts) + total)


class Spend:
    """Running total of known cost per seat, and how many calls had no cost to add."""

    def __init__(self):
        self.usd = {s: 0.0 for s in SEATS}
        self.calls = {s: 0 for s in SEATS}
        self.unknown = {s: 0 for s in SEATS}
        self.mock = {s: False for s in SEATS}
        self.subscription = {s: True for s in SEATS}    # every call so far was on a subscription seat
        self.judge_usd = 0.0
        self.judge_calls = 0
        self.judge_unknown = 0

    def add_judge(self, cost_usd: float | None) -> None:
        self.judge_calls += 1
        if cost_usd is None:
            self.judge_unknown += 1
        else:
            self.judge_usd += cost_usd

    def add(self, cost: dict, mock_seats: dict | None = None) -> None:
        for seat in SEATS:
            c = cost.get(seat) or {}
            self.usd[seat] += c.get("known_usd", c.get("usd") or 0.0)
            self.unknown[seat] += c.get("unknown_calls", 0)
            self.calls[seat] += c.get("calls", 0)
            if c.get("calls", 0) and c.get("subscription_calls", 0) < c["calls"]:
                self.subscription[seat] = False
            if (mock_seats or {}).get(seat):
                self.mock[seat] = True

    @property
    def total(self) -> float:
        return sum(self.usd.values()) + self.judge_usd

    def line(self) -> str:
        parts = []
        for seat in SEATS:
            known = self.calls[seat] - self.unknown[seat]
            if self.mock[seat]:
                text = "mock (no cost)"
            elif not self.calls[seat]:
                text = "not called"
            elif known <= 0:
                text = "subscription (not metered)" if self.subscription[seat] else "unknown"
            elif self.unknown[seat]:
                text = f"${self.usd[seat]:.4f} + unknown for {self.unknown[seat]} calls"
            else:
                text = f"${self.usd[seat]:.4f}"
            parts.append(f"{seat} {text}")
        if self.judge_calls:
            known = self.judge_calls - self.judge_unknown
            parts.append("judge " + ("unknown" if known <= 0 else f"${self.judge_usd:.4f}"
                                     + (f" + unknown for {self.judge_unknown} calls" if self.judge_unknown else "")))
        return "spent: " + ", ".join(parts)
