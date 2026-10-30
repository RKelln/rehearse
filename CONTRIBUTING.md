# Contributing

Thanks for helping. Please read [`docs/DESIGN.md`](docs/DESIGN.md) first; it explains what rehearse
is for and what it deliberately is not.

## Ground rules

- **Python 3.11+, standard library only.** No new dependencies. Run everything with `uv run`.
- **Tests use the `mock` backend.** A unit test must never call a live model or the network.
- **Small steps.** For anything bigger than a fix, open an issue describing the change before you
  write it. The roadmap is in [`docs/PLAN.md`](docs/PLAN.md).
- **Plain words** in docs and output. Define a term the first time you use it.
- **No private data.** Do not commit real names, email addresses, local paths, keys, or saved runs
  from real conversations.

## Running the tests

```
uv run python -m unittest discover -s tests
```

CI runs the same command on Python 3.11, 3.12 and 3.13.

## Adding a backend or a check

- A backend lives in `src/rehearse/backends/` and is explained in [`docs/BACKENDS.md`](docs/BACKENDS.md).
  Nothing outside a backend's own file may assume one vendor.
- Checks are explained in [`docs/CHECKS.md`](docs/CHECKS.md).

## Pull requests

Keep each one focused, add or update tests, and say in the description what you ran.
