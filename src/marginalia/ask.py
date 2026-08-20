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

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import Config
from .core.ids import is_video_id
from .core.ledger import Ledger, LedgerError
from .core.library import Library
from .llm import LlmClient, LlmError, Prompt, load_prompt

log = logging.getLogger(__name__)

__all__ = [
    "Answer",
    "Citation",
    "CitationScan",
    "LoadedNote",
    "answer_question",
    "deep_link",
    "load_notes",
    "normalise_dashes",
    "parse_citations",
    "scan_citations",
    "parse_escalations",
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


@dataclass(frozen=True)
class Citation:
    video_id: str
    seconds: int
    raw: str


@dataclass(frozen=True)
class CitationScan:
    """What the parser found, and what it could not read.

    ``unparsed`` is the whole point. Three times in this project something was
    dropped silently and looked like success — a hardcoded temperature, an
    ASCII-only word class, and this parser. A count that says "104 shaped, 2
    unparsed" turns the fourth occurrence into a visible line instead of an
    absence nobody thinks to look for.
    """

    citations: list[Citation]
    unparsed: list[str]

    @property
    def shaped(self) -> int:
        return len(self.citations) + len(self.unparsed)


def _seconds_of(match: re.Match[str]) -> int:
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def _find_id(entry: str, known: Sequence[str] | None) -> str | None:
    """The video this entry refers to, resolved against the library when we can.

    Without ``known`` this falls back to the eleven-character shape, which is
    right often enough for a unit test and wrong exactly where the ambiguity
    lives — `--_0uiiJfsBPI` yields `--_0uiiJfsB`. That is why the real path
    always passes the ledger's IDs.
    """
    if known:
        for video_id in known:
            if video_id in entry:
                return video_id
        return None
    match = _ID_SHAPE_RE.search(entry)
    return match.group(0) if match else None


def scan_citations(text: str, known: Sequence[str] | None = None) -> CitationScan:
    """Every citation in an answer, plus every citation-shaped thing that failed.

    A range contributes its start: `01:00-01:09` is a link to 01:00. Entries
    after the first in one bracket inherit the last ID seen, because that is
    what `[-YTupW6YG1k 01:00-01:09, 02:28]` means.
    """
    flat = normalise_dashes(text)
    citations: list[Citation] = []
    unparsed: list[str] = []

    for group in _GROUP_RE.finditer(flat):
        inside = group.group(1)
        if not _TS_RE.search(inside):
            continue  # ordinary parentheses, not a citation
        current: str | None = None
        for entry in _ENTRY_SPLIT_RE.split(inside):
            stamp = _TS_RE.search(entry)
            if stamp is None:
                continue
            found = _find_id(entry, known)
            if found is not None:
                current = found
            if current is None:
                # Shaped like a citation, names a video we do not hold. Never
                # linked — a liberal parser must not become an inventive one.
                unparsed.append(" ".join(entry.split())[:80])
                continue
            citations.append(
                Citation(
                    video_id=current,
                    seconds=_seconds_of(stamp),
                    raw=f"[{current} {stamp.group(0)}]",
                )
            )

    for fragment in unparsed:
        log.debug("ask: citation-shaped but unreadable: %s", fragment)
    return CitationScan(citations=citations, unparsed=unparsed)


def parse_citations(text: str, known: Sequence[str] | None = None) -> list[Citation]:
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


def render_links(citations: Sequence[Citation], titles: dict[str, str]) -> list[str]:
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


# -- loading -----------------------------------------------------------------


@dataclass(frozen=True)
class LoadedNote:
    video_id: str
    title: str
    text: str


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


def _notes_block(notes: Sequence[LoadedNote]) -> str:
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


@dataclass(frozen=True)
class Opened:
    """A transcript read for one question, and why it was read."""

    video_id: str
    why: str
    text: str


@dataclass
class Answer:
    question: str
    text: str = ""
    version: str = ""
    model: str = ""
    notes_loaded: int = 0
    opened: list[tuple[str, str]] = field(default_factory=list)  # video_id, why
    citations: list[Citation] = field(default_factory=list)
    # Citation-shaped strings the parser could not read. Reported rather than
    # discarded: a parser that drops most of its input must say so.
    unparsed: list[str] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    truncated: bool = False

    @property
    def escalated(self) -> bool:
        return bool(self.opened)


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
    notes = load_notes(library, ledger)
    result = Answer(question=question, version=prompt.version, model=client.model)
    result.notes_loaded = len(notes)
    result.titles = {note.video_id: note.title for note in notes}

    if not notes:
        result.text = "The library is empty — there are no notes to answer from."
        result.elapsed_s = time.monotonic() - started
        return result

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
    scan = scan_citations(result.text, known)
    result.citations = scan.citations
    result.unparsed = scan.unparsed
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
            notes=_notes_block(notes),
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
