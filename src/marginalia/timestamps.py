"""Snap cited timestamps in notes onto the transcript line they refer to.

Measured across two providers on the same prompt: 22 of 36 cited timestamps ran
late under kimi-k3, 19 of 30 under deepseek-v4-flash. Same prompt, different
models, same failure, and v3 already tried to fix it in prose. A model asked to
cite an exact number lands near it, not on it — so this is code, not a prompt.

The correction is small and consistent (a median of one to three seconds), which
is exactly what makes it worth fixing here: a citation that is *nearly* right is
the kind that survives review and then sends a reader to the wrong moment.

Three properties this file is built around:

* **No model calls.** It runs over notes that already exist, costs nothing, and
  works with the network down.
* **Idempotent.** Snapping computes the target from the transcript, never from
  the current value, so a second run finds the same line and writes the same
  number. The only thing the current value is used for is disambiguating a
  phrase that occurs more than once.
* **A wrong snap is worse than a late original.** A moved timestamp looks
  authoritative. Every match must clear a threshold, and anything that does not
  is left alone and reported as left alone — never guessed at.

Parsing and matching are pure functions over strings at the top; the walk over
the library is at the bottom. Same split as ``judge.py`` and ``note.py``, for
the same reason: the note format will change and these must keep telling the
truth about it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .library import Library

log = logging.getLogger(__name__)

__all__ = [
    "Change",
    "SnapResult",
    "TranscriptIndex",
    "build_index",
    "parse_note_timestamps",
    "snap_text",
    "snap_video",
    "snap_library",
    "REASON_NO_ANCHOR",
    "REASON_NO_MATCH",
    "REASON_ALREADY",
]

# A cue line: "[00:01:33.500 --> 00:01:36.000] some words".
_CUE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->[^\]]*\]\s*(.*)$")

# A cited timestamp inside a note: 9:33, 00:09:33, 00:00:17.030.
# Guarded on both sides so "$4", "1.25x" and "95/5" cannot match, and so the
# hour group of an already-full timestamp is not mistaken for minutes.
_TS_RE = re.compile(r"(?<![\d.:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?:\.(\d{1,3}))?(?![\d:])")

# Inference the note-writer added. Never transcript text, so never an anchor —
# matching on it would locate the note's own reasoning inside the video.
_NOTE_MARKER_RE = re.compile(r"\[note:[^\]]*\]?", re.IGNORECASE | re.DOTALL)

_QUOTED_RE = re.compile(r"[\"“]([^\"”]{6,})[\"”]")

# Unicode, not [a-z0-9]. The library holds Arabic, Persian and Spanish videos,
# and an ASCII-only pattern extracts nothing from an Arabic quote — so the
# stray Latin words around it ("Arabic", the section label) became the anchors
# and one citation snapped from 3:28 to 00:00:00. A word class that cannot see
# the quote does not fail loudly; it matches something else.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# A word occurring more than this often in one transcript carries no location
# information. The audit's first matcher anchored on common words and reported
# ten-minute errors that hand-checking disproved; this is that lesson, encoded.
_RARE_MAX = 3

# How many transcript words a quote is allowed to span while still counting as
# one match. Captions break mid-sentence, so a quote of a dozen words routinely
# straddles four or five cue lines.
_WINDOW = 40

# Distinct rare anchors required before a snap is allowed. Quoted text is the
# note reproducing the video, so two is enough. An unquoted description is the
# note *paraphrasing* the video, where a coincidence is far likelier.
_NEED_QUOTED = 2
_NEED_DESCRIBED = 3

# How far from the original citation a match may be found, by kind. These are
# search bounds, not filters: the best match *within* the window wins, so a
# description is refined against nearby lines rather than relocated across the
# video.
#
# The numbers come from the measured error, not from taste. Citations run late
# by a median of one to three seconds and at most about six, so a candidate two
# minutes away is not a late citation — it is a different passage that happened
# to share vocabulary. A first version of this file allowed 180s for
# descriptions and duly produced snaps of -94s, -157s and -174s: confident,
# authoritative, and wrong. That is the exact failure this module exists to
# avoid, so the bound is now tight enough that a snap can only ever be a
# correction of the known defect.
#
# Quotes get more room because they are self-identifying — the note reproduces
# the video's own words, so a match is evidence rather than coincidence — but
# they are still bounded, because a phrase repeated across a long video would
# otherwise be free to snap anywhere it recurred.
_QUOTED_DRIFT_S = 60
_DESCRIBED_DRIFT_S = 20

# Passes before a note is declared settled. Two is the most any real note has
# needed; the rest is headroom.
_MAX_PASSES = 5

REASON_NO_ANCHOR = "no distinctive words to match on"
REASON_NO_MATCH = "no confident transcript match"
REASON_ALREADY = "already on the line"

SECTION_VERBATIM = "Verbatim"


# -- the transcript side -----------------------------------------------------


@dataclass(frozen=True)
class TranscriptIndex:
    """Transcript words in order, each with the start of the cue it came from.

    ``freq`` is what makes matching safe: it is per-transcript, so "cache" is a
    common word in a caching video and a rare one in a SQLite video, and the
    anchors adapt without anyone maintaining a stop-word list.
    """

    words: tuple[str, ...]
    times_ms: tuple[int, ...]
    freq: dict[str, int]
    positions: dict[str, tuple[int, ...]]

    def __bool__(self) -> bool:
        return bool(self.words)


def build_index(transcript: str) -> TranscriptIndex:
    words: list[str] = []
    times: list[int] = []
    for line in transcript.splitlines():
        match = _CUE_RE.match(line.strip())
        if match is None:
            continue
        hours, minutes, seconds, millis, text = match.groups()
        start = ((int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000) + int(millis)
        for word in _WORD_RE.findall(text.lower()):
            words.append(word)
            times.append(start)

    freq: dict[str, int] = {}
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(words):
        freq[word] = freq.get(word, 0) + 1
        positions.setdefault(word, []).append(index)

    return TranscriptIndex(
        words=tuple(words),
        times_ms=tuple(times),
        freq=freq,
        positions={word: tuple(where) for word, where in positions.items()},
    )


def anchors_for(text: str, index: TranscriptIndex) -> list[str]:
    """The words in ``text`` that could identify a place in this transcript."""
    seen: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        count = index.freq.get(word, 0)
        if 0 < count <= _RARE_MAX and word not in seen:
            seen.append(word)
    return seen


def locate(
    anchors: Sequence[str],
    index: TranscriptIndex,
    *,
    need: int,
    near_ms: int | None,
    drift_ms: int | None = None,
) -> tuple[int, int] | None:
    """Where ``anchors`` occur together. Returns ``(start_ms, matched)``.

    Scans only the positions where an anchor actually occurs, so cost is set by
    how rare the anchors are rather than by transcript length.

    ``drift_ms`` bounds the search around ``near_ms`` rather than filtering
    afterwards, which is the difference between "find the best match near where
    the note points" and "find the best match anywhere, then object". Among
    equally good windows the nearest wins: a phrase repeated twice should snap
    to the occurrence the note already pointed at, which is also what stops a
    second run from moving it somewhere else.
    """
    wanted = set(anchors)
    if len(wanted) < need:
        return None

    starts = sorted({p for word in wanted for p in index.positions.get(word, ())})
    best: tuple[int, int, int] | None = None  # (matched, -distance, start_ms)

    for start in starts:
        window = index.words[start : start + _WINDOW]
        matched = sum(1 for word in wanted if word in window)
        if matched < need:
            continue
        # The citation should point at the first anchor, not at the window edge.
        first = next(
            (start + offset for offset, word in enumerate(window) if word in wanted),
            start,
        )
        start_ms = index.times_ms[first]
        distance = 0 if near_ms is None else abs(start_ms - near_ms)
        if drift_ms is not None and near_ms is not None and distance > drift_ms:
            continue
        # Coverage first, proximity only to separate genuinely distinct
        # occurrences, then earliest to make the order total.
        #
        # Ranking by proximity instead looks safer and is useless: a late
        # citation is by definition nearest to itself, so the window starting
        # where the note already points always wins and nothing is ever
        # corrected. That version "snapped" 414 citations and moved the audit
        # from 19 late to 16.
        #
        # Coverage is what actually locates a quote: the window overlapping
        # most of it starts where the quote starts, and `first` below then
        # points at that word rather than at the window edge. Relocation across
        # the video is prevented by `drift_ms`, which is a bound on the search,
        # not a preference inside it — the two jobs kept separate.
        candidate = (matched, -distance, -start_ms)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        return None
    return -best[2], best[1]


# -- the note side -----------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One timestamp token in a note, and where it sits in the text."""

    start: int
    end: int
    raw: str
    ms: int
    parts: int
    has_millis: bool


