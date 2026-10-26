"""Adapters: how the subject (the thing under test) is reached.

Only `chat` exists so far. The `agent` adapter (a coding agent working in a folder) and the
`python` adapter (your own program) are added in later steps. Every adapter offers the same
two things the loop uses: `reply(turns) -> SeatResult` and `last_request`. The adapter owns
its own state (a session id, a folder copy), so the loop only calls `reply`.
"""

from __future__ import annotations

from dataclasses import asdict

from .backends import Backend, BackendError, Msg, SeatResult


def require(backend: Backend, capability: str) -> None:
    """Fail early and plainly if a backend cannot fill a seat."""
    if capability not in backend.capabilities:
        raise BackendError(f"backend {backend.describe()!r} cannot do {capability!r}")


def subject_messages(turns) -> list[Msg]:
    """The transcript from the subject's view: the person is "user", the subject "assistant"."""
    return [Msg("user" if t.side == "person" else "assistant", t.text) for t in turns]


def sim_messages(turns) -> list[Msg]:
    """The transcript from the sim's view: the subject is "user", the sim "assistant".

    The scripted opening is left out: it goes in the sim's system prompt (the frame's
    `{opening}`), so the messages start with the subject's first reply, as a "user" message.
    """
    return [Msg("user" if t.side == "subject" else "assistant", t.text)
            for t in turns if not (t.side == "person" and t.n == 0)]


class ChatAdapter:
    """The subject is a system prompt (the variant) and a model. Each turn replays the whole transcript."""

    def __init__(self, backend: Backend, system: str):
        require(backend, "chat")
        self.backend = backend
        self.system = system
        self.last_request: dict = {}

    def reply(self, turns) -> SeatResult:
        """Send the subject the transcript so far and return its reply."""
        messages = subject_messages(turns)
        self.last_request = {"system": self.system, "messages": [asdict(m) for m in messages]}
        return self.backend.chat(self.system, messages)


def make_adapter(name: str, backend: Backend, variant_text: str) -> ChatAdapter:
    """Build the adapter a case names."""
    if name == "chat":
        return ChatAdapter(backend, variant_text)
    raise ValueError(f"adapter {name!r} is not built yet; only 'chat' exists")
