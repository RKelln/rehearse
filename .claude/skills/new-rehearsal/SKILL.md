---
name: new-rehearsal
description: Set up and run a new rehearse case (a simulated-person test of a prompt or agent). Use when asked to evaluate, test, rehearse or re-run an eval of a prompt, tutor, interviewer or agent skill with rehearse.
---

# New rehearsal

Follow `docs/NEW-CASE.md`; this is the checklist.

1. Read `docs/DESIGN.md` (short) and `AGENTS.md`. Find any existing case for the project first
   (`grep -ril` for the project name, look for a `rehearse/` folder, read `HANDOFF.md` if present). If
   there is one, add a variant or sims to it instead of starting another.
2. Get the **exact prompt** under test from the source of truth (the shipped file, not a paraphrase) and
   copy it into a new variant file. Confirm with `diff` that it is identical.
3. Find any earlier evals or known failures in the project (personas, notes, changelog). Reuse their
   wording word for word so results are comparable, and put field-reported attacks in as sim escalations.
4. Choose seats: codex and opencode can only be the subject (agent). Sim and judge need a chat backend.
   If the owner wants to avoid a vendor's usage, use a cheap API backend for sim and judge.
5. `uv run rehearse init CASE_DIR ...`, fill in sims, knowledge and rubric, then
   `uv run rehearse suite CASE_DIR --dry-run`. Run it once on `mock` seats.
6. Show the owner the plan (seats, sims, runs, estimate). **Wait for a yes before any live run.**
7. Run live with `--background --max-usd N`, keep working, poll with `rehearse status CASE_DIR`. Do not
   start a second copy of a run that is going.
8. Read every transcript. Report the counts as "k of n" with the models in each seat, what the run did not
   test, and any problem with the sim or the setup. Never call one run a result.
9. Commit only the case folder, by path. Scrub email addresses and home paths first.
