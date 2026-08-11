"""Canonical YouTube video ID extraction.

Video IDs are **case-sensitive**: ``dQw4w9WgXcQ`` and ``dqw4w9wgxcq`` are two
different videos. Nothing in this module lowercases, uppercases, or otherwise
normalises an ID. Hostnames are lowercased for comparison; IDs never are.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

__all__ = ["extract_video_id", "is_video_id"]

_VIDEO_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")

# Hosts that serve /watch, /embed, /shorts, /live style URLs.
_LONG_HOSTS = frozenset(
    {
        "youtube.com",
        "youtube-nocookie.com",
    }
)

# Hosts where the first path segment is the video ID.
_SHORT_HOSTS = frozenset({"youtu.be"})

# Path prefixes on the long hosts whose next segment is the video ID.
_ID_BEARING_PREFIXES = frozenset({"embed", "shorts", "live", "v", "e"})


def is_video_id(value: str) -> bool:
    """True if ``value`` is exactly an 11-character YouTube video ID."""
    return isinstance(value, str) and _VIDEO_ID_RE.match(value) is not None


def extract_video_id(url_or_id: str) -> str | None:
    """Return the canonical 11-character video ID, or ``None``.

    Accepts every YouTube URL shape we have seen plus a bare ID. Never raises:
    anything unrecognised returns ``None``.
    """
    if not isinstance(url_or_id, str):
        return None

    candidate = url_or_id.strip()
    if not candidate:
        return None

    # A bare ID is already canonical.
    if is_video_id(candidate):
        return candidate

    # Tolerate scheme-less URLs such as "youtu.be/ID".
    to_parse = candidate if "//" in candidate else "https://" + candidate
    try:
        parsed = urlparse(to_parse)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None

    if host.startswith("www."):
        host = host[4:]
    # m., music., gaming., etc.
    base_host = host.split(".", 1)[1] if host.count(".") > 1 else host

    segments = [segment for segment in parsed.path.split("/") if segment]

    if host in _SHORT_HOSTS or base_host in _SHORT_HOSTS:
        return _valid(segments[0]) if segments else None

    if host in _LONG_HOSTS or base_host in _LONG_HOSTS:
        if segments and segments[0] == "watch":
            for value in parse_qs(parsed.query).get("v", []):
                found = _valid(value)
                if found is not None:
                    return found
            return None
        if len(segments) >= 2 and segments[0] in _ID_BEARING_PREFIXES:
            return _valid(segments[1])

    return None


def _valid(value: str) -> str | None:
    return value if is_video_id(value) else None
