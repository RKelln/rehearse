"""What every backend shares: the result of a call, the interface, and the errors.

Parts adapted from an earlier CC0-licensed harness by the same author.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BackendError(RuntimeError):
    """A call failed. The run stops and records why; it is not retried silently.

    `raw` is whatever output the backend had before it failed, so the run folder still keeps it.
    """

    def __init__(self, message: str = "", raw: object = None):
        super().__init__(message)
        self.raw = raw


class QuotaError(BackendError):
    """The backend says it is out of quota. Retrying will not help; stop the whole suite."""


class SetupError(QuotaError):
    """A missing key or similar. Nothing will work until a person fixes it; stop the suite."""


@dataclass
class Msg:
    """One message. `role` is "user" or "assistant", from the point of view of the seat being called."""
    role: str
    content: str


@dataclass
class SeatResult:
    """What a seat answered. Unknown cost or tokens are None, never 0."""
    text: str
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    session_id: str | None = None
    model: str | None = None
    raw: object = None          # the backend's own output; must be JSON-serialisable
    error: str | None = None
    extras: dict = field(default_factory=dict)            # anything else worth recording
    tool_calls: list[dict] = field(default_factory=list)  # agent seats: name and main argument


class AgentSession:
    """A conversation with an agent working in a folder. The session owns its own state."""

    session_id: str | None = None

    def send(self, message: str) -> SeatResult:
        """Give the agent the next message and return its reply. Raise BackendError on failure."""
        raise NotImplementedError

    def fork(self) -> "AgentSession":
        """A copy that continues from here without changing this one. Needs the "fork" capability."""
        raise BackendError("this backend cannot fork a session")

    def close(self) -> None:
        """Release anything the session holds."""


class Backend:
    """Fills a seat. Subclasses say what they can do in `capabilities`."""

    # What the backend can do: any of "chat", "agent", "resume", "fork". Adapters read it:
    # the chat adapter needs "chat"; the agent adapter needs "agent" and "resume".
    capabilities: frozenset[str] = frozenset({"chat"})
    is_mock: bool = False
    max_budget_usd: float | None = None     # set by the runner; backends that can, pass it on

    def describe(self) -> str:
        """Short name for records, such as "mock" or "claude:sonnet"."""
        return "backend"

    def chat(self, system: str, messages: list[Msg]) -> SeatResult:
        """Answer the conversation so far. Raise BackendError on failure.

        `messages` always starts with a "user" message and alternates user, assistant, user, ...
        ending with a "user" message, from this seat's own point of view. Anything the seat
        needs to know before that (such as how the chat began) goes in `system`.
        """
        raise NotImplementedError

    def open_session(self, workdir, *, allowed_tools: list[str] | None = None,
                     options: dict | None = None) -> AgentSession:
        """Start an agent working in `workdir`. Needs the "agent" capability."""
        raise BackendError(f"backend {self.describe()!r} cannot run an agent subject")
