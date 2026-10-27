# Design

Rehearse tests an AI that talks with people, by having another model play the person. You give it
the thing under test (a prompt, an agent with a skill, or your own program), one or more simulated
people, and the checks that say what "good" means. It runs the conversations, keeps every word, and
gives you a scoreboard you can compare from one change to the next.

It exists because three projects grew three versions of the same harness. The parts were always the
same; only the thing under test changed.

Status: design only. Nothing is built yet. The build plan, with answers to the open questions, is
in [`PLAN.md`](PLAN.md).

---

## What a test is made of

| Part | What it is | Example |
|---|---|---|
| **Subject** | The thing under test, behind an adapter | a weekly-review skill run by Claude Code in a folder |
| **Variant** | One version of the subject's instructions | `CLAUDE.md` with "be cheerful" versus "be plain" |
| **Sim** | A model playing the person, from a persona file | Dana, tired, answers in a few words |
| **Scenario** | The world the conversation happens in | a folder of week files and a work log; a maths problem |
| **Checks** | Code that reads the run and says pass, partial or fail | "the planned minutes add up to no more than the free time" |
| **Rubric** | Questions a judge model answers about the transcript | "Did it ask why before suggesting a fix?" |
| **Run** | One conversation, saved in its own folder | `runs/2026-10-26-review-dana-terse-1/` |
| **Suite** | Variants × sims × scenarios × repeats | 2 variants × 3 sims × 2 repeats = 12 runs |

A **case** is a folder in the project being tested (not in this repo) that names all of these in
`case.toml`. Rehearse is the tool; each project owns its case.

---

## Principles

When two conflict, the earlier one wins.

1. **Don't fool yourself.** One run is an anecdote. Report "4 of 6", never a percentage. Record the
   model in every seat, every time. A number without its denominator and its models is not a result.
2. **Watch the sim, not just the subject.** A simulated person who gives up, solves the problem, or
   leaks facts it was never given makes a clean scoreboard that means nothing. Every run gets a sim
   check, and a failed sim check voids that run's other checks.
3. **Isolation is the design.** The sim never sees the subject's instructions, the checks, the rubric
   or the answer key. The subject never sees the persona file. Each seat gets only what its role
   would really have.
4. **Read the transcripts.** Checks and the judge are a floor. The scoreboard points you at the runs to
   read; it does not replace reading them.
5. **Cost is visible and asked for.** Live runs spend someone's subscription or key. Every live
   command shows an estimate from past runs first, takes a budget cap, and records what it spent.
6. **Never edit sims or scenarios to move a number.** If a persona is unrealistic, change it in its
   own commit with a reason, and rerun the baseline.
7. **Plain files.** Personas, rubrics and runs are Markdown and JSON that diff in git. No database,
   no server, no dashboard.
8. **Any model.** Every seat (subject, sim, judge) can be any backend. Nothing outside a backend
   assumes one vendor. When a backend lacks a feature, the tool falls back (replay instead of
   resume) or records that it could not, and never pretends: unknown cost is "unknown", not zero.
9. **Small and dependency-free.** Python 3.11+ standard library. Run with `uv run`. Backends call the
   CLIs and HTTP endpoints people already have. Tests: `uv run python -m unittest discover -s tests`.

---

## Subjects: three adapters

The loop is the same for all three. Only "send a message, get a reply" differs.

### `chat`: a prompt in a plain chat

The subject is a system prompt (or a prompt pasted as the first message) and a model. Each turn
replays the whole transcript to the backend, as a consumer chat does. No tools. This covers prompts
people paste into a free chat, and interview or tutor prompts tested on their own.

### `agent`: an agent working in a folder

The subject is a headless coding agent (Claude Code `claude -p`, Codex `codex exec`, Gemini CLI)
running in a **fresh copy** of the scenario folder, with its own instructions and skills. The
session is kept and resumed each turn, so the agent keeps its own context and tool results the way it
would with a real person. Rehearse captures:

- the reply text, and every tool call (name and main argument)
- the files the agent wrote or changed (a diff of the folder before and after)
- cost and token use from the CLI's JSON output

Allowed tools are passed on the command line. A folder that has not been trusted interactively may
ignore its own settings file, so never rely on it.

