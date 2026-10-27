"""Test doubles for the outside world: a fake `claude` program and a local OpenAI-style server.

Nothing here touches the network beyond 127.0.0.1, and nothing calls a real model.
"""

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

FAKE_CLAUDE = """#!{python}
import json, os, sys
log = os.environ["FAKE_LOG"]
script = json.load(open(os.environ["FAKE_SCRIPT"]))
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
argv = sys.argv[1:]
rec = {{"argv": argv, "stdin": sys.stdin.read(), "cwd": os.getcwd(), "listing": sorted(os.listdir("."))}}
if "--system-prompt-file" in argv:
    path = argv[argv.index("--system-prompt-file") + 1]
    rec["system_file"] = {{"path": path, "content": open(path).read(), "mode": os.stat(path).st_mode & 0o777,
                           "dir_mode": os.stat(os.path.dirname(path)).st_mode & 0o777}}
with open(log, "a") as f:
    f.write(json.dumps(rec) + "\\n")
step = script[min(n, len(script) - 1)]
if step.get("sleep"):
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(log + ".pid", "w").write(str(child.pid))
    time.sleep(step["sleep"])
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("exit", 0))
"""


FAKE_OPENCODE = """#!{python}
import json, os, sys
log = os.environ["FAKE_LOG"]
script = json.load(open(os.environ["FAKE_SCRIPT"]))
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
home = os.environ.get("HOME", "")
rec = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": sys.stdin.read(),
        "home_listing": sorted(os.listdir(home)) if os.path.isdir(home) else None,
        "home_mode": os.stat(home).st_mode & 0o777 if os.path.isdir(home) else None}}
with open(log, "a") as f:
    f.write(json.dumps(rec) + "\\n")
step = script[min(n, len(script) - 1)]
for rel, text in (step.get("write") or {{}}).items():
    with open(rel, "w") as f:
        f.write(text)
if step.get("sleep"):
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(log + ".pid", "w").write(str(child.pid))
    time.sleep(step["sleep"])
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("exit", 0))
"""


class FakeOpencode:
    """A script standing in for the `opencode` program. Records argv, cwd and environment; replies from `steps`.

    Each step is {"stdout", "stderr", "exit", "write": {file: text}, "sleep"}; the last step repeats.
    """

    def __init__(self, folder: Path, steps: list[dict]):
        folder.mkdir(parents=True, exist_ok=True)
        self.bin = folder / "opencode"
        self.log = folder / "calls.jsonl"
        self.script = folder / "script.json"
        self.bin.write_text(FAKE_OPENCODE.format(python=sys.executable))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.script.write_text(json.dumps(steps))
        self.patcher = mock.patch.dict(os.environ, {
            "REHEARSE_OPENCODE_BIN": str(self.bin), "FAKE_LOG": str(self.log), "FAKE_SCRIPT": str(self.script)})

    def __enter__(self):
        self.patcher.start()
        return self

    def __exit__(self, *exc):
        self.patcher.stop()

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines()]


def oc_events(texts=("done",), sid="ses_1", tools=(), tokens=(10, 4, 0, 2, 1), cost=0):
    """JSON lines for one opencode turn.

    `tools` is a list of (tool, input dict), each before the final texts. `tokens` is
    (input, output, reasoning, cache read, cache write).
    """
    def ev(kind, part):
        return json.dumps({"type": kind, "timestamp": 1, "sessionID": sid, "part": part})
    t_in, t_out, t_reason, c_read, c_write = tokens
    lines = [ev("step_start", {"type": "step-start"}), ev("text", {"type": "text", "text": "thinking aloud"})] if tools else []
    for i, (name, tool_input) in enumerate(tools):
        lines.append(ev("tool_use", {"type": "tool", "tool": name, "callID": f"c{i}",
                                     "state": {"status": "completed", "input": tool_input, "output": "ok", "title": name}}))
    lines += [ev("text", {"type": "text", "text": t}) for t in texts]
    lines.append(ev("step_finish", {"type": "step-finish", "reason": "stop", "cost": cost,
                                    "tokens": {"input": t_in, "output": t_out, "reasoning": t_reason,
                                               "cache": {"read": c_read, "write": c_write}}}))
    return "\n".join(lines) + "\n"


class FakeClaude:
    """A script standing in for the `claude` program. Records every call; replies from `steps`.

    Each step is {"stdout": ..., "stderr": ..., "exit": ...}; the last step repeats.
    """

    def __init__(self, folder: Path, steps: list[dict]):
        folder.mkdir(parents=True, exist_ok=True)
        self.bin = folder / "claude"
        self.log = folder / "calls.jsonl"
        self.script = folder / "script.json"
        self.bin.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.script.write_text(json.dumps(steps))
        self.patcher = mock.patch.dict(os.environ, {
            "REHEARSE_CLAUDE_BIN": str(self.bin), "FAKE_LOG": str(self.log), "FAKE_SCRIPT": str(self.script)})

    def __enter__(self):
        self.patcher.start()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        return self

    def __exit__(self, *exc):
        self.patcher.stop()

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines()]


