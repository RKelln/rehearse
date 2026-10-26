"""The claude backend: the `claude` command line tool, for chat seats and agent seats.

Parts adapted from an earlier CC0-licensed harness by the same author: the prompt flattening, the
quota pattern, and the idea of running a seat in an empty folder.

Chat seats (the sim, or a chat subject) run with no tools, no settings files, no extra servers and
no saved session, in an empty folder, so nothing but the prompt reaches the model. Agent seats run
in the work folder, keep one session across turns, and get their tools on the command line.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import weakref
from pathlib import Path

from .base import AgentSession, Backend, BackendError, Msg, QuotaError, SeatResult, SetupError

_QUOTA = re.compile(r"session limit|usage limit|rate limit|quota|resets \d", re.I)
_ARG_KEYS = ("file_path", "path", "command", "pattern", "skill", "url", "description")


def flatten(messages: list[Msg]) -> str:
    """A conversation as one prompt, for tools that take a single string.

    Lifted from the earlier harness, with vendor-neutral wording.
    """
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    lines = ["The conversation so far:", ""]
    for m in messages:
        lines.append(f"{'THEM' if m.role == 'user' else 'YOU'}: {m.content}")
        lines.append("")
    lines.append("Write only your next message. No label, no narration.")
    return "\n".join(lines)


def claude_bin() -> str:
    return os.environ.get("REHEARSE_CLAUDE_BIN") or "claude"


def _kill(proc: subprocess.Popen) -> None:
    """Kill the program and everything it started (it runs in its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def _run(argv: list[str], stdin: str, cwd, timeout: float, label: str) -> dict:
    """Run the tool and return {"stdout", "stderr", "returncode"}; the raw output is kept as is.

    The tool gets its own process group, which is killed on a timeout or Ctrl-C, so no orphan is left.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(cwd), start_new_session=True)
    except FileNotFoundError:
        raise SetupError(f"cannot find the claude program ({argv[0]!r}). Install it, or set "
                         "REHEARSE_CLAUDE_BIN to its path") from None
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, err = proc.communicate()
        raise BackendError(f"{label}: timed out after {timeout:g} seconds",
                           raw={"stdout": out, "stderr": err, "returncode": None}) from None
    except BaseException:
        _kill(proc)
        raise
    return {"stdout": out, "stderr": err, "returncode": proc.returncode}


def _tail(text: str, n: int = 400) -> str:
    return text.strip()[-n:]


def _tokens(usage) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    known = [p for p in parts if isinstance(p, int) and not isinstance(p, bool)]
    out = usage.get("output_tokens")
    return (sum(known) if known else None), (out if isinstance(out, int) and not isinstance(out, bool) else None)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_model(data: dict) -> str | None:
    usage = data.get("modelUsage")
    return next(iter(usage)) if isinstance(usage, dict) and usage else None


def base_tool_names(allowed: list[str] | None) -> list[str]:
    """Distinct tool names from allowed-tool patterns: ["Read", "Write(a.md)", "Write(b.md)"] -> ["Read", "Write"]."""
    names: list[str] = []
    for item in allowed or []:
        name = item.split("(", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


class ClaudeBackend(Backend):
    capabilities = frozenset({"chat", "agent", "resume", "fork"})

    def __init__(self, spec: dict):
        self.spec = spec
        self.model = spec.get("model")
        self.timeout = float(spec.get("timeout") or 900)
        self._cwd = tempfile.mkdtemp(prefix="rehearse-seat-")      # an empty folder for chat calls
        weakref.finalize(self, shutil.rmtree, self._cwd, True)

    def describe(self) -> str:
        return f"claude:{self.model}" if self.model else "claude"

    def _budget(self) -> list[str]:
        return ["--max-budget-usd", f"{self.max_budget_usd:g}"] if self.max_budget_usd is not None else []

    # chat seats -----------------------------------------------------------------------------

    def chat(self, system: str, messages: list[Msg]) -> SeatResult:
        # The system prompt goes in a private file, not on the command line, where `ps` would show it.
        # The file is in its own folder, not the seat's working folder, and is removed after the call.
        private = tempfile.mkdtemp(prefix="rehearse-system-")
        try:
            path = os.path.join(private, "system.txt")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(system)
            argv = [claude_bin(), "-p", "--output-format", "json"]
            if self.model:
                argv += ["--model", self.model]
            argv += ["--system-prompt-file", path, "--tools", "", "--setting-sources", "",
                     "--strict-mcp-config", "--no-session-persistence"] + self._budget()
            if os.environ.get("ANTHROPIC_API_KEY"):
                argv.append("--bare")
            raw = _run(argv, flatten(messages), self._cwd, self.timeout, self.describe())
        finally:
            shutil.rmtree(private, ignore_errors=True)
        return self._read_chat(raw)

    def _read_chat(self, raw: dict) -> SeatResult:
        try:
            data = json.loads(raw["stdout"].strip())
        except ValueError:
            data = None
        failed = raw["returncode"] != 0
        if not isinstance(data, dict):
            # Unreadable output: it may be an error message, but only if the program also failed.
            self._fail(raw, "could not read the output", raw["stderr"] + (raw["stdout"] if failed else ""))
        is_error = bool(data.get("is_error"))
        if failed or is_error:
            result = str(data.get("result") or "")
            self._fail(raw, result, raw["stderr"] + (" " + result if is_error else ""))
        try:
            tin, tout = _tokens(data.get("usage"))
            return SeatResult(text=str(data.get("result") or "").strip(), cost_usd=_number(data.get("total_cost_usd")),
                              tokens_in=tin, tokens_out=tout, session_id=_text_or_none(data.get("session_id")),
                              model=_first_model(data) or self.model, raw=raw)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.describe()}: could not read the output ({type(e).__name__}: {e})",
                               raw=raw) from None

    def _fail(self, raw: dict, why: str, quota_text: str):
        """Raise the right error for a failed call, keeping the raw output on it.

        Only `quota_text` (the error output, or an error result's text) is searched for quota wording,
        so a normal reply that happens to mention a limit never stops a suite.
        """
        evidence = " ".join([why, _tail(raw["stderr"])]).strip()
        if _QUOTA.search(quota_text):
            raise QuotaError(f"{self.describe()}: {_tail(evidence or quota_text)}", raw=raw)
        raise BackendError(f"{self.describe()}: exit {raw['returncode']}: {_tail(evidence)}", raw=raw)

    # agent seats ----------------------------------------------------------------------------

    def open_session(self, workdir, *, allowed_tools=None, options=None) -> "ClaudeSession":
        return ClaudeSession(self, Path(workdir), allowed_tools, {**self.spec, **(options or {})})


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


class ClaudeSession(AgentSession):
    """One `claude -p` conversation, resumed turn by turn.

    `--allowedTools` only pre-approves tools; it does not restrict them. So the session also passes
    `--tools` (only the named tools exist) and `--permission-mode dontAsk` (anything not pre-approved is
    denied). Both can be changed with the `tools` and `permission_mode` options.
    """

    def __init__(self, backend: ClaudeBackend, workdir: Path, allowed_tools, options: dict):
        self.backend = backend
        self.workdir = workdir
        self.allowed_tools = allowed_tools
        self.options = options
        self.session_id: str | None = None
        self._fork_next = False
        self._after_fork = False
        self._last_cost = 0.0

    def fork(self) -> "ClaudeSession":
        # TODO(step 6): check live that a forked session's reported total continues from its parent's.
        if self.session_id is None:
            raise BackendError("cannot fork a session that has not started")
        child = ClaudeSession(self.backend, self.workdir, self.allowed_tools, self.options)
        child.session_id, child._last_cost = self.session_id, self._last_cost
        child._fork_next = child._after_fork = True
        return child

    def _argv(self) -> list[str]:
        o = self.options
        argv = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose"]
        if self.backend.model:
            argv += ["--model", self.backend.model]
        sources = o.get("setting_sources")
        argv += ["--setting-sources", "project" if sources is None else str(sources), "--strict-mcp-config"]
        tools = o.get("tools")
        names = [str(t) for t in tools] if tools is not None else base_tool_names(self.allowed_tools)
        if names:
            argv += ["--tools", ",".join(names)]
        argv += ["--permission-mode", str(o.get("permission_mode") or "dontAsk")]
        budget = o.get("max_budget_usd") if self.backend.max_budget_usd is None else self.backend.max_budget_usd
        if budget is not None:
            argv += ["--max-budget-usd", f"{budget:g}"]
        if self.session_id:
            argv += ["--resume", self.session_id]
            if self._fork_next:
                argv.append("--fork-session")
        if self.allowed_tools:
            argv += ["--allowedTools", *self.allowed_tools]
        return argv

    def send(self, message: str) -> SeatResult:
        # The message goes on stdin, so a message starting with "-" or a very long one is safe.
        raw = _run(self._argv(), message, self.workdir, float(self.options.get("timeout") or 900),
                   self.backend.describe())
        self._fork_next = False
        after_fork, self._after_fork = self._after_fork, False
        try:
            return self._read(raw, after_fork)
        except BackendError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.backend.describe()}: could not read the output ({type(e).__name__}: {e})",
                               raw=raw) from None

    def _read(self, raw: dict, after_fork: bool) -> SeatResult:
        init, result, texts, calls = None, None, [], []
        for line in raw["stdout"].splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") in (None, "init"):
                init = init or ev
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        calls.append({"name": block.get("name"), "arg": self._arg(block.get("input"))})
            elif kind == "result":
                result = ev
        failed = raw["returncode"] != 0
        if result is None:
            self.backend._fail(raw, "no result event in the output", raw["stderr"] + (raw["stdout"] if failed else ""))
        is_error = bool(result.get("is_error"))
        if is_error and _QUOTA.search(raw["stderr"] + " " + str(result.get("result") or "")):
            self.backend._fail(raw, str(result.get("result") or ""), raw["stderr"] + " " + str(result.get("result") or ""))
        if failed and _QUOTA.search(raw["stderr"]):
            self.backend._fail(raw, str(result.get("result") or ""), raw["stderr"])
        extras = {"exit_code": raw["returncode"]} if failed else {}
        sid = _text_or_none(result.get("session_id")) or _text_or_none((init or {}).get("session_id")) or self.session_id
        self.session_id = sid
        cost, total = None, _number(result.get("total_cost_usd"))
        if total is not None:
            extras["cost_total_reported"] = total
            if total >= self._last_cost:
                # The program reports a total that grows over the session: this turn's cost is the growth.
                cost = total - self._last_cost
                extras["cost_basis"] = "fork-unverified" if after_fork else "delta"
            else:
                extras["cost_basis"] = "unknown: total decreased"
            self._last_cost = total
        if init:
            extras["tools"] = init.get("tools")
        tin, tout = _tokens(result.get("usage"))
        text = result.get("result")
        text = "\n".join(texts) if text is None else text
        return SeatResult(text=str(text).strip(), cost_usd=cost, tokens_in=tin, tokens_out=tout,
                          session_id=sid, model=_text_or_none((init or {}).get("model")) or _first_model(result),
                          raw=raw, error=(str(result.get("result") or "the agent reported an error")
                                          if is_error else None),
                          extras=extras, tool_calls=calls)

    def _arg(self, tool_input) -> str | None:
        """The main argument of a tool call, short, with the work folder shown as "./"."""
        if not isinstance(tool_input, dict):
            return None
        for key in _ARG_KEYS:
            if tool_input.get(key):
                text = str(tool_input[key])
                for root in {str(self.workdir), str(self.workdir.resolve())}:
                    text = text.replace(root.rstrip("/") + "/", "./").replace(root, ".")
                return text[:200]
        return None
