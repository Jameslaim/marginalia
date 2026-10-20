"""A `[note: ...]` marker must never cut a quotation.

A note quotes two kinds of string: words the video said (or its captions
garbled), and words of the writer's own. A marker says "the writer's, not the
video's". Measured on 37 stored notes: 47 markers in 22 notes opened or closed
inside a quoted string, cutting 49 quoted units — 35 of them in Wrong or shaky.
About half put a caption string the note quotes (what the transcript literally
says) inside the marker, so the transcript's own words read as the writer's
guess; the rest left one quote mark on the wrong side of the writer's
correction. The marking pass returns spans and code wraps them exactly (rule
32), and nothing made a span hold whole quoted units; the writer's own markers
showed the same shape. So this runs on the final text, whoever placed the
marker (rule 25).

**Each straddle is resolved one way or the other, and counted** (rule 14):

* **outside** — the quoted unit is moved out of the marker, because the
  transcript holds it at the unit's own moment;
* **inside** — the marker grows to hold the whole unit, when that evidence is
  missing or weak;
* **refused** — a marker whose content was only the quoted unit, once the unit
  is put outside, is dropped: what is left is record.

**Which side needs evidence, and weak evidence keeps the unit inside.**
Slightly discounting record is cheap; leaving the writer's words unmarked is the
expensive error. Evidence is tied to the unit's own timestamp: the unit must
occur in the transcript within `_NEAR_MS` of a time that belongs to it (see
`_own_stamps`). Of the 53 straddled units in the stored notes, 30 occur
somewhere in their transcript and 27 within 30 seconds of a timestamp on their
line; the count does not grow from 30 to 120 seconds. "Somewhere in the
transcript" is not used: a writer's correction can occur elsewhere in its own
transcript. "Any timestamp on the line" was used first, and is not enough: a
line listing five caption garbles, each with its own time and the writer's
correction after it, had one correction said 24 seconds before a *different*
garble's time, and it was put outside — the writer's guess read as caption
record. A single short word is
weak evidence even at the right moment — a common word turns up within any
30 seconds of speech — so a unit must have two words, or one of at least
`_MIN_FLAT` letters, to be put outside.

Pure: strings in, string and counts out. Never changes a word — only where the
marker's brackets sit, or, for a refused marker, that they are gone.
"""

from __future__ import annotations

import re

from ..contracts import MarkerSettle, NoteCitation
from ..quotes import flatten
from .timestamps import parse_note_timestamps

__all__ = ["settle_markers"]

# One level of nested brackets inside a marker, on one line.
_MARKER_RE = re.compile(r"\[note:[ \t]*((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]", re.IGNORECASE)

# The quotation styles a quoted unit may use. Straight and curly double quotes
# are almost all of the library; guillemets, low-high quotes, corner brackets
# and curly single quotes are handled the same way. Straight single quotes are
# not: an apostrophe and a quote mark are the same character, so they are
# counted as unhandled, never paired.
_UNIT_RE = re.compile(
    r"\"[^\"\n]+\"|“[^”\n]+”|«[^»\n]+»|„[^„“”\n]+[“”]|「[^」\n]+」|‘[^’\n]+’"
)
_SINGLE_RE = re.compile(r"(?<![\w'])'[^'\n]{2,}?'(?![\w'])")

# The unit's own moment, measured: see the module docstring.
_NEAR_MS = 30_000
# One word shorter than this is too common to place by itself.
_MIN_FLAT = 6

# Where a clause ends: a semicolon, or a sentence's end. A full stop inside a
# timestamp (`00:01:02.500`) is followed by a digit, so it is not one.
_CLAUSE_END_RE = re.compile(r";|[.!?](?=\s|$)")
# A letter, so two timestamps with only punctuation, digits or spaces between
# them (`(2:50, 3:00)`, `2:50–3:00`) are one group: one moment cited twice.
_LETTER_RE = re.compile(r"[^\W\d_]")

_CUE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->[^\]]*\]\s*(.*)$")


