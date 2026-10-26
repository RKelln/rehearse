"""Backends fill seats. A seat is written `backend[:model]`, for example `mock`.

Only `mock` exists so far. Claude, HTTP and other backends are added in later steps.
"""

from __future__ import annotations

from .base import Backend, BackendError, Msg, QuotaError, SeatResult, SetupError
from .mock import MockBackend

KNOWN = ("mock",)


def parse_seat_spec(spec: str) -> dict:
    """Read "backend[:model]" into {"backend": ..., "model": ... or None}."""
    name, _, model = (spec or "").strip().partition(":")
    if name not in KNOWN:
        raise BackendError(
            f"unknown backend {name!r} in seat {spec!r}; the known backends are: {', '.join(KNOWN)}")
    return {"backend": name, "model": model or None}


def make_backend(spec: str, **opts) -> Backend:
    """Build the backend for a seat spec.

    `opts` are hints from the case; each backend takes the ones it knows and ignores the rest.
    The mock backend takes `replies` and `default`.
    """
    parsed = parse_seat_spec(spec)
    if parsed["backend"] == "mock":
        return MockBackend(opts.get("replies", ()), default=opts.get("default", "Okay."))
    raise BackendError(f"backend {parsed['backend']!r} is not built yet")  # unreachable for now


__all__ = ["Backend", "BackendError", "Msg", "QuotaError", "SeatResult", "SetupError",
           "MockBackend", "KNOWN", "parse_seat_spec", "make_backend"]
