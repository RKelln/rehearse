"""The http backend: any OpenAI-compatible chat endpoint (OpenAI, OpenRouter, DeepSeek, local servers).

Parts adapted from an earlier CC0-licensed harness by the same author: retries, pacing, quota handling.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .base import Backend, BackendError, Msg, QuotaError, SeatResult, SetupError
from .keys import read_key

_DAILY = re.compile(r"per.day|daily|free-models-per-day", re.I)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry the key to another host, so redirects are refused."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendError(f"the server answered with a redirect (HTTP {code}) to {newurl}; "
                           "redirects are refused. Use the final address as base_url")


_OPENER = urllib.request.build_opener(_NoRedirect)


def retry_wait(header: str | None, fallback: float) -> float:
    """Seconds to wait before a retry: the Retry-After header (seconds or a date), clamped to 0..60."""
    if header is not None:
        try:
            return max(0.0, min(60.0, float(header)))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, min(60.0, (when - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                pass
    return max(0.0, min(60.0, fallback))


class _Pacer:
    """At most `rpm` requests per minute per endpoint, shared by every thread."""
    _all: dict = {}
    _lock = threading.Lock()

    def __init__(self, rpm: float):
        self.gap = 60.0 / rpm if rpm else 0.0
        self.next = 0.0
        self.lock = threading.Lock()

    @classmethod
    def get(cls, key: str, rpm: float) -> "_Pacer":
        with cls._lock:
            if key not in cls._all:
                cls._all[key] = cls(rpm)
            return cls._all[key]

    def wait(self) -> None:
        if not self.gap:
            return
        with self.lock:
            now = time.time()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)


class HttpBackend(Backend):
    """Sends the whole conversation to `{base_url}/chat/completions`, system prompt first."""

    capabilities = frozenset({"chat"})
    backoff = 4.0        # seconds before the first retry; doubles each time (tests make it small)
    attempts = 6

    def __init__(self, spec: dict):
        self.spec = spec
        self.base_url = spec["base_url"].rstrip("/")
        self.model = spec.get("model") or ""
        self.key_env = spec.get("key_env")
        self.key_given = bool(spec.get("key_env_given"))
        self.timeout = float(spec.get("timeout") or 180)
        self.price_in = spec.get("price_in_per_mtok")
        self.price_out = spec.get("price_out_per_mtok")
        self.host = urlparse(self.base_url).netloc
        self.pacer = _Pacer.get(self.base_url + "|" + (self.key_env or ""), float(spec.get("rpm") or 0))

    def describe(self) -> str:
        return f"http:{self.model}@{self.host}"

    def _local(self) -> bool:
        return (urlparse(self.base_url).hostname or "") in LOCAL_HOSTS

    def _key(self) -> str:
        """The key to send. A local server gets one only if the seat names its key variable."""
        if self._local():
            return read_key(self.key_env) if self.key_given and self.key_env else ""
        key = read_key(self.key_env)
        if not key:
            raise SetupError(f"{self.describe()}: no key. Set {self.key_env} in your environment, or add "
                             f"{self.key_env}=... to ~/.config/rehearse/keys (then chmod 600 that file)")
        if urlparse(self.base_url).scheme != "https":
            raise SetupError(f"{self.describe()}: refusing to send a key over plain http to a remote host. "
                             "Use an https base_url")
        return key

    def cost(self, tokens_in, tokens_out) -> float | None:
        """Tokens times the seat's prices, or None if either price or either count is missing."""
        if None in (self.price_in, self.price_out, tokens_in, tokens_out):
            return None
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def chat(self, system: str, messages: list[Msg]) -> SeatResult:
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system}]
                            + [{"role": m.role, "content": m.content} for m in messages]}
        for opt in ("max_tokens", "temperature"):
            if self.spec.get(opt) is not None:
                body[opt] = self.spec[opt]
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        last = None
        for attempt in range(self.attempts):
            self.pacer.wait()
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, headers=headers)
            wait = self.backoff * 2 ** attempt
            try:
                with _OPENER.open(req, timeout=self.timeout) as r:
                    text = r.read().decode("utf-8", "replace")
                return self._parse(text)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                if e.code in (401, 403):
                    raise SetupError(f"{self.describe()}: HTTP {e.code}: the key was refused. {detail}") from None
                if e.code == 402 or (e.code == 429 and _DAILY.search(detail)):
                    raise QuotaError(f"{self.describe()}: HTTP {e.code}: {detail}") from None
                last = f"HTTP {e.code}: {detail}"
                if e.code not in (429, 500, 502, 503, 504):
                    raise BackendError(f"{self.describe()}: {last}", raw={"status": e.code, "body": detail}) from None
                wait = retry_wait(e.headers.get("Retry-After"), wait)
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < self.attempts - 1:
                time.sleep(min(60.0, max(0.0, wait)))
        raise BackendError(f"{self.describe()}: gave up after {self.attempts} tries: {last}")

    def _parse(self, text: str) -> SeatResult:
        """Read a reply. The raw body is kept on the result, or on the error if it cannot be read."""
        try:
            data = json.loads(text)
            if "error" in data:
                raise BackendError(f"{self.describe()}: {str(data['error'])[:300]}", raw={"body": text})
            choice = data["choices"][0]
            content = _content(choice["message"].get("content"))
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            tin, tout = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
            finish = choice.get("finish_reason")
            extras = {}
            if isinstance(finish, str):
                extras["finish_reason"] = finish
                if finish == "length":
                    extras["truncated"] = True
            model = data.get("model") if isinstance(data.get("model"), str) else self.model
            return SeatResult(text=content.strip(), cost_usd=self.cost(tin, tout), tokens_in=tin, tokens_out=tout,
                              model=model, raw={"body": text}, extras=extras)
        except BackendError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise BackendError(f"{self.describe()}: could not read the reply ({type(e).__name__}: {e})",
                               raw={"body": text}) from None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content(value) -> str:
    """Reply text: a string, or a list of parts with a "text" each."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in value)
    raise TypeError(f"reply content is a {type(value).__name__}")
