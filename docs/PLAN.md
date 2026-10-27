# Plan

Status: steps 1 to 3 and 5 built; step 4 built except its live agent run; step 6a built (chat continuations, context, prompt limit; the agent fork, codex backend and interviewer live runs remain). Later steps need approval before they start.

This plan builds the tool described in [`DESIGN.md`](DESIGN.md) in small steps. Each step ends with
passing tests on the `mock` backend (a scripted stand-in for a model, so no model is called and
nothing is spent). Live runs come only where a step says so, and only with the owner's go-ahead.

---

## Any model

Rehearse works with any model. Claude is built first because the first case needs it, but nothing
outside a backend's own file may assume Claude:

- Every seat goes through one interface. A chat seat takes a system prompt and messages and returns a
  `SeatResult` (text, cost if known, tokens if known, raw output). An agent seat adds: start in a
  folder, resume a session, and optionally fork one. Each backend says which of these it supports.
- When a backend cannot do something, the tool falls back or says so plainly. No session resume:
  replay the transcript. No fork: replay up to the checkpoint. No cost in the output: record cost as
  unknown, never as zero.
- Frames, personas, rubrics and the judge prompt are plain text with no vendor-specific tags.
- A second backend is built in the same step as the first, so the interface is tested against two
  vendors from the start: the OpenAI-compatible HTTP backend. It covers OpenAI, OpenRouter and
  local models served by tools such as Ollama, llama.cpp or vLLM.
- The mock backend implements the full interface, so tests exercise each fallback.

---

## What already exists, and what we take from it

Three harnesses grew separately. We read all three, and surveyed open-source tools.

**Open-source tools.** promptfoo, DeepEval, tau2-bench, OpenEvals, Inspect AI and PyRIT all have some
form of simulated user. None fits: each needs a framework (Node, LangChain, Docker, or a database),
and each expects the thing under test to be a function in the same process, not a coding agent in
a folder whose session is resumed each turn. The loop itself is small. **We build our own and
borrow ideas:**

| Idea | From | Where it lands |
|---|---|---|
| Scripted first turns, then the model takes over | OpenEvals | persona `opening`, continuations |
| "All k passed" next to "k of n passed" | tau2-bench (pass^k) | `board` |
| Judge answers per question with a quoted piece of evidence, yes or no, not a 1-10 score | several | judge output |
| A sim stop word the subject never sees | tau2-bench | persona `stop` |
| Variants × personas × repeats as one grid | promptfoo | `suite` |

**The tutor harness** (a separate CC0 project, the most mature of the three). Its backends and
the bones of its runner are generic. We lift these, with credit in the file header:

- `backends.py`: error types (a quota error stops the whole suite), `scrub()`, the HTTP backend with
  retries and rate pacing, CLI presets, the human backend, and the `backend[:model]` seat parser.
  About 300 lines. Changed: a call returns a result (text, cost, tokens, session id, raw output),
  not a bare string; `scrub()` also removes local home paths, and runs on everything written to disk.
- From its runner: the frontmatter parser, the sim turn (role mapping, label stripping, retry on an
  empty reply), the thread pool with stop-on-quota, the git hash in every record, the JSON salvage
  for judge output, and the "plumbing only, not a result" banner on mock runs.
- Its test doubles: a recording backend that keeps every request it sees, and a sentinel value used
  to prove a seat never saw something.
- Its iteration rules, already the design's principles.

Not lifted: its scoring, fixtures and transcript format, which are specific to tutoring.

**The weekly-review harness** (first case to move over). It confirmed the `claude -p` recipe for an
isolated sim and the cost reading. It also showed four problems the tool must fix:

1. The subject's copy of the scenario folder still held the sim's knowledge page. Only an
   instruction kept the agent from reading it. The design now says the scenario lists files to
   leave out of the subject's copy.
2. The transcript was rebuilt by globbing the CLI's private session store. We read the CLI's
   streamed JSON output instead (`--output-format stream-json`), which carries every tool call and
   the cost.
3. No folder diff, so "only review.md was written" could not be checked. The agent adapter takes one.
4. A non-zero exit from one call aborted the run with nothing saved. Every call's raw output is
   written before it is parsed, and errors are recorded as a stop reason.

**The interviewer harness.** Strong on isolation by procedure and on focused continuations (one
prefilled transcript, one subject reply, checked against plain-language expectations). Two things
the design lacked: a **subject-side stop** (the interviewer ends the session with a debrief) and
**per-case expectations** that only a judge or a person can check. Both are added to the design.

---

## Answers to the design's open questions (proposals)

1. **Name.** Keep `rehearse`. It is short, a plain word, and says what it does.
2. **Prior art.** Lift the tutor harness's generic code (above) and rewrite the rest. Its CC0
   licence allows that under MIT.
3. **Judge by default.** Off. A case can turn it on in `case.toml`, and `--judge` turns it on for
   one command. The code part of the sim check always runs (see step 2).
4. **Where runs live.** `runs/` next to `case.toml`, changeable with `runs_dir` in the case. The
   case decides what to commit.
5. **Concurrency.** `--jobs 1` by default. Subscription CLIs share one rate limit, and a quota error
   midway wastes the runs in flight. HTTP backends keep the lifted per-endpoint pacing.
6. **Parsing artifacts.** Keep parsing loose and give checks helpers that tolerate differences:
   find a section by heading text, read a Markdown table by column name in any order, pull the
   weekday out of "Tue 27 (evening)". Do not change the subject's instructions to make testing
   easier: that changes the thing under test.

---

## Code layout

