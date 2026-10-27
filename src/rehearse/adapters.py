"""Adapters: how the subject (the thing under test) is reached.

Only `chat` exists so far. The `agent` adapter (a coding agent working in a folder) and the
`python` adapter (your own program) are added in later steps. Every adapter offers the same
two things the loop uses: `reply(turns) -> SeatResult` and `last_request`. The adapter owns
its own state (a session id, a folder copy), so the loop only calls `reply`.
"""

from __future__ import annotations

import hashlib

from dataclasses import asdict
from pathlib import Path

from .backends import Backend, BackendError, Msg, SeatResult


def require(backend: Backend, capability: str, doing: str = "") -> None:
    """Fail early and plainly if a backend cannot fill a seat."""
    if capability not in backend.capabilities:
        what = f"cannot {doing} (it lacks {capability!r})" if doing else f"cannot do {capability!r}"
        raise BackendError(f"backend {backend.describe()} {what}")


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

    def close(self) -> None:
        pass

    def reply(self, turns) -> SeatResult:
        """Send the subject the transcript so far and return its reply."""
        messages = subject_messages(turns)
        self.last_request = {"system": self.system, "messages": [asdict(m) for m in messages]}
        return self.backend.chat(self.system, messages)


class AgentAdapter:
    """The subject is an agent working in a folder. It keeps its own session across turns.

    The agent already remembers the conversation, so each turn sends only the person's newest
    message. The session is started on the first reply.
    """

    def __init__(self, backend: Backend, workdir: Path, allowed_tools: list[str] | None = None,
                 options: dict | None = None):
        require(backend, "agent", "run an agent subject")
        require(backend, "resume", "run an agent subject")
        self.backend = backend
        self.workdir = Path(workdir)
        self.allowed_tools = allowed_tools
        self.options = options or {}
        self.session = None
        self.last_request: dict = {}

    def reply(self, turns) -> SeatResult:
        """Send the agent the person's newest message and return its reply."""
        message = turns[-1].text
        self.last_request = {"message": message, "allowed_tools": self.allowed_tools,
                             "session_id": self.session.session_id if self.session else None}
        if self.session is None:
            self.session = self.backend.open_session(self.workdir, allowed_tools=self.allowed_tools,
                                                     options={**(getattr(self.backend, "spec", None) or {}), **self.options})
        return self.session.send(message)

    def artifact_exists(self, rel: str) -> bool:
        """Is there a file at this path in the work copy right now?"""
        return (self.workdir / rel).is_file()

    def artifact_digest(self, rel: str) -> str | None:
        """A fingerprint of that file now, or None if it is not there; tells a fresh write from an old one."""
        path = self.workdir / rel
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def make_adapter(name: str, backend: Backend, variant_text: str = "", *, workdir=None,
                 allowed_tools=None, options=None):
    """Build the adapter a case names."""
    if name == "chat":
        return ChatAdapter(backend, variant_text)
    if name == "agent":
        if workdir is None:
            raise ValueError("the agent adapter needs a work folder")
        return AgentAdapter(backend, workdir, allowed_tools, options)
    raise ValueError(f"adapter {name!r} is not built yet; the adapters are 'chat' and 'agent'")
