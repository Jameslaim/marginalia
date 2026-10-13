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
import unicodedata
import re
from typing import Iterable, Sequence

__all__ = [
    "NOTE_SECTIONS",
    "QUOTE_RE",
    "Words",
    "closeness",
    "find_quotes",
    "flatten",
    "normalise_heading",
    "note_lines",
    "note_sections",
    "quoted_spans",
    "section_heading",
    "spoken_text",
    "standing_verbatim",
    "VERBATIM_UNFOUND",
    "verbatim_of",
]

# Quoted spans. Four characters minimum, because "a" and "AI" in quotes are
# punctuation rather than attribution.
QUOTE_RE = re.compile(r"[\"“]([^\"“”]{4,300})[\"”]")

# Quotation marks other languages use, mapped one character to one character
# onto the pair `QUOTE_RE` reads, so match offsets stay valid in the original
# text. Guillemets, low-high marks and corner brackets were invisible to every
# check — not checked, not counted — while `prompts/answer.md` tells the model
# to answer in the question's language (R2 F7). `‘’` are left out on purpose:
# `’` is also the apostrophe, and pairing it would invent quotes inside words.
_QUOTE_MARKS = str.maketrans({"«": "“", "»": "”", "「": "“", "」": "”", "『": "“", "』": "”"})
# German and several Central European languages open low and close with `“` —
# the character English opens with — so they cannot share the mapping above.
_LOW_HIGH_RE = re.compile(r"„([^„“”\"]{4,300})[“”]")


def find_quotes(text: str) -> list[re.Match[str]]:
    """Every quoted span in ``text``, in any of the quotation styles above,
    in order. Offsets refer to ``text`` itself; ``group(1)`` is the quoted span.
    """
    low_high = list(_LOW_HIGH_RE.finditer(text))
    taken = [(match.start(), match.end()) for match in low_high]
    others = [
        match
        for match in QUOTE_RE.finditer(text.translate(_QUOTE_MARKS))
        if not any(start < match.end() and match.start() < end for start, end in taken)
    ]
    return sorted(low_high + others, key=lambda match: match.start())

# Below this many characters once flattened, a quoted span is a term rather than
# a quotation, and matching it says nothing.
_MIN_FLAT = 8

# A stored cue's timing prefix, `[00:01:33.500 --> 00:01:36.000] `, or the
# shorter `[00:01:33.500] ` stage 2 reads. Stripped so a quote is matched
# against the words that were said and never against the clock.
_CUE_PREFIX_RE = re.compile(r"^\[[^\]\n]*\]\s?", re.MULTILINE)

_NOT_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# -- note sections ---------------------------------------------------------
#
# The note format's sections, spelled as `prompts/write_note.md` spells them.
# Here rather than in `enrich/note.py` because four readers need them — the
# validator, the Verbatim check, the answer checker's Verbatim fallback and the
# selector's one-line summary — and each used to find sections its own way. A
# `# install the dependencies` line inside Verbatim ended the section for the
# validator (so the note "had no Verbatim" and failed every run) and for the
# quote checks (so quotes after it went unchecked), while `### Verbatim` passed
# validation and was invisible to the answer side. One splitter, used by all.
NOTE_SECTIONS: tuple[str, ...] = (
    "Claims",
    "Wrong or shaky",
    "Assumes you know",
    "How current is it",
    "Leaves out",
    "Key moments",
    "Who's talking, what they want",
    "Verbatim",
)

# Any indentation: the validator this replaced stripped leading whitespace, and
# an indented `## Verbatim` that validated before must still validate.
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s*(.*?)\s*#*\s*$")
# A fence line opens or closes a block; a line that also closes on itself
# (```cmd```) is inline code, not a fence.
_FENCE_RE = re.compile(r"^\s*(```|~~~)[^`~]*$")


def normalise_heading(text: str) -> str:
    """A heading's text, reduced to what it must match.

    Models substitute a typographic apostrophe for a typed one about as often
    as not, and "Who's talking" failing to match "Who’s talking" would reject a
    perfectly good note. Case, emphasis and a trailing colon go the same way.
    """
    text = text.strip().lstrip("#").strip().strip("*_").strip()
    return " ".join(text.replace("’", "'").casefold().split()).rstrip(":").strip()


_KNOWN = {normalise_heading(name): name for name in NOTE_SECTIONS}


def section_heading(line: str) -> tuple[bool, str | None]:
    """``(is_a_boundary, section name or None)`` for one line, outside a fence.

    * A known section name, at any heading level, starts that section.
    * Any other **level-2** heading ends the current one: `##` is the note
      format's own section level, so an unknown `##` is a section nobody asked
      for, and its contents are not the previous section's.
    * Every other `#` line — a shell comment, a level-3 subheading, a hashtag —
      is content.
    """
    match = _HEADING_RE.match(line)
    if match is None:
        return False, None
    name = _KNOWN.get(normalise_heading(match.group(2)))
    if name is not None:
        return True, name
    return (len(match.group(1)) == 2 and bool(match.group(2))), None


