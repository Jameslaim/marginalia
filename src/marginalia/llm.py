"""Every model call in marginalia goes through here.

Nothing else may name a model, build a request, or load a prompt. That rule is
what makes the provider swappable, and it is about to be tested for real: the
free tier this was built against expires in roughly six months and the
replacement is not chosen.

**Portability is a hard requirement, not an aspiration.** This module uses only
the OpenAI-compatible baseline — ``chat/completions``, a system message, a user
message, plain text back. Deliberately *not* used, because each is something
some future provider will lack or implement differently:

* JSON mode and structured outputs
* tool / function calling
* assistant-message prefills
* any provider-specific request parameter

Robust text parsing works everywhere. The parsers in ``judge.py`` are the price
of that, and they are cheaper than a migration.

Rate limiting blocks rather than failing. A discovery run legitimately needs
more requests than the per-minute ceiling allows, and it should pace itself
instead of erroring at request 41.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import LlmConfig

log = logging.getLogger(__name__)

__all__ = [
    "LlmClient",
    "LlmError",
    "LlmConfigError",
    "LlmUnavailable",
    "Prompt",
    "Reply",
    "RateLimiter",
    "Usage",
    "load_prompt",
    "prompts_dir",
]


class LlmError(RuntimeError):
    """A model call failed."""


class LlmConfigError(LlmError):
    """The provider rejected the request in a way only the user can fix.

    A bad key, an unknown model, a disabled account. Retrying cannot help, and
    quietly degrading would hide the one thing that needs attention. This is
    the same missing-versus-rejected line drawn for the YouTube key.
    """


class LlmUnavailable(LlmConfigError):
    """No API key is configured, so there is nothing to call.

    A subclass, because both are "you must fix this" and neither is worth
    retrying. The distinction survives in the message rather than the control
    flow: unlike search, there is no second model to fall through to, so the
    absent-versus-rejected line changes what the user is told, not what
    happens next.
    """


# -- prompts ----------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    """A prompt file, its version, and its metadata.

    The version is recorded on every artifact the prompt produces. Without it,
    a library built over months is a mix of outputs from prompts nobody can
    identify, and "is this note stale?" becomes unanswerable.
    """

    name: str
    version: str
    text: str
    meta: dict[str, str] = field(default_factory=dict)

    def render(self, **values: object) -> str:
        """Substitute ``{{ name }}`` placeholders.

        Deliberately not ``str.format``: prompts contain literal braces in
        their output examples, and escaping every one of them is a trap that
        only springs when someone edits a prompt months later.
        """
        text = self.text
        for key, value in values.items():
            text = text.replace("{{ " + key + " }}", str(value))
        leftover = re.findall(r"\{\{\s*(\w+)\s*\}\}", text)
        if leftover:
            raise LlmError(f"prompt {self.name!r} has unfilled placeholder(s): {leftover}")
        return text


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


def prompts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(name: str, *, directory: Path | None = None) -> Prompt:
    path = (directory or prompts_dir()) / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LlmError(f"could not read prompt {path}: {exc}") from exc

    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise LlmError(f"prompt {path} has no frontmatter; a version is required")

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")

    version = meta.get("version")
    if not version:
        raise LlmError(f"prompt {path} has no `version:` in its frontmatter")

    return Prompt(name=name, version=version, text=match.group(2).strip(), meta=meta)


# -- rate limiting ----------------------------------------------------------


class RateLimiter:
    """Token bucket. Blocks until a request is allowed; never raises.

    Failing at request 41 would be the wrong shape entirely — a discovery run
    is *supposed* to make more calls than the per-minute ceiling permits, just
    not all at once. Clock and sleep are injected so the tests can assert the
    pacing without spending a real minute doing it.
    """

    def __init__(
        self,
        requests_per_minute: int,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.rate = requests_per_minute / 60.0
        self.capacity = float(requests_per_minute)
        self._tokens = float(requests_per_minute)
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        self._last = self._now()
        self._lock = threading.Lock()
        self.waited_s = 0.0
        self.waits = 0

    def acquire(self) -> float:
        """Take one token, waiting if the bucket is empty. Returns time waited."""
        with self._lock:
            self._refill()
            if self._tokens < 1.0:
                delay = (1.0 - self._tokens) / self.rate
                log.info("rate limiter: pausing %.2fs to stay under the ceiling", delay)
                self._sleep(delay)
                self.waited_s += delay
                self.waits += 1
                self._refill()
            self._tokens -= 1.0
            return 0.0 if self.waits == 0 else self.waited_s

    def _refill(self) -> None:
        moment = self._now()
        elapsed = max(0.0, moment - self._last)
        self._last = moment
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)


# -- the client -------------------------------------------------------------


@dataclass
class Usage:
    """What a run cost. Requests are the scarce resource here, not dollars."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    usage: Usage
    latency_s: float


