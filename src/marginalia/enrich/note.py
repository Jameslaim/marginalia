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
from datetime import date
import time
from typing import Any, Sequence

from ..config import Config
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
from ..core.ledger import Ledger
from ..core.library import Library, format_duration
from ..llm import LlmClient, LlmConfigError, LlmError, Prompt, load_prompt

log = logging.getLogger(__name__)

__all__ = [
    "NoteInvalid",
    "REQUIRED_SECTIONS",
    "missing_sections",
    "validate_note",
    "note_header",
    "note_version",
    "select",
    "write_notes",
]

# The required sections, spelled as the prompt spells them. Consistent structure is
# what lets M6 reason across fifty notes at once; free prose inside each is what
# keeps them worth reading.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Claims",
    "Wrong or shaky",
    "Assumes you know",
    # v5. Required, so a note that skips it fails validation and is retried
    # rather than stored silent about era. An optional section is one the model
    # omits on exactly the videos that most need it: the ones giving no version
    # signal, where "I cannot tell" is the answer worth recording.
    "How current is it",
    "Leaves out",
    "Key moments",
    "Who's talking, what they want",
    "Verbatim",
)

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


def _normalise_heading(line: str) -> str:
    """A heading, reduced to what it must match.

    Models substitute a typographic apostrophe for a typed one about as often
    as not, and "Who's talking" failing to match "Who’s talking" would reject a
    perfectly good note. Case and surrounding decoration go the same way.
    """
    text = line.strip().lstrip("#").strip().strip("*").strip()
    return " ".join(text.replace("’", "'").lower().split()).rstrip(":")


def missing_sections(text: str) -> list[str]:
    """Required sections that are absent, or present with nothing under them.

    Empty counts as missing on purpose. The prompt requires a section to *say*
    when there is nothing to report — "no errors found" is a claim, and a
    useful one — so a heading followed by whitespace means the model ran out,
    not that the video was flawless.
    """
    wanted = {_normalise_heading(name): name for name in REQUIRED_SECTIONS}
    bodies: dict[str, list[str]] = {}
    current: str | None = None

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            heading = _normalise_heading(line)
            current = heading if heading in wanted else None
            if current is not None:
                bodies.setdefault(current, [])
            continue
        if current is not None and line.strip():
            bodies[current].append(line.strip())

    return [name for key, name in wanted.items() if not bodies.get(key)]


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


# -- results ----------------------------------------------------------------


# -- selection ---------------------------------------------------------------


def select(
    ledger: Ledger,
    *,
    version: str,
    video_ids: Sequence[str] = (),
    limit: int | None = None,
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

    rows = ledger.videos_needing_note(
        version=version,
        stale_only=not every,
        missing_only=missing_only,
        limit=limit,
    )
    return rows, skipped


# -- the worker --------------------------------------------------------------


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
    run = NoteRun(version=prompt.version, model=client.model)
    started = time.monotonic()

    rows, skipped = select(
        ledger,
        version=prompt.version,
        video_ids=video_ids,
        limit=limit,
        regenerate=regenerate,
        every=every,
        missing_only=missing_only,
    )
    run.results.extend(skipped)

    streak = 0
    try:
        for row in rows:
            if limit is not None and run.count(NOTED) >= limit:
                run.stopped_because = STOP_LIMIT
                break
            if _overspent(config, client):
                run.stopped_because = STOP_MAX_COST
                break

            try:
                result = _one(config, ledger, library, client, prompt, row)
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
                log.error("note: stopping — %s", exc)
                run.stopped_because = STOP_REFUSED
                break
            run.results.append(result)

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
) -> NoteResult:
    video_id = row.video_id

    if not library.has_transcript(video_id):
        # The ledger says stored and the file is gone. Not this pass's problem
        # to fix, but silently producing no note would be worse than saying so.
        return NoteResult(video_id, SKIPPED, "no transcript on disk")

    transcript = library.read_transcript(video_id)
    ledger.touch_accessed(video_id)

    floor = config.note.min_transcript_chars
    if floor and len(transcript) < floor:
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
    if len(transcript) > ceiling:
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
    except LlmError as exc:
        log.info("note: %s failed (%s)", video_id, exc)
        return NoteResult(video_id, FAILED, f"transient: {_brief(exc)}")

    body = reply.text.strip()
    try:
        validate_note(body)
    except NoteInvalid as exc:
        # Not retried here. A whole transcript has just been paid for, and a
        # second reading of the same prompt is more likely to cost twice than
        # to succeed. The video stays retryable for the next run.
        log.info("note: %s produced an invalid note (%s)", video_id, exc)
        return NoteResult(video_id, FAILED, f"invalid: {exc}")

    # File first, ledger second. A crash between them costs one model call; the
    # other order would leave a row claiming a note that does not exist, and
    # nothing would ever revisit it.
    library.write_note(video_id, note_header(video_id, meta) + "\n" + body)
    ledger.mark_noted(video_id, version=prompt.version, model=reply.model)
    return NoteResult(
        video_id,
        NOTED,
        f"{reply.usage.output_tokens:,} tokens",
        tokens=reply.usage.output_tokens,
        version=prompt.version,
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


def _brief(exc: Exception) -> str:
    return " ".join(str(exc).split())[:120]
