"""The agent subject's work folder: a fresh copy of the scenario, and a record of what changed.

Each run copies the scenario (minus the files the case excludes) into a new temporary folder
outside the case folder, so the agent cannot walk up into the case's own files. The folder is
hashed before and after, and the difference is saved with the run. Symbolic links are never
followed or copied.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from .backends.keys import secret_values
from .case import Case, CaseError

PATCH_LIMIT = 1_000_000      # bytes; larger or binary files get no patch


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scan(root: Path) -> dict[str, dict]:
    """{relative path: {"sha", "mode", "link"}} for every file and symbolic link under `root`.

    Links are not followed: a link is recorded with its target text and no content hash, and a
    link to a folder is recorded as a link too.
    """
    out = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        base = Path(folder)
        for name in list(dirs):
            if (base / name).is_symlink():
                dirs.remove(name)
                files.append(name)
        for name in files:
            p = base / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                out[rel] = {"sha": "symlink:" + os.readlink(p), "mode": 0, "link": os.readlink(p)}
            elif p.is_file():
                out[rel] = {"sha": sha256(p.read_bytes()), "mode": p.stat().st_mode & 0o777, "link": None}
    return dict(sorted(out.items()))


def hash_tree(root: Path) -> dict[str, str]:
    """{relative path: sha256} of every file under `root` (a link's value is "symlink:" and its target)."""
    return {rel: e["sha"] for rel, e in scan(root).items()}


def _glob(pattern: str) -> str:
    """A glob as regular expression text: `*` stays inside a folder, `**` crosses folders."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return out


def _compile(pattern: str) -> tuple[re.Pattern, bool]:
    """(regex, folders_only). Like .gitignore: no "/" means any depth; a trailing "/" means folders only."""
    p = pattern.strip()
    while p.startswith("./"):
        p = p[2:]
    folders_only = p.endswith("/")
    p = p.strip("/")
    anchored = "/" in p
    return re.compile(("" if anchored else "(?:.*/)?") + _glob(p) + r"\Z"), folders_only


def matches(rel: str, patterns: list[str]) -> set[int]:
    """Indexes of the patterns that match this file, or a folder above it."""
    parts = rel.split("/")
    hit = set()
    for i, pattern in enumerate(patterns):
        rx, folders_only = _compile(pattern)
        for n in range(1, len(parts) + 1):
            if folders_only and n == len(parts):
                continue                      # the last part is the file itself
            if rx.match("/".join(parts[:n])):
                hit.add(i)
                break
    return hit


def excluded(rel: str, patterns: list[str]) -> bool:
    """Does any pattern match this file, or a folder above it?"""
    return bool(matches(rel, patterns))


def diff_hashes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(set(after) - set(before)),
            "changed": sorted(k for k in after if k in before and after[k] != before[k]),
            "removed": sorted(set(before) - set(after))}


def _inside(path: Path, folder: Path) -> bool:
    path, folder = path.resolve(), folder.resolve()
    return path == folder or folder in path.parents


class Workspace:
    """A work copy for one run."""

    def __init__(self, path: Path):
        self.path = path
        self.scenario: dict[str, str] = {}     # hashes of the scenario files that were copied
        self.overlay: dict[str, str] = {}      # hashes of the variant's files laid over them
        self.skipped_symlinks: list[str] = []
        self.unmatched_excludes: list[str] = []
        self.warnings: list[str] = []
        self.before: dict[str, dict] = {}
        self._before_text: dict[str, str] = {}

    @classmethod
    def create(cls, case: Case, variant: str) -> "Workspace":
        """Copy the scenario and lay the variant's folder over it."""
        ws = cls(Path(tempfile.mkdtemp(prefix="rehearse-work-")))
        try:
            for guarded in (case.root, case.scenario_dir):
                if guarded and _inside(ws.path, guarded):
                    raise CaseError(f"the temporary work folder ({ws.path}) is inside the case folder or the "
                                    "scenario folder. Set TMPDIR to a folder outside the case")
            if case.scenario_dir:
                ws._copy(case.scenario_dir, case.scenario_exclude, record=ws.scenario)
            ws.scenario = hash_tree(ws.path)
            overlay = case.variants.get(variant)
            if overlay:
                ws._copy(overlay, [], record=ws.overlay)
            ws.before = scan(ws.path)
            for rel, entry in ws.before.items():
                p = ws.path / rel
                if entry["link"] is None and p.stat().st_size <= PATCH_LIMIT:
                    try:
                        ws._before_text[rel] = p.read_text()
                    except UnicodeDecodeError:
                        pass
        except BaseException:
            ws.cleanup()
            raise
        return ws

    def _copy(self, source: Path, exclude: list[str], record: dict) -> None:
        """Copy regular files from `source`. Links are skipped (and listed), never followed."""
        used: set[int] = set()
        for folder, dirs, files in os.walk(source, followlinks=False):
            base = Path(folder)
            for name in sorted(dirs):
                if (base / name).is_symlink():
                    files.append(name)
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
            for name in sorted(files):
                src = base / name
                rel = src.relative_to(source).as_posix()
                hit = matches(rel, exclude)
                used |= hit
                if hit:
                    continue
                if src.is_symlink():
                    self.skipped_symlinks.append(rel)
                    self.warnings.append(f"skipped a symbolic link in the scenario: {rel}")
                    continue
                if not _inside(src, source):
                    raise CaseError(f"{rel} resolves to a place outside {source.name}; refusing to copy it")
                dest = self.path / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                record[rel] = sha256(dest.read_bytes())
        for i, pattern in enumerate(exclude):
            if i not in used:
                self.unmatched_excludes.append(pattern)
                self.warnings.append(f"the exclude pattern {pattern!r} matched nothing")
        for w in self.warnings:
            print(f"warning: {w}")
        self.warnings = []

    def guard(self, case: Case) -> None:
        """Refuse to run if the work copy holds anything the sims were given.

        That is every sim's persona file and knowledge page, and the sim frame: a file is a leak if its
        content is identical to one of them, or if its name or path matches theirs.
        """
        secret_hashes, secret_paths = {}, set()

        def note(label, path, text):
            secret_hashes[sha256(text.encode())] = label
            if path is not None:
                secret_paths.add(Path(path).name)
                try:
                    secret_paths.add(Path(path).resolve().relative_to(case.root).as_posix())
                except ValueError:
                    pass

        for sim in case.sims:
            persona = case.persona(sim)
            note("persona file", persona.path, persona.raw)
            if persona.knowledge_path:
                note("knowledge page", persona.knowledge_path, persona.knowledge_raw)
        note("sim frame", case.frame_path, case.frame_text())
        leaks = [f"{rel} ({secret_hashes[e['sha']]})" for rel, e in self.before.items() if e["sha"] in secret_hashes]
        leaks += [f"{rel} (same name as a persona, knowledge or frame file)" for rel in self.before
                  if (rel in secret_paths or rel.rsplit("/", 1)[-1] in secret_paths)
                  and not any(x.startswith(rel + " ") for x in leaks)]
        if leaks:
            raise CaseError("the subject's work folder would contain files the sim was given: "
                            + ", ".join(leaks) + ". Add them to [scenario] exclude.")

    def write_artifacts(self, run_path: Path, scrub_text, write_json) -> dict:
        """Save the diff, the added and changed files, and a patch for text files that were added or changed.

        Files are copied byte for byte. A text file holding an email address, the home folder path or a
        key is saved scrubbed instead (marked `scrubbed`); a binary file holding the home folder path or a
        key is not saved (marked `withheld`). Links are recorded and never followed.
        """
        after = scan(self.path)
        art = run_path / "artifacts"
        art.mkdir()
        flags: dict[str, dict] = {}
        added = sorted(set(after) - set(self.before))
        removed = sorted(set(self.before) - set(after))
        changed = []
        for rel in sorted(set(after) & set(self.before)):
            old, new = self.before[rel], after[rel]
            if old["sha"] != new["sha"]:
                changed.append(rel)
            elif old["mode"] != new["mode"]:
                changed.append(rel)
                flags.setdefault(rel, {})["mode"] = f"{old['mode']:04o} -> {new['mode']:04o}"
        patch = []
        private = [v.encode() for v in secret_values()] + [str(Path.home()).encode()]
        for rel in added + changed:
            entry = after[rel]
            if entry["link"] is not None:
                flags.setdefault(rel, {})["symlink_to"] = entry["link"]
                continue
            data = (self.path / rel).read_bytes()
            dest = art / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                text = data.decode()
            except UnicodeDecodeError:
                if any(len(p) > 1 and p in data for p in private):
                    flags.setdefault(rel, {})["withheld"] = "contains private data"
                else:
                    dest.write_bytes(data)
                continue
            clean = scrub_text(text)
            if clean != text:
                flags.setdefault(rel, {})["scrubbed"] = True
            if clean == text:
                dest.write_bytes(data)           # byte for byte
            else:
                dest.write_text(clean)
            if len(data) <= PATCH_LIMIT:
                old_text = self._before_text.get(rel, "") if rel in changed else ""
                patch += difflib.unified_diff(
                    old_text.splitlines(keepends=True), text.splitlines(keepends=True),
                    f"a/{rel}" if rel in changed else "/dev/null", f"b/{rel}")

        def listed(paths):
            return [{"path": r, **flags[r]} if r in flags else r for r in paths]

        diff = {"added": listed(added), "changed": listed(changed), "removed": removed}
        write_json(art / "diff.json", diff)
        (art / "diff.patch").write_text(scrub_text("".join(patch)))
        return diff

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
