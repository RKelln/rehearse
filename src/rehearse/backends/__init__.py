"""Backends fill seats. A seat is written `backend[:model]`, or as a table in case.toml.

    mock                         scripted replies, for testing the tool
    claude[:model]               the `claude` command line tool (chat seats and agent seats)
    http:MODEL@BASE_URL          any OpenAI-compatible endpoint
    openai:MODEL  openrouter:MODEL  deepseek[:MODEL]     http with the endpoint and key name filled in
"""

from __future__ import annotations

from .base import AgentSession, Backend, BackendError, Msg, QuotaError, SeatResult, SetupError
from .mock import MockBackend

PRESETS = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "rpm": 18},
    "deepseek": {"base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
}
KNOWN = ("mock", "claude", "http", *PRESETS)
TABLE_KEYS = ("backend", "model", "base_url", "key_env", "price_in_per_mtok", "price_out_per_mtok",
              "timeout", "rpm", "setting_sources", "max_tokens", "temperature", "max_budget_usd",
              "tools", "permission_mode")
_NUMBERS = ("price_in_per_mtok", "price_out_per_mtok", "timeout", "rpm", "max_tokens", "temperature",
            "max_budget_usd")


def _unknown(name, seat):
    return BackendError(f"unknown backend {name!r} in seat {seat!r}; the known backends are: {', '.join(KNOWN)}")


def parse_seat_spec(seat) -> dict:
    """Read a seat, a "backend[:model]" string or a table, into one spec dict.

    The dict always has "backend" ("mock", "claude" or "http") and "model". An http spec also has
    "base_url" and "key_env". Presets (openai, openrouter, deepseek) become http specs.
    """
    if isinstance(seat, dict):
        for key in seat:
            if key not in TABLE_KEYS:
                raise BackendError(f"unknown setting {key!r} in a seat table; the settings are: {', '.join(TABLE_KEYS)}")
        name = seat.get("backend")
        if name not in KNOWN:
            raise _unknown(name, seat)
        spec = {k: v for k, v in seat.items() if k != "backend"}
        for key in _NUMBERS:
            if key in spec and (isinstance(spec[key], bool) or not isinstance(spec[key], (int, float))):
                raise BackendError(f"seat setting {key!r} must be a number")
    else:
        name, _, rest = (seat or "").strip().partition(":")
        if name not in KNOWN:
            raise _unknown(name, seat)
        spec = {}
        if name == "http":
            model, _, url = rest.partition("@")
            if not model or not url:
                raise BackendError(f"an http seat looks like http:MODEL@BASE_URL, not {seat!r}")
            spec = {"model": model, "base_url": url}
        elif rest:
            spec = {"model": rest}
    if "tools" in spec and not (isinstance(spec["tools"], list) and all(isinstance(t, str) for t in spec["tools"])):
        raise BackendError("seat setting 'tools' must be a list of tool names")
    preset = name if name in PRESETS else None
    given = bool(spec.get("key_env"))          # did the seat itself name its key variable?
    spec = {**PRESETS.get(name, {}), **spec}
    backend = "http" if name in PRESETS else name
    if backend == "http":
        if not spec.get("base_url"):
            raise BackendError("an http seat needs a base_url")
        if not spec.get("model"):
            raise BackendError(f"seat {seat!r} needs a model, such as {name}:MODEL")
        spec.setdefault("key_env", "OPENAI_API_KEY")
    spec.setdefault("model", None)
    return {"backend": backend, "preset": preset, "key_env_given": given, **spec}


def is_mock_spec(seat) -> bool:
    """True if the seat is the mock backend (no cost, no live call)."""
    return parse_seat_spec(seat)["backend"] == "mock"


def make_backend(seat, **opts) -> Backend:
    """Build the backend for a seat (string or table).

    `opts` are hints from the case; each backend takes the ones it knows and ignores the rest.
    The mock backend takes `replies` and `default`.
    """
    spec = parse_seat_spec(seat)
    if spec["backend"] == "mock":
        return MockBackend(opts.get("replies", ()), default=opts.get("default", "Okay."))
    if spec["backend"] == "http":
        from .http import HttpBackend
        return HttpBackend(spec)
    from .claude import ClaudeBackend
    return ClaudeBackend(spec)


__all__ = ["AgentSession", "Backend", "BackendError", "Msg", "QuotaError", "SeatResult", "SetupError",
           "MockBackend", "KNOWN", "parse_seat_spec", "is_mock_spec", "make_backend"]
