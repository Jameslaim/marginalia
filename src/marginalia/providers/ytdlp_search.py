"""yt-dlp search — the overflow provider. No key, no quota, no allowance.

Used when the official API's daily units run out, or when no key is
configured at all.

A separate file from ``ytdlp.py`` on purpose. Fetching a transcript and
searching for videos are different responsibilities that will break and change
independently — YouTube can kill caption extraction while search keeps working,
and vice versa. The one thing the two share is yt-dlp's error *vocabulary*, so
``_classify`` is imported rather than duplicated; nothing else is.

``extract_flat`` is what makes this affordable: yt-dlp returns the search page's
own metadata instead of resolving all N videos individually. The cost is thin
results — no description, and sometimes no publication date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import PermanentError, SearchResult, TransientError
from .ytdlp import _SilentLogger, _classify

log = logging.getLogger(__name__)

__all__ = ["YtDlpSearchProvider"]


class YtDlpSearchProvider:
    """Search YouTube via yt-dlp's ``ytsearchN:`` pseudo-URL."""

    name = "ytdlp_search"

    def search(self, query: str, limit: int = 25) -> list[SearchResult]:
        import yt_dlp  # local import: nothing outside providers/ may import this

        cleaned = " ".join(query.split())
        if not cleaned:
            raise PermanentError("empty query")

        target = f"ytsearch{max(1, int(limit))}:{cleaned}"
        with yt_dlp.YoutubeDL(_OPTIONS) as ydl:
            try:
                info = ydl.extract_info(target, download=False)
            except Exception as exc:  # noqa: BLE001 - classified below
                raise _classify(exc) from exc

        if not isinstance(info, dict):
            raise TransientError("yt-dlp returned no search results")

        results: list[SearchResult] = []
        for entry in info.get("entries") or []:
            result = self._to_result(entry)
            if result is not None:
                results.append(result)
        return results

    def quota_remaining(self) -> int | None:
        """``None``: this provider has no quota concept. That is the point."""
        return None

    def _to_result(self, entry: Any) -> SearchResult | None:
        if not isinstance(entry, dict):
            return None
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not video_id:
            return None

        return SearchResult(
            video_id=video_id,
            title=_text(entry.get("title")),
            channel=_text(entry.get("channel") or entry.get("uploader")),
            # Flat extraction gives at most a truncated blurb, and often
            # nothing. M2b's metadata judge has to cope with an empty string.
            description=_text(entry.get("description")),
            duration_s=_seconds(entry.get("duration")),
            published_at=_published(entry),
            view_count=_int_or_none(entry.get("view_count")),
            provider=self.name,
        )


_OPTIONS: dict[str, Any] = {
    # The whole reason this is cheap: do not resolve each hit individually.
    "extract_flat": True,
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "no_color": True,
    "logger": _SilentLogger(),
}


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value else ""


def _seconds(value: Any) -> int | None:
    try:
        return int(float(value)) or None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _published(entry: dict[str, Any]) -> str | None:
    """Flat entries carry a unix timestamp at best, and usually nothing.

    ``upload_date`` (``YYYYMMDD``) is normalised to the same ISO-ish shape the
    API returns, so a consumer never has to ask which provider produced it.
    """
    raw = entry.get("upload_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"

    for key in ("timestamp", "release_timestamp"):
        stamp = entry.get(key)
        if stamp is None:
            continue
        try:
            return (
                datetime.fromtimestamp(float(stamp), tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError, OSError, OverflowError):
            log.debug("ytdlp_search: unusable %s %r", key, stamp)
    return None
