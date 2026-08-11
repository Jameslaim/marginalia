"""The on-disk library layout.

::

    <library_root>/
    ├── ledger.db
    └── videos/
        └── <video_id>/
            ├── transcript.txt
            └── meta.json

Every library path is built here. No other module constructs one.

``transcript.txt`` is immutable: written once, never modified, never deleted.
It is the one artifact that cannot be cheaply recreated, because acquisition is
the fragile, blockable step. Everything else is regenerable from it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Sequence

from .ids import is_video_id
from .providers.base import TranscriptLine

log = logging.getLogger(__name__)

__all__ = ["Library", "format_timestamp", "format_duration"]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def format_timestamp(milliseconds: int) -> str:
    """``93500`` -> ``00:01:33.500``."""
    total = max(0, int(milliseconds))
    seconds, millis = divmod(total, 1000)
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}.{millis:03d}"


def format_duration(seconds: int | None) -> str:
    """Compact and fixed-vocabulary: ``45s``, ``18m``, ``1h02m``, or ``?``."""
    if not seconds or seconds < 0:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h{remainder:02d}m"


class Library:
    """Read and write the library folder. Constructs every path in the system."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    # -- paths -------------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.db"

    @property
    def videos_dir(self) -> Path:
        return self.root / "videos"

    def video_dir(self, video_id: str) -> Path:
        if not is_video_id(video_id):
            raise ValueError(f"not a video ID: {video_id!r}")
        return self.videos_dir / video_id

    def transcript_path(self, video_id: str) -> Path:
        return self.video_dir(video_id) / "transcript.txt"

    def meta_path(self, video_id: str) -> Path:
        return self.video_dir(video_id) / "meta.json"

    def note_path(self, video_id: str) -> Path:
        return self.video_dir(video_id) / "note.md"

    # -- lifecycle ---------------------------------------------------------

    def ensure(self) -> None:
        """Create the library on first run."""
        created = not self.root.exists()
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        if created:
            log.info("created library at %s", self.root)

    def has_transcript(self, video_id: str) -> bool:
        return self.transcript_path(video_id).is_file()

    def has_note(self, video_id: str) -> bool:
        return self.note_path(video_id).is_file()

    # -- writes ------------------------------------------------------------

    def write_transcript(self, video_id: str, lines: Sequence[TranscriptLine]) -> int:
        """Write ``transcript.txt`` once. Refuses to overwrite an existing one.

        Returns the number of lines written.
        """
        path = self.transcript_path(video_id)
        if path.exists():
            raise FileExistsError(f"transcript.txt already exists for {video_id}; it is immutable")
        path.parent.mkdir(parents=True, exist_ok=True)

        body = "".join(
            f"[{format_timestamp(line.start_ms)} --> {format_timestamp(line.end_ms)}] "
            f"{_one_line(line.text)}\n"
            for line in lines
        )
        _atomic_write(path, body)
        return len(lines)

    def write_meta(self, video_id: str, meta: dict[str, Any]) -> None:
        path = self.meta_path(video_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    def write_note(self, video_id: str, text: str) -> int:
        """Write ``note.md``, replacing any previous one. Returns bytes written.

        The exact opposite rule to ``write_transcript``: a note is disposable
        and overwriting is the normal case, because a better prompt is supposed
        to be able to rewrite the whole library. The write is atomic, so a crash
        mid-write leaves either the old note or none — never half of one, which
        would silently fail every required-section check that follows.
        """
        path = self.note_path(video_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = text if text.endswith("\n") else text + "\n"
        _atomic_write(path, body)
        return len(body.encode("utf-8"))

    def read_note(self, video_id: str) -> str | None:
        """Callers MUST also call ``Ledger.touch_accessed(video_id)``."""
        path = self.note_path(video_id)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def read_meta(self, video_id: str) -> dict[str, Any] | None:
        path = self.meta_path(video_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read %s", path)
            return None

    # -- reads -------------------------------------------------------------

    def read_transcript(self, video_id: str) -> str:
        """The only transcript read path.

        Callers MUST also call ``Ledger.touch_accessed(video_id)``; the
        deletion policy that arrives later depends on it and it cannot be
        reconstructed retroactively.
        """
        return self.transcript_path(video_id).read_text(encoding="utf-8")


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _one_line(text: str) -> str:
    """A caption line must never contain a newline: the format is line-based."""
    return _WS_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
