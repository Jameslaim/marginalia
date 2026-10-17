"""The note worker: one transcript in, one understanding out.

The premise, tested by hand before any of this existed: a model reading a whole
transcript once understands it far better than a retrieval system reading
fragments. So do that reading once, at ingest, and store what was understood.
Query time then reads notes — small, dense, and many — instead of transcripts,
which are large and few. Roughly 15k tokens against 1.5k: the difference
between holding two videos in context and holding fifty.

Consequences that shape this file:

* **No chunking, ever.** A transcript too long for the model's context is
  logged and skipped, not split. A note assembled from pieces would look like a
  note and would not be one — the whole value is the reading that spans four
  minutes and notices the contradiction.
* **Failure leaves the video note-less and retryable.** Nothing here writes to
  ``transcript.txt``, and ``mark_noted`` runs only after ``note.md`` is on disk.
* **Notes are disposable.** ``--regenerate`` rewrites the whole library from
  stored transcripts with no network call to YouTube. That property is what
  makes acquisition being fragile survivable, so it is a design requirement
  rather than a convenience.

Parsing sits at the top of this file and the loop at the bottom, for the same
reason ``judge.py`` is not inside ``discover.py``: what a reply must contain
changes with the prompt, and the drain loop does not.
"""

from __future__ import annotations

import logging
import re
from datetime import date
import time
from typing import Any, Sequence

from ..config import Config, NoteConfig
from ..contracts import (
    FAILED,
    NOTED,
    SKIPPED,
    STATUS_HAS_TRANSCRIPT,
    STOP_LIMIT,
    STOP_MAX_COST,
    STOP_REFUSED,
    STOP_REPEATED_FAILURES,
    NoteResult,
    NoteRun,
    VideoRow,
)
from ..core.ledger import DecisionRow, Ledger, wanted_for_a_note
from ..core.library import Library, format_duration
from ..llm import (
    LlmClient,
    LlmConfigError,
    LlmError,
    LlmRequestTooLarge,
    Prompt,
    load_prompt,
    safe_reason,
)
from ..quotes import (
    NOTE_SECTIONS,
    VERBATIM_UNFOUND as _VERBATIM_UNFOUND,
    Words,
    closeness,
    flatten,
    note_lines,
    note_sections,
    quoted_spans,
    section_heading,
    spoken_text,
)
from .timestamps import build_index, snap_note

log = logging.getLogger(__name__)

__all__ = [
    "NoteInvalid",
    "REQUIRED_SECTIONS",
    "missing_sections",
    "validate_note",
    "note_header",
    "note_version",
    "note_refusal",
    "select",
    "check_verbatim",
    "VERBATIM_UNFOUND",
    "write_notes",
]

# The required sections, spelled as the prompt spells them. Consistent structure is
# what lets M6 reason across fifty notes at once; free prose inside each is what
# keeps them worth reading.
#
# Defined once, in `quotes.py`, where every reader of a note's sections finds
# them. "How current is it" (v5) is required on purpose: an optional section is
# one the model omits on exactly the videos that most need it — the ones giving
# no version signal, where "I cannot tell" is the answer worth recording.
REQUIRED_SECTIONS: tuple[str, ...] = NOTE_SECTIONS

# Consecutive failures before the drain gives up. Observed, not hypothetical: a
# run against an unreachable endpoint failed all thirty-six videos, four
# attempts each with backoff, and took six minutes to report the same error
# thirty-six times. Nothing had gone wrong with the videos. A note pass is
# resumable by construction — every one of them stays retryable — so stopping
# early costs nothing and saves a run that cannot possibly succeed.
_FAILURE_STREAK = 3


class NoteInvalid(RuntimeError):
    """The reply is not a note: a required section is missing or empty.

    An error rather than a silent acceptance. A note missing "Leaves out" reads
    exactly like a video that leaves nothing out, and M6 would believe it.
    """


# -- parsing ----------------------------------------------------------------
#
# Pure functions over strings. No model, no filesystem — the same split that
# makes `judge.py`'s parsers testable, and for the same reason: the prompt will
# change many times and these must keep telling the truth about its output.