def note_lines(text: str) -> list[tuple[str | None, str, bool]]:
    """Every line of a note as ``(section, line, is_heading)``, line endings kept.

    ``section`` is the canonical name of the section the line belongs to, or
    ``None`` before the first one and after an unknown one. A fenced code block
    is content throughout, whatever its lines start with.
    """
    out: list[tuple[str | None, str, bool]] = []
    section: str | None = None
    fenced = False
    lines = text.splitlines(keepends=True)
    # A fence with no closer is not a fence: treating it as one turned every
    # later heading into content, and a note with a stray ``` lost its sections.
    fences = [index for index, line in enumerate(lines) if _FENCE_RE.match(line)]
    unmatched = fences[-1] if len(fences) % 2 else None
    for index, line in enumerate(lines):
        if index != unmatched and _FENCE_RE.match(line):
            fenced = not fenced
            out.append((section, line, False))
            continue
        if not fenced:
            boundary, name = section_heading(line)
            if boundary:
                section = name
                out.append((section, line, True))
                continue
        out.append((section, line, False))
    return out


def note_sections(text: str) -> dict[str, str]:
    """The note's known sections by canonical name, bodies stripped. A section
    that appears twice is read as one, in order — as the validator this
    replaced read it, so a note whose first copy was empty still validates."""
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for section, line, is_heading in note_lines(text):
        if is_heading:
            current = section
            if section is not None:
                bodies.setdefault(section, [])
            continue
        if current is not None:
            bodies[current].append(line)
    return {name: "".join(lines).strip() for name, lines in bodies.items()}

# Candidate passages scored per quote, once the vote has been taken. Twenty is
# what lets a whole transcript be searched in milliseconds where scoring every
# window took minutes.
_ALIGNMENTS = 20

# How far, in words, one passage's alignments may spread and still be counted
# as one passage. **An inserted or dropped word shifts every later word's
# alignment by one**, so a near-exact rendering splits its votes between
# neighbouring starts: an eight-word quote with one extra word in the middle
# polls 4 and 4. Twenty repeats of a five-word fragment elsewhere then held all
# twenty slots on 5 votes each, and the real passage — 0.8 by the exhaustive
# measure — scored 0.5 and was reported as a quote the transcript does not
# hold. Votes are pooled over this neighbourhood before ranking, and each
# passage takes one slot however many of its starts polled.
_SPREAD = 3

# Below this many words, every window is scored. The vote is a speed-up for
# transcripts; on a Verbatim section or a short video exhaustive scoring costs
# milliseconds and cannot be outvoted by anything.
_EXHAUSTIVE_MAX = 1_000


def flatten(text: str) -> str:
    """Case, whitespace and punctuation removed, for substring matching.

    **Deliberately blunt.** A cleverer matcher is a second thing that can be
    wrong, and every caller's job is to raise a flag rather than to adjudicate.
    """
    return _NOT_WORD_RE.sub("", _normal(text).casefold())


def _normal(text: str) -> str:
    """NFKC. A quote typed in composed form (`é` as one code point) failed the
    exact test against a transcript in decomposed form (`e` plus an accent) —
    measured for Spanish and Vietnamese (R2 F16) — and full-width letters
    never matched their ordinary forms. Neutral for plain ASCII."""
    return unicodedata.normalize("NFKC", text)


def spoken_text(transcript: str) -> str:
    """A transcript's words with every cue's timing prefix removed."""
    return _CUE_PREFIX_RE.sub(" ", transcript)


def verbatim_of(note_text: str) -> str:
    """A note's ``## Verbatim`` section, or empty if it has none."""
    return note_sections(note_text).get("Verbatim", "")


# The marker the note worker puts on a Verbatim line whose quote its own
# transcript does not hold (rule 26). Defined here, beside the section reader,
# because both sides of the quote chain read it.
VERBATIM_UNFOUND = "[note: not found in the transcript as worded]"


def standing_verbatim(note_text: str) -> str:
    """The Verbatim lines that stand as speech: every line but the ones the note
    worker already marked as not found in the transcript.

    The answer check falls back to a note's Verbatim when the cited video has no
    transcript on disk, and a line marked unfound still verified an answer's
    quote there (R2 F15) — the note had said the quote was not the speaker's,
    and the answer was then told it was.
    """
    return "\n".join(
        line for line in verbatim_of(note_text).splitlines() if VERBATIM_UNFOUND not in line
    )


