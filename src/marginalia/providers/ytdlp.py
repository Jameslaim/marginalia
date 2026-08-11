"""yt-dlp transcript provider.

The only module in the project permitted to import yt-dlp.

Metadata and subtitle listings come from a single ``extract_info`` call with
``skip_download=True``; the chosen caption track is then downloaded through
yt-dlp's own HTTP client so it shares headers and any configured cookies.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree

from .base import (
    PermanentError,
    TranscriptLine,
    TranscriptResult,
    TransientError,
)

log = logging.getLogger(__name__)

# Language preference when the video's own language is unknown.
_DEFAULT_LANGUAGES: tuple[str, ...] = ("en", "en-US", "en-GB")

# Caption serialisation formats, best first. json3 carries clean per-cue timing
# and marks rollup duplicates; the rest are fallbacks.
_PREFERRED_EXTS: tuple[str, ...] = ("json3", "srv3", "srv1", "vtt", "ttml")

# Substrings that mean "this video will never work". Checked before the
# transient patterns; anything unmatched defaults to transient, because
# retrying a permanent failure is cheaper than never retrying a temporary one.
_PERMANENT_PATTERNS: tuple[str, ...] = (
    "video unavailable",
    "private video",
    "this video is private",
    "sign in if you've been granted access",
    "has been removed by the uploader",
    "removed for violating",
    "account associated with this video has been terminated",
    "no longer available",
    "not available in your country",
    "blocked it in your country",
    "blocked it on copyright grounds",
    "who has blocked it",
    "members-only",
    "join this channel",
    "available to this channel's members",
    "is not a valid url",
    "incomplete youtube id",
    "does not exist",
    "sign in to confirm your age",
    "age-restricted",
    "inappropriate for some users",
    "this live event has ended",
    "premieres in",
)

_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "http error 429",
    "too many requests",
    "http error 5",
    "timed out",
    "timeout",
    "temporary failure",
    "connection",
    "network",
    "getaddrinfo",
    "ssl",
    "not a bot",
    "unable to download webpage",
    "unable to download api page",
    "failed to extract",
    "read operation",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_VTT_CUE_RE = re.compile(
    r"^(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})"
)


class _SilentLogger:
    """yt-dlp writes to a logger; send everything to ours, never to stdout."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


