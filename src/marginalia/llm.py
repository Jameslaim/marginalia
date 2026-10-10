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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import LlmConfig
from .contracts import Usage

log = logging.getLogger(__name__)

__all__ = [
    "LlmClient",
    "LlmError",
    "LlmConfigError",
    "LlmBillingError",
    "LlmRateLimited",
    "LlmRequestTooLarge",
    "LlmUnavailable",
    "Prompt",
    "Reply",
    "RateLimiter",
    "load_prompt",
    "prompts_dir",
    "safe_reason",
]


class LlmError(RuntimeError):
    """A model call failed."""


class LlmConfigError(LlmError):
    """The provider rejected the request in a way only the user can fix.

    A bad key, an unknown model, a disabled account. Retrying cannot help, and
    quietly degrading would hide the one thing that needs attention. This is
    the same missing-versus-rejected line drawn for the YouTube key.
    """


class LlmBillingError(LlmConfigError):
    """The account cannot pay for the request. HTTP 402.

    A subclass of `LlmConfigError` because it is the same kind of claim — the
    provider has refused, and no retry addresses the reason — but it is worth
    telling apart from a bad key, because the two send the reader to completely
    different places and the generic message sends them to the wrong one.

    Measured, on the note drain that found this: three videos each retried four
    times against an account with no balance. Twelve calls that could only fail,
    on a condition that, unlike a rate limit, does not clear in seconds.
    """


class LlmRateLimited(LlmConfigError):
    """The provider asked for a wait longer than this run should take.

    A subclass for the same reason `LlmBillingError` is one: every caller that
    already stops on "you must fix this" stops here too, without learning a new
    type — and the message can say what actually happened instead of pointing
    at the API key.

    Not every 429 lands here. A rate limit with a short `Retry-After` is waited
    out and retried, because that is what the header is for. This is only the
    case where honouring the header would mean sleeping past the end of the
    run, which rule 24 calls a refusal in a different costume.
    """