def _cues(transcript: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for line in transcript.splitlines():
        match = _CUE_RE.match(line.strip())
        if match:
            hours, minutes, seconds, millis, text = match.groups()
            out.append(
                ((int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(millis), text)
            )
    return out


class _Line:
    """A line as plain text plus its markers' extents in that text."""

    def __init__(self, line: str) -> None:
        self.plain_parts: list[str] = []
        self.markers: list[list[int]] = []  # [start, end) in plain coordinates
        self.openers: list[str] = []
        at = 0
        plain_len = 0
        for match in _MARKER_RE.finditer(line):
            before = line[at : match.start()]
            self.plain_parts.append(before)
            plain_len += len(before)
            content = match.group(1)
            self.openers.append(line[match.start() : match.start(1)])
            self.markers.append([plain_len, plain_len + len(content)])
            self.plain_parts.append(content)
            plain_len += len(content)
            at = match.end()
        self.plain_parts.append(line[at:])
        self.plain = "".join(self.plain_parts)

    def render(self) -> str:
        inserts: list[tuple[int, int, str]] = []
        for (start, end), opener in zip(self.markers, self.openers):
            inserts.append((end, 0, "]"))
            inserts.append((start, 1, opener))
        text = self.plain
        # Right to left, closers before openers at the same point, so an
        # insertion never shifts a position still to be used.
        for position, _order, token in sorted(inserts, key=lambda item: (-item[0], item[1])):
            text = text[:position] + token + text[position:]
        return text


def _straddles(start: int, end: int, unit_start: int, unit_end: int) -> str | None:
    """How marker [start, end) cuts unit [unit_start, unit_end), if it does."""
    opens_inside = unit_start < start < unit_end
    closes_inside = unit_start < end < unit_end
    if opens_inside and closes_inside:
        return "within"
    if opens_inside:
        return "open"
    if closes_inside:
        return "close"
    return None


def _own_stamps(
    plain: str, unit: tuple[int, int], units: list[tuple[int, int]]
) -> list[int]:
    """The times that belong to one quoted unit, or none.

    Timestamps next to each other form one group (`_LETTER_RE`). A unit owns
    the group nearest to it **within its own clause**: a line that lists
    several quotes, each with its time, separates them with semicolons or
    sentences, and a time across that boundary belongs to another quote. A
    clause with no time of its own borrows the line's only when the line has
    exactly one group — a heading time for the whole line. Otherwise, and when
    two groups are equally near, nothing belongs to the unit and it stays
    inside the marker: the cheap direction. Timestamps and clause ends inside
    a quoted unit are the quote's words, not the note's, and are not read.
    """

    def outside(position: int) -> bool:
        return not any(start <= position < end for start, end in units)

    citations = [c for c in parse_note_timestamps(plain) if outside(c.start)]
    if not citations:
        return []
    groups: list[list[NoteCitation]] = [[citations[0]]]
    for citation in citations[1:]:
        if _LETTER_RE.search(plain[groups[-1][-1].end : citation.start]):
            groups.append([citation])
        else:
            groups[-1].append(citation)
    ends = [m.start() for m in _CLAUSE_END_RE.finditer(plain) if outside(m.start())]
    low = max((end for end in ends if end < unit[0]), default=-1)
    high = min((end for end in ends if end >= unit[1]), default=len(plain))
    mine = [group for group in groups if low < group[0].start and group[-1].end <= high]
    if not mine:
        return [c.ms for c in groups[0]] if len(groups) == 1 else []

    def distance(group: list[NoteCitation]) -> int:
        if group[0].start >= unit[1]:
            return group[0].start - unit[1]
        return max(0, unit[0] - group[-1].end)

    ranked = sorted(mine, key=distance)
    if len(ranked) > 1 and distance(ranked[0]) == distance(ranked[1]):
        return []
    return [c.ms for c in ranked[0]]


def settle_markers(body: str, transcript: str) -> tuple[str, MarkerSettle]:
    """Move every marker boundary that cuts a quoted unit off it. Counted."""
    cues = _cues(transcript)
    lines = body.splitlines(keepends=True)
    straddles = inside = outside = refused = unhandled = checked = 0

    def strong(unit_text: str, stamps: list[int]) -> bool:
        flat = flatten(unit_text)
        if not flat or not stamps or not cues:
            return False
        if len(unit_text.split()) < 2 and len(flat) < _MIN_FLAT:
            return False
        local = flatten(" ".join(
            text for at, text in cues if any(abs(at - stamp) <= _NEAR_MS for stamp in stamps)
        ))
        return flat in local

    for index, raw in enumerate(lines):
        ending = raw[len(raw.rstrip("\r\n")) :]
        line = raw[: len(raw) - len(ending)]
        unhandled += len(_SINGLE_RE.findall(_MARKER_RE.sub(" ", line)))
        if "[note:" not in line.lower():
            continue
        parsed = _Line(line)
        if not parsed.markers:
            continue
        units = [(m.start(), m.end()) for m in _UNIT_RE.finditer(parsed.plain)]
        if not units:
            continue
        checked += 1
        changed = False
        keep: list[bool] = [True] * len(parsed.markers)

        for number, marker in enumerate(parsed.markers):
            for unit_start, unit_end in units:
                kind = _straddles(marker[0], marker[1], unit_start, unit_end)
                if kind is None:
                    continue
                straddles += 1
                changed = True
                # The unit's words as the reader sees them. A marker sitting
                # wholly inside a quotation is the writer's gloss on it; that
                # case always grows the marker (the gloss cannot be moved
                # without moving words), so no evidence is read for it.
                text = parsed.plain[unit_start + 1 : unit_end - 1]
                own = _own_stamps(parsed.plain, (unit_start, unit_end), units)
                if kind == "within" or not strong(text, own):
                    # Inside: the marker grows to hold the whole unit.
                    marker[0] = min(marker[0], unit_start)
                    marker[1] = max(marker[1], unit_end)
                    inside += 1
                    continue
                # Outside: the marker shrinks to leave the unit out.
                if kind == "open":
                    marker[0] = unit_end
                    while marker[0] < marker[1] and parsed.plain[marker[0]] in " \t":
                        marker[0] += 1
                else:
                    marker[1] = unit_start
                    while marker[1] > marker[0] and parsed.plain[marker[1] - 1] in " \t":
                        marker[1] -= 1
                if not re.search(r"\w", parsed.plain[marker[0] : marker[1]]):
                    keep[number] = False
                    refused += 1
                else:
                    outside += 1

        if not changed:
            continue
        # Growing a marker can make it overlap its neighbour: one marker then.
        merged: list[list[int]] = []
        openers: list[str] = []
        for (start, end), opener, kept in sorted(
            zip(parsed.markers, parsed.openers, keep), key=lambda item: item[0][0]
        ):
            if not kept:
                continue
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
                continue
            merged.append([start, end])
            openers.append(opener)
        parsed.markers, parsed.openers = merged, openers
        lines[index] = parsed.render() + ending

    return "".join(lines), MarkerSettle(
        straddles=straddles,
        kept_inside=inside,
        put_outside=outside,
        refused=refused,
        single_quoted_unhandled=unhandled,
        lines_checked=checked,
    )