The scenario lists files to **leave out** of the subject's copy (`exclude` in `case.toml`), such as
the sim's knowledge page or notes for facilitators. Telling the agent not to open a file is not
isolation; leaving it out is.

### `python`: your own program

The subject is a callable: `respond(state, message) -> (reply, state, extras)`. For a program that
makes several model calls of its own, such as a tutor engine with a checking pass. `extras` carries
anything the program wants recorded (blocked drafts, internal verdicts).

---

## Sims

A persona file is Markdown with frontmatter:

```markdown
---
name: dana-terse
knowledge: knowledge/dana.md     # the only facts the sim knows
opening: "Do Dana's weekly review for the week of October 19."
mood: "Types as little as possible: one to four words, no punctuation."
escalation:                      # optional: one line per turn after the first; the last repeats
  - "..."
stop: DONE                       # the sim says this when there is nothing left to answer
---

Who this person is and the rules they follow, in the body.
```

Every sim is wrapped in a shared frame (`frames/sim.md`, overridable per case): you are typing into
a chat; reply only with what the person types; keep it short; use only the knowledge page or small,
boring details that fit it; never invent facts that matter; answer only what was asked; don't
volunteer what the other side has not raised; say the stop word when nothing is left.
The run stops when the sim's reply, ignoring spaces, quotes and punctuation, is the stop word (any
case), or when its last word is the stop word written exactly as given, so "Thanks! DONE" stops but
"It's done." does not (the run records when the sim added other words).

Sims run with no tools, in an empty working folder, with no settings or memory loaded, on a model
named in the suite. A cheaper model than the subject is usually fine for a sim.

A `human` sim seat lets a person type the person's side, for the runs that matter most.

---

## The loop

```
opening line (from the persona, or the case)
repeat until a stop rule fires:
    subject replies
    sim replies
stop rules: the sim says its stop word; the subject's reply matches the case's subject stop pattern
            (an interviewer's closing debrief); the artifact rule below; the turn cap;
            a backend error (recorded, not retried silently)
```

**The artifact rule** (`agent` subjects). A sim that never says its stop word after the subject has
finished would run to the turn cap. The case can name the subject's deliverable in `[limits]`:
`stop_after_artifact = "review.md"` (a path in the work folder). After each subject turn the run
stops with reason `artifact-done` when that file exists, the reply asks nothing (no "?"), and the
reply names the file or the file changed in that turn: the subject is saying it is saved. A quiet
reply alone is not enough, because some agents write a draft early and keep talking (a live run was
cut short at turn 3 that way). `turns_after_artifact = N` (optional, off by default) also stops the
run N subject turns after the file first appeared. The
sim's stop word works as before and wins if it comes first. The turn after which the file was first
seen is recorded in `meta.json` as `artifact_seen_turn` (null if it never appeared). Without the
setting nothing changes. The judge's `stopped_on_cue` field says whether the sim should have stopped
earlier (reported, never voids a run).

**Focused continuations.** Testing one step should not need a whole conversation:

