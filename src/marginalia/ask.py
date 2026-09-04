"""Answering, entirely inside Python.

This is the payoff, and the reason it lives here rather than in a Claude Code
session is the same reason the whole system is a CLI. Thirty-seven notes is
roughly 55k tokens. Loading them into the conversation to answer one question
rebuilds the exact cost curve the note layer was built to flatten, and
escalating from there would put transcript text into the conversation, which
rule 1 forbids outright. So ``marg ask`` does the whole job — load, answer,
escalate, answer again — and returns finished prose. The skill prints it.

No selection, ranking or retrieval step. Every note, every question. Under a
hundred videos the honest answer to "which notes are relevant?" is *all of
them*, and at this provider's rates the whole library costs about a cent a
question. Ranking would add a component that can be wrong in a way nobody
notices — a video silently dropped before the model ever saw it — to save
money that is not being spent.

Escalation is the model's decision, not a flag. A question about what videos
cover is answerable from notes; a question about someone's exact words is not,
and only the thing reading the notes knows which it is looking at.

Parsing is at the top and pure. The two-call flow is at the bottom. Same split
as ``judge.py`` and ``note.py``, and for the same reason: the protocol will
change and these must keep telling the truth about it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import replace
from typing import Any, Sequence

from .config import Config
from .brain.selector import Selector
from .contracts import (
    SELECTION_ALL,
    SELECTION_FALLBACK,
    Answer,
    AnswerCitation,
    CitationScan,
    LoadedNote,
    NoteSummary,
    Opened,
    Selection,
)
from .core.ids import is_video_id
from .core.ledger import Ledger, LedgerError
from .core.library import Library
from .llm import LlmClient, LlmError, Prompt, load_prompt

log = logging.getLogger(__name__)


class SelectionFailed(RuntimeError):
    """The library needs selection and the selector could not deliver one.

    Raised instead of answering from an arbitrary slice. The original
    design filled the budget by position when selection failed, on the
    reasoning that more notes is safer — and at 422 notes that produced
    worse answers than selection did, because 159 notes went unread on an
    ordering unrelated to the question and the answer said nothing about
    it. Rule 8 is about exclusion nobody can see; an arbitrary fill is
    exactly that.
    """


class AskTooLarge(RuntimeError):
    """The library no longer fits the configured context window.

    Raised *before* any request, so the run ends with a sentence naming both
    numbers rather than an opaque provider 400 several seconds later. It carries
    counts only — never a note, a title or an excerpt — because a message about
    size has no business quoting content (hard rule 1).
    """


# Characters per token, **measured rather than assumed**. Four real answers over
# the same 309 notes reported 868,392–868,399 input tokens against 3,466,968
# characters of notes: 3.992 characters per token.
#
# It lands within 0.2% of the usual chars/4 folklore, and that coincidence is
# worth writing down rather than leaving for someone to rediscover — otherwise
# the next reader either "corrects" a measured constant to the folklore value it
# already equals, or distrusts the folklore and re-measures what is recorded
# here. The number is right *for this corpus on this tokenizer*; a different
# provider or a library of a different language would need re-measuring, and the
# `input_tokens` the API returns on every answer is what to re-measure against.
_CHARS_PER_TOKEN = 3.992

# **Prose only. A list of video ids tokenises at 1.44.** Measured on five
# truncated selector replies: 4,096 output tokens against 5,828-5,910
# characters, every time. Eleven random alphanumerics do not sit on token
# boundaries, so an id list burns an output budget about 2.8x faster than
# text does.
#
# Recorded here rather than where it is used because the trap is using the
# constant above to size a call that emits ids — which would be wrong by
# nearly three times, in the direction that truncates.
_ID_CHARS_PER_TOKEN = 1.44

# The prompt template, the system message and the question, none of which scale
# with the library. Measured as the gap between notes/3.992 and the reported
# total on those same four answers.
_PROMPT_OVERHEAD_TOKENS = 1_700

# Warn here. Refusal is at 100%; this exists so the wall is visible while there
# is still room to decide what to do about it.
_HEADROOM_WARN = 0.80


def estimate_tokens(notes: Sequence[LoadedNote]) -> int:
    """Input tokens this question will cost, before spending them.

    From characters, because a tokenizer is a dependency and the dependency list
    — `yt-dlp`, `openai`, `pytest` — is a rule. The ratio is measured; see
    `_CHARS_PER_TOKEN`.
    """
    characters = sum(len(note.text) + len(note.title) for note in notes)
    return int(characters / _CHARS_PER_TOKEN) + _PROMPT_OVERHEAD_TOKENS

__all__ = [
    "answer_question",
    "AskTooLarge",
    "SelectionFailed",
    "check_grounding",
    "note_digest",
    "choose_notes",
    "estimate_tokens",
    "fill_to_budget",
    "summarise",
    "deep_link",
    "load_notes",
    "render_notes",
    "render_notes",
    "normalise_dashes",
    "parse_citations",
    "scan_citations",
    "parse_escalations",
    "render_answer",
    "render_links",
]

# `ESCALATE: <video_id>: <why>`, at the start of a line. Anchored so that a
# sentence mentioning escalation inside an answer cannot be mistaken for a
# request for one.
_ESCALATE_RE = re.compile(r"^\s*ESCALATE:\s*([A-Za-z0-9_-]{11})\s*:?\s*(.*)$", re.MULTILINE)

# Citation parsing. Liberal on purpose, and the reason is a measured failure:
# a real answer carrying well over a hundred inline citations reported two,
# because the parser accepted only the one shape the prompt asks for. Every
# other shape the model actually writes was discarded in silence.
#
# What real output contains, all of it dropped by the strict version:
#
#     [AMwrLF_TeWo 00:00:42-00:01:02]              a range
#     [GfNdXFS8O0w 00:00:37; 6cEQEba0i2A 00:02:29] two citations in one bracket
#     [-YTupW6YG1k 01:00-01:09, 02:28]             a list, second entry has no ID
#     [--_0uiiJfsBPI 15:48-16:40]                  stray punctuation before the ID
#     [-YTupW6YG1k 00:00-00:42? - under-specifies] prose trailing inside
#
# Two things make this tractable rather than a guessing game.
#
# **Dashes are normalised to ASCII first.** The model writes U+2011 inside IDs,
# rendering `-YTupW6YG1k` as `‑YTupW6YG1k`. A pattern that cannot see the real
# character does not fail loudly, it matches something else — the same lesson
# the Arabic quotes taught the timestamp snapper.
#
# **IDs are resolved against the ledger, not inferred from punctuation.** A
# YouTube ID may legitimately begin with a hyphen, which makes `-YTupW6YG1k
# 01:00-01:09` ambiguous to any amount of clever regex: the leading dash could
# be part of the ID or a stray, and the middle one could be a range or an ID
# character. Looking the ID up in the set the library actually holds resolves
# both, and simultaneously satisfies the rule that a liberal parser must never
# invent a link. Every ID is exactly eleven characters, so none can contain
# another and substring matching is safe.
_DASHES = str.maketrans(
    {ch: "-" for ch in "‐‑‒–—―−﹘﹣－"}
)

# A bracketed or parenthesised span, not nested. Bounded so a runaway bracket
# cannot make this quadratic over a long answer.
_GROUP_RE = re.compile(r"[\[(]([^\[\]()]{1,400})[\])]")

# MM:SS or HH:MM:SS, optional milliseconds. Guarded so "$4", "1.25x" and a
# ratio like "2:1" cannot match.
_TS_RE = re.compile(r"(?<![\d.:])(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?:\.\d{1,3})?(?![\d:])")

# Fallback when no ledger is available (unit tests, mostly). Shape only.
_ID_SHAPE_RE = re.compile(r"[A-Za-z0-9_-]{11}")

# The same shape, but only where it is a whole token.
#
# `_ID_SHAPE_RE` slides: given `single-scalar` it matches the first eleven
# characters, `single-scal`, and reports it as an id naming no loaded video. The
# all-lowercase guard below cannot catch that one, because the hyphen makes
# `isalpha()` false. Measured on one 646-note answer: 2 of 3 `unresolved_ids`
# were word fragments — the counter over-reporting by two in three.
#
# Used *only* by the unresolved-id scan. Citation parsing keeps the sliding
# regex deliberately: there the id sits inside a bracket the model wrote, and
# tightening it would turn a citation glued to its timestamp from parsed into
# unparsed, which is a different counter and a change nobody asked for.
_ID_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])")

# Candidate tokens for prefix repair. Eight characters is the floor: shorter
# than that and a prefix stops identifying one video and starts matching
# whatever the library happens to hold. Ten of eleven characters is the real
# case; eight leaves room without inviting a guess.
_ID_SHAPE_PREFIX_RE = re.compile(r"[A-Za-z0-9_-]{8,11}")

# Entries inside one bracket. A range separator is a dash and must NOT split.
_ENTRY_SPLIT_RE = re.compile(r"[;,]")


def normalise_dashes(text: str) -> str:
    """Every Unicode dash becomes ASCII ``-`` so one pattern can see them all."""
    return text.translate(_DASHES)

ESCALATION_ALLOWED = "allowed — you have not been given any transcripts yet"
ESCALATION_SPENT = (
    "not allowed — you already have the transcripts you asked for, or none could be "
    "opened. Answer from what you have, and say plainly if it is not enough."
)

NO_TRANSCRIPTS = "(none — you have not asked for any)"


# -- parsing -----------------------------------------------------------------


def _seconds_of(match: re.Match[str]) -> int:
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def _find_id(entry: str, known: Sequence[str] | None) -> tuple[str | None, bool]:
    """The video this entry refers to, and whether its casing had to be repaired.

    Without ``known`` this falls back to the eleven-character shape, which is
    right often enough for a unit test and wrong exactly where the ambiguity
    lives — `--_0uiiJfsBPI` yields `--_0uiiJfsB`. That is why the real path
    always passes the ledger's IDs.

    **The case-insensitive pass is repair against a known set, not
    normalisation, and the difference is the whole of hard rule 5.** Two YouTube
    IDs differing only in case are routine — `dQw4w9WgXcQ` and `dqw4w9wgxcq` are
    different videos — so lowercasing an ID to compare it against YouTube is
    always wrong and always will be. But this compares against *the IDs this
    library holds*, where a case-only collision would need two videos we already
    store whose IDs differ by nothing else. It has never happened and is
    vanishingly unlikely; if it ever does, ``len(matches) == 1`` fails and the
    entry is refused exactly as before.

    So: **never turn this into a `.lower()` on the ID itself.** The repair is
    safe only because it ends by returning the library's own spelling, and only
    because it refuses when the answer is ambiguous. Measured need: seven
    citations across three real answers were lost to `57AOoAngaJA` written as
    `57AOoangAJA` and `UVEjThz8DSo` as `UVejThz8DSo`. The parser was right to
    refuse them; the claims beside them still read as sourced while their links
    had silently vanished.

    **A truncated ID is repaired on the same terms**: an id-shaped token that is
    a prefix of exactly one library ID resolves to that ID. `VIg_7sqS2D` for
    `VIg_7sqS2dE` — the model dropped the last character, and it wrote that same
    video's ID three different ways in one answer (correct, mis-cased, cut
    short). One rule, one justification: match what the library holds, and
    refuse the moment the answer is not unique.

    **What is deliberately NOT repaired: a dropped character, a transposition,
    or any other edit-distance match.** The same answer also produced
    `aYfZN8tAQs` for `aYfZN8t6AQs` and `o6vU4u_yEuE` for `o6v4Uu_yEuE`, and both
    stay refused. Two YouTube IDs one character apart are two different videos,
    so an edit-distance "repair" can silently point a claim at the wrong video —
    and a wrong link is worse than a missing one, because a missing link is
    visible in the count while a wrong one reads as a working citation. Prefix
    and case are safe because neither invents a character the model did not
    write; edit distance does exactly that. Do not add it.
    """
    if known:
        for video_id in known:
            if video_id in entry:
                return video_id, False
        folded = entry.lower()
        matches = [video_id for video_id in known if video_id.lower() in folded]
        if len(matches) == 1:
            return matches[0], True
        if matches:
            # Ambiguous: never guess which of several the model meant.
            return None, False
        # Truncation. Only id-shaped tokens are considered, so a stray word in
        # the entry cannot become a prefix that matches half the library.
        for token in _ID_SHAPE_PREFIX_RE.findall(entry):
            starts = [v for v in known if v.lower().startswith(token.lower())]
            if len(starts) == 1:
                return starts[0], True
        return None, False
    match = _ID_SHAPE_RE.search(entry)
    return (match.group(0) if match else None), False


# -- what the answer layer invents -------------------------------------------
#
# **Every existing check in this system reads shape.** `_classify` reads an HTTP
# status, rule 13 reads five lines, the citation parser reads syntax. A reply
# that terminates cleanly, parses, and is simply wrong passes all three — the
# semantic failure rule 24 records as a known class.
#
# An external check of one Q4 answer found 5 of 23 checkable strings appearing
# in neither the transcript nor the note, four of them inside quotation marks,
# and two cited video ids that do not exist. The run reported 23 parsed, 0
# unparsed, because none of those is a shape problem.
#
# You cannot stop a model inventing a plausible quote. You can count it.
#
# **Four outcomes, four counters, per rule 14.** An id naming no video, a
# *sentence* attributed to a video whose note does not contain it, a *phrase*
# in the same position, and a quote attributed to nothing are four different
# failures with four different fixes, and summing them would report a number
# nobody can act on.

# Quoted spans the checker will look at. Four characters minimum, because "a"
# and "AI" in quotes are punctuation rather than attribution.
_QUOTE_RE = re.compile(r"[\"\u201c]([^\"\u201c\u201d]{4,300})[\"\u201d]")

# How far from a quote a citation may sit and still be its attribution. Two
# hundred characters is about a sentence either side.
_CITE_WINDOW = 200

# Where a quoted phrase stops being a phrase and starts being speech.
#
# `"an agent loop"` next to a citation is the model quoting its own concept.
# `"agents haven't been particularly reliable to date"` next to a citation is a
# claim about what somebody said. Both used to land on one counter, and the
# ratio between them is large: across four samples of one question the totals
# were 14, 11, 34 and 22 while the sentence-length subsets were 4, 4, 5 and 9.
# A single number therefore over-reported fabrication by roughly 3x.
#
# **Forty characters, and the corpus does not offer a better answer.** Every
# quoted span in `answers/` \u2014 940 of them across 54 files \u2014 was measured, and
# there is no break to snap to:
#
#   * the word-count histogram is a single smooth decay from a peak at two
#     words, with no trough anywhere to put a boundary in;
#   * the share containing a finite verb rises gradually with length \u2014 3% under
#     20 characters, 16%, 32%, 51%, then 72% above 70 \u2014 rather than stepping;
#   * every candidate cut from 25 to 60 separates the verb-bearing spans by
#     38-43 points, and 40 is the marginal best at 43. Inside the noise.
#
# So this is a convention the data declines to contradict, not a discovered
# threshold, and it is written here as one. It will misfile a long noun phrase
# and a four-word sentence, which is why both counters are reported and neither
# is a gate.
#
# **The split does not separate a wrong class from a right one.** The share of
# quoted spans their cited note actually contains is roughly 50% at *every*
# length band, from 40% under ten characters to 75% over eighty. What the
# boundary separates is two kinds of claim, not two error rates.
_SPEECH_CHARS = 40


def _flatten(text: str) -> str:
    """Case, whitespace and punctuation removed, for substring matching.

    **Deliberately blunt.** A cleverer matcher is a second thing that can be
    wrong, and this counter's job is to raise a flag rather than to adjudicate.
    False positives are expected and acceptable: the output is a number a
    reader weighs, not a gate that suppresses anything.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def check_grounding(
    text: str, notes: Sequence[LoadedNote], loaded: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """Count what the answer asserts that its own sources do not support.

    Returns `(unresolved_ids, unverified_speech, unverified_phrases,
    uncited_quotes)`.

    **The two unverified lists are never summed.** A sentence attributed to a
    video its note does not contain is a claim about what somebody said; a
    quoted phrase in the same position is usually the model quoting its own
    vocabulary. See `_SPEECH_CHARS` for where the line sits and why the corpus
    could not place it better.

    **Renamed rather than narrowed.** `unverified_quotes` used to mean both, so
    keeping the name for the smaller half would leave a count that fell ~3x
    without saying so — the mistake `note_counts` made with `unwanted` and had
    to be renamed out of.

    **Hard rule 1 governs the return value.** A failing quote may travel — it
    came from the answer, which the reader is already holding. The *note* may
    not: not the passage searched, not the nearest match, not "the note says X
    instead". This reports whether a string was found and never what stood in
    its place, and the trap is that the note text is sitting right there in the
    same function — the same shape as the size-warning that wanted to name the
    largest note.
    """
    known = set(loaded)
    folded = {video_id.lower(): video_id for video_id in known}
    flat = {note.video_id: _flatten(note.text) for note in notes}

    # Brackets only. Scanning free prose would flag every eleven-character word.
    unresolved: list[str] = []
    for group in _GROUP_RE.finditer(text):
        for candidate in _ID_TOKEN_RE.findall(group.group(1)):
            if candidate in known or candidate in unresolved:
                continue
            # An eleven-letter lowercase run is an English word, not an id:
            # `consolidate`, `fabricating`, `contradicti` all matched the shape
            # inside the model's own prose. A real id carries a digit, a case
            # change, or `-`/`_` — this drops the words without inventing a
            # dictionary to check against.
            if candidate.isalpha() and candidate.islower():
                continue
            # A case variant of a loaded id is the citation parser's `repaired`
            # outcome, not a missing video. Counting it here would report one
            # event under two names — rule 14 in the other direction.
            if candidate.lower() in folded:
                continue
            unresolved.append(candidate)

    speech: list[str] = []
    phrases: list[str] = []
    uncited: list[str] = []
    for quote in _QUOTE_RE.finditer(text):
        body = quote.group(1).strip()
        if len(_flatten(body)) < 8:
            continue
        window = text[max(0, quote.start() - _CITE_WINDOW) : quote.end() + _CITE_WINDOW]
        # Case-folded, matching what the citation parser already repairs: an
        # id the model mis-cased still names a real video, and treating the
        # quote as unattributed would report a *citation* problem as an
        # *attribution* one.
        ids = [
            folded[found.lower()]
            for group in _GROUP_RE.finditer(window)
            for found in _ID_SHAPE_RE.findall(group.group(1))
            if found.lower() in folded
        ]
        if not ids:
            uncited.append(body)
            continue
        needle = _flatten(body)
        if not any(needle in flat[video_id] for video_id in ids):
            # Every nearby id, not the first: the check passed if *any* of them
            # held the string, so naming one would misreport what was tested.
            entry = f"{'/'.join(dict.fromkeys(ids))}: {body}"
            # Measured on the raw span, not the flattened one: the boundary was
            # placed against quoted text as a reader sees it, and flattening
            # strips the spaces that make a sentence long.
            (speech if len(body) >= _SPEECH_CHARS else phrases).append(entry)
    return unresolved, speech, phrases, uncited


def scan_citations(text: str, known: Sequence[str] | None = None) -> CitationScan:
    """Every citation in an answer, plus every citation-shaped thing that failed.

    A range contributes its start: `01:00-01:09` is a link to 01:00. Entries
    after the first in one bracket inherit the last ID seen, because that is
    what `[-YTupW6YG1k 01:00-01:09, 02:28]` means.
    """
    flat = normalise_dashes(text)
    citations: list[AnswerCitation] = []
    unparsed: list[str] = []
    repaired: list[str] = []

    for group in _GROUP_RE.finditer(flat):
        inside = group.group(1)
        if not _TS_RE.search(inside):
            continue  # ordinary parentheses, not a citation
        current: str | None = None
        for entry in _ENTRY_SPLIT_RE.split(inside):
            stamp = _TS_RE.search(entry)
            if stamp is None:
                continue
            found, was_repaired = _find_id(entry, known)
            if found is not None:
                current = found
                if was_repaired:
                    repaired.append(" ".join(entry.split())[:80])
            if current is None:
                # Shaped like a citation, names a video we do not hold. Never
                # linked — a liberal parser must not become an inventive one.
                unparsed.append(" ".join(entry.split())[:80])
                continue
            citations.append(
                AnswerCitation(
                    video_id=current,
                    seconds=_seconds_of(stamp),
                    raw=f"[{current} {stamp.group(0)}]",
                )
            )

    for fragment in unparsed:
        log.debug("ask: citation-shaped but unreadable: %s", fragment)
    for fragment in repaired:
        log.info("ask: repaired a mis-cased video id in %s", fragment)
    return CitationScan(citations=citations, unparsed=unparsed, repaired=repaired)


def parse_citations(text: str, known: Sequence[str] | None = None) -> list[AnswerCitation]:
    """Just the citations. See ``scan_citations`` for what was dropped."""
    return scan_citations(text, known).citations


def parse_escalations(text: str, *, known: Sequence[str], limit: int) -> list[tuple[str, str]]:
    """Transcript requests the model made. Unknown IDs are dropped, not fetched.

    A reply that is *mostly* an answer is treated as an answer even if it
    contains the marker: the prompt asks for escalation lines alone, and acting
    on a stray marker inside real prose would throw away an answer that was
    already written.
    """
    matches = list(_ESCALATE_RE.finditer(text))
    if not matches:
        return []

    remainder = _ESCALATE_RE.sub("", text).strip()
    if len(remainder) > 400:
        log.info("ask: ignoring an ESCALATE marker inside what is already an answer")
        return []

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for match in matches:
        video_id, why = match.group(1), " ".join(match.group(2).split())
        if video_id in seen:
            continue
        seen.add(video_id)
        if video_id not in known:
            log.info("ask: model asked for %s, which is not in the library", video_id)
            continue
        out.append((video_id, why or "no reason given"))
        if len(out) >= limit:
            break
    return out


def deep_link(video_id: str, seconds: int) -> str:
    """The product. A claim you can click into at the moment it was said."""
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, int(seconds))}s"


