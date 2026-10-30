# AGENTS.md

Rehearse tests an AI that talks with people by having another model play the person. The design is
in [`docs/DESIGN.md`](docs/DESIGN.md). Read it before changing anything. To make a new case, see [`docs/NEW-CASE.md`](docs/NEW-CASE.md)
(or the `new-rehearsal` skill in `.claude/skills`). `rehearse init` scaffolds one; `--background` plus
`rehearse status` runs a live suite detached.

Status: steps 1 to 3, 5, 6a, 6b and 6c built (core loop, checks, sim check, board, compare, claude, opencode, codex and http backends, agent adapter, cost, rubric and judge, focused continuations for chat subjects). See `docs/PLAN.md`.

## Rules for working here

- **Plan before building.** Propose a plan (`docs/PLAN.md`) and get the owner's approval before writing
  code. Build in small, tested steps.
- **Python 3.11+, standard library only.** Run everything with `uv run`. No bare `pip`.
- **Run the tests with `uv run python -m unittest discover -s tests`.**
- **Test with the `mock` backend.** Unit tests never call a live model.
- **Live model runs spend real money or quota.** Ask before any live run, keep it small, and
  report what it cost.
- **Keep the docs generic.** This is a standalone tool. Do not name the projects, events or
  organisations that use it in committed files. Private context, if any, goes in `HANDOFF.md`, which is
  gitignored.
- **Scrub before writing.** Raw CLI output can carry the account's email address and local paths.
  Never commit either.
- **Plain words** in docs and output. Define a term the first time it is used.
