"""API keys: read from the environment or a private keys file. Never printed, never saved.

Parts adapted from an earlier CC0-licensed harness by the same author.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import SetupError

_seen: set[str] = set()
_COMMON = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")


def keys_file() -> Path:
    return Path.home() / ".config" / "rehearse" / "keys"


def read_key(name: str) -> str:
    """The key called `name`: the environment first, then ~/.config/rehearse/keys.

    The keys file holds NAME=value lines (# starts a comment). It must not be readable by
    anyone else: `chmod 600 ~/.config/rehearse/keys`. Returns "" if there is no such key.
    Every key read is remembered so that `scrub_keys` can remove it from anything written.
    """
    if not name:
        return ""
    value = os.environ.get(name, "")
    path = keys_file()
    if not value and path.exists():
        if path.stat().st_mode & 0o077:
            raise SetupError(f"{path} can be read by other users. Run: chmod 600 {path}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise SetupError(f"cannot read the keys file {path} ({type(e).__name__}). It should be plain text "
                             "with NAME=value lines") from None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                value = v.strip().strip('"').strip("'")
                break
    if value:
        _seen.add(value)
    return value


def scrub_keys(text: str) -> str:
    """Replace any key value read so far (or set in a known key variable) with a marker."""
    values = _seen | {os.environ[n] for n in _COMMON if os.environ.get(n)}
    for value in sorted(values, key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[key removed]")
    return text


def secret_values() -> set[str]:
    """Every key value read so far, or set in a known key variable (for checking files before they are saved)."""
    return {v for v in (_seen | {os.environ[n] for n in _COMMON if os.environ.get(n)}) if len(v) >= 4}
