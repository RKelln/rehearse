"""What every backend shares: the result of a call, the interface, and the errors.

Parts adapted from an earlier CC0-licensed harness by the same author.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BackendError(RuntimeError):
    """A call failed. The run stops and records why; it is not retried silently."""


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


class Backend:
    """Fills a seat. Subclasses say what they can do in `capabilities`."""

    # What the backend can do: any of "chat", "agent", "resume", "fork". Adapters check it
    # (the chat adapter requires "chat").
    capabilities: frozenset[str] = frozenset({"chat"})
    is_mock: bool = False

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
