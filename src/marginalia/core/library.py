"""The on-disk library layout.

::

    <library_root>/
    ├── ledger.db
    ├── answers/                  one file per question `marg ask` answered
    ├── exports/<run_id>/         a readable view of one discovery run
    ├── logs/                     one file per day
    └── videos/
        └── <video_id>/
            ├── transcript.txt
            ├── meta.json
            └── note.md

Every library path is built here. No other module constructs one.

The three directories beside ``videos/`` are all observability rather than
material: an answer, a run and a log are each something the system did that was
previously visible only until the terminal scrolled. None of them is a source of
truth — the ledger stays that — and none is created until something writes into
it, so a library that never asked a question has no ``answers/``.

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

from ..contracts import TranscriptLine
from .ids import is_video_id

log = logging.getLogger(__name__)

__all__ = ["Library", "format_timestamp", "format_duration", "slugify"]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_RUN_ID_RE = re.compile(r"[0-9a-f]{6,32}")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Long enough to recognise the question at a glance, short enough that the date
# prefix and a collision counter still fit inside every filesystem limit anyone
# is going to hit.
_SLUG_LIMIT = 60
# A counter, not a timestamp. Two answers to the same question on the same day
# is the case this exists for, and `-2` says which came second where a clock
# reading says only that a clock was read.
_MAX_COLLISIONS = 999


def format_timestamp(milliseconds: int) -> str:
    """``93500`` -> ``00:01:33.500``."""
    total = max(0, int(milliseconds))
    seconds, millis = divmod(total, 1000)
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}.{millis:03d}"


def slugify(text: str, limit: int = _SLUG_LIMIT) -> str:
    """A question as a filename component. Never empty, never a path.

    ASCII letters and digits survive; everything else becomes a single hyphen.
    That is deliberately brutal — a question may contain quotes, slashes, colons
    and non-Latin script, and a filename that round-trips all of those is a
    filename some tool later cannot open. The question itself is stored verbatim
    inside the file, so the slug only has to be recognisable.
    """
    slug = _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    if len(slug) > limit:
        # Cut at a word boundary when there is one near the limit, so the name
        # ends on a word rather than mid-syllable.
        slug = slug[:limit].rsplit("-", 1)[0] or slug[:limit]
    return slug or "question"


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

    @property
    def answers_dir(self) -> Path:
        return self.root / "answers"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def export_dir(self, run_id: str) -> Path:
        """One run's folder. ``run_id`` is checked because it arrives from the
        command line and is about to become a directory name."""
        if not _RUN_ID_RE.fullmatch(run_id or ""):
            raise ValueError(f"not a run ID: {run_id!r}")
        return self.exports_dir / run_id

    def log_path(self, day: str) -> Path:
        """One file per day, named for the UTC date the run started."""
        if not _DAY_RE.fullmatch(day or ""):
            raise ValueError(f"not a YYYY-MM-DD date: {day!r}")
        return self.logs_dir / f"marg-{day}.log"

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

    def write_answer(self, question: str, text: str, *, day: str) -> Path:
        """Write one answer under ``answers/``. Never overwrites; returns the path.

        ``<day>-<slug of the question>.md``, and on collision ``-2``, ``-3``.
        A counter rather than a clock: two answers to the same question on the
        same day is exactly the case worth telling apart, and "which came
        second" is the useful distinction where a time-of-day suffix says only
        that a clock was read.

        The no-overwrite rule is the same one ``write_transcript`` follows and
        for a related reason. An answer is cheap to regenerate in principle and
        expensive in practice — it costs a model call over the whole library —
        and the earlier answer to the same question is the thing you compare
        against when a prompt change is supposed to have improved something.
        """
        if not _DAY_RE.fullmatch(day or ""):
            raise ValueError(f"not a YYYY-MM-DD date: {day!r}")
        directory = self.answers_dir
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{day}-{slugify(question)}"
        for attempt in range(1, _MAX_COLLISIONS + 1):
            suffix = "" if attempt == 1 else f"-{attempt}"
            path = directory / f"{stem}{suffix}.md"
            if not path.exists():
                _atomic_write(path, text if text.endswith("\n") else text + "\n")
                return path
        raise FileExistsError(
            f"{_MAX_COLLISIONS} answers already exist for {stem!r}; refusing to overwrite one"
        )

    def write_export(self, run_id: str, name: str, text: str) -> Path:
        """One file inside ``exports/<run_id>/``. Overwrites freely.

        The opposite of ``write_answer``, because an export is not a record of
        anything: it is a rendering of the ledger, regenerable exactly, and a
        second export of the same run should replace the first rather than
        accumulate near-identical folders. The ledger stays the source of truth
        and this stays a view of it.
        """
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise ValueError(f"not an export file name: {name!r}")
        directory = self.export_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        _atomic_write(path, text if text.endswith("\n") else text + "\n")
        return path

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
