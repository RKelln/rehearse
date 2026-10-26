"""One conversation: the opening line, then subject and sim take turns until a stop rule fires.

Stop rules, in the order they are checked after each subject reply:
  subject-stop  the subject's reply matches the case's stop_pattern
  turn-cap      the subject has replied max_turns times
then after each sim reply:
  sim-stop      the sim's reply is its stop word, or ends with it (see `is_stop`)
  error: ...    a backend failed, or the sim answered with nothing twice

Isolation: the sim is only ever sent the filled frame and the transcript. The subject is only
ever sent the variant text and the transcript.

Parts adapted from an earlier CC0-licensed harness by the same author: the sim turn (label stripping, retry once
on an empty reply).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .adapters import ChatAdapter, sim_messages
from .backends import Backend, BackendError, Msg, QuotaError, SeatResult
from .case import Case, Persona

PLACEHOLDER = re.compile(r"\{(persona|mood|knowledge|stop|turn_line|opening)\}")
_EDGE = " \t\r\n\"'`\u201c\u201d\u2018\u2019.,!?;:()[]{}*_-\u2013\u2014"


@dataclass
class Turn:
    side: str      # "person" (the sim) or "subject"
    n: int         # turn number within that side; the person's scripted opening is 0
    text: str
    call: str | None = None   # file name in raw/ of the seat call that made this turn
    label_stripped: bool = False   # the sim wrote a "Name:" label and it was removed


@dataclass
class Outcome:
    turns: list[Turn] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)   # every seat call: request and result
    stop: str = ""
    stop_with_text: bool = False                       # the sim stopped but said more than the stop word
    interrupted: bool = False
    started: str = ""
    ended: str = ""
    quota: QuotaError | None = None                    # set when a QuotaError ended the run

    def count(self, side: str) -> int:
        return sum(1 for t in self.turns if t.side == side and not (side == "person" and t.n == 0))


class _Halt(Exception):
    def __init__(self, reason: str, quota: QuotaError | None = None):
        super().__init__(reason)
        self.reason = reason
        self.quota = quota


def is_stop(text: str, stop: str) -> tuple[bool, bool]:
    """Does the sim's reply end the talk? Returns (stops, with_extra_text).

    It stops when the reply, minus surrounding spaces, quotes and punctuation, is the stop word,
    or when the reply's last word is the stop word. Case does not matter.
    """
    word = stop.strip(_EDGE).lower()
    if text.strip(_EDGE).lower() == word:
        return True, False
    tokens = text.split()
    if tokens and tokens[-1].strip(_EDGE).lower() == word:
        return True, True
    return False, False


def fill_frame(frame: str, persona: Persona, turn_line: str, opening: str = "") -> str:
    """Put the persona into the frame. One pass, so braces in a knowledge page are safe."""
    values = {"persona": persona.body, "mood": persona.mood, "knowledge": persona.knowledge,
              "stop": persona.stop, "turn_line": turn_line, "opening": opening}
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], frame).strip() + "\n"


def clean_sim_reply(text: str, persona: Persona) -> str:
    """Strip a leading label such as "Dana:" or "Person:" from the sim's reply."""
    names = {"person", "user", "human", "me", persona.name.lower(), persona.sim.lower(),
             re.split(r"[-_ ]", persona.sim)[0].lower(), re.split(r"[-_ ]", persona.name)[0].lower()}
    text = text.strip()
    m = re.match(r"^\W*([\w .'-]{1,40}?)\s*:\s*", text)
    if m and m.group(1).strip().lower() in names:
        text = text[m.end():]
    return text.strip()


def _call(out: Outcome, seat: str, do, request) -> tuple[SeatResult, str]:
    """One seat call, recorded. `do()` makes the call; `request()` says what was sent.

    Any failure stops the run by raising _Halt; the failed call is still recorded.
    """
    index = 1 + sum(1 for c in out.calls if c["seat"] == seat)
    name = f"{seat}-{index:02d}.json"
    error = None
    try:
        res = do()
    except Exception as e:      # a backend may fail in any way; the run must still be saved
        error = e
        msg = str(e) if isinstance(e, BackendError) else f"{type(e).__name__}: {e}"
        res = SeatResult(text="", error=msg, raw=getattr(e, "raw", None))
    out.calls.append({"seat": seat, "index": index, "file": name, "request": request(),
                      "result": asdict(res)})
    if error is not None:
        raise _Halt(f"error: {res.error}", error if isinstance(error, QuotaError) else None)
    if res.error:
        raise _Halt(f"error: {res.error}")
    return res, name


def _sim_reply(out: Outcome, sim: Backend, persona: Persona, frame: str, turn_no: int,
               opening: str) -> tuple[str, str, bool]:
    system = fill_frame(frame, persona, persona.turn_line(turn_no), opening)
    messages = sim_messages(out.turns)
    request = {"system": system, "messages": [asdict(m) for m in messages]}
    for _ in range(2):
        res, name = _call(out, "sim", lambda: sim.chat(system, messages), lambda: request)
        text = clean_sim_reply(res.text, persona)
        if text:
            return text, name, text != res.text.strip()
    raise _Halt("error: the sim gave an empty reply twice")


def run_conversation(case: Case, adapter: ChatAdapter, sim: Backend, persona: Persona,
                     frame: str) -> Outcome:
    """Run one conversation and return everything that happened.

    Backend failures end the run with a stop reason; a QuotaError is returned in `quota` and a
    Ctrl-C sets `interrupted`, so the caller can save the run first and then re-raise.
    """
    out = Outcome(started=_now())
    opening = persona.opening or case.opening
    try:
        if not opening:
            raise _Halt("error: no opening line: set 'opening' in the persona or in [subject]")
        out.turns.append(Turn("person", 0, opening))
        while True:
            res, name = _call(out, "subject", lambda: adapter.reply(out.turns), lambda: adapter.last_request)
            if not res.text.strip():
                raise _Halt("error: the subject gave an empty reply")
            n = out.count("subject") + 1
            out.turns.append(Turn("subject", n, res.text.strip(), name))
            if case.stop_pattern and case.stop_pattern.search(res.text):
                out.stop = "subject-stop"
                break
            if n >= case.max_turns:
                out.stop = "turn-cap"
                break
            text, name, stripped = _sim_reply(out, sim, persona, frame, n, opening)
            out.turns.append(Turn("person", n, text, name, stripped))
            stops, with_text = is_stop(text, persona.stop)
            if stops:
                out.stop, out.stop_with_text = "sim-stop", with_text
                break
    except _Halt as h:
        out.stop = h.reason
        out.quota = h.quota
    except KeyboardInterrupt:
        out.stop = "interrupted"
        out.interrupted = True
    out.ended = _now()
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
