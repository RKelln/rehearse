"""Compare two sets of saved runs: what moved, and whether the comparison is fair."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter

from .board import (N_MEANS, SIM_CHECK, Record, _judge_names, cell_for, exclusions, load_records, not_asked,
                    same_models, scrub, seat_groups, tally)

FOOTER = "Small counts move by chance. Rerun before believing a change of one or two."


def _set(values) -> list:
    return sorted(set(values), key=str)


def _hashes(records: list[Record], name: str, key: str) -> dict:
    """Hash of an input file, per key (sim name or case), as sorted lists.

    The hash of what was written to disk (`written_sha256`) is always recorded, so both sides use
    it; the source hash is only a fallback for a manifest that lacks it.
    """
    out: dict = {}
    for r in records:
        entry = r.manifest.get(name, {})
        h = entry.get("written_sha256") or entry.get("source_sha256")
        if h:
            out.setdefault(key_of(r, key), set()).add(h)
    return {k: sorted(v) for k, v in out.items()}


def key_of(r: Record, key: str):
    return (r.case, r.sim) if key == "sim" else (r.case, r.variant) if key == "variant" else r.case


def _models(groups: list) -> set:
    return {m for g in groups for m in (json.loads(g)[1] or [])}


def _alias_note(gb: list, ga: list) -> str:
    """If the models differ only because one side recorded a short alias (no digits, such as "sonnet")
    and the other the full id that contains it, say so. Empty text if that is not the case."""
    mb, ma = _models(gb), _models(ga)
    only_b, only_a = mb - ma, ma - mb
    if not only_b or not only_a:
        return ""
    for side, short, full in (("before", only_b, only_a), ("after", only_a, only_b)):
        aliases = sorted(s for s in short if not any(c.isdigit() for c in s) and any(s in f for f in full))
        if aliases and len(aliases) == len(short):
            return "model recorded as alias " + ", ".join(repr(a) for a in aliases) + f" in {side} runs"
    return ""


def warnings(before: list[Record], after: list[Record]) -> list[str]:
    """Reasons the two sets may not be fairly comparable."""
    w = []

    def differ(label, a, b):
        if a != b:
            w.append(f"{label} differ: before {a}, after {b}")

    (sb, mb), (sa, ma) = seat_groups(before), seat_groups(after)
    for label, gb, ga in (("subject seats or models", sb, sa),
                          ("sim seats or models (runs that used a sim)", mb, ma)):
        # Seats written differently that ran the same known models are the same seat.
        if sorted(gb) != sorted(ga) and not (gb and ga and same_models(gb + ga)):
            alias = _alias_note(gb, ga)
            if alias:
                w.append(f"{label}: {alias}")
            else:
                differ(label, sorted(gb), sorted(ga))
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
        kb, ka = {key_of(r, key) for r in before}, {key_of(r, key) for r in after}
        for k in sorted(kb & ka, key=str):
            what = f"the {label} for {' / '.join(k) if isinstance(k, tuple) else k}"
            if k not in hb or k not in ha:
                w.append(f"{what} is not recorded in " + " or ".join(
                    side for side, h in (("before", hb), ("after", ha)) if k not in h) + " runs")
            elif hb[k] != ha[k]:
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


def render(before: list[Record], after: list[Record], pairs: dict[str, str] | None = None) -> str:
    pairs = pairs or {}
    cells = list(dict.fromkeys((r.case, r.variant, r.sim) for r in before + after))
    pairs_back = {new: old for old, new in pairs.items()}
    rows, odd = [], []
    for case, variant, sim in cells:
        b = [r for r in before if (r.case, r.variant, r.sim) == (case, variant, sim)]
        a = [r for r in after if (r.case, r.variant, r.sim) == (case, variant, sim)]
        names = list(dict.fromkeys(
            [x["name"] for r in b + a for x in (r.checks or {}).get("results", [])] + [SIM_CHECK] + _judge_names(b + a)))
        shown = f"{pairs_back[variant]}\u2192{variant}" if variant in pairs_back else variant
        for name in names:
            if not_asked(b, name) and not_asked(a, name):
                continue
            tb, ta = tally(b, name), tally(a, name)
            if _same_denominator(tb, ta):
                change = _direction(tb, ta)
            else:
                change = "denominators differ"
                odd.append(f"{case} / {variant} / {sim} / {name}")
            rows.append(f"| {case} | {shown} | {sim} | {name} | {cell_for(b, name)} | {cell_for(a, name)} | {change} |")
    warns = warnings(before, after)
    warns += [f"variants paired by hand: {old}\u2192{new}" for old, new in pairs.items()]
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


def pair_variants(before: list[Record], pairs: dict[str, str]) -> list[Record]:
    """The before runs with the paired variants renamed, in memory only; nothing on disk changes."""
    have = {r.variant for r in before}
    for old in pairs:
        if old not in have:
            raise ValueError(f"--pair {old}={pairs[old]}: no before run has the variant {old!r} "
                             f"(the before runs have: {', '.join(sorted(have))})")
    return [dataclasses.replace(r, meta={**r.meta, "variant": pairs[r.variant]}) if r.variant in pairs else r
            for r in before]


def compare(before_spec: str, after_spec: str, pairs: dict[str, str] | None = None) -> str:
    """Compare two sets of runs. Each spec is a folder, or comma-separated folders.

    `pairs` maps a before variant to the after variant it should be compared with.
    """
    pairs = pairs or {}
    before, after = load_records(before_spec), load_records(after_spec)
    return scrub(render(pair_variants(before, pairs), after, pairs))