```
pyproject.toml          no dependencies; entry point `rehearse`
src/rehearse/
  cli.py                commands
  case.py               case.toml, personas, variants, scenario manifest
  frontmatter.py
  backends/             base (SeatResult), mock, claude, http, cli presets, human
  adapters.py           chat, agent, python
  loop.py               one run: turns, stop rules
  runs.py               run folders, scrub, snapshots with hashes
  checks.py             Check, the Run view, parsing helpers
  simcheck.py
  judge.py
  board.py              board and compare
  cost.py               estimates and budget caps
  importers/
  frames/sim.md
tests/                  unittest, run with `uv run python -m unittest`
examples/mock-case/     a tiny case that runs end to end on the mock backend
```

---

## Steps

Each step: build, test, review, then the owner sees a short report. One step at a time.

### Step 1. Core loop on the mock backend

- `case.toml` loader, persona files with frontmatter, the sim frame.
- `SeatResult` and the `mock` backend: scripted replies, records every request.
- The `chat` adapter and the loop: opening line, turns, stop on the sim's stop word, a subject stop
  pattern, the turn cap, or a backend error.
- Run folders exactly as the design lays out, never overwritten, everything scrubbed.
- `rehearse run` and `rehearse suite --dry-run`.
- Tests: the turn order; each stop rule; isolation (a sentinel in the subject's instructions never
  reaches the sim's requests, and one in the persona never reaches the subject's); no run folder is
  overwritten; an email address and a home path are scrubbed from every file written.

### Step 2. Checks, sim check, board, compare

- `checks.py` loader, the `Run` view (transcript, subject turns, tool calls, artifacts, folder diff,
  scenario files), the parsing helpers, `rehearse check`.
- The code part of the sim check: empty or labelled sim replies, stage directions, stop word on the
  first turn, and numbers or dates in the sim's replies that are on neither its knowledge page nor
  in the subject's earlier turns. A failed sim check voids that run's other checks on the board.
- `rehearse board` ("k of n", "all k", models per seat, a mock banner) and `rehearse compare` (warns
  when seats, turn caps or repeats differ between the two sets).

### Step 3. Claude and HTTP backends, agent adapter, cost

- `claude` backend: sim seat (no tools, no settings, empty folder, system prompt) and chat seat.
  Streamed JSON for tool calls; `total_cost_usd` is cumulative per session, so per-turn cost is the
  difference between turns.
- `http` backend (OpenAI-compatible, keys from the environment) for chat and sim seats, with the
  lifted retries and pacing; tokens read from `usage`, cost recorded as unknown unless the case gives
  prices.
- `agent` adapter, written against the agent-seat interface, not against Claude: a fresh copy of the scenario minus `exclude`, variant files laid over it,
  allowed tools on the command line, session resumed each turn, folder diff before and after.
- `cost.py`: estimate from past runs of the same case; `--max-usd` stops new runs once reached and
  is passed to each call as `--max-budget-usd`; live commands show the estimate and ask, unless
  `--yes`.
- Tests use recorded CLI output (scrubbed) played back, not live calls.
- **One live smoke test**, with the owner's go: one sim turn and one subject turn on the cheapest
  Claude model, to confirm the recorded output still matches the real CLI. Expected cost under $0.05.
  The HTTP backend gets the same smoke test only if the owner names an endpoint (a local model costs
  nothing).

### Step 4. First case: the weekly review

The case lives in the project that owns it, not in this repo.

- `case.toml`, the five moods as five persona files sharing one knowledge page, the knowledge page
  (without the participants' tips), the scenario manifest leaving out the knowledge page, and
  `checks.py` with the answer key's three code checks: quoted items exist, planned minutes fit the
  free days, only `review.md` was written.
- `rehearse import` for the three existing run folders. Their transcripts come from `transcript.md`;
  missing fields are marked missing, not guessed. One of them was played by a person, not a sim.
- `rehearse check` on the imported runs, compared with their hand-written assessments.
- **One live run**, with the owner's go: the cooperative mood, compared against the existing
  cooperative run. Expected cost about $0.55.

Target: done well before the second delivery of the workshop that uses it.

### Step 5. Judge and rubric

- Rubric Markdown, judge prompt, JSON parsing (one verdict and one quote per question), the judge
  part of the sim check, `rehearse judge`, judge results on the board marked "directional".
- Live test only with the owner's go, on saved runs (the judge alone, one or two runs).

### Step 6. Focused continuations and the interviewer case

- `chat`: `continuations/*.md`, each a prefilled transcript, a purpose, and expectations that become
  rubric questions. The purpose and expectations never reach the subject.
- `agent`: checkpoint after turn N, then fork (`claude -p --resume ID --fork-session`).
- A `codex` agent backend (`codex exec`, which is installed here), so the agent adapter runs on a
  second vendor. Its resume and fork support is checked first; where it is missing, the fallbacks apply.
- Port the interviewer case and its focused suite; import its earlier runs.

### Step 7. The rest

- `python` adapter; `gemini` and other CLI backends; `human` sim seat.
- Moving the tutor project over, only once rehearse does everything its own eval does.

---

## How the work is done

The building is done by agents, with the owner approving each step:

- An **implementer** agent builds one step from this plan, with tests, and reports what it built and
  what it ran.
- A **reviewer** agent reads the step's changes against the design and this plan: isolation,
  scrubbing, run folders never overwritten, no dependencies, plain words. It reports problems;
  it does not fix them.
- The coordinating session checks the review, sends fixes back, runs the tests itself, and reports to
  the owner. Nothing is committed unless the owner asks.
