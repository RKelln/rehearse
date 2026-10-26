"""The mock backend: scripted replies, no model. For testing the tool itself.

Parts adapted from an earlier CC0-licensed harness by the same author: the recording test-double idea.
"""

from __future__ import annotations

from .base import Backend, Msg, SeatResult


class MockBackend(Backend):
    """Answers from a script and keeps every request it receives in `.calls`.

    `replies` is a list. Each item is a string, a ready-made SeatResult, or an exception
    (which is raised). When the list runs out, the backend repeats it (`cycle=True`) or
    answers `default`.
    """

    # TODO(step 3): add "agent", "resume" and "fork" when the agent adapter can use them.
    capabilities = frozenset({"chat"})
    is_mock = True

    def __init__(self, replies=(), default: str = "Okay.", cycle: bool = False):
        self.replies = list(replies)
        self.default = default
        self.cycle = cycle
        self.calls: list[tuple[str, list[Msg]]] = []

    def describe(self) -> str:
        return "mock"

    def chat(self, system: str, messages: list[Msg]) -> SeatResult:
        n = len(self.calls)
        self.calls.append((system, [Msg(m.role, m.content) for m in messages]))
        if n < len(self.replies):
            item = self.replies[n]
        elif self.cycle and self.replies:
            item = self.replies[n % len(self.replies)]
        else:
            item = self.default
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, SeatResult):
            return item
        return SeatResult(text=item, model="mock", raw={"mock": True})