def quoted_spans(text: str) -> list[str]:
    """The quoted spans in ``text`` long enough to be worth checking."""
    return [
        match.group(1).strip()
        for match in find_quotes(text)
        if len(flatten(match.group(1))) >= _MIN_FLAT
    ]


class Words:
    """One text as a word sequence, indexed for the loose match."""

    def __init__(self, text: str) -> None:
        self.words = _WORD_RE.findall(_normal(text).casefold())
        self.positions: dict[str, list[int]] = {}
        for index, word in enumerate(self.words):
            self.positions.setdefault(word, []).append(index)


def closeness(quote: str, hay: Words) -> float:
    """Best word-sequence similarity of ``quote`` against any window of ``hay``.

    A short text — a Verbatim section, a brief video — is scored at every
    window. A transcript is scored only where the quote's own words line up:
    each word the two share votes for the alignment it implies, votes are
    pooled over a few words either side so an edit cannot split one passage's
    support, and the best-supported passages are compared with
    ``SequenceMatcher`` (see `_candidate_starts`). Exhaustive scoring takes
    minutes on a transcript. ``difflib`` is stdlib, so this adds no dependency.

    A number about closeness and nothing else: it never says where the match
    was or what it read, which is what lets a caller report it under rule 1.
    """
    needle = _WORD_RE.findall(_normal(quote).casefold())
    if not needle or not hay.words:
        return 0.0
    if len(hay.words) <= _EXHAUSTIVE_MAX:
        starts: Iterable[int] = range(max(1, len(hay.words) - len(needle) + 1))
    else:
        starts = _candidate_starts(needle, hay)
    best = 0.0
    for start in starts:
        window = hay.words[start : start + len(needle) + 4]
        matcher = difflib.SequenceMatcher(None, needle, window)
        score = matcher.ratio()
        if score > best and _flips_polarity(needle, window, matcher):
            continue
        best = max(best, score)
    return best


# Words that reverse a claim. A twenty-word quote with "never" where the
# speaker said "always", or with a "not" inserted, scored 0.85 — "loosely
# quoted" in an answer, and inside the 0.8 tolerance of a note's Verbatim, for
# a quote saying the opposite of the source (R2 F5). English only: the scorer's
# thresholds were calibrated on this library, and this narrows only the cases
# it can name. `t` is what a contraction's `n't` tokenises to.
_POLARITY = frozenset(
    {
        "not", "no", "never", "nothing", "none", "nobody", "neither", "nor",
        "without", "cannot", "t",
    }
)


def _flips_polarity(
    needle: Sequence[str], window: Sequence[str], matcher: difflib.SequenceMatcher
) -> bool:
    """Do quote and window differ by a word that reverses the claim?

    Only words that differ count — the window's extra trailing context does
    not, or an ordinary "not" in the next sentence would disqualify an exact
    quote.

    **Counted, not merely present.** A contraction swap is one negation for one
    — "don't" tokenises to `don t`, "do not" to `do not`, "can't" to `can t`
    against `cannot` — and asking only whether a negation word appeared among the
    differences flagged every such faithful quote as a reversal and scored it 0
    (found by the independent review). A reversal or an inserted "not" changes
    the count.
    """
    quote_side = 0
    source_side = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert" and i1 in (0, len(needle)):
            continue  # context before or after the quote, not a change inside it
        quote_side += sum(word in _POLARITY for word in needle[i1:i2])
        source_side += sum(word in _POLARITY for word in window[j1:j2])
    return quote_side != source_side


def _candidate_starts(needle: Sequence[str], hay: Words) -> list[int]:
    """The window starts worth scoring: the best-supported passages, each whole.

    Each shared word votes for the alignment it implies; a passage's support is
    the votes within `_SPREAD` of its start, so one edit cannot halve it; and a
    passage already chosen suppresses its neighbours, so twenty slots are
    twenty different places. Every start in a chosen passage's neighbourhood is
    then scored, because the best window may sit either side of the peak.
    """
    votes: dict[int, int] = {}
    for offset, word in enumerate(needle):
        for position in hay.positions.get(word, ()):
            start = max(0, position - offset)
            votes[start] = votes.get(start, 0) + 1
    pooled = {
        start: sum(votes.get(near, 0) for near in range(start - _SPREAD, start + _SPREAD + 1))
        for start in votes
    }
    chosen: list[int] = []
    for start in sorted(votes, key=lambda key: (-pooled[key], -votes[key], key)):
        if any(abs(start - other) <= _SPREAD for other in chosen):
            continue
        chosen.append(start)
        if len(chosen) == _ALIGNMENTS:
            break
    return sorted(
        {
            near
            for start in chosen
            for near in range(max(0, start - _SPREAD), start + _SPREAD + 1)
        }
    )
