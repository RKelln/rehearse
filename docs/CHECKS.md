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
| `run.artifact(name)`, `run.artifacts` | a file the subject added or changed (by its path in the work folder), as text or None; the `artifacts` folder |
| `run.diff` | `{"added": [], "changed": [], "removed": []}` for the subject's folder. An entry is a path, or a dict with a `"path"` and a note: `"symlink_to"`, `"mode"`, `"scrubbed"` or `"withheld"` |
| `run.knowledge` | the sim's knowledge page |
| `run.scenario_file(rel)` | a file from the case's scenario folder (the original, not the work copy), or None |

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

## Rubric and judge

Some things cannot be checked in code, such as "did the review say what a good review says". For those a
case can have a **rubric** and a **judge**: another model that reads a saved run and answers the rubric's
questions. Judging is optional and off by default. Its results are one model's opinion, so they are
**directional**: read the transcripts before believing them.

In `case.toml`:

```toml
rubric = "rubric.md"
answer_key = "answer-key.md"      # optional; only shown to the judge if the rubric says so

[judge]
seat = "deepseek:deepseek-chat"   # backend[:model], or a table like the other seats
default = false                   # true: `run` and `suite` judge each run after its checks
```

`rubric.md` has optional frontmatter, then numbered questions. Each may have `Pass:` and `Fail:` lines:

```markdown
---
answer_key: true     # show the answer key to the judge (default false)
artifacts: true      # show the files the subject wrote (default true)
---
1. Did it ask what the person needs before writing?
   Pass: it asks an open question first
   Fail: it starts writing with no question
2. Does the plan fit the free time?
   Pass: the minutes add up to no more than the free time
```

A malformed rubric (no questions, numbers out of order, text before question 1) or a missing answer key
stops the case from loading, with a plain message.

**What the judge sees:** the transcript (person and subject turns, the subject's tool calls inline),
the text of the files the subject wrote (each cut at 30 kB), the answer key only if the rubric asks, and
the sim's mood and knowledge page. It never sees the variant text, so it cannot be nudged by what the
variant says. It must reply with one JSON object: a verdict (`pass`, `partial`, `fail` or `unclear`) and
one quote per question, plus a `sim` part (in character, kept up its mood, used only its knowledge).

**Quote verification.** Every quote must appear in what the judge was shown (ignoring case, spacing and
quote marks). A verdict whose quote is not found, or is empty, becomes `unclear` and is marked
`quote_not_found`. A question the judge skipped is `unclear`. If the reply is not JSON, the judge is
asked once more; after that the error is recorded in `judge.json` and nothing else breaks.

**Sim part.** If the judge says the sim was not in character, or used facts outside its knowledge page,
and gives a quote that is found, the run is void: the board leaves it out of every column.

**Files.** `judge.json` goes in the run folder (the raw replies are `raw/judge-NN.json`, saved before
parsing). A rerun moves the old file to `history/judge-<time>.json`. The board adds `judge N` columns:
`pass` counts as a pass, `partial` as partial, `unclear` is left out and shown as `+1 unclear`, and runs
with no `judge.json` show `+1 unjudged` once any run in the cell has one.

```
rehearse judge RUN... [--case CASE] [--seat SPEC] [--dry-run] [--yes] [--max-usd X]
rehearse run CASE ... --judge        # judge each run right after its checks (--no-judge to turn off)
```

A live judge seat prints a plan and asks first (or needs `--yes`).
