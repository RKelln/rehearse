"""Read the frontmatter of a Markdown file.

Parts adapted from an earlier CC0-licensed harness by the same author.

Frontmatter is a block between two `---` lines at the top of the file. It is flat:
`key: value` lines, where the value may be quoted. A key with no value followed by
`  - item` lines is a list. A trailing ` # comment` after an unquoted value is dropped.
"""

from __future__ import annotations

import re

_ITEM = re.compile(r"^\s+-\s*(.*)$")


def _value(raw: str) -> str:
    """One value: strip a quote pair, or else a trailing comment."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[1:end]
    return re.sub(r"\s+#.*$", "", raw).strip()


def parse(text: str) -> tuple[dict, str]:
    """Split a file into (frontmatter dict, body). No frontmatter gives ({}, text)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, text
    data: dict = {}
    key = None
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        item = _ITEM.match(ln)
        if item and key is not None:
            if not isinstance(data[key], list):
                data[key] = []
            data[key].append(_value(item.group(1)))
            continue
        name, sep, rest = ln.partition(":")
        if not sep:
            continue
        key = name.strip()
        data[key] = _value(rest)
    return data, "\n".join(lines[end + 1:]).strip("\n")
