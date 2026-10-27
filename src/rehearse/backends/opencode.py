"""The opencode backend: the `opencode` command line tool, for agent seats.

Each session runs with a private, empty HOME and all of its configuration passed in one environment
variable (OPENCODE_CONFIG_CONTENT), so nothing from the person's own opencode setup, skills or
accounts reaches the model. A key is read with rehearse's key reader and goes only into the child's
environment; the configuration names it as {env:NAME}, never its value.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import weakref
from pathlib import Path
from urllib.parse import urlparse

from . import PRESETS
from .base import AgentSession, Backend, BackendError, QuotaError, SeatResult, SetupError
from .claude import _QUOTA, _number, _tail, _text_or_none
from .http import LOCAL_HOSTS
from .keys import read_key
from .process import run as run_program

_ARG_KEYS = ("filePath", "path", "file_path", "command", "pattern", "name", "id")   # the skill tool uses "id"
_SKILL_DIRS = (".claude/skills", ".agents/skills", ".opencode/skills")
_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_SIMPLE = {"Read": "read", "Glob": "glob", "Grep": "grep", "Skill": "skill", "WebFetch": "webfetch",
           "WebSearch": "websearch"}
_BASE_URLS = {name: preset["base_url"] for name, preset in PRESETS.items()}
_BASE_URLS["deepseek"] = "https://api.deepseek.com/v1"      # opencode wants the /v1 form
_DEFAULT_NPM = "@ai-sdk/openai-compatible"


def opencode_bin() -> str:
    return os.environ.get("REHEARSE_OPENCODE_BIN") or "opencode"


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def map_permissions(allowed_tools: list[str] | None, has_skills: bool = False) -> dict:
    """Claude-style allowed tools as an opencode `permission` table. Everything else is denied.

    Read -> read, Glob -> glob, Grep -> grep, Skill -> skill, WebFetch/WebSearch -> webfetch/websearch,
    Write(X), Edit(X) -> edit {X: allow} (opencode's `edit` covers write, edit and patch),
    Bash(cmd *) -> bash {"cmd *": allow}. A name with no argument is allowed outright. Other tools
    have no counterpart and are left out, so they stay denied.
    """
    perm: dict = {"*": "deny"}
    patterns: dict[str, dict] = {}        # tool -> {pattern: allow}
    open_tools: set[str] = set()          # tools allowed with no narrowing
    for item in allowed_tools or []:
        name, _, rest = item.strip().partition("(")
        name, arg = name.strip(), rest.rstrip(")").strip()
        tool = "edit" if name in _EDIT_TOOLS else "bash" if name == "Bash" else _SIMPLE.get(name)
        if tool is None:
            continue
        if not arg or tool in ("glob", "grep", "skill", "webfetch", "websearch"):
            open_tools.add(tool)
        else:
            if tool == "bash" and arg.endswith(":*"):
                arg = arg[:-2] + " *"
            patterns.setdefault(tool, {})[arg] = "allow"
    if has_skills:
        open_tools.add("skill")
    for tool in dict.fromkeys([*open_tools, *patterns]):
        # a tool allowed outright wins over narrowed entries for the same tool
        perm[tool] = "allow" if tool in open_tools else {"*": "deny", **patterns[tool]}
    return perm


class OpenCodeBackend(Backend):
    capabilities = frozenset({"agent", "resume", "fork"})

    def __init__(self, spec: dict):
        self.spec = spec
        self.model = spec.get("model") or ""
        provider, _, model_id = self.model.partition("/")
        if not provider or not model_id:
            raise BackendError(f"an opencode seat looks like opencode:PROVIDER/MODEL, not {self.model!r}")
        self.provider, self.model_id = provider, model_id
        self.timeout = float(spec.get("timeout") or 900)
        self.price_in = spec.get("price_in_per_mtok")
        self.price_out = spec.get("price_out_per_mtok")
        table = spec.get("provider") or {}
        for key in table:
            if key not in ("base_url", "key_env", "npm", "models"):
                raise BackendError(f"unknown setting {key!r} in an opencode provider table; the settings are: "
                                   "base_url, key_env, npm, models")
        self.base_url = table.get("base_url") or _BASE_URLS.get(provider)
        preset = PRESETS.get(provider, {})
        self.key_env = table.get("key_env") or (preset.get("key_env") if not table.get("base_url") else None)
        self.npm = table.get("npm") or _DEFAULT_NPM
        models = table.get("models") or []
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise BackendError("the opencode provider setting 'models' must be a list of model ids")
        self.models = list(dict.fromkeys([model_id, *models]))

    def describe(self) -> str:
        return f"opencode:{self.model}"

    def cost(self, tokens_in, tokens_out) -> float | None:
        """Tokens times the seat's prices, or None if either price or either count is missing."""
        if None in (self.price_in, self.price_out, tokens_in, tokens_out):
            return None
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def _local(self) -> bool:
        return (urlparse(self.base_url or "").hostname or "") in LOCAL_HOSTS

    def key(self) -> str:
        """The key for the provider ("" if it needs none). Missing, or over plain http to a remote host: setup error."""
        if not self.key_env:
            return ""
        key = read_key(self.key_env)
        if not key and not self._local():
            raise SetupError(f"{self.describe()}: no key. Set {self.key_env} in your environment, or add "
                             f"{self.key_env}=... to ~/.config/rehearse/keys (then chmod 600 that file)")
        if key and self.base_url and urlparse(self.base_url).scheme != "https" and not self._local():
            raise SetupError(f"{self.describe()}: refusing to send a key over plain http to a remote host. "
                             "Use an https base_url")
        return key

    def provider_config(self) -> dict:
        """The `provider` part of the opencode config. The key is named as {env:NAME}, never written."""
        if not self.base_url:
            return {}          # a provider opencode already knows: nothing to add
        options = {"baseURL": self.base_url}
        if self.key_env:
            options["apiKey"] = "{env:%s}" % self.key_env
        return {self.provider: {"npm": self.npm, "options": options, "models": {m: {} for m in self.models}}}

    def open_session(self, workdir, *, allowed_tools=None, options=None) -> "OpenCodeSession":
        return OpenCodeSession(self, Path(workdir), allowed_tools, {**self.spec, **(options or {})})


class _Home:
    """A private empty HOME shared by a session and its forks (a fork needs its parent's saved session)."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="rehearse-oc-home-")
        self.refs = 1
        self._finalizer = weakref.finalize(self, shutil.rmtree, self.path, True)

    def release(self) -> None:
        self.refs -= 1
        if self.refs <= 0:
            self._finalizer()


class OpenCodeSession(AgentSession):
    """One `opencode run` conversation, continued turn by turn with `-s`."""

    def __init__(self, backend: OpenCodeBackend, workdir: Path, allowed_tools, options: dict):
        self.backend = backend
        self.workdir = workdir
        self.allowed_tools = allowed_tools
        self.options = options
        self.session_id: str | None = None
        self._fork_next = False
        self._home = _Home()
        self._closed = False

    def fork(self) -> "OpenCodeSession":
        if self.session_id is None:
            raise BackendError("cannot fork a session that has not started")
        child = OpenCodeSession.__new__(OpenCodeSession)
        child.__dict__.update(self.__dict__)
        child.session_id, child._fork_next, child._closed = self.session_id, True, False
        self._home.refs += 1
        return child

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._home.release()

    # what goes to the program ----------------------------------------------------------------

    def _has_skills(self) -> bool:
        return any((self.workdir / d).is_dir() for d in _SKILL_DIRS)

    def permission(self) -> dict:
        given = self.options.get("permission")
        return given if isinstance(given, dict) else map_permissions(self.allowed_tools, self._has_skills())

    def _instructions(self) -> list[str]:
        # With a clean HOME, AGENTS.md is read on its own but CLAUDE.md is not, unless it is listed.
        if (self.workdir / "CLAUDE.md").is_file() and not (self.workdir / "AGENTS.md").is_file():
            return ["CLAUDE.md"]
        return []

    def _config(self, permission: dict, instructions: list[str]) -> dict:
        config: dict = {}
        provider = self.backend.provider_config()
        if provider:
            config["provider"] = provider
        config["permission"] = permission
        if instructions:
            config["instructions"] = instructions
        return config

    def _argv(self, message: str) -> list[str]:
        argv = [opencode_bin(), "run", "--standalone", "--format", "json", "-m", self.backend.model]
        if self.session_id:
            argv += ["-s", self.session_id]
            if self._fork_next:
                argv.append("--fork")
        return argv + ["--", message]

    def _env(self, config: dict) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        # opencode takes its project folder from PWD, not from the process's working directory, so an
        # inherited PWD would point it at whatever folder rehearse was started from.
        env.update(HOME=self._home.path, PWD=str(self.workdir), OPENCODE_DISABLE_AUTOUPDATE="1",
                   OPENCODE_CONFIG_CONTENT=json.dumps(config))
        key = self.backend.key()
        if key:
            env[self.backend.key_env] = key
        return env

    def send(self, message: str) -> SeatResult:
        if self._closed:
            raise BackendError("the session is closed")
        permission, instructions = self.permission(), self._instructions()
        env = self._env(self._config(permission, instructions))
        raw = run_program(self._argv(message), "", self.workdir, float(self.options.get("timeout") or 900),
                          self.backend.describe(), program="opencode", env_name="REHEARSE_OPENCODE_BIN", env=env)
        self._fork_next = False
        try:
            return self._read(raw, permission, instructions)
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

    def _read(self, raw: dict, permission: dict, instructions: list[str]) -> SeatResult:
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
        if bad:
            raise BackendError(f"{self.backend.describe()}: could not read line {bad[0]} of the output "
                               "(it is not a JSON object)", raw=raw)
        texts: list[str] = []          # text parts since the last tool call
        all_texts: list[str] = []
        calls: dict = {}
        sid = None
        tin = tout = 0
        seen_tokens = False
        reported = 0.0
        error = None
        for ev in events:
            sid = _text_or_none(ev.get("sessionID")) or sid
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            kind = ev.get("type")
            if kind == "text":
                texts.append(str(part.get("text", "")))
                all_texts.append(texts[-1])
            elif kind == "tool_use":
                texts = []
                key = part.get("callID") or part.get("id") or len(calls)
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                calls[key] = {"name": part.get("tool"), "arg": self._arg(state.get("input"))}
            elif kind == "step_finish":
                tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                parts = (tokens.get("input"), cache.get("read"), cache.get("write"), tokens.get("output"),
                         tokens.get("reasoning"))
                if any(isinstance(p, int) and not isinstance(p, bool) for p in parts):
                    seen_tokens = True
                tin += _count(tokens.get("input")) + _count(cache.get("read")) + _count(cache.get("write"))
                tout += _count(tokens.get("output")) + _count(tokens.get("reasoning"))
                reported += _number(part.get("cost")) or 0.0
            elif kind == "error":
                err = ev.get("error") if ev.get("error") is not None else part
                error = (json.dumps(err) if not isinstance(err, str) else err)[:400]
        if error and _QUOTA.search(raw["stderr"] + " " + error):
            self._fail(raw, error, raw["stderr"] + " " + error)
        if failed and _QUOTA.search(raw["stderr"]):
            self._fail(raw, "", raw["stderr"])
        self.session_id = sid or self.session_id
        extras: dict = {"permission": permission}
        if failed:
            extras["exit_code"] = raw["returncode"]
        if instructions:
            extras["instructions_added"] = instructions
        tokens_in, tokens_out = (tin, tout) if seen_tokens else (None, None)
        if reported > 0:
            cost, extras["cost_basis"] = reported, "reported"
        else:
            cost = self.backend.cost(tokens_in, tokens_out)          # None without prices: never 0 as known
            if cost is not None:
                extras["cost_basis"] = "computed from tokens and the seat's prices"
        text = "\n\n".join(texts if texts else all_texts)
        return SeatResult(text=text.strip(), cost_usd=cost, tokens_in=tokens_in, tokens_out=tokens_out,
                          session_id=self.session_id, model=self.backend.model, raw=raw, error=error,
                          extras=extras, tool_calls=list(calls.values()))

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
