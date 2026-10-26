"""The mock backend: scripted replies, no model. For testing the tool itself.

Parts adapted from an earlier CC0-licensed harness by the same author: the recording test-double idea.
"""

from __future__ import annotations

from pathlib import Path

from .base import AgentSession, Backend, BackendError, Msg, SeatResult


class MockBackend(Backend):
    """Answers from a script and keeps every request it receives in `.calls`.

    `replies` is a list. Each item is a string, a ready-made SeatResult, an exception (which is
    raised), or a dict `{"text": ..., "write": {"path": "content"}, "tool_calls": [...]}`. In an
    agent session a dict's files are written into the work folder, so the folder diff can be
    tested. When the list runs out, the backend repeats it (`cycle=True`) or answers `default`.
    """

    capabilities = frozenset({"chat", "agent", "resume", "fork"})
    is_mock = True

    def __init__(self, replies=(), default: str = "Okay.", cycle: bool = False):
        self.replies = list(replies)
        self.default = default
        self.cycle = cycle
        self.calls: list[tuple[str, list[Msg]]] = []
        self.session_calls: list[dict] = []      # every agent-session send: session id, message, workdir
        self._count = 0
        self._sessions = 0

    def describe(self) -> str:
        return "mock"

    def _next(self):
        n, self._count = self._count, self._count + 1
        if n < len(self.replies):
            item = self.replies[n]
        elif self.cycle and self.replies:
            item = self.replies[n % len(self.replies)]
        else:
            item = self.default
        if isinstance(item, BaseException):
            raise item
        return item

    def chat(self, system: str, messages: list[Msg]) -> SeatResult:
        self.calls.append((system, [Msg(m.role, m.content) for m in messages]))
        item = self._next()
        if isinstance(item, SeatResult):
            return item
        text = item["text"] if isinstance(item, dict) else item
        return SeatResult(text=text, model="mock", raw={"mock": True})

    def open_session(self, workdir, *, allowed_tools=None, options=None) -> "MockSession":
        self._sessions += 1
        return MockSession(self, Path(workdir), f"mock-session-{self._sessions}", allowed_tools)


class MockSession(AgentSession):
    """A pretend agent: answers from the mock's script and writes the files the script names."""

    def __init__(self, backend: MockBackend, workdir: Path, session_id: str, allowed_tools=None):
        self.backend = backend
        self.workdir = workdir
        self.session_id = session_id
        self.allowed_tools = allowed_tools

    def send(self, message: str) -> SeatResult:
        self.backend.session_calls.append({"session_id": self.session_id, "message": message,
                                           "workdir": str(self.workdir)})
        item = self.backend._next()
        if isinstance(item, SeatResult):
            return item
        if isinstance(item, str):
            return SeatResult(text=item, session_id=self.session_id, model="mock", raw={"mock": True})
        calls = list(item.get("tool_calls") or [])
        for rel, content in (item.get("write") or {}).items():
            target = (self.workdir / rel).resolve()
            if self.workdir.resolve() not in target.parents:
                raise BackendError(f"mock script tried to write outside the work folder: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            if not item.get("tool_calls"):
                calls.append({"name": "Write", "arg": rel})
        return SeatResult(text=item["text"], session_id=self.session_id, model="mock",
                          raw={"mock": True}, tool_calls=calls)

    def fork(self) -> "MockSession":
        self.backend._sessions += 1
        return MockSession(self.backend, self.workdir, f"mock-session-{self.backend._sessions}",
                           self.allowed_tools)
