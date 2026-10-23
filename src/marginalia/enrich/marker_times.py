"""A time difference stated inside a `[note: ...]` marker, checked against the
timestamps on its own line. Counted, never rewritten.

A marker is the note-writer's own reasoning, and some of it is arithmetic on
the video's clock: "four minutes apart" of two moments the same line cites 21
seconds apart, "ten seconds" of a 39-second gap. Nothing checked it. A marker
is marked inference, so the reader is warned — but a stated difference is
something code can check, and rule 33 says a reading that names nothing is
counted.

**What counts as a stated difference.** A number (in digits or words) and a
unit of seconds, minutes or hours, with a word nearby that makes it a
difference or a span — apart, later, earlier, after, before, gap, between,
within, over, across, span — inside a marker. A bare duration ("a 20-minute
video") is not one: no difference word, no check.

**What it is checked against.** Every pair of timestamps on the same line,
inside markers or out. The statement holds when any pair's difference is
within the tolerance of the stated value; a line with fewer than two
timestamps cannot be checked and is not counted. The tolerance is generous —
a quarter of the stated value, at least 10 seconds — because a writer rounds,
and only a statement no pair of the line's own times could support is
flagged.

Pure: strings in, counts and line numbers out. Never prints note text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .timestamps import parse_note_timestamps

__all__ = ["MarkerTimeClaim", "marker_time_claims"]

_MARKER_RE = re.compile(r"\[note:[ \t]*((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]", re.IGNORECASE)

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}
# "21 seconds", "4-minute", "ten secs", "1.5 min", "90s"
_AMOUNT_RE = re.compile(
    r"\b(?P<n>\d+(?:\.\d+)?|" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")"
    r"[\s-]*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|s|h)\b",
    re.IGNORECASE,
)
_DIFFERENCE_RE = re.compile(
    r"\b(?:apart|later|earlier|after|before|gap|between|within|over|across|spans?|spanning|separat\w*)\b",
    re.IGNORECASE,
)
# How far either side of the amount a difference word may sit, in characters.
_NEAR = 40
_TOLERANCE_SHARE = 0.25
_TOLERANCE_MIN_S = 10.0


@dataclass(frozen=True)
class MarkerTimeClaim:
    """One stated difference in a marker, and what its line's times allow."""

    line: int  # 1-based line number in the note
    stated_s: float
    nearest_s: float  # the line's pairwise difference closest to the statement
    holds: bool


def _seconds(amount: str, unit: str) -> float | None:
    text = amount.lower()
    value = _WORD_NUMBERS.get(text)
    if value is None:
        try:
            value = float(text)
        except ValueError:
            return None
    first = unit.lower()[0]
    return value * (3600 if first == "h" else 60 if first == "m" else 1)


def marker_time_claims(body: str) -> list[MarkerTimeClaim]:
    """Every stated time difference inside a marker on a line with at least two
    timestamps, with whether any pair of those timestamps supports it."""
    claims: list[MarkerTimeClaim] = []
    for number, line in enumerate(body.splitlines(), start=1):
        if "[note:" not in line.lower():
            continue
        times = sorted({c.ms / 1000.0 for c in parse_note_timestamps(line)})
        if len(times) < 2:
            continue
        pairs = [b - a for i, a in enumerate(times) for b in times[i + 1:]]
        for marker in _MARKER_RE.finditer(line):
            content = marker.group(1)
            for amount in _AMOUNT_RE.finditer(content):
                window = content[max(0, amount.start() - _NEAR): amount.end() + _NEAR]
                if not _DIFFERENCE_RE.search(window):
                    continue
                stated = _seconds(amount.group("n"), amount.group("unit"))
                if stated is None or stated <= 0:
                    continue
                nearest = min(pairs, key=lambda gap: abs(gap - stated))
                tolerance = max(_TOLERANCE_MIN_S, stated * _TOLERANCE_SHARE)
                claims.append(
                    MarkerTimeClaim(number, stated, nearest, abs(nearest - stated) <= tolerance)
                )
    return claims
