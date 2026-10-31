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

* takes optional :class:`SearchFilters`, and applies whatever of them it can —
  a provider that cannot apply a filter logs it and runs the query unfiltered,
  because the filters that matter *exclude* and ignoring one returns a superset
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

import re
from typing import Any, Protocol, Sequence, runtime_checkable

from ..contracts import SearchFilters, SearchResult, TranscriptLine, TranscriptResult

__all__ = [
    "ProviderError",
    "PermanentError",
    "TransientError",
    "QuotaExhausted",
    "ProviderUnavailable",
    "TranslatedOnly",
    "clean_language",
    "TranscriptLine",
    "TranscriptResult",
    "TranscriptProvider",
    "SearchResult",
    "SearchFilters",
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


# A language subtag after the primary one: a script (`Hans`), a region (`US`,
# `419`). Anything else after a hyphen is not part of a language code — YouTube
# appends a track id to a named manual track (`en-eEY6OEpapPo`) — and is cut.
_SUBTAG_RE = re.compile(r"\A(?:[A-Za-z]{4}|[A-Za-z]{2}|\d{3})\Z")


def clean_language(key: str) -> str:
    """A caption track's key as a clean language code: `en-orig` -> `en`,
    `en-eEY6OEpapPo` -> `en`, `de-DE-orig` -> `de-DE`, `es-419` -> `es-419`."""
    parts = [part for part in (key or "").split("-") if part]
    if parts and parts[-1].lower() == "orig":
        parts = parts[:-1]
    if not parts:
        return ""
    code = [parts[0].lower()]
    for part in parts[1:]:
        if not _SUBTAG_RE.match(part):
            break
        code.append(part.upper() if len(part) == 2 else part)
    return "-".join(code)


class TranslatedOnly(ProviderError):
    """Caption tracks exist, and none is in the language the video is spoken in.

    Neither permanent nor transient: the video is fine and a retry returns the
    same tracks. The caller routes it where a captionless video goes and counts
    it apart. ``original`` is the spoken language, ``offered`` what was listed.
    """

    def __init__(self, original: str, offered: Sequence[str] = ()) -> None:
        self.original = original
        self.offered = tuple(offered)
        super().__init__(f"only translated captions (spoken language {original})")


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


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def search(
        self, query: str, limit: int = 25, filters: SearchFilters | None = None
    ) -> list[SearchResult]: ...

    def quota_remaining(self) -> int | None:
        """Units left today, or ``None`` for providers with no quota concept."""
        ...

    def units_per_search(self) -> int:
        """What one :meth:`search` will cost, before it is made.

        Asked *before* a round, so the loop can stop cleanly rather than run
        out of quota halfway through and record half a round's evidence. Zero
        for a provider with no allowance to run out of.
        """
        ...

    def applied_filters(self) -> SearchFilters:
        """Which of the last search's filters this provider actually applied.

        Not the ones it was handed. A provider that cannot filter runs the query
        unfiltered — the safe direction, since the filters that matter exclude —
        but the `queries` row must then say so. B5's planner reads query history
        to decide what to try next, and a row claiming a filter ran when it did
        not is a row that will mislead it about what has already been tried.
        """
        ...

    def units_spent(self) -> int:
        """What the last :meth:`search` cost this provider, in its own units.

        Read after each search so the run can record what a query cost beside
        what it found. On the protocol rather than computed by the caller
        because pricing is the provider's own fact — ``search.list`` costing 100
        units and ``videos.list`` costing 1 is a YouTube API detail, and a
        module above `providers/` working it out would be a module that knows
        which provider ran.

        ``0`` is the honest answer for an unmetered provider, not a placeholder.
        """
        ...


# ``filters`` is optional on every provider, and a provider that cannot honour
# one **must say so and run the query anyway**. The reason is the direction of
# the error: the two filters that do anything irreversible — `publishedAfter`
# and `videoDuration` — *exclude*, so a provider that ignores them returns a
# superset of what was asked for. A superset is judged; a subset is invisible.


class QuotaLedger(Protocol):
    """The slice of the ledger a metered provider needs.

    Narrow on purpose: a provider must be constructible in a test with three
    lines of fake, and must not be able to reach the rest of the ledger.
    """

    def quota_used(self, provider: str) -> int: ...

    def spend_quota(self, provider: str, units: int) -> int: ...