class LlmRequestTooLarge(LlmError):
    """This one request is over the model's context window. HTTP 413, or a 400
    that says so.

    **Deliberately not an `LlmConfigError`.** That class means "every later
    call fails identically", and it stops a discovery run or a note drain. A
    context overflow is a fact about *this* request: the next video's
    transcript may fit. Classed as config, one long video ended a whole
    discovery run at stage 2, and in the note drain it was worse — the drain
    stopped on the video, recorded nothing about it, and met it first again on
    the next run, so a single transcript blocked the queue for good.

    Not retried either. An identical request is refused identically, which is
    rule 24's reason for not retrying a refusal, applied to one request.
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


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    usage: Usage
    latency_s: float
    # Why the model stopped. `"stop"` is a completed answer; `"length"` means
    # the ceiling was hit and what came back is a fragment — which on a
    # reasoning model is routinely an *empty* fragment, because the budget went
    # to thinking and none was left to answer with. That failure cost eighteen
    # minutes to diagnose on the note run, from a reply that looked merely
    # disappointing rather than truncated. Recorded so a convergence check can
    # ask instead of guess. `None` when a provider does not report it.
    finish_reason: str | None = None


# Retry only what a retry can fix. Everything else is the user's to fix, and
# hiding it behind three silent attempts wastes both time and credits.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
_CONFIG_STATUSES = frozenset({400, 401, 403, 404, 422})
# Billing. Its own set for its own message: "check llm.api_key and llm.model"
# is actively misleading when the key is fine and the balance is not.
_BILLING_STATUSES = frozenset({402})


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
        # Consecutive requests refused as over the context window. See
        # `_TOO_LARGE_STREAK`.
        self._too_large = 0

    @property
    def model(self) -> str:
        return self.config.model

    def forget_overflows(self) -> None:
        """Start the overflow count again, for a caller whose requests are
        independent of each other.

        The note drain is one: each video is its own request, a skipped one
        costs nothing, and its queue is ordered by `first_seen`, so three
        oversized transcripts at its head would otherwise stop every drain on
        the same three videos — the jam `LlmRequestTooLarge` exists to prevent.
        """
        self._too_large = 0

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
                if isinstance(error, LlmRequestTooLarge):
                    self._too_large += 1
                    if self._too_large >= _TOO_LARGE_STREAK:
                        # Every request lately has been over the window, which is
                        # not one long input — it is a ceiling or a model that
                        # cannot fit any of them. A config error, so the run
                        # stops instead of keeping every video unread.
                        raise LlmConfigError(
                            f"{self._too_large} requests in a row were too large for "
                            f"{self.config.model!r}'s context window — the configured "
                            f"max_tokens or the inputs do not fit this model. Lower "
                            f"[note] max_tokens / [ask] max_tokens, or [note] "
                            f"max_transcript_chars."
                        ) from exc
                    raise error from exc
                if isinstance(error, LlmConfigError):
                    raise error from exc
                last = error

                # The provider's own answer beats our schedule. A rate limit is
                # the one failure where it states when to come back, and
                # guessing is strictly worse than reading — but a wait longer
                # than the run is a refusal, so it stops instead of hanging.
                asked = _retry_after(exc)
                if asked is not None and asked > _MAX_RETRY_AFTER_S:
                    raise LlmRateLimited(
                        f"{self.config.provider} asked for a {asked:.0f}s wait "
                        f"(Retry-After), which is longer than this run should "
                        f"take — stopping instead {_safe(error, exc)}"
                    ) from exc

                if attempt == self._max_attempts:
                    break
                backoff = 2.0 ** (attempt - 1) if asked is None else asked
                log.info(
                    "llm: attempt %d/%d failed (%s); retrying in %.0fs%s",
                    attempt,
                    self._max_attempts,
                    # Never the exception and never `_brief`: both carry the
                    # provider's message, which is a response body, which is
                    # what rule 1 keeps out of `logs/`.
                    _safe(error, exc),
                    backoff,
                    " (as the provider asked)" if asked is not None else "",
                )
                self._sleep(backoff)
                continue

            reply = self._to_reply(response, time.monotonic() - started)
            self._too_large = 0
            self.usage.add(reply.usage)
            log.info(
                "llm: %s replied in %.1fs (in=%d out=%d, finish=%s, %d requests so far)",
                reply.model,
                reply.latency_s,
                reply.usage.input_tokens,
                reply.usage.output_tokens,
                reply.finish_reason or "unreported",
                self.usage.requests,
            )
            if reply.finish_reason == "length":
                # Loud, because the symptom is silence. A truncated reply from a
                # reasoning model is often empty, and an empty reply reads as a
                # bad answer rather than as no answer at all.
                log.warning(
                    "llm: %s hit the token ceiling (%d out, %d chars back) — "
                    "the reply is a fragment, not an answer",
                    reply.model,
                    reply.usage.output_tokens,
                    len(reply.text),
                )
            return reply

        raise LlmError(f"model call failed after {self._max_attempts} attempts: {last}")

    # -- what a failure may say about itself -------------------------------

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
        # `extra`, where config owns it.
        #
        # `user` is the one addition, and it is in the baseline rather than
        # provider-specific — every OpenAI-compatible endpoint accepts it, so
        # naming it here binds this project to nobody. It is sent because the
        # xKiro endpoint replays byte-identical blocking requests within a short
        # window with no opt-out header: five identical calls returned one
        # distinct answer, four of them in under 0.4s. A retry of a failed note
        # would then be handed the same failure back instead of a fresh sample,
        # and three notes recovered on retry in one week. A unique value per
        # call makes every request distinct at the byte level, which is the
        # cheapest possible cache-buster and costs nothing on a provider that
        # does not cache.
        #
        # extra_body is the SDK's pass-through for fields it does not model.
        # Sending it only when non-empty keeps the no-extras request exactly
        # the baseline, which is what a future provider gets.
        return self._openai().chat.completions.create(
            model=self.config.model,
            messages=messages,
            max_tokens=max_tokens,
            user=_call_id(),
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

        finish_reason = getattr(choice, "finish_reason", None)

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
            finish_reason=str(finish_reason) if finish_reason else None,
        )

    def _classify(self, exc: Exception) -> LlmError:
        """Split "retry this" from "you must fix this".

        **A 4xx this list does not recognise is not retried.** The default used
        to be the other way round, and 402 fell through it: an account with no
        balance was asked four times per video, which is the one shape of call
        that is guaranteed to fail. The general rule is the one the retry set
        was already written for — a 4xx means the request was refused, and an
        identical request is refused identically. The two exceptions are named
        in `_RETRY_STATUSES` and are exactly the ones that clear on their own:
        a timeout, a conflict, a rate limit.
        """
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status, int):
            if status == 413 or (status in (400, 422) and _says_too_long(exc)):
                # Before the generic 4xx rule: one request over the window is not
                # a broken configuration. See `LlmRequestTooLarge`.
                return LlmRequestTooLarge(
                    f"{self.config.provider} refused the request as too large for "
                    f"{self.config.model!r}'s context window (HTTP {status})"
                )
            if status in _RETRY_STATUSES:
                return LlmError(f"transient HTTP {status}")
            # Status, the provider's error code and the request id — never the
            # message. These errors are printed, saved into a failed answer and
            # recorded as a note's failure detail, and the message is the
            # provider's response body, which can echo the request.
            if status in _BILLING_STATUSES:
                return LlmBillingError(
                    f"{self.config.provider} refused the request (HTTP {status}): "
                    f"the account cannot pay for it{_identifiers(exc)}"
                )
            if status in _CONFIG_STATUSES:
                return LlmConfigError(
                    f"{self.config.provider} rejected the request (HTTP {status}): "
                    f"check llm.api_key and llm.model={self.config.model!r}{_identifiers(exc)}"
                )
            if 400 <= status < 500:
                return LlmConfigError(
                    f"{self.config.provider} refused the request (HTTP {status}): "
                    f"retrying it unchanged cannot help{_identifiers(exc)}"
                )

        name = type(exc).__name__
        if "Authentication" in name or "PermissionDenied" in name or "NotFound" in name:
            return LlmConfigError(
                f"{self.config.provider} rejected the request ({name}): "
                f"check llm.api_key and llm.model={self.config.model!r}"
            )
        # The exception's class and nothing it says. `str(exc)` here was the
        # one path where the body reached the log: `_safe` formats this error,
        # so the retry line carried whatever an unrecognised exception held.
        status_part = f" (HTTP {status})" if isinstance(status, int) else ""
        return LlmError(f"{name}{status_part}{_code_part(exc)}")


# A rate limit is the one failure where the provider states the answer, so
# guessing at it is strictly worse than reading it. Two wire formats are legal:
# delay-seconds, and an HTTP date.
_RETRY_AFTER_HEADER = "retry-after"

# Above this, waiting is not retrying. A server answering "3600" is telling you
# to come back after the run is over, and rule 24's judgement applies: a wait
# longer than the run is a refusal wearing a different costume, so it stops
# rather than hanging. Two minutes is longer than any backoff this file
# schedules and far shorter than a note drain.
_MAX_RETRY_AFTER_S = 120.0

# Where a provider puts the one field its support will ask for. Headers first,
# because a header cannot contain a transcript; the SDK attribute second.
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray")


def _headers(exc: Exception) -> Mapping[str, str]:
    """The response headers, or an empty mapping. Never raises, never parses a body."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return headers if hasattr(headers, "get") else {}


