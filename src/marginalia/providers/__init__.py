"""Transcript and search providers.

Imports of concrete providers are deliberately lazy so that importing this
package — or running the tests — never requires yt-dlp to be installed.

The factories take explicit arguments rather than a ``Config``: providers must
be constructible in a test without a config file, and must not learn the shape
of one.
"""

from __future__ import annotations

from .base import (
    PermanentError,
    ProviderError,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaLedger,
    SearchProvider,
    SearchResult,
    TranscriptLine,
    TranscriptProvider,
    TranscriptResult,
    TransientError,
)

__all__ = [
    "PermanentError",
    "ProviderError",
    "ProviderUnavailable",
    "QuotaExhausted",
    "QuotaLedger",
    "SearchProvider",
    "SearchResult",
    "TranscriptLine",
    "TranscriptProvider",
    "TranscriptResult",
    "TransientError",
    "get_provider",
    "get_search_provider",
    "known_providers",
    "known_search_providers",
    "get_web_search_provider",
]


def known_providers() -> tuple[str, ...]:
    return ("ytdlp",)


def known_search_providers() -> tuple[str, ...]:
    return ("youtube_api", "ytdlp_search")


def get_provider(name: str) -> TranscriptProvider:
    """Instantiate a transcript provider by name. Raises KeyError if unknown."""
    if name == "ytdlp":
        from .ytdlp import YtDlpProvider

        return YtDlpProvider()
    raise KeyError(name)


def get_search_provider(
    name: str,
    *,
    api_key: str | None = None,
    quota: QuotaLedger | None = None,
    daily_units: int | None = None,
) -> SearchProvider:
    """Instantiate a search provider by name.

    Raises ``KeyError`` if unknown, and :class:`ProviderUnavailable` if the
    provider exists but has nothing to run with — a missing API key.
    """
    if name == "youtube_api":
        from .youtube_api import DEFAULT_DAILY_UNITS, YouTubeApiProvider

        return YouTubeApiProvider(
            api_key,
            quota=quota,
            daily_units=DEFAULT_DAILY_UNITS if daily_units is None else daily_units,
        )
    if name == "ytdlp_search":
        from .ytdlp_search import YtDlpSearchProvider

        return YtDlpSearchProvider()
    raise KeyError(name)


def get_web_search_provider(name: str, *, api_key: str | None, base_url: str):
    """The start-of-run web lookup's provider (step 4). Raises ``KeyError`` if
    unknown, and ``WebSearchRefused`` when there is no key to run with."""
    if name == "host_search":
        from .host_search import HostSearchProvider

        return HostSearchProvider(api_key, base_url)
    raise KeyError(name)