- `chat`: start from a prefilled transcript in the case (`continuations/*.md`). Each one has a
  purpose and a list of expectations in plain words. The expectations become rubric questions for
  that continuation; neither the purpose nor the expectations reach the subject. The prefilled turns
  are sent as ordinary messages (the subject's own earlier turns as assistant messages), but the
  subject did not really produce them, so a reply to them is evidence about one step, not about a
  whole conversation. The run records `prefill_mode: "messages"` and marks each prefilled turn.
  By default the subject gives exactly one reply; a continuation may name a sim to carry on from
  there. See [`CHECKS.md`](CHECKS.md#focused-continuations).
- `agent`: save a checkpoint session after turn N of a reference run, then fork it
  (`claude -p --resume ID --fork-session`) with a different sim reply. One reference run can feed
  many cheap checks of a single step.

---

## Checks, rubric, judge

**Checks** are Python in the case, `checks.py`, one function per check:

```python
def minutes_fit(run) -> Check:
    """The planned minutes in review.md add up to no more than the free time."""
```

`run` gives the transcript, the subject's turns, tool calls, the artifacts, the folder diff, and the
scenario's own files. A check returns pass, partial or fail with one line of evidence. Typical ones:
only the allowed file was written; every quoted item exists in the scenario; a total adds up; no
reply over N sentences; a known wrong fact in the scenario was caught.

**Rubric** is Markdown: numbered questions with what pass and fail look like. The **judge** is a model
call that reads the transcript (and, if the rubric says so, the answer key) and returns JSON, one
verdict and one short quote per question. The judge's model is recorded and its results are
labelled directional. Judging is optional and off by default.

**Sim check** (always on when judging, and listed for human review otherwise): did the sim stay in
character, keep up the pressure its persona describes, and use only its knowledge page?

---

## Runs on disk

```
runs/<date>-<case>-<variant>-<sim>-<n>/
  transcript.md        both sides, tool calls inline, paths relative
  artifacts/           files the subject wrote, and the folder diff
  inputs/              a snapshot of the variant, persona, frame, scenario manifest (with hashes)
  raw/                 every request and reply, per seat, as JSON
  meta.json            models per seat, cost per seat, turns, stop reason, case git commit, tool version
  checks.json          per check: result and evidence
  judge.json           if judged
  assessment.md        a stub for a person to fill in
```

Run folders are never overwritten. Raw output is scrubbed before it is written: a subscription CLI can
put the account's email address and local paths into its output.

Where runs live is the case's choice (in its repo, or in a gitignored folder with only the
assessments committed).

---

## Commands

```
rehearse run CASE --variant V --sim S [--repeats N] [--max-usd X]     one cell
rehearse suite CASE [--sims a,b] [--variants a,b] [--repeats N] [--jobs J] [--max-usd X] [--dry-run]
rehearse continue CASE [--only ids] [--variants a,b] [--repeats N]    focused continuations (chat)
rehearse check RUN...            rerun checks on saved runs (free, no model calls)
rehearse judge RUN...            judge saved runs
rehearse board RUNS...           scoreboard: variant × sim × check, "k of n"
rehearse compare BEFORE AFTER [--pair OLD=NEW]   what moved between two sets of runs
rehearse import FOLDER           bring in runs made before this tool existed
```

`--dry-run` lists the runs and estimates the cost from past runs of the same case. Live commands
print that estimate and ask before starting, unless `--yes` is passed.

---

## Backends

| Backend | How | Notes |
|---|---|---|
| `claude` | `claude -p --output-format stream-json`, `--resume`, `--fork-session`, `--allowedTools`, `--tools ""` for sims, `--setting-sources ""` | subscription or key; JSON has `total_cost_usd`, cumulative per session |
| `opencode` | `opencode run --standalone --format json`, `-s ID [--fork]`, permissions and provider in `OPENCODE_CONFIG_CONTENT` | agent seats on any provider; cost only from configured prices |
| `codex` | `codex exec` | |
| `gemini` | `gemini -p` | |
| `openai` | any OpenAI-compatible HTTP endpoint (keys from the environment) | for chat subjects and sims |
| `mock` | scripted replies | for testing the tool itself; mock runs pass or fail by construction |
| `human` | a person types | |

A seat is written `backend[:model]`, for example `claude:sonnet` or `http:llama3@http://localhost:11434/v1`.

Every backend implements one interface. A chat seat takes a system prompt and messages and returns
the reply text, cost and tokens when known, and the raw output. An agent seat can also start in a
folder, resume a session, and fork one. A backend declares what it supports; where it lacks resume or
fork, the tool replays the transcript instead and records that it did.

---

## The iteration loop it supports

1. Baseline: run the suite, keep the run folders.
2. Read the failed cells' transcripts.
3. Name one failure in a sentence.
4. Change one thing in the variant.
5. Rerun with the same sims, scenarios, models and repeats.
6. `compare`. If the case has a holdout set, check it before believing a gain.
7. Write it down: the failure, the change, both run sets, the numbers with denominators.

---

## Non-goals

- A hosted service, dashboard or database.
- Automatic prompt tuning. People change the variant; the tool measures.
- Sandboxing beyond a fresh folder copy. Scenarios are made-up data; the agent's tool permissions
  are the boundary, set per case.

---

## Open questions

Answered as proposals in [`PLAN.md`](PLAN.md): name, prior art, judge default, where runs live,
concurrency, and parsing artifacts.
