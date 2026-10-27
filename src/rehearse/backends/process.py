"""Running a command line program as its own process group, shared by the program-based backends."""

from __future__ import annotations

import os
import signal
import subprocess

from .base import BackendError, SetupError


def kill(proc: subprocess.Popen) -> None:
    """Kill the program and everything it started (it runs in its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def run(argv: list[str], stdin: str, cwd, timeout: float, label: str, *, program: str, env_name: str,
        env: dict | None = None) -> dict:
    """Run the tool and return {"stdout", "stderr", "returncode"}; the raw output is kept as is.

    The tool gets its own process group, which is killed on a timeout or Ctrl-C, so no orphan is left.
    `program` and `env_name` only shape the "cannot find it" message.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(cwd), start_new_session=True, env=env)
    except FileNotFoundError:
        raise SetupError(f"cannot find the {program} program ({argv[0]!r}). Install it, or set "
                         f"{env_name} to its path") from None
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(proc)
        out, err = proc.communicate()
        raise BackendError(f"{label}: timed out after {timeout:g} seconds",
                           raw={"stdout": out, "stderr": err, "returncode": None}) from None
    except BaseException:
        kill(proc)
        raise
    return {"stdout": out, "stderr": err, "returncode": proc.returncode}