def parse_note_timestamps(text: str) -> list[Citation]:
    out: list[Citation] = []
    for match in _TS_RE.finditer(text):
        first, second, third, millis = match.groups()
        if third is None:
            total = int(first) * 60 + int(second)
            parts = 2
        else:
            total = int(first) * 3600 + int(second) * 60 + int(third)
            parts = 3
        ms = total * 1000 + (int(millis.ljust(3, "0")) if millis else 0)
        out.append(
            Citation(
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                ms=ms,
                parts=parts,
                has_millis=millis is not None,
            )
        )
    return out


def render_timestamp(ms: int, *, parts: int, has_millis: bool) -> str:
    """Format ``ms`` the way the citation being replaced was already written.

    A note that cites 9:33 should keep citing m:ss, not switch to 00:09:33 —
    the rewrite is meant to be invisible except for the number.
    """
    total, millis = divmod(max(0, ms), 1000)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if parts == 3:
        body = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        body = f"{hours * 60 + minutes:02d}:{seconds:02d}"
    return f"{body}.{millis:03d}" if has_millis else body


def anchor_text(line: str) -> tuple[str, bool]:
    """The part of a note line that claims to come from the video.

    Returns the text and whether it was quoted. Quoted text is the note
    reproducing the video and can be trusted to a lower threshold; everything
    else is the note describing it. ``[note: ...]`` spans are stripped either
    way — that is the note-writer's own reasoning, and matching on it would
    look for the note's inference inside the transcript.
    """
    body = _NOTE_MARKER_RE.sub(" ", line)
    quoted = _QUOTED_RE.findall(body)
    if quoted:
        return " ".join(quoted), True
    body = _TS_RE.sub(" ", body)
    body = re.sub(r"[*_`#\[\]()>-]", " ", body)
    return body, False


