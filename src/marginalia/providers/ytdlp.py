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
from typing import Any, Iterable, NamedTuple, Sequence
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

# Error classification. yt-dlp messages are long, wrap their cause, and change
# wording between releases; a truncated one is a severed sentence, not a
# summary. Each known shape maps to a short stable label, because the report
# line is parsed by a skill. The full text goes to the DEBUG log.
#
# Order matters: first match wins, so specific patterns precede general ones.
# `permanent` means "do not retry" — deleted, private, blocked.


class _Shape(NamedTuple):
    pattern: str
    label: str
    permanent: bool


_ERROR_SHAPES: tuple[_Shape, ...] = (
    # -- permanent -----------------------------------------------------------
    _Shape("this video is private", "private", True),
    _Shape("private video", "private", True),
    _Shape("sign in if you've been granted access", "private", True),
    # Matches both "is not available in your country" and the wording YouTube
    # actually uses: "has not made this video available in your country".
    _Shape("available in your country", "region blocked", True),
    _Shape("blocked it in your country", "region blocked", True),
    _Shape("blocked it on copyright grounds", "region blocked", True),
    _Shape("who has blocked it", "region blocked", True),
    _Shape("members-only", "members only", True),
    _Shape("available to this channel's members", "members only", True),
    _Shape("join this channel", "members only", True),
    _Shape("has been removed by the uploader", "removed", True),
    _Shape("removed for violating", "removed", True),
    _Shape("account associated with this video has been terminated", "account terminated", True),
    _Shape("sign in to confirm your age", "age restricted", True),
    _Shape("age-restricted", "age restricted", True),
    _Shape("inappropriate for some users", "age restricted", True),
    _Shape("this live event has ended", "unavailable", True),
    _Shape("no longer available", "unavailable", True),
    _Shape("video unavailable", "unavailable", True),
    _Shape("incomplete youtube id", "not found", True),
    _Shape("is not a valid url", "not found", True),
    _Shape("does not exist", "not found", True),
    # -- transient -----------------------------------------------------------
    _Shape("http error 429", "rate limited", False),
    _Shape("too many requests", "rate limited", False),
    _Shape("not a bot", "bot check", False),
    _Shape("actively refused", "connection refused", False),
    _Shape("connection refused", "connection refused", False),
    _Shape("winerror 10061", "connection refused", False),
    _Shape("timed out", "timeout", False),
    _Shape("timeout", "timeout", False),
    _Shape("temporary failure in name resolution", "dns failure", False),
    _Shape("name or service not known", "dns failure", False),
    _Shape("getaddrinfo", "dns failure", False),
    _Shape("certificate verify failed", "tls error", False),
    _Shape("ssl", "tls error", False),
    _Shape("http error 5", "server error", False),
    _Shape("unable to download webpage", "download failed", False),
    _Shape("unable to download api page", "download failed", False),
    _Shape("read operation", "download failed", False),
    _Shape("failed to extract", "extraction failed", False),
    _Shape("unable to extract", "extraction failed", False),
    _Shape("connection", "connection failed", False),
    _Shape("network", "connection failed", False),
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

            candidates = self._candidate_tracks(info)
            if not candidates:
                log.info("%s: no caption tracks available", video_id)
                return None

            # The preferred track first; the next only if that one parsed to
            # nothing. A manual track that parsed to zero lines used to send the
            # video to `needs_asr` — a queue with no worker — while a perfectly
            # good automatic track sat beside it (R1 F2).
            lines = []
            for language, kind, entry in candidates:
                try:
                    raw = ydl.urlopen(entry["url"]).read()
                except Exception as exc:  # noqa: BLE001 - classified below
                    raise _classify(exc) from exc
                lines = _parse_captions(entry.get("ext", ""), raw)
                if lines:
                    break
                log.info(
                    "%s: caption track %s/%s parsed to zero lines", video_id, kind, language
                )

        if not lines:
            return None

        return TranscriptResult(
            lines=lines,
            language=language,
            source="captions",
            caption_kind=kind,
            title=str(info.get("title") or ""),
            channel=str(info.get("channel") or info.get("uploader") or ""),
            channel_id=str(info.get("channel_id") or info.get("uploader_id") or ""),
            duration_s=int(info.get("duration") or 0),
            upload_date=info.get("upload_date") or None,
            chapters=_chapters(info),
        )

    def fetch_metadata(self, video_id: str) -> dict[str, Any] | None:
        """Metadata only. Reuses the info dict from the last ``fetch`` if any."""
        # This cache assumes one provider instance serves one video at a time:
        # true while the CLI is single-shot and single-threaded. Revisit in M3,
        # when provider fallbacks change the call pattern. (deferred, M1 review)
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
        candidates = self._candidate_tracks(info)
        return candidates[0] if candidates else None

    def _candidate_tracks(self, info: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        """Every track worth trying, best first: the chosen manual track, then
        the chosen automatic one. The preference order is unchanged; what is
        new is that a second choice exists when the first parses to nothing."""
        hint = info.get("language")
        manual = _languages_only(info.get("subtitles") or {})
        automatic = _languages_only(info.get("automatic_captions") or {})

        found: list[tuple[str, str, dict[str, Any]]] = []
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
            found.append((language, kind, entry))
        return found

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


# Keys yt-dlp lists beside the caption languages that are not captions. A
# livestream's chat replay sits in `subtitles` as `live_chat`, and as the only
# "manual" entry it beat a real automatic track, then parsed to nothing (R1 F2).
_NOT_A_LANGUAGE = frozenset({"live_chat", "rechat"})


def _languages_only(table: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in table.items() if key not in _NOT_A_LANGUAGE}


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
    """Map a provider exception to a short, stable label.

    The label — not the original message — becomes the CLI's report line, so
    it must stay short and must not change wording when yt-dlp does. The full
    text is logged at DEBUG for whoever is actually debugging.
    """
    message = _flatten(exc)
    log.debug("provider error (%s): %s", type(exc).__name__, message)

    lowered = message.lower()
    for shape in _ERROR_SHAPES:
        if shape.pattern in lowered:
            error = PermanentError if shape.permanent else TransientError
            return error(shape.label)

    # Unrecognised. Default to transient: never retrying something temporary
    # is a silent permanent loss, while retrying something permanent costs one
    # wasted request. Name the exception class so the shape can be added here.
    return TransientError(f"unclassified ({type(exc).__name__})")


def _flatten(exc: Exception) -> str:
    """The exception's full text, including any wrapped cause."""
    parts = [" ".join(str(arg) for arg in exc.args) or str(exc)]
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        parts.append(str(cause))
    return _WS_RE.sub(" ", " ".join(parts)).strip()