def missing_sections(text: str) -> list[str]:
    """Required sections that are absent, or present with nothing under them.

    Empty counts as missing on purpose. The prompt requires a section to *say*
    when there is nothing to report — "no errors found" is a claim, and a
    useful one — so a heading followed by whitespace means the model ran out,
    not that the video was flawless.

    Sections are found by `quotes.note_sections`, the splitter every reader of
    a note uses, so a `# comment` line inside a section is content here exactly
    as it is to the quote checks.
    """
    bodies = note_sections(text)
    return [name for name in REQUIRED_SECTIONS if not bodies.get(name)]


def validate_note(text: str) -> None:
    """Raise ``NoteInvalid`` unless every required section is present and filled."""
    missing = missing_sections(text)
    if missing:
        raise NoteInvalid("missing or empty section(s): " + ", ".join(missing))


def note_header(video_id: str, meta: dict[str, Any] | None) -> str:
    """The two identifying lines, built from ``meta.json`` rather than asked for.

    The model never writes these. A hallucinated video ID is a citation that
    points at the wrong video, and M6 escalates from a note to a transcript by
    that ID — the one field in the whole note that must not be generated.
    """
    meta = meta or {}
    title = " ".join(str(meta.get("title") or "untitled").split())
    facts = [
        " ".join(str(meta.get("channel") or "unknown channel").split()),
        format_duration(meta.get("duration_s")),
        str(meta.get("upload_date") or "date unknown"),
    ]
    source = str(meta.get("source") or "?")
    kind = meta.get("caption_kind")
    facts.append(f"{source}/{kind}" if kind else source)
    return f"# {video_id} — {title}\n{' · '.join(facts)}\n"


def _chapter_line(meta: dict[str, Any] | None) -> str:
    """Author-written structure, when there is any. Better than anything inferred."""
    chapters = (meta or {}).get("chapters") or []
    parts = []
    for chapter in chapters[:40]:
        if not isinstance(chapter, dict):
            continue
        title = " ".join(str(chapter.get("title") or "").split())
        if title:
            parts.append(f"{_stamp(chapter.get('start_ms'))} {title}")
    return "CHAPTERS: " + ("; ".join(parts) if parts else "none published")


def _stamp(start_ms: object) -> str:
    try:
        total = int(start_ms) // 1000  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    minutes, seconds = divmod(max(0, total), 60)
    return f"{minutes:02d}:{seconds:02d}"


# -- is Verbatim verbatim? ----------------------------------------------------
#
# `## Verbatim` is the one section a note presents as the speaker's own words,
# and the answer prompt sends a model there for anything it puts in quotation
# marks. Nothing checked it. Measured on the archived library: 571 of 5,733
# Verbatim quotes (10%, in 153 of 719 notes) have no close match in the
# transcript the note was written from.
#
# So the worker checks, and a line it cannot find carries the note format's own
# marker for "this did not come from the transcript". The convention already
# means exactly that, the answer prompt already says to keep it, and it puts
# the finding where the next reader is — inside the note — rather than in a
# counter they would have to know to look up.

# Defined in `quotes.py`, which both sides of the quote chain read.
VERBATIM_UNFOUND = _VERBATIM_UNFOUND

# Found means word for word once case and punctuation are set aside, or close
# enough to be the same sentence with a caption error fixed — "cash control"
# written as "cache control". Below that it is a paraphrase, and a paraphrase
# is not what this section promises.
_VERBATIM_TOLERANCE = 0.8

# The note-writer's own inference marker, bracket to bracket.
_NOTE_MARKER_RE = re.compile(r"\[note:[^\]]*\]?", re.IGNORECASE)


def check_verbatim(body: str, transcript: str) -> tuple[str, int, int]:
    """Mark the ``## Verbatim`` lines whose quotes the transcript does not hold.

    Returns ``(body, checked, unfound)``. Pure: strings in, string out.

    Only quoted spans are checked. An unquoted Verbatim line is usually a
    command or a configuration line, which is often on screen rather than
    spoken, and holding it to the transcript would flag it for being correct.
    Text *inside* a ``[note: ...]`` marker is not checked either — a translation
    of the quote beside it belongs there — but the quote outside it still is,
    or a note-writer's own marker would exempt the line it sits on.
    """
    said_text = spoken_text(transcript)
    said = flatten(said_text)
    words: Words | None = None
    out: list[str] = []
    checked = unfound = 0

    for section, line, is_heading in note_lines(body):
        if is_heading:
            out.append(line)
            continue
        if section != "Verbatim" or VERBATIM_UNFOUND in line:
            out.append(line)
            continue
        spans = quoted_spans(_NOTE_MARKER_RE.sub(" ", line))
        missing = 0
        for span in spans:
            checked += 1
            if flatten(span) in said:
                continue
            if words is None:
                words = Words(said_text)
            if closeness(span, words) >= _VERBATIM_TOLERANCE:
                continue
            missing += 1
        if missing:
            unfound += missing
            ending = line[len(line.rstrip("\r\n")) :]
            line = f"{line.rstrip()} {VERBATIM_UNFOUND}{ending}"
        out.append(line)
    return "".join(out), checked, unfound


