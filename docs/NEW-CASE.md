# Making a new case

A **case** is a folder, kept in the project being tested, that holds everything one test needs. This is
the shortest path from "I have a prompt" to a first live run. Read [`DESIGN.md`](DESIGN.md) for why.

## 1. Pick the subject type

| The thing under test is... | `adapter` | Notes |
|---|---|---|
| a prompt pasted into a plain chat | `chat` | The whole transcript is replayed each turn. Any chat backend (`claude`, `deepseek`, `openai`, `http`...). |
| an agent working in a folder (reads files, writes files) | `agent` | Needs a backend that can resume a session: `claude`, `codex`, `opencode`. |
| a prompt, but you want to test it through `codex` or `opencode` | `agent` | Those two cannot chat. Put the prompt in the variant folder as `AGENTS.md` (codex) or `CLAUDE.md`, with an empty `scenario/`. The agent's own built-in instructions wrap it, so say so in the case's comments. |

The sim and the judge are always chat seats, so they cannot be `codex` or `opencode`. To avoid spending
one vendor's subscription, run the subject on one backend and the sim and judge on a cheap API one such as
`deepseek` (see [`BACKENDS.md`](BACKENDS.md)).

## 2. Scaffold it

```
uv run rehearse init PATH/rehearse --adapter chat|agent --prompt prompt.txt \
    --subject-seat codex --sim-seat deepseek:deepseek-chat --judge-seat deepseek:deepseek-chat
```

This writes a case that loads and runs on `mock` seats (or on the seats you name): `case.toml`, one
variant holding your prompt, one sim with a knowledge page, a one-question rubric and a `.gitignore` that
keeps run folders out of git except each `assessment.md`. It never overwrites an existing `case.toml`.

## 3. Fill it in

- **Variant** (`variants/`): the prompt under test, copied byte for byte. A new version is a new file, never
  an edit, so old runs stay explained. For `agent` it is a folder laid over the scenario copy.
- **Sims** (`sims/NAME.md`): one file per kind of person: frontmatter (`knowledge`, `opening`, `mood`,
  `escalation`, `stop`) and a body. `escalation` is one line per turn after the first and the last repeats,
  which is how you test pressure (a student who asks for the answer three ways). If a real failure was seen
  in the field, put the exact words in an escalation line.
- **Knowledge** (`knowledge/NAME.md`): the only facts the sim may use. Include what the person got wrong,
  and what they do not know. Never put the answer key here.
- **Rubric** (`rubric.md`): numbered questions with `Pass:` and `Fail:` lines a judge can decide from the
  transcript alone. Judging is off by default (`[judge] default = false`); pass `--judge`.
- **Checks** (`checks.py`, optional): code, for what a program can decide exactly. See [`CHECKS.md`](CHECKS.md).
- **Do not** tune a sim or rubric to move a number; change it in its own commit with a reason.

## 4. Check, then run

```
uv run rehearse suite CASE --dry-run          # lists the runs, the seats and a cost estimate; calls nothing
uv run rehearse suite CASE --yes --max-usd 1  # on mock seats this is free
```

Run on `mock` first: it catches broken personas, bad paths and missing keys before anything is spent.

## 5. Live runs: ask, then run in the background

Live runs spend someone's subscription or key, so **ask the owner first** and keep the first run small
(two sims, two repeats). A suite can take many minutes (an agent seat takes about a minute a turn), so run
it detached and carry on:

```
uv run rehearse suite CASE --sims a,b --repeats 2 --judge --max-usd 1 --background
uv run rehearse status CASE      # state, log path and last line of recent background runs
```

`--background` checks the case, then starts the run detached with `--yes` (so only use it after the owner
said yes). The log is `runs/.jobs/STAMP.log`; the exit code lands in `STAMP.exit`. When it is done:

```
uv run rehearse board CASE/runs      # k of n per check
```

then **read the transcripts** (`runs/*/transcript.md`): the board only says where to look.

**Writing `assessment.md` is the job of whoever ran the suite** (the agent or the person). Rehearse only
creates a stub that says `NOT YET ASSESSED`, and `rehearse board` lists every run still in that state. Replace
the stub with what you saw: the counts as k of n, the model in each seat, what the run did not test, any problem
with the sim or the setup (a sim that did not do its job voids the run), and any check that raised a false
alarm. Only then commit it.

## Testing one step (focused continuations)

To check one weak turn after a prompt change, do not rerun whole conversations. Cut the conversation
where it went wrong and see what a new prompt says next:

```
uv run rehearse extract RUN --at 3 --id compound_question --purpose "earlier, a two-part question" \
    --expect "asks exactly one question" --expect "does not state the answer"
uv run rehearse continue CASE --repeats 3 --judge --dry-run
```

`extract` writes `continuations/ID.md` from a saved run: every turn before subject turn 3, ending on the
person's turn, so the subject's reply is the step under test. The prefilled subject turns are not the
tested model's own words, so a reply is evidence about one step, not a whole conversation.

**Continuations need a chat subject.** An agent subject (codex, opencode) cannot run them yet. To test one
step of an agent case, make a second case file for the same prompt with a chat subject, such as
`case-chat.toml` with its own `runs_dir`, and run `continue` on that. Say in its comments that the model
differs from the agent case. Compare two prompt versions (`current` against `one-question`) on the *same*
model, and run each continuation several times: one reply is an anecdote.

## Common mistakes

- A `codex` or `opencode` seat in `[sims]` or `[judge]`: they are agent-only.
- `uv run python -m unittest` with no arguments finds 0 tests; use `discover -s tests`.
- Calling one run a result. One run is an anecdote; say "2 of 2", name the models, and say what was not tested.
- Committing a run folder. Only `assessment.md` is committed, and it must carry no email address or home path.
