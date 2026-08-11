"""The transcript provider interface.

YouTube will eventually break yt-dlp. This interface exists so that adding a
replacement source means adding one file under ``providers/``, never a
refactor. **Nothing outside this package may import yt-dlp.**

Contract for ``fetch``:

* returns a :class:`TranscriptResult` when captions were obtained
* returns ``None`` when the video simply has no captions — this is a route
  (``needs_asr``), not a failure
* raises :class:`PermanentError` for deleted, private, or region-blocked videos
* raises :class:`TransientError` for network and rate-limit failures
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ProviderError",
    "PermanentError",
    "TransientError",
    "TranscriptLine",
    "TranscriptResult",
    "TranscriptProvider",
]


class ProviderError(Exception):
    """Base class for provider failures."""


class PermanentError(ProviderError):
    """Deleted, private, region-blocked. Do not retry."""


class TransientError(ProviderError):
    """Network error, rate limit, timeout. Retry with backoff."""


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