# -- selection ---------------------------------------------------------------


def select(
    ledger: Ledger,
    *,
    version: str,
    video_ids: Sequence[str] = (),
    regenerate: bool = False,
    every: bool = False,
    missing_only: bool = False,
) -> tuple[list[VideoRow], list[NoteResult]]:
    """Decide what this run will read, and say why anything named was skipped.

    Named videos are returned even when they are current, unless they carry a
    note this prompt wrote — the same logic as ``skip_reason``: a direct request
    outranks a batch policy, but repeating work already done is not what the
    user meant either.
    """
    skipped: list[NoteResult] = []

    if video_ids:
        chosen: list[VideoRow] = []
        for video_id in video_ids:
            row = ledger.get(video_id)
            if row is None:
                skipped.append(NoteResult(video_id, SKIPPED, "not in the ledger"))
            elif row.status != STATUS_HAS_TRANSCRIPT:
                skipped.append(NoteResult(video_id, SKIPPED, f"status is {row.status}"))
            elif row.note_is_current(version) and not regenerate:
                skipped.append(
                    NoteResult(video_id, SKIPPED, f"current note is v{version}", version=version)
                )
            else:
                chosen.append(row)
        return chosen, skipped

    # **Not limited here.** The queue is ordered by `first_seen`, and a video
    # the worker always skips — a transcript under the length floor, or past
    # the ceiling — never leaves it. Cutting the queue to `limit` before the
    # worker ran meant those videos took the first slots of every `--limit`
    # run, until a run made no notes at all. The limit is applied to attempts
    # in `write_notes`, where a skip is known to be free.
    rows = ledger.videos_needing_note(
        version=version,
        stale_only=not every,
        missing_only=missing_only,
    )
    return rows, skipped


# -- the worker --------------------------------------------------------------


def note_refusal(
    row: VideoRow | None,
    decisions: Sequence[DecisionRow],
    transcript_chars: int,
    config: NoteConfig,
) -> str:
    """Why the note worker will refuse this video, or ``""`` if it will not.

    The one statement of those grounds, for the two places that need them: the
    worker, which refuses, and discovery, which must not count as coverage a
    video that will never have a note. Discovery used to check the length
    ceiling alone, so a transcript under the floor, or one a stage 2 marked
    DEGRADED for some other topic, was counted as covering the brief and then
    never noted (R1 F3) — rule 25's sibling, missed twice.
    """
    if row is not None and not wanted_for_a_note(row, decisions):
        return "unreadable: a stage 2 called the transcript degraded"
    length = _length_refusal(transcript_chars, config)
    return length[1] if length is not None else ""


_FLOOR = "floor"
_CEILING = "ceiling"


def _length_refusal(transcript_chars: int, config: NoteConfig) -> tuple[str, str] | None:
    """``(which limit, detail)`` when the transcript's length is refused, else
    ``None``. The worker and `note_refusal` both decide through this, so the two
    cannot drift apart on where the limits sit."""
    floor = config.min_transcript_chars
    if floor and transcript_chars < floor:
        return _FLOOR, f"transcript is {transcript_chars:,} chars, under the {floor:,} floor"
    if transcript_chars > config.max_transcript_chars:
        return _CEILING, f"transcript is {transcript_chars:,} chars, over {config.max_transcript_chars:,}"
    return None


# -- the marking pass ---------------------------------------------------------
#
# Audited on one note written under write_note v9: the record was faithful —
# every claim supported, every quote exact, every key moment on time — and 14
# statements of the writer's own knowledge, arithmetic or extrapolation carried
# no marker, 8 of 10 Leaves-out items among them. Ask reads a note as what the
# video says (rule 26), so each became something the video would be cited for.
#
# **The model lists; the code marks.** The second call returns spans copied
# from the note, and the code wraps each one it finds exactly once in a
# `[note: ...]` marker and changes nothing else. A pass that rewrote the note
# could drift the record it was meant to protect; this one cannot touch a word
# of it. Its visible reply is a few hundred tokens; its cost is reasoning — at
# the provider's default effort, 96-97% of the pass's output tokens — so its
# effort is set in config, `[llm.tasks.mark_note]`, never here (rule 7).