def chat_json(text="hello", cost=0.01, model="claude-test", is_error=False, **extra):
    return json.dumps({"type": "result", "result": text, "total_cost_usd": cost, "is_error": is_error,
                       "session_id": "chat-session", "usage": {"input_tokens": 10, "output_tokens": 5},
                       "modelUsage": {model: {}}, **extra})


def stream(text="done", cost=0.1, sid="sess-1", tools=(), is_error=False, model="claude-test"):
    """stream-json lines for one agent turn. `tools` is a list of (name, input dict)."""
    events = [{"type": "system", "subtype": "init", "session_id": sid, "model": model, "tools": ["Read", "Write"]}]
    for name, tool_input in tools:
        events.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": tool_input}]}})
    events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    events.append({"type": "result", "subtype": "success", "result": text, "total_cost_usd": cost,
                   "is_error": is_error, "session_id": sid, "usage": {"input_tokens": 7, "output_tokens": 3}})
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FakeServer:
    """A local OpenAI-style server. `replies` is a list of (status, headers, body); the last repeats."""

    def __init__(self, replies=None):
        self.replies = replies or [ok_body()]
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(body) if body else None})
                n = len(outer.requests) - 1
                status, headers, payload = outer.replies[min(n, len(outer.replies) - 1)]
                data = payload if isinstance(payload, bytes) else payload.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok_body(text="Hello there", tokens_in=11, tokens_out=7, model="fake-model"):
    body = {"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}}
    return (200, {"Content-Type": "application/json"}, json.dumps(body))


FAKE_CODEX = """#!{python}
import json, os, sys
log = os.environ["FAKE_LOG"]
script = json.load(open(os.environ["FAKE_SCRIPT"]))
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
home = os.environ.get("CODEX_HOME", "")
auth = os.path.join(home, "auth.json")
rec = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": sys.stdin.read(),
        "stdin_target": os.readlink("/proc/self/fd/0"),
        "home_listing": sorted(os.listdir(home)) if os.path.isdir(home) else None,
        "home_mode": os.stat(home).st_mode & 0o777 if os.path.isdir(home) else None,
        "auth_mode": os.stat(auth).st_mode & 0o777 if os.path.exists(auth) else None,
        "auth_content": open(auth).read() if os.path.exists(auth) else None}}
with open(log, "a") as f:
    f.write(json.dumps(rec) + "\\n")
step = script[min(n, len(script) - 1)]
for rel, text in (step.get("write") or {{}}).items():
    with open(rel, "w") as f:
        f.write(text)
for rel, text in (step.get("session") or {{}}).items():
    path = os.path.join(home, "sessions", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
if step.get("sleep"):
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(log + ".pid", "w").write(str(child.pid))
    time.sleep(step["sleep"])
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("exit", 0))
"""


class FakeCodex(FakeOpencode):
    """A script standing in for the `codex` program. Records argv, cwd, stdin, environment and CODEX_HOME.

    Each step is {"stdout", "stderr", "exit", "write": {file: text}, "session": {path under sessions/: text},
    "sleep"}; the last step repeats.
    """

    def __init__(self, folder: Path, steps: list[dict]):
        folder.mkdir(parents=True, exist_ok=True)
        self.bin = folder / "codex"
        self.log = folder / "calls.jsonl"
        self.script = folder / "script.json"
        self.bin.write_text(FAKE_CODEX.format(python=sys.executable))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.script.write_text(json.dumps(steps))
        self.patcher = mock.patch.dict(os.environ, {
            "REHEARSE_CODEX_BIN": str(self.bin), "FAKE_LOG": str(self.log), "FAKE_SCRIPT": str(self.script)})


def codex_events(texts=("done",), tid="thr_1", items=(), usage=(100, 60, 0, 20, 5)):
    """JSON lines for one codex turn.

    `items` are extra item dicts, each completed before the texts (all but the last text come before them
    too, as preamble messages). `usage` is (input, cached input, cache write, output, reasoning output).
    """
    def ev(kind, **rest):
        return json.dumps({"type": kind, **rest})
    t_in, cached, c_write, t_out, reasoning = usage
    texts = list(texts)
    lines = [ev("thread.started", thread_id=tid), ev("turn.started")]
    for i, text in enumerate(texts[:-1]):
        lines.append(ev("item.completed", item={"id": f"m{i}", "type": "agent_message", "text": text}))
    for i, item in enumerate(items):
        lines.append(ev("item.started", item={"id": f"i{i}", **item}))
        lines.append(ev("item.completed", item={"id": f"i{i}", **item}))
    if texts:
        lines.append(ev("item.completed", item={"id": "last", "type": "agent_message", "text": texts[-1]}))
    lines.append(ev("turn.completed", usage={"input_tokens": t_in, "cached_input_tokens": cached,
                                             "cache_write_input_tokens": c_write, "output_tokens": t_out,
                                             "reasoning_output_tokens": reasoning}))
    return "\n".join(lines) + "\n"
