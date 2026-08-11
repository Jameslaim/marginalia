"""Transcript providers.

Imports of concrete providers are deliberately lazy so that importing this
package — or running the tests — never requires yt-dlp to be installed.
"""

from __future__ import annotations

from .base import (
    PermanentError,
    ProviderError,
    TranscriptLine,
    TranscriptProvider,
    TranscriptResult,
    TransientError,
)

__all__ = [
    "PermanentError",
    "ProviderError",
    "TranscriptLine",
    "TranscriptProvider",
    "TranscriptResult",
    "TransientError",
    "get_provider",
    "known_providers",
]


def known_providers() -> tuple[str, ...]:
    return ("ytdlp",)


def get_provider(name: str) -> TranscriptProvider:
    """Instantiate a provider by name. Raises KeyError if unknown."""
    if name == "ytdlp":
        from .ytdlp import YtDlpProvider

        return YtDlpProvider()
    raise KeyError(name)