MARK_PROMPT = "mark_note"
_MARK_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?MARK:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_MARK_NONE_RE = re.compile(r"\A\s*(?:`{3}\s*)?NONE\.?\s*(?:`{3})?\s*\Z", re.IGNORECASE)
# A runaway guard, not a quota: spans past it stay unmarked and are counted and
# printed (rules 14, 30). Measured on three notes of 2-3k words: passes with
# reasoning listed 20-63 spans; the one list over 80 was 108, from a pass with
# no reasoning that wrapped whole Claims bullets — record — which is what this
# guard is for. v1's prompt said "at most 40", and 2 of 3 passes stopped at
# exactly 40 with more left; the prompt now sets no limit and this cap is it.
_MAX_MARKS = 80
# Shorter than this, an exact match is too likely to land on the wrong words.
_MIN_MARK_CHARS = 12
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


def parse_marks(text: str) -> list[str] | None:
    """The spans a marking reply lists; ``[]`` for NONE; ``None`` if unreadable."""
    spans = [m.group(1).strip().strip(_QUOTE_CHARS).strip() for m in _MARK_LINE_RE.finditer(text)]
    spans = [span for span in spans if span]
    if spans:
        return spans
    if _MARK_NONE_RE.match(text):
        return []
    return None


def _unmarkable(body: str) -> list[tuple[int, int]]:
    """Ranges no mark may touch: existing markers, headings, all of Verbatim."""
    ranges = [(m.start(), m.end()) for m in _NOTE_MARKER_RE.finditer(body)]
    position, section = 0, None
    for line in body.splitlines(keepends=True):
        boundary, name = section_heading(line)
        if boundary:
            section = name
            ranges.append((position, position + len(line)))
        elif section == "Verbatim":
            ranges.append((position, position + len(line)))
        position += len(line)
    return ranges


def apply_marks(body: str, spans: Sequence[str]) -> tuple[str, int, int]:
    """Wrap each span found exactly once, outside markers and Verbatim.

    Returns ``(body, applied, rejected)``. A span that is too short, holds a
    bracket or a line break, is not in the note, is there more than once, or
    falls in a range `_unmarkable` protects is rejected and counted, never
    guessed at: a mark on the wrong words would move the record into a marker.
    """
    applied = rejected = 0
    for span in spans:
        if len(span) < _MIN_MARK_CHARS or "[" in span or "]" in span or "\n" in span:
            rejected += 1
            continue
        blocked = _unmarkable(body)
        hits = []
        start = body.find(span)
        while start != -1:
            end = start + len(span)
            if not any(start < stop and end > begin for begin, stop in blocked):
                hits.append(start)
            start = body.find(span, start + 1)
        if len(hits) != 1:
            rejected += 1
            continue
        at = hits[0]
        body = body[:at] + f"[note: {span}]" + body[at + len(span):]
        applied += 1
    return body, applied, rejected


def source_facts(video_id: str, meta: dict[str, Any] | None) -> str:
    """What the note-writer was told besides the transcript.

    The marking pass needs it too. Without it, the channel, the upload date, the
    caption kind and the chapter titles look like outside knowledge — they are
    not in the transcript — and the pass wrapped them: of 19 marked spans whose
    content was record, across three v10 notes, 6 were these facts.
    """
    m = meta or {}
    return "\n".join([
        f"TITLE: {m.get('title') or video_id}",
        f"CHANNEL: {m.get('channel') or 'unknown'}",
        f"DURATION: {format_duration(m.get('duration_s'))}",
        f"UPLOADED: {m.get('upload_date') or 'unknown'}",
        f"CAPTIONS: {m.get('caption_kind') or 'unknown'}",
        _chapter_line(meta),
        f"TODAY: {date.today().isoformat()}",
    ])


