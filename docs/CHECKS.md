# Writing checks

A **check** is a small Python function that reads one saved run and says pass, partial or fail. Checks
read files only. They never call a model, so they cost nothing and can be rerun at any time with
`rehearse check`.

## Where checks live

In the case folder, in a file named in `case.toml`:

```toml
name = "my-case"
checks = "checks.py"
```

## Which functions count

A function is a check when all of these are true:

- it is defined in the checks file (functions imported from elsewhere are ignored);
- its name does not start with `_`;
- it takes exactly one parameter, and that parameter is named `run`.

So `def total(text)` is a helper and is ignored, and `def minutes_fit(run)` is a check. The check's name is
the function name. Its description is the first line of its docstring. The checks file may import other
files that sit next to it.

## What a check returns

```python
from rehearse.checks import Check

def asked_a_question(run):
    """The subject asked at least one question."""
    if any("?" in t for t in run.subject_turns):
        return Check.passed("asked in the first reply")
    return Check.failed("no question mark in any reply")
```

| Constructor | Meaning |
|---|---|
| `Check.passed(evidence)` | the run did what was wanted |
| `Check.partial(evidence)` | partly; counts inside the total on the board |
| `Check.failed(evidence)` | it did not |
| `Check.skip(reason)` | the check does not apply to this run; left out of the total |

Evidence is one line saying why. Returning `True` or `False` also works, with the evidence
"(no evidence given)". If a check raises an error, the result is `error` with the error's type and
message, and the other checks still run. An `error` is left out of the total and shown on the board.

## The run view

| What | Gives |
|---|---|
| `run.meta` | the run's `meta.json` as a dict |
| `run.turns` | every turn, as dicts with `side`, `n`, `text` |
| `run.subject_turns`, `run.person_turns` | the texts only, in order (the person's list starts with the opening line) |
| `run.transcript` | `transcript.md` as text |
| `run.tool_calls` | every tool call the subject made (empty for a chat subject) |
| `run.artifact(name)`, `run.artifacts` | a file the subject wrote, as text or None; the folder |
| `run.diff` | `{"added": [], "changed": [], "removed": []}` for the subject's folder |
| `run.knowledge` | the sim's knowledge page |
| `run.scenario_file(rel)` | a file from the case's scenario, or None |

Each check gets its own copy of the run, so one check cannot change what another sees.

## Parsing helpers

`rehearse.parse` reads what a subject wrote without breaking on small differences. Each returns None
or an empty list when it finds nothing.

```python
from rehearse.parse import section, tables, column, weekday, minutes, quoted

section(md, "next steps")            # text under the first heading that starts with (or contains) it
tables(md)                           # [[{"day": "Tue", "minutes": "30"}, ...], ...]
column(row, "minutes", "min")        # first value whose header contains any of the names
weekday("Tues 27 (evening)")         # "Tue"; "next month" gives None
minutes("1h 30m")                    # 90; "5-10 min" gives 10, the cautious upper value
quoted(md)                           # pieces in "straight" quotes, curly quotes or **bold**
```

Tables may lack outer pipes or a header (then the keys are "1", "2", ...), and rows may be uneven.

## Sim check

Every run also gets a sim check, with no setup. It looks for a simulated person that gave up, said it
was an AI, or stated numbers it was never given. A **fail** voids the run: its other checks are saved but
left out of the board. A **partial** does not void. See `checks.json` in the run folder for each item.
