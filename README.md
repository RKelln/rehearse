# rehearse

Test an AI that talks with people, by having another model play the person.

Give it the thing under test (a prompt, an agent with a skill, or your own program), a few simulated
people, and checks that say what "good" means. It runs the conversations, keeps every word, and
gives you a scoreboard you can compare from one change to the next.

Status: steps 1 to 3, 5, 6a, 6b and 6c (the codex backend; focused continuations for chat subjects) of [`docs/PLAN.md`](docs/PLAN.md) are built: the core loop, checks, the sim
check, the board and compare commands, the rubric and judge, and real backends (the `claude`, `opencode` and `codex` programs and any
OpenAI-compatible endpoint), including agents working in a copy of a scenario folder. Tests use scripted
stand-ins only; they never call a live model. The design is in [`docs/DESIGN.md`](docs/DESIGN.md) and the
backends are explained in [`docs/BACKENDS.md`](docs/BACKENDS.md).

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). No other dependencies.

```
uv run rehearse suite examples/mock-case --dry-run     # list the runs, run nothing
uv run rehearse suite examples/mock-case               # run them on the mock backend
uv run rehearse suite examples/agent-mock-case         # an agent subject, on the mock backend
uv run rehearse continue CASE --dry-run                 # focused continuations: prefilled transcripts, one reply each
uv run rehearse check examples/mock-case/runs          # rerun the checks on saved runs (free)
uv run rehearse judge examples/mock-case/runs --dry-run   # judge saved runs with the rubric (a model call; --dry-run lists only)
uv run rehearse board examples/mock-case/runs          # a scoreboard: "k of n" per check
uv run rehearse compare BEFORE_RUNS AFTER_RUNS         # what moved between two sets of runs
uv run python -m unittest discover -s tests            # the tests
```

A live command (any seat that is not `mock`) shows an estimate and asks first; `--yes` skips the
question and `--max-usd X` caps the spend. `examples/http-case` is a tiny live case.

Runs are saved under `examples/mock-case/runs/`. Each run gets a `checks.json`: the case's own checks
(`checks.py`, see the example) and the sim check, which says whether the simulated person behaved
like a person. A failed sim check voids that run, and the board leaves it out of the counts.
Writing checks is explained in [`docs/CHECKS.md`](docs/CHECKS.md). Mock runs test the tool, not any model.

MIT licence.
