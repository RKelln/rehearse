"""The codex backend: the `codex exec` command line tool, for agent seats.

Each session runs with a private CODEX_HOME (mode 700) into which the person's login file is copied
(mode 600, by file copy only, never read), so nothing else from their own codex setup reaches the
agent. The private home is removed when the session and its forks are closed. The login file is never
written to a run folder.

The sandbox is codex's own, and it is coarse: read-only or workspace-write. The /tmp exclusions are
always passed because work copies live under the system temp folder, which workspace-write would
otherwise leave writable (an agent wrote `../outside.txt` next to its work copy without them).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import weakref
from pathlib import Path

from .base import AgentSession, Backend, BackendError, QuotaError, SeatResult, SetupError
from .claude import _QUOTA, _tail, base_tool_names
from .keys import read_key
from .process import run as run_program

_SKILL_DIRS = (".claude/skills", ".agents/skills")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")
_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
_MODEL = re.compile(r'"model"\s*:\s*"([^"]+)"')
_KINDS = {"add": "write", "update": "edit", "delete": "delete"}
_SUBSCRIPTION = "subscription (not metered)"


def codex_bin() -> str:
    return os.environ.get("REHEARSE_CODEX_BIN") or "codex"


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def sandbox_for(allowed_tools: list[str] | None) -> str:
    """workspace-write if the case allows any tool that writes or runs commands, else read-only."""
    names = base_tool_names(allowed_tools)
    return "workspace-write" if any(n in _WRITE_TOOLS for n in names) else "read-only"


def _login_file() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "auth.json"


class CodexBackend(Backend):
    capabilities = frozenset({"agent", "resume", "fork"})

    def __init__(self, spec: dict):
        self.spec = spec
        self.model = spec.get("model") or None
        self.timeout = float(spec.get("timeout") or 900)
        self.price_in = spec.get("price_in_per_mtok")
        self.price_out = spec.get("price_out_per_mtok")
        self.key_env = spec.get("key_env") or None       # set only when the seat names an API key to use
        sandbox = spec.get("sandbox")
        if sandbox is not None and sandbox not in _SANDBOXES:
            raise BackendError(f"codex sandbox must be one of {', '.join(_SANDBOXES)}, not {sandbox!r}")
        self.config = list(spec.get("config") or [])

    def describe(self) -> str:
        return f"codex:{self.model}" if self.model else "codex"

    def cost(self, tokens_in, tokens_out) -> float | None:
        """Tokens times the seat's prices, or None if either price or either count is missing."""
        if None in (self.price_in, self.price_out, tokens_in, tokens_out):
            return None
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def key(self) -> str:
        """The API key if the seat names one with key_env ("" if not). Named but missing: setup error."""
        if not self.key_env:
            return ""
        key = read_key(self.key_env)
        if not key:
            raise SetupError(f"{self.describe()}: no key. Set {self.key_env} in your environment, or add "
                             f"{self.key_env}=... to ~/.config/rehearse/keys (then chmod 600 that file)")
        return key

    def open_session(self, workdir, *, allowed_tools=None, options=None) -> "CodexSession":
        return CodexSession(self, Path(workdir), allowed_tools, {**self.spec, **(options or {})})


