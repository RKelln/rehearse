"""Tolerant helpers for reading what a subject wrote, so checks do not break on small differences.

Every helper returns None (or an empty list) when it finds nothing, and never raises on odd input.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _norm_heading(text: str) -> str:
    """Lowercase, drop leading numbering like "5." and punctuation, collapse spaces."""
    text = re.sub(r"^\s*\d+[.)]?\s+", "", text.strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def section(md: str, *titles: str) -> str | None:
    """The text under the first heading that matches any of `titles`.

    A heading whose words *start with* a title is preferred over one that merely contains it.
    The text runs to the next heading of the same or a higher level. Case, numbering and
    punctuation in headings are ignored. Returns None if no heading matches.

    >>> section("# Plan\\n## 5. Next steps!\\nDo X.\\n## Other\\nY", "next steps")
    'Do X.'
    """
    wanted = [t for t in (_norm_heading(t) for t in titles) if t]
    lines = md.splitlines()
    found = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m:
            found.append((i, len(m.group(1)), _norm_heading(m.group(2))))
    for test in (lambda h, w: h.startswith(w), lambda h, w: w in h):
        for i, level, heading in found:
            if any(test(heading, w) for w in wanted):
                body = []
                for later in lines[i + 1:]:
                    h = _HEADING.match(later)
                    if h and len(h.group(1)) <= level:
                        break
                    body.append(later)
                return "\n".join(body).strip()
    return None


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _is_rule(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-+:?", c) or c == "" for c in cells) and any(cells)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "").replace("`", "")).strip().lower()


def tables(md: str) -> list[list[dict]]:
    """Every Markdown table in the text, each as a list of rows (dicts).

    Row keys are the header cells, lowercased with spaces collapsed. Leading and trailing pipes
    are optional. A table with no alignment row (`|---|---|`) has no header, and its keys are
    "1", "2", ... Cells past the header's width get numbered keys; missing cells are absent.

    >>> tables("| Day | Min |\\n|---|---|\\n| Tue | 30 |")
    [[{'day': 'Tue', 'min': '30'}]]
    """
    blocks, current = [], []
    for line in md.splitlines():
        if "|" in line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    result = []
    for block in blocks:
        rows = [_cells(l) for l in block]
        header = None
        if len(rows) >= 2 and _is_rule(rows[1]) and not _is_rule(rows[0]):
            header = [_key(c) for c in rows[0]]
            rows = rows[2:]
        else:
            rows = [r for r in rows if not _is_rule(r)]
        table = []
        for r in rows:
            row = {}
            for i, cell in enumerate(r):
                k = header[i] if header and i < len(header) and header[i] else str(i + 1)
                row[k] = cell
            table.append(row)
        if table or header:
            result.append(table)
    return result


def column(row: dict, *names: str) -> str | None:
    """The first value in `row` whose key contains any of `names` (case-insensitive).

    >>> column({"planned minutes": "30"}, "minutes", "min")
    '30'
    """
    for key, value in row.items():
        if any(n.lower() in key for n in names):
            return value
    return None


_DAYS = {"Mon": r"mon(?:day)?", "Tue": r"tue(?:s|sday)?", "Wed": r"wed(?:s|nesday)?",
         "Thu": r"thu(?:r|rs|rsday)?", "Fri": r"fri(?:day)?", "Sat": r"sat(?:urday)?",
         "Sun": r"sun(?:day)?"}
_DAY_RES = {k: re.compile(rf"\b{v}\b", re.I) for k, v in _DAYS.items()}


def weekday(text: str) -> str | None:
    """The first weekday named in the text, as "Mon" .. "Sun", or None.

    Matches whole words only, so "month" and "Monica" do not count.

    >>> weekday("Tues 27 (evening)"), weekday("Saturday morning leak"), weekday("next month")
    ('Tue', 'Sat', None)
    """
    best = None
    for short, rx in _DAY_RES.items():
        m = rx.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), short)
    return best[1] if best else None


_N = r"(\d+(?:\.\d+)?)"
_RANGE = _N + r"(?:\s*(?:-|\u2013|to)\s*" + _N + r")?"       # "5" or "5-10": the upper value wins
_HOURS = re.compile(_RANGE + r"\s*(?:h|hr|hrs|hour|hours)\b(?:\s*(?:and\s*)?(\d+)\s*(?:m|min|mins|minute|minutes)\b)?")
_MINS = re.compile(_RANGE + r"\s*(?:m|min|mins|minute|minutes)\b")


def minutes(text: str) -> int | None:
    """A length of time as whole minutes, or None.

    The leftmost time expression wins. A range such as "5-10 min" gives its upper value, the
    cautious choice when adding up totals.

    >>> [minutes(t) for t in ("90", "90 min", "1.5 h", "1h 30m", "2 hours", "45 minutes", "—")]
    [90, 90, 90, 90, 120, 45, None]
    >>> minutes("30 min (of 2 hours)"), minutes("5-10 min")
    (30, 10)
    """
    t = (text or "").lower().replace("*", "")
    found = []
    m = _HOURS.search(t)
    if m:
        found.append((m.start(), round(float(m.group(2) or m.group(1)) * 60 + int(m.group(3) or 0))))
    m = _MINS.search(t)
    if m:
        found.append((m.start(), round(float(m.group(2) or m.group(1)))))
    if found:
        return min(found)[1]
    m = re.fullmatch(r"\W*" + _N + r"\W*", t)
    return round(float(m.group(1))) if m else None


_QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|\*\*([^*\n]+)\*\*')


def quoted(md: str) -> list[str]:
    """Pieces in "straight" or curly quotes, or in **bold**, in the order they appear."""
    return [next(g for g in m.groups() if g is not None).strip() for m in _QUOTED.finditer(md or "")]
