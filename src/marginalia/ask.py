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
from typing import Any, Sequence

from .config import Config
from .contracts import (
    Answer,
    AnswerCitation,
    CitationScan,
    LoadedNote,
    Opened,
)
from .core.ids import is_video_id
from .core.ledger import Ledger, LedgerError
from .core.library import Library
from .llm import LlmClient, LlmError, Prompt, load_prompt

log = logging.getLogger(__name__)

__all__ = [
    "answer_question",
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
        f"- notes loaded: {answer.notes_loaded}",
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