class _Home:
    """A private CODEX_HOME shared by a session and its forks (a fork needs its parent's saved thread)."""

    def __init__(self, copy_login: bool):
        self.path = tempfile.mkdtemp(prefix="rehearse-codex-home-")      # mode 700
        self.refs = 1
        self._finalizer = weakref.finalize(self, shutil.rmtree, self.path, True)
        if copy_login:
            try:
                self._copy_login()
            except BaseException:
                self._finalizer()
                raise

    def _copy_login(self) -> None:
        source = _login_file()
        if not source.is_file():
            raise SetupError("codex has no login on this machine: log in with `codex login` first")
        # a file copy only; the contents are never read here. Created 600 so there is no open window.
        fd = os.open(os.path.join(self.path, "auth.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out)

    def release(self) -> None:
        self.refs -= 1
        if self.refs <= 0:
            self._finalizer()


class CodexSession(AgentSession):
    """One codex thread, continued turn by turn with `exec resume` and branched with `exec fork`."""

    def __init__(self, backend: CodexBackend, workdir: Path, allowed_tools, options: dict):
        self.backend = backend
        self.workdir = workdir
        self.allowed_tools = allowed_tools
        self.options = options
        self.session_id: str | None = None
        self._fork_next = False
        self._warned = False
        self._api_key = backend.key()
        self._home = _Home(copy_login=not self._api_key)
        self._closed = False

    def fork(self) -> "CodexSession":
        if self.session_id is None:
            raise BackendError("cannot fork a session that has not started")
        child = CodexSession.__new__(CodexSession)
        child.__dict__.update(self.__dict__)
        child.session_id, child._fork_next, child._closed = self.session_id, True, False
        self._home.refs += 1
        return child

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._home.release()

    # what goes to the program ----------------------------------------------------------------

    def _skills(self) -> list[str]:
        names: list[str] = []
        for d in _SKILL_DIRS:
            folder = self.workdir / d
            if folder.is_dir():
                names += sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name not in names)
        return names

    def sandbox(self) -> str:
        return self.options.get("sandbox") or sandbox_for(self.allowed_tools)

    def _fallback(self) -> list[str]:
        if (self.workdir / "CLAUDE.md").is_file() and not (self.workdir / "AGENTS.md").is_file():
            return ["CLAUDE.md"]
        return []

    def warning(self) -> str:
        """What codex cannot enforce for this case, in plain words."""
        narrowed = []
        for item in self.allowed_tools or []:
            name, _, rest = item.partition("(")
            if rest and name.strip() in _WRITE_TOOLS:
                narrowed.append(rest.rstrip(")").strip())
        mode = self.sandbox()
        if narrowed:
            what = f"codex cannot limit writes to {', '.join(narrowed)}; it may write anywhere in its work folder"
        elif mode == "read-only":
            what = "codex cannot limit which tools it uses; it may run shell commands that read files"
        else:
            what = "codex cannot limit which tools it uses; it may write anywhere in its work folder and run shell commands"
        return what + ". The work-folder check and the folder diff still apply."

    def _argv(self, message: str, sandbox: str, fallback: list[str]) -> list[str]:
        argv = [codex_bin(), "exec"]
        if self.session_id:
            argv.append("fork" if self._fork_next else "resume")
        argv += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        if not self.session_id:
            # `exec resume` and `exec fork` refuse -C; they use the process's folder and PWD, set too.
            argv += ["-C", str(self.workdir)]
        argv += ["-c", f'sandbox_mode="{sandbox}"',
                 "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                 "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"]
        if self.backend.model:
            argv += ["-m", self.backend.model]
        if fallback:
            argv += ["-c", "project_doc_fallback_filenames=" + json.dumps(fallback)]
        for setting in [*self.backend.config, *(self.options.get("config") or [])]:
            argv += ["-c", setting]
        if self.session_id:
            argv.append(self.session_id)
        if message.startswith("-"):
            argv.append("--")
        return argv + [message]

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
        env.update(CODEX_HOME=self._home.path, PWD=str(self.workdir))
        if self._api_key:
            env[self.backend.key_env] = self._api_key
            env["OPENAI_API_KEY"] = self._api_key
        return env

    def send(self, message: str) -> SeatResult:
        if self._closed:
            raise BackendError("the session is closed")
        sandbox, fallback = self.sandbox(), self._fallback()
        if not self._warned:
            self._warned = True
            print(f"warning: {self.warning()}", file=sys.stderr)
        raw = run_program(self._argv(message, sandbox, fallback), None, self.workdir,
                          float(self.options.get("timeout") or 900), self.backend.describe(),
                          program="codex", env_name="REHEARSE_CODEX_BIN", env=self._env())
        self._fork_next = False
        try:
            return self._read(raw, sandbox, fallback)
        except BackendError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.backend.describe()}: could not read the output ({type(e).__name__}: {e})",
                               raw=raw) from None

    # what comes back --------------------------------------------------------------------------

    def _fail(self, raw: dict, why: str, quota_text: str):
        evidence = " ".join([why, _tail(raw["stderr"])]).strip()
        if _QUOTA.search(quota_text):
            raise QuotaError(f"{self.backend.describe()}: {_tail(evidence or quota_text)}", raw=raw)
        raise BackendError(f"{self.backend.describe()}: exit {raw['returncode']}: {_tail(evidence)}", raw=raw)

    def _rel(self, text: str) -> str:
        for root in {str(self.workdir), str(self.workdir.resolve())}:
            text = text.replace(root.rstrip("/") + "/", "./").replace(root, ".")
        return text

    def _command(self, command: str) -> str:
        """A shell command without codex's `/bin/bash -lc '...'` wrapper, short, with the work folder as "./"."""
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = []
        if len(parts) == 3 and os.path.basename(parts[0]) in ("bash", "sh", "zsh") and parts[1] in ("-lc", "-c"):
            command = parts[2]
        return self._rel(command)[:200]

    def _calls(self, item: dict) -> list[dict]:
        kind = item.get("type")
        if kind == "command_execution":
            return [{"name": "shell", "arg": self._command(str(item.get("command") or ""))}]
        if kind == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            return [{"name": _KINDS.get(c.get("kind"), "edit"), "arg": self._rel(str(c.get("path") or ""))[:200]}
                    for c in changes if isinstance(c, dict)]
        return [{"name": str(kind), "arg": ""}] if kind and kind != "agent_message" else []

    def _model_from_session_file(self, thread_id: str | None) -> str | None:
        """The model, from the saved session file in the private home (the events never name it)."""
        if not thread_id:
            return None
        base = Path(self._home.path) / "sessions"
        if not base.is_dir():
            return None
        files = [p for p in base.rglob("*") if p.is_file()]
        named = [p for p in files if thread_id in p.name]
        for path in named + [p for p in files if p not in named]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if path in named or thread_id in text:
                found = _MODEL.search(text)
                if found:
                    return found.group(1)
        return None

    def _read(self, raw: dict, sandbox: str, fallback: list[str]) -> SeatResult:
        events, bad = [], []
        for n, line in enumerate(raw["stdout"].splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = None
            if isinstance(ev, dict):
                events.append(ev)
            else:
                bad.append(n)
        failed = raw["returncode"] != 0
        if not events:
            self._fail(raw, "no events in the output", raw["stderr"] + (raw["stdout"] if failed else ""))
        if bad and not failed:
            raise BackendError(f"{self.backend.describe()}: could not read line {bad[0]} of the output "
                               "(it is not a JSON object)", raw=raw)
        messages: list[str] = []
        calls: list[dict] = []
        sid = None
        tin = tout = 0
        seen_tokens = completed = False
        errors: list[str] = []
        turn_failed = False
        for ev in events:
            kind = ev.get("type")
            if kind == "thread.started":
                sid = ev.get("thread_id") if isinstance(ev.get("thread_id"), str) else sid
            elif kind == "item.completed" and isinstance(ev.get("item"), dict):
                item = ev["item"]
                if item.get("type") == "agent_message":
                    messages.append(str(item.get("text") or ""))
                calls += self._calls(item)
            elif kind == "turn.completed":
                completed = True
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
                parts = [usage.get(k) for k in ("input_tokens", "output_tokens", "reasoning_output_tokens")]
                seen_tokens = seen_tokens or any(isinstance(p, int) and not isinstance(p, bool) for p in parts)
                tin += _count(usage.get("input_tokens"))        # already includes the cached part
                tout += _count(usage.get("output_tokens")) + _count(usage.get("reasoning_output_tokens"))
            elif kind in ("turn.failed", "error"):
                turn_failed = turn_failed or kind == "turn.failed"
                err = ev.get("error") if ev.get("error") is not None else ev.get("message")
                if isinstance(err, dict):
                    err = err.get("message", err)
                errors.append((err if isinstance(err, str) else json.dumps(err))[:400])
        if errors and (turn_failed or not completed):
            self._fail(raw, "; ".join(errors), raw["stderr"] + " " + " ".join(errors))
        if failed and _QUOTA.search(raw["stderr"]):
            self._fail(raw, "", raw["stderr"])
        self.session_id = sid or self.session_id
        extras: dict = {"tool_limits": f"approximate: sandbox {sandbox}"}
        if len(messages) > 1:
            extras["preamble"] = messages[:-1]
        if failed:
            extras["exit_code"] = raw["returncode"]
        if fallback:
            extras["instructions_added"] = fallback
        skills = self._skills()
        if skills:
            extras["skills_present"] = skills
        tokens_in, tokens_out = (tin, tout) if seen_tokens else (None, None)
        cost = None
        if self._api_key:
            cost = self.backend.cost(tokens_in, tokens_out)         # None without prices: never 0 as known
            extras["cost_basis"] = ("computed from tokens and the seat's prices" if cost is not None
                                    else "unknown (API key, no prices given)")
        else:
            extras["cost_basis"] = _SUBSCRIPTION
        model = self.backend.model or self._model_from_session_file(self.session_id)
        return SeatResult(text=(messages[-1] if messages else "").strip(), cost_usd=cost, tokens_in=tokens_in,
                          tokens_out=tokens_out, session_id=self.session_id, model=model, raw=raw,
                          error=None, extras=extras, tool_calls=calls)
