# rehearse

Test an AI that talks with people, by having another model play the person.

You give it the thing under test (a prompt, an agent with a skill, or your own program), a few
simulated people, and checks that say what "good" means. It runs the conversations, keeps every word,
and gives you a scoreboard you can compare from one change to the next.

- **Standard library only.** Python 3.11+, no dependencies.
- **Any model.** Works with the `claude`, `codex` and `opencode` programs and any OpenAI-compatible
  endpoint (OpenAI, OpenRouter, DeepSeek, a local server).
- **Honest by design.** Every cell is a count ("2 of 3 runs"), never a percentage. A simulated person
  that stops behaving like a person voids its run. Runs on the scripted `mock` backend are labelled
  "plumbing only, not a result".

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/RKelln/rehearse && cd rehearse
uv run rehearse suite examples/mock-case --dry-run   # list the runs, run nothing
uv run rehearse suite examples/mock-case             # run them on the mock backend (free)
uv run rehearse board examples/mock-case/runs        # the scoreboard
```

The board shows one row per variant and simulated person, one column per check:

```
| variant  | sim  | short_replies | asked_a_question | never_says_banned_phrase | sim check |
|----------|------|---------------|------------------|--------------------------|-----------|
| baseline | dana | 1 of 1        | 1 of 1           | 1 of 1                   | 1 of 1    |
| cheerful | dana | 1 of 1        | 1 of 1           | 1 of 1                   | 1 of 1    |
```

A command that uses a live model shows a cost estimate and asks first. `--yes` skips the question and
`--max-usd X` caps the spend.

## Commands

```
rehearse init PATH            scaffold a new case
rehearse run CASE             run one variant with one simulated person
rehearse suite CASE           run every variant against every simulated person
rehearse check RUNS           rerun the checks on saved runs (free)
rehearse judge RUNS           score saved runs against the rubric with a model
rehearse board RUNS           a scoreboard: "k of n" per check
rehearse compare BEFORE AFTER what moved between two sets of runs
rehearse continue CASE        focused continuations: prefilled transcript, one reply each
rehearse extract RUN          write a focused continuation from a saved run
rehearse status CASE          progress of a run started with --background
```

Run `rehearse COMMAND --help` for the options.

## Where to go next

| I want to... | Read |
|---|---|
| test my own prompt or agent | [`docs/NEW-CASE.md`](docs/NEW-CASE.md) |
| understand how and why it works | [`docs/DESIGN.md`](docs/DESIGN.md) |
| write checks | [`docs/CHECKS.md`](docs/CHECKS.md) |
| pick or configure a backend | [`docs/BACKENDS.md`](docs/BACKENDS.md) |
| see what is built and what is next | [`docs/PLAN.md`](docs/PLAN.md) |
| work on rehearse itself | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

The [`examples/`](examples) folder has a runnable case for each kind of subject.

## Tests

```
uv run python -m unittest discover -s tests
```

The tests never call a live model.

## Licence

[MIT](LICENSE).
