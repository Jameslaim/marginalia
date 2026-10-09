"""Is a quoted string in a text? The one matcher both checks share.

Two places ask it. ``ask.py`` asks it of an answer: is what the answer puts in
quotation marks in the transcript of the video it cites? ``enrich/note.py``
asks it of a note: is what the note's ``## Verbatim`` section quotes in the
transcript the note was written from? Two copies of the matcher would drift
apart, and a quote would then pass one check and fail the other for reasons
nobody could see — so both import this.

Pure functions over strings. No model, no disk.

**Unicode, not ``[a-z0-9]``.** The library holds Arabic, Persian and Spanish
videos. An ASCII-only flattening reduced an Arabic quote to nothing, so it fell
under the minimum length and was never checked at all — the timestamp snapper
learned the same lesson first, from a citation that snapped to 00:00:00.
"""

from __future__ import annotations

import difflib
import re

__all__ = [
    "QUOTE_RE",
    "Words",
    "closeness",
    "flatten",
    "quoted_spans",
    "spoken_text",
    "verbatim_of",
]

# Quoted spans. Four characters minimum, because "a" and "AI" in quotes are
# punctuation rather than attribution.
QUOTE_RE = re.compile(r"[\"“]([^\"“”]{4,300})[\"”]")

# Below this many characters once flattened, a quoted span is a term rather than
# a quotation, and matching it says nothing.
_MIN_FLAT = 8

# A stored cue's timing prefix, `[00:01:33.500 --> 00:01:36.000] `, or the
# shorter `[00:01:33.500] ` stage 2 reads. Stripped so a quote is matched
# against the words that were said and never against the clock.
_CUE_PREFIX_RE = re.compile(r"^\[[^\]\n]*\]\s?", re.MULTILINE)

_NOT_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

_VERBATIM_RE = re.compile(r"^## Verbatim\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^#{1,4}\s", re.MULTILINE)

# Candidate alignments scored per quote. The best window is the one sharing the
# most words with the quote, so it is always near the top of the vote; twenty is
# headroom, and it is what lets a whole transcript be searched in milliseconds
# where scoring every window took minutes.
_ALIGNMENTS = 20


def flatten(text: str) -> str:
    """Case, whitespace and punctuation removed, for substring matching.

    **Deliberately blunt.** A cleverer matcher is a second thing that can be
    wrong, and every caller's job is to raise a flag rather than to adjudicate.
    """
    return _NOT_WORD_RE.sub("", text.casefold())


def spoken_text(transcript: str) -> str:
    """A transcript's words with every cue's timing prefix removed."""
    return _CUE_PREFIX_RE.sub(" ", transcript)


def verbatim_of(note_text: str) -> str:
    """A note's ``## Verbatim`` section, or empty if it has none."""
    heading = _VERBATIM_RE.search(note_text)
    if not heading:
        return ""
    rest = note_text[heading.end() :]
    following = _SECTION_RE.search(rest)
    return (rest[: following.start()] if following else rest).strip()


def quoted_spans(text: str) -> list[str]:
    """The quoted spans in ``text`` long enough to be worth checking."""
    return [
        match.group(1).strip()
        for match in QUOTE_RE.finditer(text)
        if len(flatten(match.group(1))) >= _MIN_FLAT
    ]


class Words:
    """One text as a word sequence, indexed for the loose match."""

    def __init__(self, text: str) -> None:
        self.words = _WORD_RE.findall(text.casefold())
        self.positions: dict[str, list[int]] = {}
        for index, word in enumerate(self.words):
            self.positions.setdefault(word, []).append(index)


def closeness(quote: str, hay: Words) -> float:
    """Best word-sequence similarity of ``quote`` against any window of ``hay``.

    Only windows that line up with the quote's own words are scored: each word
    the two share votes for the alignment it implies, and the best-voted few are
    compared with ``SequenceMatcher``. Exhaustive scoring gives the same answer
    on a 150-word Verbatim section and takes minutes on a transcript. ``difflib``
    is stdlib, so this adds no dependency.

    A number about closeness and nothing else: it never says where the match
    was or what it read, which is what lets a caller report it under rule 1.
    """
    needle = _WORD_RE.findall(quote.casefold())
    if not needle or not hay.words:
        return 0.0
    votes: dict[int, int] = {}
    for offset, word in enumerate(needle):
        for position in hay.positions.get(word, ()):
            start = max(0, position - offset)
            votes[start] = votes.get(start, 0) + 1
    best = 0.0
    for start in sorted(votes, key=lambda key: (-votes[key], key))[:_ALIGNMENTS]:
        window = hay.words[start : start + len(needle) + 4]
        best = max(best, difflib.SequenceMatcher(None, needle, window).ratio())
    return best