def render_links(citations: Sequence[AnswerCitation], titles: dict[str, str]) -> list[str]:
    """One line per distinct citation, ordered as the answer used them."""
    lines: list[str] = []
    seen: set[tuple[str, int]] = set()
    for citation in citations:
        key = (citation.video_id, citation.seconds)
        if key in seen:
            continue
        seen.add(key)
        title = titles.get(citation.video_id) or citation.video_id
        stamp = f"{citation.seconds // 60:02d}:{citation.seconds % 60:02d}"
        lines.append(f"{citation.raw}  {stamp}  {title}\n    {deep_link(*key)}")
    return lines


def note_digest(text: str) -> str:
    """A short fingerprint of one note's text.

    Eight hex characters of SHA-256. Not a security boundary and not a
    deduplication key — just enough to answer "is this the note the answer
    read?", where the alternative is assuming it and being wrong for 646 notes
    out of 646. Whitespace is stripped so a trailing newline is not a rewrite.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


def _read_marks(answer: Answer) -> list[str]:
    """`id:digest` per loaded note, or a bare id where no digest was recorded.

    The fallback covers `Answer`s built by hand — tests, and any future caller
    that fills the ids without the text behind them. An answer file that says
    `id` alone is saying "I do not know what this note contained", which is the
    honest thing for it to say and exactly what the pre-digest history says by
    omission.
    """
    ids = answer.selection.video_ids or sorted(answer.note_digests)
    return [
        f"{video_id}:{answer.note_digests[video_id]}"
        if video_id in answer.note_digests
        else video_id
        for video_id in ids
    ]


def render_answer(answer: Answer, *, asked_at: str) -> str:
    """One answer as a file. Pure: no clock, no disk, no config.

    What goes in is everything needed to judge the answer later without the
    terminal it was printed to. The answer text **verbatim** — reformatting it
    would make the stored copy differ from the one the user read, and the point
    of keeping it is comparison. Then the citations resolved to links, which
    notes were loaded, whether a transcript was opened and the model's own
    reason for opening it, and what the whole thing cost.

    ``version`` is here for the reason every other version field in this project
    is: an answer that looks wrong six weeks from now is a question about which
    prompt wrote it, and that is unanswerable from prose.

    **No note text and no transcript text.** Only video IDs and titles, which is
    the same line hard rule 1 draws at every other surface. A file that quoted
    the notes it read would be a copy of a tenth of the library per question.
    """
    lines = [
        f"# {answer.question}",
        "",
        f"- asked: {asked_at}",
        f"- notes loaded: {answer.notes_loaded}"
        + (
            f" of {answer.notes_considered} considered"
            if answer.notes_considered > answer.notes_loaded
            else ""
        ),
        *(
            [
                f"- selection: {answer.selection.mode} — {answer.selection.reason}",
                # The decision itself, not only the account of it. Rule 23
                # arriving one layer late: the selector stored its reasoning and
                # its counts and not its output, so "was that note dropped, or
                # loaded and never cited?" needed a second selector call to
                # answer. 263 ids is a few kilobytes and turns that question
                # into a grep.
                #
                # **Ids and digests only.** No titles, no summary lines, no note
                # text — the lines handed to the selector are built from notes,
                # and hard rule 1 does not stop at the answer file.
                #
                # `id:digest`, and always the full list — `all` used to stand in
                # for a whole-library answer and it names nothing about a library
                # that grows. See `Answer.note_digests` for why the digest is
                # here at all.
                "- notes read: " + " ".join(_read_marks(answer)),
            ]
        ),
        *(
            [
                # The pair that made the last finding legible, and which existed
                # nowhere on disk when it was needed: a reply naming 409 drops
                # against a quota of 159 is a model thresholding rather than
                # ranking, and it reads exactly like a reply naming 160 unless
                # the request is recorded beside the allowance.
                f"- selector asked to drop: {answer.selection.requested_drops}"
                f" of {answer.notes_considered}"
                f", quota allowed {answer.selection.allowed_drops}"
                f", {answer.selection.refused_drops} re-included"
            ]
            if answer.selection.requested_drops
            else []
        ),
        f"- input estimated: {answer.estimated_input_tokens:,} tokens"
        + (f" ({answer.headroom:.0%} of the ceiling)" if answer.headroom is not None else ""),
        f"- prompt: answer v{answer.version}",
        f"- requests: {answer.requests}"
        f" ({answer.input_tokens} in, {answer.output_tokens} out)",
        f"- cost: ${answer.cost_usd:.4f}",
        f"- duration: {answer.elapsed_s:.1f}s",
        "",
        "## Answer",
        "",
        answer.text.strip() or "(the model returned nothing)",
        "",
        "## Citations",
        "",
    ]
    links = render_links(answer.citations, answer.titles)
    lines.extend(links or ["(none)"])
    if answer.unparsed:
        # Kept, for the same reason the counter is printed: a citation the
        # parser could not read is the next shape to teach it, and it is
        # unrecoverable once the answer scrolls away.
        lines.extend(["", "Citation-shaped but unreadable:"])
        lines.extend(f"  {fragment}" for fragment in answer.unparsed)

    if (
        answer.unresolved_ids
        or answer.unverified_speech
        or answer.unverified_phrases
        or answer.uncited_quotes
    ):
        # **The failing strings, and never the notes.** A quote may be printed:
        # it came from the answer, which the reader is already holding. What the
        # note contained instead may not — this section reports whether a string
        # was found, never what stood in its place.
        lines.extend(["", "## Grounding", ""])
        # Two lines, because the reader's question is "did the model quote a
        # video, or quote itself?" and neither number answers it alone. Speech
        # first: it is the one that asserts somebody said something.
        lines.append(
            f"- {len(answer.unverified_speech)} sentence(s) attributed to a video"
            f" whose note does not contain them"
            f", {len(answer.unverified_phrases)} shorter quoted phrase(s) likewise"
        )
        lines.append(
            f"- {len(answer.unresolved_ids)} cited id(s) name no loaded video"
            f", {len(answer.uncited_quotes)} quoted string(s) are attributed to nothing"
        )
        lines.append(
            "- counted, not corrected, and never summed: a paraphrase inside quote"
            " marks flags here too, and a quoted phrase is usually the model"
            " quoting its own vocabulary rather than a video"
        )
        for video_id in answer.unresolved_ids:
            lines.append(f"- no such loaded video: {video_id}")
        for item in answer.unverified_speech:
            lines.append(f"- sentence not in the cited note: {item}")
        for item in answer.unverified_phrases:
            lines.append(f"- phrase not in the cited note: {item}")
        for item in answer.uncited_quotes:
            lines.append(f"- quoted, uncited: {item}")

    lines.extend(["", "## Notes read", ""])
    if answer.titles:
        lines.extend(
            f"- {video_id}  {answer.titles.get(video_id, '')}".rstrip()
            for video_id in sorted(answer.titles)
        )
    else:
        lines.append("(none)")

    lines.extend(["", "## Transcripts opened", ""])
    if answer.opened:
        lines.extend(f"- {video_id} — {why}" for video_id, why in answer.opened)
    else:
        lines.append("(none — the notes were enough)")

    return "\n".join(lines) + "\n"


# -- loading -----------------------------------------------------------------


def load_notes(library: Library, ledger: Ledger | None = None) -> list[LoadedNote]:
    """Every note on disk. A video without one is skipped, not an error.

    Notes go missing for ordinary reasons — a failed note pass, a transcript
    that was too long — and a question should still be answerable from the
    other thirty-six.
    """
    directory = library.videos_dir
    if not directory.is_dir():
        return []

    notes: list[LoadedNote] = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or not is_video_id(path.name):
            continue
        video_id = path.name
        try:
            text = library.read_note(video_id)
        except OSError as exc:
            log.warning("ask: could not read the note for %s (%s)", video_id, exc)
            continue
        if not text or not text.strip():
            continue
        meta = library.read_meta(video_id) or {}
        _touch(ledger, video_id)
        notes.append(
            LoadedNote(
                video_id=video_id,
                title=" ".join(str(meta.get("title") or video_id).split()),
                text=text.strip(),
            )
        )
    return notes


# The first claim is where a note says what the video actually asserts, and it

# is the closest thing a note has to a description of itself. Truncated, because

# the selector needs a hint rather than the note — that is the next stage's job.

_GIST_CHARS = 200

_CLAIMS_RE = re.compile(r"^##\s*Claims\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)$", re.MULTILINE)

_DATE_RE = re.compile(r"\b(\d{8})\b")





def summarise(note: LoadedNote) -> NoteSummary:

    """One line about one note, for the selector.



    **Built from the note, and it never leaves this process except to the

    model.** Hard rule 1 does not stop at whole notes: `gist` is note text, so

    nothing here may be printed, logged or written into an answer file. What is

    reported is the *counts* these produce.



    The date comes from the note's own second line rather than from `meta.json`,

    so a summary needs no second read of the disk.

    """

    published = ""

    head = note.text.split("\n", 2)[:2]

    if len(head) > 1:

        found = _DATE_RE.search(head[1])

        published = found.group(1) if found else ""



    gist = ""

    claims = _CLAIMS_RE.search(note.text)

    if claims:

        bullet = _BULLET_RE.search(claims.group(1))

        if bullet:

            gist = " ".join(bullet.group(1).split())[:_GIST_CHARS]

    return NoteSummary(

        video_id=note.video_id,

        title=" ".join(note.title.split())[:120],

        published=published,

        gist=gist or "(the note names no claim)",

    )





def fill_to_budget(

    notes: Sequence[LoadedNote], budget_tokens: int

) -> tuple[list[LoadedNote], int]:

    """As many notes as fit, in the order given, plus how many did not.



    Used twice, and the second use is the important one. It packs a selection

    that came back larger than the budget, and it is also what a *failed*

    selection falls through to — rule 8, because filling the budget with an

    arbitrary but as-complete-as-possible set beats answering from a handful

    the selector never actually chose.

    """

    kept: list[LoadedNote] = []

    spent = _PROMPT_OVERHEAD_TOKENS

    for note in notes:

        cost = int((len(note.text) + len(note.title)) / _CHARS_PER_TOKEN)

        if kept and spent + cost > budget_tokens:

            break

        spent += cost

        kept.append(note)

    return kept, len(notes) - len(kept)





def choose_notes(
    config: Config,
    selector: Selector | None,
    question: str,
    notes: Sequence[LoadedNote],
) -> tuple[list[LoadedNote], Selection]:
    """Decide which notes the answering call reads. M7's whole decision.

    **Below the budget, everything loads.** That is not a fallback, it is the
    measured-better path — 182 notes produced better answers than 87 — and the
    selector exists because of a ceiling rather than because selection improves
    an answer.

    **Above the budget, a failed selection refuses rather than fills.** The
    original spec said the opposite, on rule 8's logic that including more is
    safer, and a run at 422 notes disproved it: the two questions that fell
    through to a positional fill loaded 263 notes and cited 6 and 3 videos,
    while the two that selected loaded 104 and cited 29 and 8. Filling by sorted
    video id is silent exclusion wearing a different hat — 159 notes go unread,
    chosen by an ordering that has nothing to do with the question, and the
    answer reads as confident either way. Rule 8 protects against exclusion
    nobody can see, and an arbitrary fill is exactly that.

    So the selector gets one retry, and a second failure raises. A visible
    failure someone can act on beats a degradation they cannot see.
    """
    ceiling = config.ask.max_input_tokens
    if not ceiling:
        return list(notes), Selection(mode=SELECTION_ALL, considered=len(notes))

    budget = int(ceiling * config.ask.select_budget_share)
    if estimate_tokens(notes) <= budget:
        return list(notes), Selection(mode=SELECTION_ALL, considered=len(notes))

    room, _ = fill_to_budget(notes, budget)
    if estimate_tokens(room) > ceiling:
        log.warning(
            "select: the smallest loadable set is still over the ceiling — "
            "not calling the selector"
        )
        return room, Selection(
            video_ids=[note.video_id for note in room],
            mode=SELECTION_FALLBACK,
            reason="a single note exceeds the input ceiling; selection cannot help",
            considered=len(notes),
            overflowed=len(notes) - len(room),
        )

    if len(room) >= len(notes):
        # Over the budget, but nothing actually has to go — one note wider
        # than the budget and narrower than the ceiling lands here. There
        # is no quota to ask for, so asking would spend a call to be told
        # to drop nothing.
        return room, Selection(mode=SELECTION_ALL, considered=len(notes))

    if selector is None:
        # The one case a positional fill is still right for: nothing was
        # configured to choose, and the ordering is all there is.
        log.warning(
            "select: %d notes exceed the budget and no selector is configured — "
            "loading the %d that fit",
            len(notes),
            len(room),
        )
        return room, Selection(
            video_ids=[note.video_id for note in room],
            mode=SELECTION_FALLBACK,
            reason="no selector configured; filled the budget",
            considered=len(notes),
            overflowed=len(notes) - len(room),
        )

    summaries = [summarise(note) for note in notes]
    decision = selector.select(question, summaries, keep=len(room))
    if not decision.parsed or not decision.video_ids:
        # Once. The quota makes the runaway reply much less likely, so most of
        # what used to fall through now succeeds on a second attempt.
        log.warning(
            "select: the selector returned nothing usable (%s) — retrying once",
            decision.problem or "no ids",
        )
        decision = selector.select(question, summaries, keep=len(room))

    if not decision.parsed or not decision.video_ids:
        raise SelectionFailed(
            f"the library is {len(notes)} notes and {estimate_tokens(notes):,} "
            f"estimated tokens, past the {budget:,}-token selection budget, so "
            f"{len(notes) - len(room)} of them have to go — and the selector "
            f"failed twice ({decision.problem or 'no ids'}). Nothing was sent. "
            f"Answering from the first {len(room)} notes by video id would drop "
            f"the other {len(notes) - len(room)} for a reason unrelated to the "
            f"question, which is the silent exclusion this refuses to make."
        )

    wanted = {note.video_id: note for note in notes}
    chosen = [wanted[video_id] for video_id in decision.video_ids if video_id in wanted]
    packed, dropped = fill_to_budget(chosen, budget)
    if dropped:
        log.warning(
            "select: the selector kept %d notes and %d fit the budget — %d trimmed",
            len(chosen),
            len(packed),
            dropped,
        )
    log.info(
        "select: %d of %d notes kept (%d loaded after the budget)",
        len(chosen),
        len(notes),
        len(packed),
    )
    return packed, replace(
        decision,
        video_ids=[note.video_id for note in packed],
        considered=len(notes),
        overflowed=dropped,
    )


def _guard_input_size(result: Answer, ceiling: int | None) -> None:
    """Report the size; warn near the ceiling; refuse past it.

    **Counts only.** Not a title, not an excerpt, not "the largest note is X" —
    hard rule 1 draws the line at content, and a message about size has no
    reason to cross it. The numbers here are safe precisely because they are
    numbers.

    With no ceiling configured this reports and returns. That is the honest
    behaviour rather than a weak one: the project has no provider-neutral
    context limit to fall back on, and inventing one would be the same mistake
    as a default `llm.model`.
    """
    estimate = result.estimated_input_tokens
    if not ceiling:
        log.info(
            "ask: %d notes, ~%d input tokens (no [ask] max_input_tokens set, so no ceiling)",
            result.notes_loaded,
            estimate,
        )
        return

    share = result.headroom or 0.0
    if share > 1.0:
        raise AskTooLarge(
            f"the library no longer fits: {result.notes_loaded} notes are about "
            f"{estimate:,} input tokens against an [ask] max_input_tokens of "
            f"{ceiling:,} ({share:.0%}). Nothing was sent. Raise the ceiling if the "
            f"provider allows it, or the answer layer needs note selection — and "
            f"read PLAN.md before building that, because both stages must read "
            f"whole notes."
        )
    if share >= _HEADROOM_WARN:
        log.warning(
            "ask: %d notes are about %d input tokens, %.0f%% of the %d-token "
            "ceiling — roughly %d more notes of room",
            result.notes_loaded,
            estimate,
            share * 100,
            ceiling,
            max(0, int((ceiling - estimate) / max(1, estimate // max(1, result.notes_loaded)))),
        )
        return
    log.info(
        "ask: %d notes, ~%d input tokens, %.0f%% of the %d-token ceiling",
        result.notes_loaded,
        estimate,
        share * 100,
        ceiling,
    )


def _touch(ledger: Ledger | None, video_id: str) -> None:
    """Record the read, and never let bookkeeping fail a question.

    ``touch_accessed`` raises when there is no ledger row, which is right for
    the ingest path — a row must exist before anything is written. Here it is
    the wrong shape: a note sitting on disk without a row is a bookkeeping
    inconsistency, and refusing to answer any question until someone repairs it
    would be a strange way to report that. The read still gets recorded for
    every video that has a row, which is what the deletion policy needs.
    """
    if ledger is None:
        return
    try:
        ledger.touch_accessed(video_id)
    except LedgerError:
        log.info("ask: %s has stored text but no ledger row; not recording the read", video_id)


def render_notes(notes: Sequence[LoadedNote]) -> str:
    """Every note as one block, each headed by its video ID.

    Public because the discovery loop's library-coverage call renders notes
    the same way. Two spellings of this would mean the coverage assessment
    and the answer read subtly different text, and any disagreement between
    them could not be attributed to either.
    """
    return "\n\n".join(
        f"===== VIDEO {note.video_id} =====\n{note.text}" for note in notes
    )


def _transcripts_block(opened: Sequence[tuple[str, str]]) -> str:
    if not opened:
        return NO_TRANSCRIPTS
    return "\n\n".join(
        f"===== TRANSCRIPT {video_id} (each line carries its start and end) =====\n{text}"
        for video_id, text in opened
    )


# -- the run -----------------------------------------------------------------


def answer_question(
    config: Config,
    library: Library,
    client: LlmClient,
    question: str,
    *,
    ledger: Ledger | None = None,
    deep: Sequence[str] = (),
    prompts_directory: Any = None,
) -> Answer:
    """Answer once from notes; open transcripts and answer again if asked to."""
    prompt = load_prompt("answer", directory=prompts_directory)
    started = time.monotonic()
    everything = load_notes(library, ledger)
    result = Answer(question=question, version=prompt.version, model=client.model)

    if not everything:
        result.text = "The library is empty — there are no notes to answer from."
        result.elapsed_s = time.monotonic() - started
        return result

    # M7 stage 1. Below the budget this returns every note unchanged, which is
    # the measured-better path and the one that must not be lost to building
    # this. Above it, the selector picks whole notes.
    notes, result.selection = choose_notes(
        config,
        Selector(client, prompts_directory=prompts_directory),
        question,
        everything,
    )
    result.notes_considered = len(everything)
    result.notes_loaded = len(notes)
    # Every note's title, not only the loaded ones: a citation the model invents
    # for a note it was not given must still resolve to a real title if one
    # exists, or the reader cannot tell a hallucination from a selection miss.
    result.titles = {note.video_id: note.title for note in everything}
    # The loaded notes only: this records what the answer was written from, not
    # what the library happened to hold.
    result.note_digests = {note.video_id: note_digest(note.text) for note in notes}

    # Before anything is sent. The whole-library approach is correct while it
    # fits, and the failure when it stops fitting is a cliff rather than a
    # slope — so the size is reported on *every* answer, which is what turns
    # the cliff into something visible long before it arrives.
    result.estimated_input_tokens = estimate_tokens(notes)
    ceiling = config.ask.max_input_tokens
    if ceiling:
        result.headroom = result.estimated_input_tokens / ceiling
    _guard_input_size(result, ceiling)

    known = [note.video_id for note in notes]
    # `--deep` forces a transcript open; it never prevents one being opened. The
    # model can still ask for more on top of what was forced.
    opened: list[Opened] = []
    for video_id in dict.fromkeys(deep):
        if video_id not in known:
            log.info("ask: --deep %s is not in the library", video_id)
            continue
        text = _read_transcript(config, library, ledger, video_id)
        if text is not None:
            opened.append(Opened(video_id, "opened by request", text))

    try:
        reply = _call(config, client, prompt, question, notes, opened, allow=not opened)
        wanted = parse_escalations(
            reply.text, known=known, limit=config.ask.max_transcripts - len(opened)
        )
        if wanted:
            for video_id, why in wanted:
                text = _read_transcript(config, library, ledger, video_id)
                if text is not None:
                    opened.append(Opened(video_id, why, text))
            # Second call happens even if nothing could be opened: the first
            # reply was a request, not an answer, so returning it would hand the
            # user an ESCALATE line instead of prose.
            reply = _call(config, client, prompt, question, notes, opened, allow=False)
    except LlmError as exc:
        result.text = f"The model call failed: {exc}"
        result.elapsed_s = time.monotonic() - started
        _account(result, client, config)
        return result

    result.text = reply.text.strip()
    result.opened = [(item.video_id, item.why) for item in opened]
    (
        result.unresolved_ids,
        result.unverified_speech,
        result.unverified_phrases,
        result.uncited_quotes,
    ) = check_grounding(result.text, notes, [note.video_id for note in notes])
    if result.unverified_speech or result.unverified_phrases or result.unresolved_ids:
        # Counts only. Naming the note that failed to contain a string is
        # one step from printing what it held instead, which is the line
        # hard rule 1 draws at every other surface.
        log.warning(
            "ask: %d sentence(s) and %d shorter phrase(s) are not in the notes "
            "they cite, %d cited id(s) name no loaded video",
            len(result.unverified_speech),
            len(result.unverified_phrases),
            len(result.unresolved_ids),
        )
    scan = scan_citations(result.text, known)
    result.citations = scan.citations
    result.unparsed = scan.unparsed
    result.repaired = scan.repaired
    result.truncated = not result.text
    _account(result, client, config)
    result.elapsed_s = time.monotonic() - started
    return result


def _call(
    config: Config,
    client: LlmClient,
    prompt: Prompt,
    question: str,
    notes: Sequence[LoadedNote],
    opened: Sequence[Opened],
    *,
    allow: bool,
):
    return client.complete(
        "You answer questions from a library of notes about videos, and you cite everything.",
        prompt.render(
            question=question,
            count=len(notes),
            notes=render_notes(notes),
            transcripts=_transcripts_block([(item.video_id, item.text) for item in opened]),
            escalation=ESCALATION_ALLOWED if allow else ESCALATION_SPENT,
        ),
        max_tokens=config.ask.max_tokens,
        task="answer",
    )


def _read_transcript(
    config: Config, library: Library, ledger: Ledger | None, video_id: str
) -> str | None:
    if not library.has_transcript(video_id):
        log.info("ask: %s has a note but no transcript on disk", video_id)
        return None
    text = library.read_transcript(video_id)
    _touch(ledger, video_id)
    ceiling = config.ask.max_transcript_chars
    if len(text) > ceiling:
        # Truncated rather than skipped: half of a transcript still answers a
        # wording question about the half it contains, and the alternative is
        # that the longest videos can never be escalated into at all.
        log.info("ask: %s is %d chars, truncating to %d", video_id, len(text), ceiling)
        text = text[:ceiling]
    return text


def _account(result: Answer, client: LlmClient, config: Config) -> None:
    result.requests = client.usage.requests
    result.input_tokens = client.usage.input_tokens
    result.output_tokens = client.usage.output_tokens
    result.cost_usd = config.llm.cost_usd(result.input_tokens, result.output_tokens)
