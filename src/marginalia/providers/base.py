"""The provider interfaces — transcripts and search.

YouTube will eventually break yt-dlp. These interfaces exist so that adding a
replacement source means adding one file under ``providers/``, never a
refactor. **Nothing outside this package may import yt-dlp, or any other
search or transcript library, or know which provider ran.**

Contract for ``TranscriptProvider.fetch``:

* returns a :class:`TranscriptResult` when captions were obtained
* returns ``None`` when the video simply has no captions — this is a route
  (``needs_asr``), not a failure
* raises :class:`PermanentError` for deleted, private, or region-blocked videos
* raises :class:`TransientError` for network and rate-limit failures

Contract for ``SearchProvider.search``:

* returns a list of :class:`SearchResult`, possibly empty
* raises :class:`QuotaExhausted` when the daily allowance is spent — the
  caller falls through to the next provider
* raises :class:`ProviderUnavailable` when the provider cannot run here at all
  (no API key configured) — the caller also falls through, and names the reason
* raises :class:`TransientError` for network failures — fall through
* raises :class:`PermanentError` for a bad query or a rejected key — these
  surface, because falling through would hide a mistake the user must fix
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ProviderError",
    "PermanentError",
    "TransientError",
    "QuotaExhausted",
    "ProviderUnavailable",
    "TranscriptLine",
    "TranscriptResult",
    "TranscriptProvider",
    "SearchResult",
    "SearchProvider",
    "QuotaLedger",
]


class ProviderError(Exception):
    """Base class for provider failures."""


class PermanentError(ProviderError):
    """Deleted, private, region-blocked, bad query. Do not retry."""


class TransientError(ProviderError):
    """Network error, rate limit, timeout. Retry with backoff."""


class QuotaExhausted(ProviderError):
    """The provider's daily allowance is spent.

    Raised *before* the request is sent, so the caller falls through to an
    overflow provider instead of paying for an opaque 403.
    """


class ProviderUnavailable(ProviderError):
    """The provider cannot run in this environment — typically no API key.

    Distinct from :class:`PermanentError` on purpose, and reviewed as such —
    **do not collapse the two.**

    An *absent* key means the provider is unconfigured. That is a legitimate
    fall-through when another provider is in the chain: refusing to search at
    all, while a working overflow provider sits right there, would be a worse
    answer than searching. So this falls through.

    A *rejected* key — ``keyInvalid``, ``accessNotConfigured`` — is a real
    configuration error. It fails identically on every retry and only the user
    can fix it, so it raises :class:`PermanentError` and surfaces.

    What keeps the fall-through from being silent is that the chain records the
    reason and the report prints it on every single run. If that reporting is
    ever dropped, this distinction stops being safe and should be revisited.
    """


@dataclass
class TranscriptLine:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptResult:
    lines: list[TranscriptLine]
    language: str
    source: str  # "captions"
    title: str
    channel: str
    duration_s: int
    upload_date: str | None
    chapters: list[dict] = field(default_factory=list)  # [{"start_ms": int, "title": str}]
    # Which caption track was used: "manual" (author-written) or "auto"
    # (machine-generated). Recorded because manual captions are materially
    # better and a later upgrade pass needs to know which it has.
    caption_kind: str = "manual"


@runtime_checkable
class TranscriptProvider(Protocol):
    name: str

    def fetch(self, video_id: str) -> TranscriptResult | None: ...


class MetadataCapable(Protocol):
    """Optional capability: return raw video metadata without captions.

    Used only on the ``needs_asr`` path, so the report can name the video that
    has no captions. Providers are not required to implement it.
    """

    def fetch_metadata(self, video_id: str) -> dict[str, Any] | None: ...


# -- search -----------------------------------------------------------------


@dataclass
class SearchResult:
    """One candidate video, as a search provider described it.

    Everything here is cheap metadata — the material stage 1 of relevance
    judging reads (M2b). No transcript text ever passes through this type.

    ``duration_s``, ``published_at`` and ``view_count`` are optional because
    providers differ in what they return: YouTube's ``search.list`` omits all
    three, and yt-dlp's flat extraction omits some.
    """

    video_id: str
    title: str
    channel: str
    description: str
    duration_s: int | None
    published_at: str | None
    view_count: int | None
    # Which provider produced this result. Set by the provider itself; the
    # report names it, and a mixed-provider result set must stay attributable.
    provider: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def search(self, query: str, limit: int = 25) -> list[SearchResult]: ...

    def quota_remaining(self) -> int | None:
        """Units left today, or ``None`` for providers with no quota concept."""
        ...


class QuotaLedger(Protocol):
    """The slice of the ledger a metered provider needs.

    Narrow on purpose: a provider must be constructible in a test with three
    lines of fake, and must not be able to reach the rest of the ledger.
    """

    def quota_used(self, provider: str) -> int: ...

    def spend_quota(self, provider: str, units: int) -> int: ...