def _retry_after(exc: Exception) -> float | None:
    """Seconds the provider asked us to wait, or `None` if it did not say.

    **Unparseable is absent, never zero.** A header this cannot read must fall
    back to the backoff schedule, because reading it as "wait 0s" would turn a
    malformed hint into a hot loop against a server that just asked for room.

    A date in the past is a real answer — "you may retry now" — and clamps to
    zero rather than going negative.
    """
    raw = _headers(exc).get(_RETRY_AFTER_HEADER)
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _request_id(exc: Exception) -> str:
    """The provider's request id, or `""`. **Reads a header, never a body.**

    The obvious implementation of "make sure the id is in the log" is to log
    the whole error body, and that is hard rule 1's worst case: provider error
    bodies echo the request, and the request here is a system prompt plus an
    entire transcript. A log file is the worst place for that to land, because
    it accumulates every run and nobody opens it until something is already
    wrong.

    So the id is extracted as its own field and only that field is logged. When
    it cannot be found the absence is logged, which is a complete and honest
    answer; a body is not.
    """
    headers = _headers(exc)
    for name in _REQUEST_ID_HEADERS:
        value = headers.get(name) or headers.get(name.title())
        if value:
            return " ".join(str(value).split())[:120]
    # The SDK lifts it onto the exception for exactly this purpose. An
    # attribute, not a parsed body.
    value = getattr(exc, "request_id", None)
    return " ".join(str(value).split())[:120] if value else ""


