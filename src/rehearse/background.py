"""Run a rehearse command detached, so an agent (or a person) can keep working, and look at it later."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def jobs_dir(runs_dir: Path) -> Path:
    return Path(runs_dir) / ".jobs"


def start(argv: list[str], runs_dir: Path) -> Path:
    """Start `rehearse ARGV` detached. Output goes to a log; the exit code is written when it ends.

    Returns the log path. The job is a shell that runs the command and then records `$?` in a `.exit` file,
    so `status` can tell finished from crashed from still running.
    """
    d = jobs_dir(runs_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log, exit_file = d / f"{stamp}.log", d / f"{stamp}.exit"
    cmd = [sys.executable, "-m", "rehearse.cli", *argv]
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in cmd)
    with open(log, "w") as out:
        proc = subprocess.Popen(["sh", "-c", f"{quoted}; echo $? > '{exit_file}'"], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    (d / f"{stamp}.json").write_text(json.dumps({"pid": proc.pid, "argv": argv}))
    return log


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status(runs_dir: Path, last: int = 5) -> list[str]:
    """One line per recent job: when, state, what, and the last line of its log."""
    d = jobs_dir(runs_dir)
    lines = []
    for meta in sorted(d.glob("*.json"))[-last:] if d.is_dir() else []:
        info = json.loads(meta.read_text())
        exit_file = meta.with_suffix(".exit")
        if exit_file.exists():
            code = exit_file.read_text().strip()
            state = "done" if code == "0" else f"ended with code {code}"
        else:
            state = "running" if _alive(info["pid"]) else "stopped without finishing"
        log = meta.with_suffix(".log")
        tail = [ln for ln in log.read_text().splitlines() if ln.strip()][-1:] if log.exists() else []
        lines.append(f"{meta.stem}  {state}  rehearse {' '.join(info['argv'])}")
        lines.append(f"    log: {log}" + (f"\n    last: {tail[0]}" if tail else ""))
    return lines
