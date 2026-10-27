"""The work folder check: did an agent stay in its own folder? Code only, no model.

An agent that works in the wrong folder leaves an empty folder diff and nothing else to show for it.
The backends write the work folder as "./" in each tool call's argument, so a path that is still
absolute (or "~", or climbs out with "..") points outside it. A "fail" voids the run, like the sim check.
"""

from __future__ import annotations

import re

from .simcheck import ORDER

FILE_TOOLS = {"read", "write", "edit", "multiedit", "glob", "grep", "list", "ls", "patch", "apply_patch",
              "notebookedit", "notebookread"}
WRITE_TOOLS = {"write", "edit", "multiedit", "patch", "apply_patch", "notebookedit"}
SHELL_TOOLS = {"bash"}
# An absolute or "~" path inside a shell command, not part of a longer word or a URL.
_SHELL_PATH = re.compile(r"(?<![\w./:~-])(?:~[\w-]*(?:/[^\s'\"`;|&)<>]*)?|/[^\s'\"`;|&)<>]+)")
_HARMLESS = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def _item(name, result, evidence):
    return {"name": name, "result": result, "evidence": evidence}


def _outside(path: str) -> bool:
    """True for a path that is absolute, starts with "~", or climbs out of the folder with "..".""" 
    return path.startswith(("/", "~")) or path == ".." or path.startswith("../") or "/../" in path


def _listed(paths: list[str]) -> str:
    return ", ".join(repr(p[:60]) for p in paths[:3]) + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")


def work_folder_check(run) -> dict:
    """Check where an agent run's tool calls pointed. Returns items, an overall result, and void info."""
    calls = [c for c in run.tool_calls if isinstance(c, dict)]
    if run.meta.get("adapter", "agent") == "chat" or not calls:
        return {"result": "skip", "items": [_item("no_tool_calls", "skip", "no tool calls: a chat or imported run")],
                "evidence": "no tool calls", "void": False, "void_reason": ""}
    names = lambda c: str(c.get("name") or "").lower()
    outside = [c["arg"] for c in calls if names(c) in FILE_TOOLS and isinstance(c.get("arg"), str)
               and _outside(c["arg"])]
    mentioned = [p for c in calls if names(c) in SHELL_TOOLS and isinstance(c.get("arg"), str)
                 for p in _SHELL_PATH.findall(c["arg"]) if p not in _HARMLESS]
    items = []
    if outside:
        items.append(_item("paths_inside_work_folder", "fail",
                           f"a file tool used a path outside the work folder: {_listed(outside)}"))
    elif mentioned:
        items.append(_item("paths_inside_work_folder", "partial",
                           f"a shell command mentions a path outside the work folder? check: {_listed(mentioned)}"))
    else:
        items.append(_item("paths_inside_work_folder", "pass", "every path is inside the work folder"))
    writes = [c for c in calls if names(c) in WRITE_TOOLS]
    d = run.diff
    if not writes:
        items.append(_item("wrote_something_when_tools_wrote", "skip", "no write or edit tool calls"))
    elif not (d.get("added") or d.get("changed") or d.get("removed")):
        items.append(_item("wrote_something_when_tools_wrote", "fail",
                           f"the agent wrote files ({len(writes)} write or edit calls), "
                           "but none changed in its work folder"))
    else:
        items.append(_item("wrote_something_when_tools_wrote", "pass", "the work folder changed"))
    worst = max((i["result"] for i in items if i["result"] != "skip"), key=ORDER.get)
    failed = [f"{i['name']}: {i['evidence']}" for i in items if i["result"] == "fail"]
    return {"result": worst, "items": items, "evidence": "; ".join(i["evidence"] for i in items if i["result"] == worst),
            "void": bool(failed), "void_reason": "; ".join(failed)}