# Overflows in a row, with no success between them, before one is treated as the
# configuration's fault rather than the input's. One overflow is a long
# transcript. Three in a row with nothing fitting in between is a `max_tokens`
# larger than the model's window, or a model too small for this library — and
# handled per request, that would keep every stage-2 candidate unread and skip
# every note, which is rule 24's "storing an unjudged library" by another route.
_TOO_LARGE_STREAK = 3

# How OpenAI-compatible servers word a context overflow. Read from the error's
# own text, which the classifier already reads for `_brief`; the result is a
# classification, and nothing from the text is logged or kept.
_TOO_LONG_RE = re.compile(
    r"context[ _-]?(?:length|window)|maximum context|too many tokens|prompt is too long"
    r"|input is too long|reduce the length|exceeds? the (?:model'?s? )?(?:maximum|token limit)",
    re.IGNORECASE,
)


def _says_too_long(exc: Exception) -> bool:
    """Does this 400 say the request was over the context window?"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and "context" in code.lower():
        return True
    return bool(_TOO_LONG_RE.search(str(exc)))


def _call_id() -> str:
    """A value unique to one request, for the `user` field.

    Its only job is to make two otherwise identical requests differ in their
    bytes. It is not an identity: nothing here has a user to name, and sending
    a real one would put an identifier on every request for no benefit. A
    random hex string carries no information about the caller and cannot become
    a tracking key by accident.
    """
    return f"marginalia-{uuid.uuid4().hex}"


# A provider's machine-readable error code — `insufficient_quota`,
# `model_not_found`. An identifier, never prose, and only kept when it looks
# like one: anything else is treated as part of the body.
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _code_part(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _SAFE_CODE_RE.match(code):
        return f" [code: {code}]"
    return ""


def _identifiers(exc: Exception) -> str:
    """The safe fields of a refusal: its error code and request id, if any."""
    request_id = _request_id(exc)
    return _code_part(exc) + (f" [request id: {request_id}]" if request_id else "")


def safe_reason(exc: BaseException) -> str:
    """What a log line or a failure record may say about any exception.

    An `LlmError` is built by `_classify` from status, code and request id
    only, so its text is safe to repeat. Anything else is named by class: its
    message is unknown text, and the exceptions that reach a catch-all around a
    model call are exactly the ones nobody classified.
    """
    if isinstance(exc, LlmError):
        return " ".join(str(exc).split())
    return type(exc).__name__


def _safe(error: Exception, exc: Exception) -> str:
    """What a *log line* may say about a failure: the classification and the id.

    Deliberately not `_brief`, and deliberately not the exception. Those carry
    the provider's message, which is a response body, which is the thing rule 1
    says must never reach `logs/`. The classification (`transient HTTP 429`)
    and the request id are both bounded, both useful, and neither can contain a
    transcript.
    """
    request_id = _request_id(exc)
    return f"{error} [request id: {request_id or 'not reported'}]"


def batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Split into chunks of ``size``. ``size == 1`` yields one item per chunk."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]
