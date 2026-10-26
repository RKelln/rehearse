"""Compare two sets of saved runs: what moved, and whether the comparison is fair."""

from __future__ import annotations

import json
from collections import Counter

from .board import N_MEANS, SIM_CHECK, Record, _judge_names, cell_text, exclusions, load_records, scrub, tally

FOOTER = "Small counts move by chance. Rerun before believing a change of one or two."


def _set(values) -> list:
    return sorted(set(values), key=str)


def _hashes(records: list[Record], name: str, key: str) -> dict:
    """source hash of an input file, per key (sim name or case), as sorted lists."""
    out: dict = {}
    for r in records:
        h = r.manifest.get(name, {}).get("source_sha256")
        if h:
            out.setdefault(key_of(r, key), set()).add(h)
    return {k: sorted(v) for k, v in out.items()}


def key_of(r: Record, key: str):
    return (r.case, r.sim) if key == "sim" else (r.case, r.variant) if key == "variant" else r.case


def warnings(before: list[Record], after: list[Record]) -> list[str]:
    """Reasons the two sets may not be fairly comparable."""
    w = []

    def differ(label, a, b):
        if a != b:
            w.append(f"{label} differ: before {a}, after {b}")

    differ("seats or models", _set((json.dumps(r.meta.get("seats")), json.dumps(r.meta.get("models"))) for r in before),
           _set((json.dumps(r.meta.get("seats")), json.dumps(r.meta.get("models"))) for r in after))
    differ("max_turns", _set(r.meta.get("max_turns") for r in before), _set(r.meta.get("max_turns") for r in after))
    nb = Counter((r.case, r.variant, r.sim) for r in before)
    na = Counter((r.case, r.variant, r.sim) for r in after)
    for cell in sorted(set(nb) | set(na)):
        if nb[cell] != na[cell]:
            w.append(f"number of runs differs for {' / '.join(cell)}: before {nb[cell]}, after {na[cell]}")
    differ("checks files (checks_sha256)",
           _set((r.case, (r.checks or {}).get("checks_sha256")) for r in before),
           _set((r.case, (r.checks or {}).get("checks_sha256")) for r in after))
    for name, key, label in (("persona.md", "sim", "persona"), ("knowledge.md", "sim", "knowledge page"),
                             ("frame.md", "case", "sim frame")):
        hb, ha = _hashes(before, name, key), _hashes(after, name, key)
        for k in sorted(set(hb) | set(ha), key=str):
            if hb.get(k) != ha.get(k):
                w.append(f"the {label} changed for {' / '.join(k) if isinstance(k, tuple) else k}")
    hb, ha = _hashes(before, "variant.md", "variant"), _hashes(after, "variant.md", "variant")
    changed = [k for k in sorted(set(hb) & set(ha)) if hb[k] != ha[k]]
    if len(changed) > 1:
        w.append("more than one variant changed: " + ", ".join(" / ".join(k) for k in changed))
    only = sorted(set(hb) ^ set(ha))
    if only:
        w.append("variants in only one set: " + ", ".join(" / ".join(k) for k in only))
    return w


def _same_denominator(b: dict, a: dict) -> bool:
    return b["n"] == a["n"] and exclusions(b) == exclusions(a)


def _direction(b: dict, a: dict) -> str:
    """up, down or same, by passes and then partials. Only meaningful when denominators match."""
    if b["n"] == 0:
        return "n/a"
    x, y = (b["k"], b["partial"]), (a["k"], a["partial"])
    return "up" if y > x else "down" if y < x else "same"


def render(before: list[Record], after: list[Record]) -> str:
    cells = list(dict.fromkeys((r.case, r.variant, r.sim) for r in before + after))
    rows, odd = [], []
    for case, variant, sim in cells:
        b = [r for r in before if (r.case, r.variant, r.sim) == (case, variant, sim)]
        a = [r for r in after if (r.case, r.variant, r.sim) == (case, variant, sim)]
        names = list(dict.fromkeys(
            [x["name"] for r in b + a for x in (r.checks or {}).get("results", [])] + [SIM_CHECK] + _judge_names(b + a)))
        for name in names:
            tb, ta = tally(b, name), tally(a, name)
            if _same_denominator(tb, ta):
                change = _direction(tb, ta)
            else:
                change = "denominators differ"
                odd.append(f"{case} / {variant} / {sim} / {name}")
            rows.append(f"| {case} | {variant} | {sim} | {name} | {cell_text(tb)} | {cell_text(ta)} | {change} |")
    warns = warnings(before, after)
    if odd:
        warns.append("the number of runs that counted, or the runs left out, differs in: " + "; ".join(odd))
    lines = ["# Compare", ""]
    if warns:
        lines += ["**Warnings: this may not be a fair comparison.**", ""] + [f"- {x}" for x in warns] + [""]
    if before + after and all(r.meta.get("mock") for r in before + after):
        lines += ["**PLUMBING ONLY, NOT A RESULT: every seat was the mock backend.**", ""]
    lines += [f"Before: {len(before)} runs. After: {len(after)} runs.", "", N_MEANS, "",
              "| case | variant | sim | check | before | after | change |", "|---|---|---|---|---|---|---|"]
    lines += rows + ["", FOOTER, ""]
    return "\n".join(lines)


def compare(before_spec: str, after_spec: str) -> str:
    """Compare two sets of runs. Each spec is a folder, or comma-separated folders."""
    return scrub(render(load_records(before_spec), load_records(after_spec)))