# Retry only what a retry can fix. Everything else is the user's to fix, and
# hiding it behind three silent attempts wastes both time and credits.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
_CONFIG_STATUSES = frozenset({400, 401, 403, 404, 422})


class LlmClient:
    """One client per run. Owns the rate limiter and the usage counters."""

    def __init__(
        self,
        config: LlmConfig,
        *,
        transport: Callable[..., Any] | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 4,
    ) -> None:
        self.config = config
        self.usage = Usage()
        self.limiter = limiter or RateLimiter(config.requests_per_minute)
        self._sleep = sleep or time.sleep
        self._max_attempts = max_attempts
        self._transport = transport
        self._client: Any = None

    @property
    def model(self) -> str:
        return self.config.model

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        task: str | None = None,
    ) -> Reply:
        """One chat completion. Blocks for the rate limiter, retries transients.

        ``task`` selects a block of extra request fields from config — thinking
        effort, typically. This module never looks inside that block: it is
        merged into the request body and sent. Knowing what it means would put
        a provider's feature set into the code, which is the one thing that
        must stay in the config file.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        extra = self.config.extra_for(task)
        last: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            self.limiter.acquire()
            started = time.monotonic()
            try:
                response = self._send(messages, max_tokens or self.config.max_tokens, extra)
            except LlmConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                error = self._classify(exc)
                if isinstance(error, LlmConfigError):
                    raise error from exc
                last = error
                if attempt == self._max_attempts:
                    break
                backoff = 2.0 ** (attempt - 1)
                log.info(
                    "llm: attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt,
                    self._max_attempts,
                    error,
                    backoff,
                )
                self._sleep(backoff)
                continue

            reply = self._to_reply(response, time.monotonic() - started)
            self.usage.add(reply.usage)
            log.info(
                "llm: %s replied in %.1fs (in=%d out=%d, %d requests so far)",
                reply.model,
                reply.latency_s,
                reply.usage.input_tokens,
                reply.usage.output_tokens,
                self.usage.requests,
            )
            return reply

        raise LlmError(f"model call failed after {self._max_attempts} attempts: {last}")

    # -- internals ---------------------------------------------------------

    def _send(
        self, messages: list[dict[str, str]], max_tokens: int, extra: dict[str, Any]
    ) -> Any:
        if self._transport is not None:
            return self._transport(
                model=self.config.model, messages=messages, max_tokens=max_tokens, extra=extra
            )
        # No temperature. It looks like part of the portable baseline and is
        # not: reasoning models frequently accept only their own default, and
        # a hardcoded `temperature=0` broke the very first provider swap this
        # design was built for. Anything a provider wants set goes through
        # `extra`, where config owns it. This module names no request field
        # beyond the four that every OpenAI-compatible endpoint requires.
        #
        # extra_body is the SDK's pass-through for fields it does not model.
        # Sending it only when non-empty keeps the no-extras request exactly
        # the baseline, which is what a future provider gets.
        return self._openai().chat.completions.create(
            model=self.config.model,
            messages=messages,
            max_tokens=max_tokens,
            **({"extra_body": dict(extra)} if extra else {}),
        )

    def _openai(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.api_key:
            raise LlmUnavailable(
                "no model API key: set llm.api_key in config.toml or MARGINALIA_LLM_API_KEY"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LlmError("the openai package is required for model calls") from exc

        self._client = OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout_s,
            # Retries are ours: the SDK's would not respect the rate limiter,
            # and would not distinguish a config error from a transient one.
            max_retries=0,
        )
        return self._client

    def _to_reply(self, response: Any, latency_s: float) -> Reply:
        try:
            choice = response.choices[0]
            text = choice.message.content or ""
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmError(f"unreadable response from {self.config.model}") from exc

        usage = getattr(response, "usage", None)
        return Reply(
            text=text.strip(),
            model=str(getattr(response, "model", None) or self.config.model),
            usage=Usage(
                requests=1,
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            latency_s=latency_s,
        )

    def _classify(self, exc: Exception) -> LlmError:
        """Split "retry this" from "you must fix this"."""
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status, int):
            if status in _CONFIG_STATUSES:
                return LlmConfigError(
                    f"{self.config.provider} rejected the request (HTTP {status}): "
                    f"check llm.api_key and llm.model={self.config.model!r} — {_brief(exc)}"
                )
            if status in _RETRY_STATUSES:
                return LlmError(f"transient HTTP {status}")

        name = type(exc).__name__
        if "Authentication" in name or "PermissionDenied" in name or "NotFound" in name:
            return LlmConfigError(
                f"{self.config.provider} rejected the request ({name}): "
                f"check llm.api_key and llm.model={self.config.model!r}"
            )
        return LlmError(f"{name}: {_brief(exc)}")


def _brief(exc: Exception) -> str:
    return " ".join(str(exc).split())[:200]


def batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Split into chunks of ``size``. ``size == 1`` yields one item per chunk."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]