def mark_pass(
    client: LlmClient,
    prompt: Prompt,
    transcript: str,
    body: str,
    *,
    max_tokens: int,
    facts: str = "",
) -> tuple[str, int, int, str, int]:
    """One marking call over a finished note.

    Returns ``(body, marked, rejected, problem, capped)``. Rule 30: only a note
    that has passed `validate_note` and was not cut off is fed in — the caller
    guarantees it. A failed, cut or unreadable reply leaves the note exactly as
    written and says why in ``problem``. A refusal is re-raised (rule 24); the
    caller has already written the note by then. ``capped`` is how many listed
    spans fell past `_MAX_MARKS` and were not applied: a cap that is reached
    leaves the rest unmarked, so it is counted rather than silent (rule 14).
    """
    try:
        reply = client.complete(
            "You check a note against its transcript and list what it did not mark.",
            prompt.render(transcript=transcript, note=body, metadata=facts),
            max_tokens=max_tokens,
            task="mark_note",
        )
    except LlmConfigError:
        raise
    except LlmError as exc:
        return body, 0, 0, f"failed: {safe_reason(exc)[:120]}", 0
    if reply.finish_reason == "length":
        return body, 0, 0, "cut off at the token ceiling", 0
    spans = parse_marks(reply.text)
    if spans is None:
        return body, 0, 0, "unreadable reply", 0
    capped = max(0, len(spans) - _MAX_MARKS)
    marked_body, applied, rejected = apply_marks(body, spans[:_MAX_MARKS])
    return marked_body, applied, rejected, "", capped


def note_version(prompts_directory: Any = None) -> str:
    """The version of the prompt currently on disk.

    What "stale" is measured against, and the only thing the CLI needs in order
    to answer ``--status`` without running anything.
    """
    return load_prompt("write_note", directory=prompts_directory).version


def write_notes(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    *,
    video_ids: Sequence[str] = (),
    limit: int | None = None,
    regenerate: bool = False,
    every: bool = False,
    missing_only: bool = False,
    prompts_directory: Any = None,
) -> NoteRun:
    """Drain videos that need a note. One transcript, one call, whole thing."""
    prompt = load_prompt("write_note", directory=prompts_directory)
    marker = (
        load_prompt(MARK_PROMPT, directory=prompts_directory) if config.note.mark_pass else None
    )
    run = NoteRun(version=prompt.version, model=client.model)
    started = time.monotonic()

    rows, skipped = select(
        ledger,
        version=prompt.version,
        video_ids=video_ids,
        regenerate=regenerate,
        every=every,
        missing_only=missing_only,
    )
    run.results.extend(skipped)

    streak = 0
    attempts = 0
    try:
        for row in rows:
            # Attempts, not successes and not skips. A skip costs nothing and
            # must not use a slot; a failure cost a whole transcript's worth of
            # model call and must, or `--limit 3` would bound nothing.
            if limit is not None and attempts >= limit:
                run.stopped_because = STOP_LIMIT
                break
            if _overspent(config, client):
                run.stopped_because = STOP_MAX_COST
                break

            # Each video's overflow is its own (see `forget_overflows`).
            client.forget_overflows()
            try:
                result = _one(config, ledger, library, client, prompt, row, marker=marker)
            except LlmConfigError as exc:
                # Stop on the first one, and write nothing about this video.
                # **Aborting the run and condemning the video are different
                # claims**, and only the first is warranted by a billing error
                # or a bad key: the transcript is fine, nobody read it, and it
                # must still be in the queue tomorrow. So no `NoteResult` is
                # appended — a FAILED row here would be the run's own problem
                # recorded as the video's.
                #
                # Caught rather than raised past the report, because the notes
                # already written are worth reporting: the drain that found
                # this had 74 of them behind the failure.
                log.error("note: stopping — %s", safe_reason(exc))
                run.stopped_because = STOP_REFUSED
                break
            run.results.append(result)
            if result.outcome == FAILED:
                ledger.mark_note_failed(row.video_id)
            if result.outcome in (NOTED, FAILED):
                attempts += 1

            streak = streak + 1 if result.outcome == FAILED else 0
            if streak >= _FAILURE_STREAK:
                log.warning(
                    "note: %d failures in a row — stopping; every video is still retryable",
                    streak,
                )
                run.stopped_because = STOP_REPEATED_FAILURES
                break
    finally:
        run.requests = client.usage.requests
        run.input_tokens = client.usage.input_tokens
        run.output_tokens = client.usage.output_tokens
        run.cost_usd = config.llm.cost_usd(run.input_tokens, run.output_tokens)
        run.elapsed_s = time.monotonic() - started
    return run