# -- results -----------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    section: str
    before: str
    after: str
    delta_ms: int
    preview: str
    # Which line of the note moved. Only used to recognise the same citation
    # across passes, so a two-step move is reported as one net correction.
    line: int = -1


@dataclass
class SnapResult:
    video_id: str
    changed: bool = False
    snapped: list[Change] = field(default_factory=list)
    left: list[tuple[str, str, str]] = field(default_factory=list)  # section, raw, reason
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.snapped) + len(self.left)


def snap_text(note: str, index: TranscriptIndex) -> tuple[str, list[Change], list[tuple[str, str, str]]]:
    """Rewrite the first citation on every line that can be located.

    Only the first: a line citing two separate moments needs two anchors to tell
    them apart, and the note does not provide them. Rewriting one and guessing
    the other is precisely the authoritative-looking error this must not make.
    """
    lines = note.splitlines(keepends=True)
    section = ""
    changes: list[Change] = []
    left: list[tuple[str, str, str]] = []

    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().strip("*").strip()
            continue

        citations = parse_note_timestamps(line)
        if not citations:
            continue

        target = citations[0]
        text, quoted = anchor_text(line)
        anchors = anchors_for(text, index)
        label = section or "?"
        preview = " ".join(stripped.split())[:70]

        # A quote only *locates* a citation inside Verbatim, where the line is
        # the quote and its timestamp and nothing else. Elsewhere a quote is
        # illustrative: a key moment reading `00:00:45 - the core correction:
        # ... "this is completely wrong"` is pointing at the passage, while the
        # phrase it borrows may be said two minutes later. Trusting the quote
        # there moved that citation 94 seconds in the wrong direction. So the
        # quote is still used as anchor material — it is the best text on the
        # line — but only Verbatim earns the loose bound.
        trusted = quoted and label == SECTION_VERBATIM
        need = _NEED_QUOTED if trusted else _NEED_DESCRIBED
        drift = (_QUOTED_DRIFT_S if trusted else _DESCRIBED_DRIFT_S) * 1000

        if len(anchors) < need:
            left.append((label, target.raw, REASON_NO_ANCHOR))
            continue

        found = locate(anchors, index, need=need, near_ms=target.ms, drift_ms=drift)
        if found is None:
            left.append((label, target.raw, REASON_NO_MATCH))
            continue

        start_ms, _matched = found
        replacement = render_timestamp(
            start_ms, parts=target.parts, has_millis=target.has_millis
        )
        if replacement == target.raw:
            left.append((label, target.raw, REASON_ALREADY))
            continue

        lines[number] = line[: target.start] + replacement + line[target.end :]
        changes.append(
            Change(
                section=label,
                before=target.raw,
                after=replacement,
                delta_ms=target.ms - start_ms,
                preview=preview,
                line=number,
            )
        )

    return "".join(lines), changes, left


