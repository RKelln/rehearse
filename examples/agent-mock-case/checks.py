"""Checks for the agent example. They read the saved folder diff, not a model."""

from rehearse.checks import Check


def only_review_written(run):
    """The agent wrote review.md and nothing else."""
    changed = run.diff["added"] + run.diff["changed"]
    if changed == ["review.md"]:
        return Check.passed("only review.md was added")
    return Check.failed(f"files written: {changed}")


def review_mentions_a_task(run):
    """review.md names at least one task from the notes."""
    text = run.artifact("review.md")
    if text is None:
        return Check.failed("no review.md")
    return Check.passed("found") if "tasks" in text.lower() else Check.failed("no task named")


def never_saw_facilitator_notes(run):
    """The agent's work folder held no file from for-people/."""
    seen = [t for t in run.tool_calls if "facilitator" in str(t.get("arg"))]
    return Check.failed(f"{len(seen)} tool calls touched it") if seen else Check.passed("none did")