def _one(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    prompt: Prompt,
    row: VideoRow,
    *,
    marker: Prompt | None = None,
) -> NoteResult:
    video_id = row.video_id

    if not library.has_transcript(video_id):
        # The ledger says stored and the file is gone. Not this pass's problem
        # to fix, but silently producing no note would be worse than saying so.
        return NoteResult(video_id, SKIPPED, "no transcript on disk")

    transcript = library.read_transcript(video_id)
    ledger.touch_accessed(video_id)

    floor = config.note.min_transcript_chars
    length = _length_refusal(len(transcript), config.note)
    if length is not None and length[0] == _FLOOR:
        # **Its own outcome, not a failure and not the ceiling's twin.** Nothing
        # went wrong and nothing was too big: there is not enough transcript for
        # the required sections to be written from, so whatever a note contained would
        # be mostly the model's own supply. Rule 14 — a skip that shared the
        # ceiling's counter would report "too short" and "too long" as one
        # number, and they have opposite causes.
        #
        # **Measured rather than chosen.** A note is normally about *half* its
        # transcript: the library-wide note/transcript ratio has a median of
        # 0.469 and a p90 of 1.42. Below roughly 1,200 characters that inverts
        # hard — 1,153 chars produced a note 7.4x its source, 887 produced 5.1x,
        # and a 37-character transcript produced a 4,681-character note, 126x
        # the thing it was written from. The break sits between 1,153 and 1,378,
        # which is where the ratio falls under 5x and stays there.
        #
        # The floor is deliberately low. Raising it to 2,000 would catch 17
        # transcripts instead of 7 and start excluding genuine short-form video,
        # which is the duration bias `judge_metadata` spends four paragraphs
        # forbidding, relocated to the note layer where nobody would look for it.
        log.info(
            "note: %s is %d characters, under the %d floor — too little to write "
            "a note from, skipped",
            video_id,
            len(transcript),
            floor,
        )
        return NoteResult(
            video_id,
            SKIPPED,
            f"transcript is {len(transcript):,} chars, under the {floor:,} floor",
        )

    ceiling = config.note.max_transcript_chars
    if length is not None and length[0] == _CEILING:
        # Logged and skipped, never split. See the module docstring.
        log.warning(
            "note: %s is %d characters, over the %d limit — skipped, not split",
            video_id,
            len(transcript),
            ceiling,
        )
        return NoteResult(
            video_id, SKIPPED, f"transcript is {len(transcript):,} chars, over {ceiling:,}"
        )

    meta = library.read_meta(video_id)
    try:
        reply = client.complete(
            "You read a transcript once, whole, and write down what you understood.",
            prompt.render(
                title=(meta or {}).get("title") or video_id,
                channel=(meta or {}).get("channel") or "unknown",
                duration=format_duration((meta or {}).get("duration_s")),
                upload_date=(meta or {}).get("upload_date") or "unknown",
                # **Passed in, not left to the model to infer.** A model with a
                # 2024 cutoff read a 2026-04 video, concluded the model name in
                # it was a hallucination, called the source speculative and
                # marked its real pricing wrong. `How current is it` is
                # mandatory, so a model that cannot place itself in time gets
                # every era judgement wrong, confidently. Rule 16 passes `today`
                # into the territory validator for the same reason.
                today=date.today().isoformat(),
                # Automatic captions mangle exactly the technical terms a note
                # is most interested in. The reader must be told which errors
                # are the speaker's and which are the transcriber's.
                caption_kind=(meta or {}).get("caption_kind") or "unknown",
                chapters=_chapter_line(meta),
                transcript=transcript,
            ),
            max_tokens=config.note.max_tokens,
            task="write_note",
        )
    except LlmConfigError:
        # A bad key or an unknown model fails identically for every video.
        # Draining thirty-six of them to say so is a waste of everyone's time.
        raise
    except LlmRequestTooLarge:
        # This transcript is past the model's window; the next one may not be.
        # Its own outcome, beside the character ceiling's, and not a failure:
        # it says nothing about the provider, so it must not feed the streak
        # that stops a drain.
        log.warning("note: %s is too large for the model's context window — skipped", video_id)
        return NoteResult(video_id, SKIPPED, "too large for the model's context window")
    except LlmError as exc:
        log.info("note: %s failed (%s)", video_id, safe_reason(exc))
        return NoteResult(video_id, FAILED, f"transient: {safe_reason(exc)[:120]}")

    if reply.finish_reason == "length":
        # A fragment, refused before it can pass for a note. `validate_note`
        # only asks that every section be present and non-empty, and the last
        # section is the one a ceiling cuts: a reply that stopped two lines into
        # `## Verbatim` passed, and was stored as complete. Every other
        # long-output call here already discards a truncated reply.
        log.info("note: %s hit the token ceiling; the reply is a fragment", video_id)
        return NoteResult(
            video_id,
            FAILED,
            f"truncated: hit the {config.note.max_tokens:,}-token ceiling",
        )

    body = reply.text.strip()
    try:
        validate_note(body)
    except NoteInvalid as exc:
        # Not retried here. A whole transcript has just been paid for, and a
        # second reading of the same prompt is more likely to cost twice than
        # to succeed. The video stays retryable for the next run.
        log.info("note: %s produced an invalid note (%s)", video_id, exc)
        return NoteResult(video_id, FAILED, f"invalid: {exc}")

    # Two corrections before the note is written, both pure and both free: the
    # transcript is already in hand. Timestamps first, because the snapper
    # anchors on Verbatim quotes; then the Verbatim check, whose markers the
    # snapper ignores.
    index = build_index(transcript)
    snapped = 0
    if index:
        body, changes, _left = snap_note(body, index, label=video_id)
        snapped = len(changes)
    body, checked, unfound = check_verbatim(body, transcript)
    if unfound:
        log.info(
            "note: %s — %d of %d verbatim quote(s) not found in the transcript; marked",
            video_id,
            unfound,
            checked,
        )

    # File first, ledger second. A crash between them costs one model call; the
    # other order would leave a row claiming a note that does not exist, and
    # nothing would ever revisit it.
    header = note_header(video_id, meta)
    library.write_note(video_id, header + "\n" + body)
    ledger.mark_noted(video_id, version=prompt.version, model=reply.model)

    # The marking pass, on a note already written. Written first so that
    # nothing the pass does — a failure, a cut reply, a refusal that stops the
    # drain — can lose the note this call paid for (rule 30: a failed second
    # pass leaves the first note intact). Only marks are written over it.
    marked = rejected = capped = 0
    mark_problem = ""
    if marker is not None:
        marked_body, marked, rejected, mark_problem, capped = mark_pass(
            client, marker, transcript, body, max_tokens=config.note.max_tokens,
            facts=source_facts(video_id, meta),
        )
        if marked:
            library.write_note(video_id, header + "\n" + marked_body)
        if mark_problem:
            log.info("note: %s — marking pass %s; note kept as written", video_id, mark_problem)
        else:
            log.info(
                "note: %s — marking pass wrapped %d statement(s), %d listed span(s) not applied",
                video_id, marked, rejected,
            )
        if capped:
            log.warning(
                "note: %s — marking pass listed %d span(s) past the %d-span cap; "
                "those stay unmarked",
                video_id, capped, _MAX_MARKS,
            )

    detail = f"{reply.usage.output_tokens:,} tokens"
    if snapped:
        detail += f", {snapped} timestamp(s) snapped"
    if unfound:
        detail += f", {unfound} of {checked} verbatim quote(s) not in the transcript"
    if marker is not None:
        detail += (
            f", marking pass {mark_problem} — note kept as written"
            if mark_problem
            else f", {marked} statement(s) marked by the second pass"
        )
        if capped:
            detail += f", {capped} span(s) past the {_MAX_MARKS}-span cap left unmarked"
    return NoteResult(
        video_id,
        NOTED,
        detail,
        tokens=reply.usage.output_tokens,
        version=prompt.version,
        snapped=snapped,
        verbatim_checked=checked,
        verbatim_unfound=unfound,
        mark_ran=marker is not None,
        marked=marked,
        mark_rejected=rejected,
        mark_problem=mark_problem,
        mark_capped=capped,
    )


def _overspent(config: Config, client: LlmClient) -> bool:
    """The same guard discovery uses, with the note pass's own ceiling.

    Checked before the call rather than after: a run that notices it overspent
    once the money is gone is a report, not a guard.
    """
    ceiling = config.note.max_cost_usd
    if ceiling <= 0:
        return False
    spent = config.llm.cost_usd(client.usage.input_tokens, client.usage.output_tokens)
    if spent >= ceiling:
        log.warning("note: estimated $%.3f spent, ceiling is $%.2f", spent, ceiling)
        return True
    return False