# -- the walk ----------------------------------------------------------------


def _coalesce(changes: Sequence[Change]) -> list[Change]:
    """One citation moved over several passes is one correction, not several."""
    merged: list[Change] = []
    by_line: dict[int, int] = {}
    for change in changes:
        seen = by_line.get(change.line)
        if seen is not None and merged[seen].after == change.before:
            first = merged[seen]
            merged[seen] = Change(
                section=first.section,
                before=first.before,
                after=change.after,
                delta_ms=first.delta_ms + change.delta_ms,
                preview=first.preview,
                line=first.line,
            )
            continue
        by_line[change.line] = len(merged)
        merged.append(change)
    return merged


def snap_video(library: Library, video_id: str, *, dry_run: bool = False) -> SnapResult:
    """Snap one note to a fixpoint. Reads the transcript, never writes it.

    Iterated rather than single-pass because a snap can cascade: the search is
    bounded relative to the citation's current value, so moving it can bring a
    better match inside the window that was outside it before. One pass left
    the library needing a second run to settle, which is the same thing as not
    being idempotent. Converging here means a re-run is always a no-op.

    The loop terminates on its own — once a citation sits on a cue start,
    recomputing it yields the same value — but it is capped anyway, because a
    bound that depends on a convergence argument is not a bound.
    """
    result = SnapResult(video_id=video_id)

    note = library.read_note(video_id)
    if note is None:
        result.error = "no note"
        return result
    if not library.has_transcript(video_id):
        result.error = "no transcript"
        return result

    index = build_index(library.read_transcript(video_id))
    if not index:
        result.error = "transcript has no cue lines"
        return result

    updated = note
    every: list[Change] = []
    left: list[tuple[str, str, str]] = []
    for attempt in range(_MAX_PASSES):
        updated, changes, left = snap_text(updated, index)
        every.extend(changes)
        if not changes:
            break
    else:
        log.warning(
            "snap: %s still changing after %d passes; leaving it where it settled",
            video_id,
            _MAX_PASSES,
        )

    changes = _coalesce(every)
    result.snapped = changes
    result.left = left

    if changes and not dry_run:
        # Through the library, which is the CLI's own write path. Nothing here
        # touches a file directly: rule 2 is enforced by a hook that matches
        # only Write/Edit, so a script walking past it would not be blocked —
        # which is the reason to be careful, not a reason to relax.
        library.write_note(video_id, updated)
    result.changed = bool(changes)
    return result


def snap_library(
    library: Library, video_ids: Iterable[str] = (), *, dry_run: bool = False
) -> list[SnapResult]:
    ids = list(video_ids)
    if not ids:
        ids = sorted(
            path.name
            for path in library.videos_dir.iterdir()
            if path.is_dir() and (path / "note.md").is_file()
        ) if library.videos_dir.is_dir() else []
    return [snap_video(library, video_id, dry_run=dry_run) for video_id in ids]