class YtDlpProvider:
    """Fetch YouTube captions via yt-dlp."""

    name = "ytdlp"

    def __init__(self, languages: Sequence[str] = _DEFAULT_LANGUAGES) -> None:
        self._languages = tuple(languages)
        self._cached_id: str | None = None
        self._cached_info: dict[str, Any] | None = None

    # -- public API --------------------------------------------------------

    def fetch(self, video_id: str) -> TranscriptResult | None:
        import yt_dlp  # local import: nothing outside providers/ may import this

        with yt_dlp.YoutubeDL(self._options()) as ydl:
            info = self._extract(ydl, video_id)

            track = self._select_track(info)
            if track is None:
                log.info("%s: no caption tracks available", video_id)
                return None

            language, kind, entry = track
            try:
                raw = ydl.urlopen(entry["url"]).read()
            except Exception as exc:  # noqa: BLE001 - classified below
                raise _classify(exc) from exc

        lines = _parse_captions(entry.get("ext", ""), raw)
        if not lines:
            log.info("%s: caption track %s/%s parsed to zero lines", video_id, kind, language)
            return None

        return TranscriptResult(
            lines=lines,
            language=language,
            source="captions",
            caption_kind=kind,
            title=str(info.get("title") or ""),
            channel=str(info.get("channel") or info.get("uploader") or ""),
            duration_s=int(info.get("duration") or 0),
            upload_date=info.get("upload_date") or None,
            chapters=_chapters(info),
        )

    def fetch_metadata(self, video_id: str) -> dict[str, Any] | None:
        """Metadata only. Reuses the info dict from the last ``fetch`` if any."""
        if self._cached_id == video_id and self._cached_info is not None:
            return self._cached_info

        import yt_dlp

        with yt_dlp.YoutubeDL(self._options()) as ydl:
            return self._extract(ydl, video_id)

    # -- internals ---------------------------------------------------------

    def _options(self) -> dict[str, Any]:
        return {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "logger": _SilentLogger(),
            # We read caption tracks ourselves; yt-dlp must not write files.
            "writesubtitles": False,
            "writeautomaticsub": False,
        }

    def _extract(self, ydl: Any, video_id: str) -> dict[str, Any]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - classified below
            raise _classify(exc) from exc
        if not isinstance(info, dict):
            raise TransientError(f"yt-dlp returned no metadata for {video_id}")
        self._cached_id = video_id
        self._cached_info = info
        return info

    def _select_track(
        self, info: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Pick (language, kind, format entry). Manual captions win over auto."""
        hint = info.get("language")
        manual = info.get("subtitles") or {}
        automatic = info.get("automatic_captions") or {}

        for kind, table, allow_any in (
            ("manual", manual, True),
            ("auto", automatic, False),
        ):
            language = self._pick_language(table, hint, allow_any=allow_any)
            if language is None:
                continue
            entry = _pick_format(table.get(language) or [])
            if entry is None:
                continue
            return language, kind, entry
        return None

    def _pick_language(
        self, table: dict[str, Any], hint: str | None, *, allow_any: bool
    ) -> str | None:
        if not table:
            return None

        preferences: list[str] = []
        if hint:
            preferences.extend([hint, f"{hint}-orig"])
        preferences.extend(self._languages)

        for preference in preferences:
            if preference in table:
                return preference

        for preference in preferences:
            base = preference.split("-")[0]
            for key in sorted(table):
                if key == base or key.startswith(base + "-"):
                    return key

        # Auto-caption tables also list machine translations into ~200
        # languages. Picking one arbitrarily would be worse than nothing, so
        # only the original track qualifies.
        for key in sorted(table):
            if key.endswith("-orig"):
                return key

        return sorted(table)[0] if allow_any else None


# -- parsing ---------------------------------------------------------------


def _pick_format(entries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [entry for entry in entries if entry.get("url")]
    if not usable:
        return None
    for ext in _PREFERRED_EXTS:
        for entry in usable:
            if entry.get("ext") == ext:
                return entry
    return usable[0]


def _parse_captions(ext: str, raw: bytes) -> list[TranscriptLine]:
    text = raw.decode("utf-8", errors="replace")
    if ext == "json3":
        lines = _parse_json3(text)
    elif ext in ("srv1", "srv2", "srv3", "ttml", "xml"):
        lines = _parse_xml(text)
    elif ext == "vtt":
        lines = _parse_vtt(text)
    else:
        lines = _parse_json3(text) or _parse_xml(text) or _parse_vtt(text)
    return _tidy(lines)


def _parse_json3(text: str) -> list[TranscriptLine]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    lines: list[TranscriptLine] = []
    for event in data.get("events") or []:
        # aAppend marks the rollup repeat of the previous caption.
        if event.get("aAppend"):
            continue
        segments = event.get("segs")
        if not segments:
            continue
        body = _clean("".join(str(segment.get("utf8", "")) for segment in segments))
        if not body:
            continue
        start = int(event.get("tStartMs") or 0)
        duration = event.get("dDurationMs")
        end = start + int(duration) if duration is not None else start
        lines.append(TranscriptLine(start_ms=start, end_ms=end, text=body))
    return lines


def _parse_xml(text: str) -> list[TranscriptLine]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []

    lines: list[TranscriptLine] = []

    # srv1: <transcript><text start="1.5" dur="3.2">...
    for node in root.iter():
        if _local_name(node.tag) != "text":
            continue
        body = _clean(_node_text(node))
        if not body:
            continue
        start = int(float(node.get("start") or 0.0) * 1000)
        duration = node.get("dur")
        end = start + int(float(duration) * 1000) if duration else start
        lines.append(TranscriptLine(start_ms=start, end_ms=end, text=body))
    if lines:
        return lines

    # srv3 / ttml: <p t="1500" d="3200"> or <p begin="..." end="...">
    for node in root.iter():
        if _local_name(node.tag) != "p":
            continue
        body = _clean(_node_text(node))
        if not body:
            continue
        if node.get("t") is not None:
            start = int(node.get("t") or 0)
            duration = node.get("d")
            end = start + int(duration) if duration else start
        elif node.get("begin") is not None:
            start = _parse_clock(node.get("begin") or "")
            end = _parse_clock(node.get("end") or "") or start
        else:
            continue
        lines.append(TranscriptLine(start_ms=start, end_ms=end, text=body))
    return lines


def _local_name(tag: object) -> str:
    """``{http://www.w3.org/ns/ttml}p`` -> ``p``. TTML is namespaced; srv1 is not."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _node_text(node: ElementTree.Element) -> str:
    """Flatten an element, turning ``<br/>`` into a space.

    ``itertext`` would glue the words on either side of a line break together.
    """
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        if _local_name(child.tag) == "br":
            parts.append(" ")
        else:
            parts.append(_node_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _parse_vtt(text: str) -> list[TranscriptLine]:
    lines: list[TranscriptLine] = []
    current: tuple[int, int] | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        body = _clean(" ".join(buffer))
        if body:
            lines.append(TranscriptLine(start_ms=current[0], end_ms=current[1], text=body))

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        match = _VTT_CUE_RE.match(stripped)
        if match:
            flush()
            current = (
                _parse_clock(match.group("start")),
                _parse_clock(match.group("end")),
            )
            buffer = []
            continue
        if not stripped:
            flush()
            current = None
            buffer = []
            continue
        if current is not None:
            buffer.append(stripped)
    flush()
    return lines


def _parse_clock(value: str) -> int:
    """'00:01:02.500' or '01:02.500' or '1.5s' -> milliseconds."""
    value = value.strip().replace(",", ".").rstrip("s")
    if not value:
        return 0
    parts = value.split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        return 0
    return int(round(seconds * 1000))


def _clean(text: str) -> str:
    text = html.unescape(text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)  # captions are frequently double-encoded
    return _WS_RE.sub(" ", text).strip()


def _tidy(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    """Drop rollup repeats and give every line a sane end time."""
    result: list[TranscriptLine] = []
    for line in lines:
        if result and result[-1].text == line.text:
            # Auto captions repeat the previous cue while it rolls up.
            result[-1].end_ms = max(result[-1].end_ms, line.end_ms)
            continue
        result.append(line)

    for index, line in enumerate(result):
        if line.end_ms > line.start_ms:
            continue
        following = result[index + 1].start_ms if index + 1 < len(result) else None
        line.end_ms = following if following and following > line.start_ms else line.start_ms
    return result


def _chapters(info: dict[str, Any]) -> list[dict]:
    """Author-written structure. Worth more than anything we could infer."""
    chapters: list[dict] = []
    for chapter in info.get("chapters") or []:
        start = chapter.get("start_time")
        if start is None:
            continue
        chapters.append(
            {
                "start_ms": int(float(start) * 1000),
                "title": str(chapter.get("title") or "").strip(),
            }
        )
    return chapters


def _classify(exc: Exception) -> PermanentError | TransientError:
    message = " ".join(str(arg) for arg in exc.args) or str(exc)
    lowered = message.lower()
    for pattern in _PERMANENT_PATTERNS:
        if pattern in lowered:
            return PermanentError(_summarise(message))
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in lowered:
            return TransientError(_summarise(message))
    return TransientError(_summarise(message))


def _summarise(message: str) -> str:
    """One short line — this ends up in terse CLI output."""
    cleaned = _WS_RE.sub(" ", message).strip()
    cleaned = re.sub(r"^ERROR:\s*", "", cleaned)
    cleaned = re.sub(r"^\[[^\]]+\]\s*[\w-]{11}:\s*", "", cleaned)
    # yt-dlp appends the wrapped cause and a "confirm you're a bot" nudge;
    # neither adds anything a one-line report can use.
    cleaned = re.sub(r"\s*\(caused by .*", "", cleaned)
    cleaned = re.sub(r"\s*;\s*(please report|confirm).*", "", cleaned, flags=re.IGNORECASE)
    return cleaned[:100].strip()
