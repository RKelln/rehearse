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

## Focused continuations

Testing one step should not need a whole conversation. A **continuation** is a transcript written in
advance, with a purpose and expectations. The subject is given the transcript and gives one reply,
which is checked against the expectations. It is for chat subjects.

Each continuation is a file in the case's `continuations` folder (or the folder named by
`continuations = "..."` in `case.toml`):

```markdown
---
purpose: "Keep a known outcome while saying what is missing."
expect:
  - Switches to feedback straight away
  - Says the outcome is known, not unknown
variant: current        # optional: run only with this variant
sim: ordinary           # optional: after the prefill, carry on as a normal conversation with this sim
max_turns: 4            # optional, needs sim: new subject replies allowed (default: the case's)
rubric: false           # optional: also ask the case rubric's questions (default: false without a sim, true with one)
---
## Person

Please start the mock interview.

## Subject

Tell me about helping a visitor.

## Person

I helped someone find a coat. Interview over.
```

- Sections are `## Person` and `## Subject`, the same headings `transcript.md` uses (a turn number in
  parentheses is allowed and ignored). The first and the last section must be Person, because the
  subject replies next, and the two sides must take turns: join two Person messages into one section.
- `purpose` and `expect` are required. `expect` lines are plain-language expectations.
- Without `sim`, the subject gives exactly one reply and the run stops with `continuation-done`.

```
rehearse continue CASE [--only id1,id2] [--variants a,b] [--repeats N] [--judge | --no-judge]
                       [--yes] [--max-usd X] [--dry-run]
```

It works like `suite`: a plan, an estimate, a question before a live run, and the same budget cap.
Runs are named `<date>-<case>-<variant>-cont-<id>-<n>`. Their `meta.json` has `continuation`,
`prefilled_turns`, `prefill_sha256` (a hash of the prefilled turns) and `prefill_mode: "messages"`;
`inputs/continuation.md` is a copy of the file. The board shows each continuation as the sim
`cont:<id>`.

**What the subject sees.** The variant, the context (below), and the prefilled turns as ordinary
messages: its own earlier turns as assistant messages, the person's as user messages. It never sees
the purpose or the expectations, and neither does a sim.

**Caveat.** The subject did not really write its earlier turns, so a good reply shows it can handle
that step, not that it would have got there. Check a step that matters in a full conversation too.

**Checks and judge.** `turns.jsonl` marks prefilled turns `prefilled: true`. In a check, use
`run.new_subject_turns` for what the subject really said (`run.subject_turns` includes the prefilled
turns). The sim check looks only at turns a sim generated; with none it is `skip` ("no sim turns: a
focused continuation") and voids nothing. The judge gets each `expect` line as a question with id `e1`,
`e2`, ... and no Pass/Fail text, and the purpose as context. It also gets the case rubric's questions when
the continuation's `rubric` is true (the default when it names a sim; a one-reply continuation asks only
its expectations). The board puts `e` questions in their own table per continuation, since they only line
up within one; a run that was never asked a question leaves that cell blank. The judge never sees the
variant text, the context, or the persona's instructions.

## Context and prompt length

Two optional settings in `[subject]` (chat subjects):

```toml
[subject]
context = "context.md"      # text appended to every variant's system prompt, under a "Context" heading
max_prompt_chars = 1500     # run, suite and continue refuse a variant file longer than this
```

The context is saved as `inputs/context.md`. The length limit counts the variant file alone, not the
context, which is shared test material. The limit needs a chat subject, and stops a prompt from growing past what the real
place allows. A sim does not get the context automatically: put what it should know on its knowledge page.

## Comparing runs of differently named variants

```
rehearse compare BEFORE AFTER --pair OLD=NEW      # repeatable
```

`--pair` compares the before runs of variant OLD with the after runs of variant NEW (the name is changed in
memory only; no run folder is touched). The variant column shows `OLD→NEW` and a warning says the variants
were paired by hand. Inputs are compared by the hash of what was written to disk; if one side never
recorded an input, compare says "not recorded" rather than "changed".
