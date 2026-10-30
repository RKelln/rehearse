# Security

## Reporting a problem

Please report security issues privately through GitHub's "Report a vulnerability" button on the
repository's Security tab, not in a public issue.

## Things to know when using rehearse

- **Saved runs hold whole conversations.** rehearse removes email addresses and your home folder path
  from what it writes, but that is a safety net, not a guarantee. Read a `runs/` folder before you
  commit or share it. A scaffolded case's `.gitignore` keeps runs out of git except each `assessment.md`.
- **Keys.** API keys are read from the environment or `~/.config/rehearse/keys`. They are never written
  into a case or a run. Keep that file private.
- **Agents run real programs.** An agent subject works in a copy of its scenario folder, but it can
  still run commands your account can run. Only test agents and skills you trust.
- **Checks are Python.** A case's `checks.py` is executed. Only run cases you have read.
