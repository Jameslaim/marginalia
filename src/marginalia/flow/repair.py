"""`marg refetch --replace-translated`: rule 3's one exception, and nothing broader.

Measured on the library: 19 stored transcripts were machine translations — 17
of English speech into Arabic, 2 into Bengali — chosen by a provider that
trusted the video's reported language over its audio (rule 40). Every layer
after the fetch read them as the speaker's words. This replaces exactly those:
a stored video is replaced only when rule 40's selector, on a fresh listing,
picks a track in a *different language* from the one stored, which is what a
translation is. A stored track in the same language is accepted and never
touched — a plain `hi` entry beside `hi-orig` is the same speech — and a
stored track id (`en-eEY6OEpapPo`) on an accepted track only has its language
cleaned in `meta.json`.

Nothing is deleted. The old file is kept as `transcript.translated.txt`, every
change is a new `transcript_repairs` row, and a replaced video's note is marked
stale so `marg note --regenerate <id>` rewrites it from the new transcript.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Sequence

from ..config import Config
from ..contracts import (
    ACQUIRE_FAILED_PERMANENT,
    ACQUIRE_FAILED_TRANSIENT,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRE_TRANSLATED_ONLY,
    REPAIR_ACCEPTED,
    REPAIR_FAILED,
    REPAIR_LANGUAGE_CLEANED,
    REPAIR_REPLACED,
    REPAIR_SKIPPED,
    STATUS_HAS_TRANSCRIPT,
    STOP_DONE,
    STOP_LIMIT,
    STOP_THROTTLED,
    RepairResult,
    RepairRun,
    TranscriptResult,
)
from ..core.ledger import Ledger, RepairRow
from ..core.library import Library
from ..enrich.timestamps import transcript_end_ms
from ..providers.base import TranscriptProvider, clean_language
from .acquire import acquire, transcript_providers

log = logging.getLogger(__name__)

__all__ = ["MIN_SPAN_SHARE", "replace_translated"]

# The new transcript must cover at least this share of the old one's span (the
# latest caption end time), or the video is skipped and counted. Span rather
# than characters or lines: a translation and its original are cut cue for cue
# from one recognition pass, so they cover the same stretch of the video, while
# their character counts differ by language and script — Arabic and English, or
# Devanagari and Latin, are not comparable that way. 0.8 leaves room for a few
# trailing cues dropped by the translation or the original and refuses what a
# real original never is: a partial or broken track standing in for a whole one.
MIN_SPAN_SHARE = 0.8

# Consecutive transient failures before the run stops, as refetch does: a
# streak is the provider being authoritative about rate.
_FAILURE_STREAK = 3


def _primary(code: str) -> str:
    return clean_language(code).split("-")[0]


def replace_translated(
    config: Config,
    ledger: Ledger,
    library: Library,
    *,
    video_ids: Sequence[str] = (),
    dry_run: bool = False,
    limit: int | None = None,
    providers: Sequence[TranscriptProvider] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RepairRun:
    """Judge each stored video's track afresh, and repair only what rule 40 rejects.

    With ``video_ids``, only those; otherwise every video with a stored
    transcript. A dry run fetches and judges exactly as a real run does, and
    writes nothing — no file, no ledger row, no `meta.json`.
    """
    started = time.monotonic()
    providers = list(providers) if providers is not None else transcript_providers(config)
    pause = sleep or time.sleep
    if video_ids:
        rows = [ledger.get(video_id) for video_id in video_ids]
    else:
        rows = ledger.list_by_status(STATUS_HAS_TRANSCRIPT)
    run = RepairRun(dry_run=dry_run)
    streak = 0
    fetched_any = False

    for number, (requested, row) in enumerate(zip(video_ids or [r.video_id for r in rows], rows)):
        if limit is not None and number >= limit:
            run.stopped_because = STOP_LIMIT
            break
        run.considered += 1
        if row is None or row.status != STATUS_HAS_TRANSCRIPT or not library.has_transcript(requested):
            run.results.append(RepairResult(requested, REPAIR_SKIPPED, "no stored transcript"))
            continue
        meta = library.read_meta(requested) or {}
        stored_raw = str(meta.get("language") or "")
        stored = clean_language(stored_raw)

        if fetched_any:
            pause(config.discover.fetch_delay_s)
        fetched_any = True
        # The one fetch path, writing no state: the row says `has_transcript`
        # and goes on saying so whatever this listing returns.
        acquisition = acquire(ledger, requested, providers, record=False)
        if acquisition.outcome == ACQUIRE_FAILED_TRANSIENT:
            streak += 1
            run.results.append(RepairResult(requested, REPAIR_FAILED, acquisition.detail[:120],
                                            old_language=stored_raw))
            if streak >= _FAILURE_STREAK:
                run.stopped_because = STOP_THROTTLED
                break
            continue
        streak = 0
        if acquisition.outcome == ACQUIRE_FAILED_PERMANENT:
            run.results.append(RepairResult(requested, REPAIR_FAILED, acquisition.detail[:120],
                                            old_language=stored_raw))
            continue
        if acquisition.outcome in (ACQUIRE_NO_CAPTIONS, ACQUIRE_TRANSLATED_ONLY):
            # Nothing better to replace it with; the stored transcript stays.
            reason = "only translated captions" if acquisition.outcome == ACQUIRE_TRANSLATED_ONLY \
                else "no caption track now"
            run.results.append(RepairResult(requested, REPAIR_SKIPPED, reason, old_language=stored_raw))
            continue

        result = acquisition.result
        assert result is not None
        if _primary(result.language) == _primary(stored):
            # The same spoken language: accepted, and the transcript untouched.
            if stored_raw != stored:
                if not dry_run:
                    _clean_language(library, ledger, requested, meta, stored_raw, stored)
                run.results.append(RepairResult(requested, REPAIR_LANGUAGE_CLEANED, "",
                                                old_language=stored_raw, new_language=stored,
                                                new_track=result.caption_track))
            else:
                run.results.append(RepairResult(requested, REPAIR_ACCEPTED, "",
                                                old_language=stored_raw, new_language=result.language,
                                                new_track=result.caption_track))
            continue

        # A different language: the stored transcript is a translation.
        # Measuring its span is not a read of its content (as hashing is not),
        # so it is not an access for the deletion policy.
        old_end = transcript_end_ms(library.read_transcript(requested))
        new_end = max((line.end_ms for line in result.lines), default=0)
        share = new_end / old_end if old_end else (1.0 if new_end else 0.0)
        if not result.lines or share < MIN_SPAN_SHARE:
            run.results.append(RepairResult(
                requested, REPAIR_SKIPPED,
                f"new track covers {share:.0%} of the old one's span (under {MIN_SPAN_SHARE:.0%})",
                old_language=stored_raw, new_language=result.language, new_track=result.caption_track,
            ))
            continue
        if not dry_run:
            _replace(library, ledger, requested, meta, result, acquisition.provider or "unknown",
                     stored_raw, old_end, new_end)
        run.results.append(RepairResult(
            requested, REPAIR_REPLACED, f"{len(result.lines)} lines, span {share:.0%} of the old",
            old_language=stored_raw, new_language=result.language, new_track=result.caption_track,
        ))
    else:
        run.stopped_because = STOP_DONE
    run.elapsed_s = time.monotonic() - started
    return run


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_language(library, ledger, video_id, meta, stored_raw, stored) -> None:
    """An accepted track whose stored language carried a track id: `meta.json`
    gets the clean code and keeps the id as `caption_track`. The transcript is
    not touched. (The ledger has no language column; the repair row is its
    record.)"""
    library.write_meta(video_id, {**meta, "language": stored,
                                  "caption_track": meta.get("caption_track") or stored_raw})
    ledger.record_repair(RepairRow(video_id, REPAIR_LANGUAGE_CLEANED, _now(),
                                   old_language=stored_raw, new_language=stored,
                                   old_track=stored_raw, new_track=stored_raw))


def _replace(library, ledger, video_id, meta, result: TranscriptResult, provider,
             stored_raw, old_end, new_end) -> None:
    """Rule 3's one exception, in its four parts: the old file kept, the new one
    written, the replacement recorded, the note marked stale."""
    old_hash = library.transcript_digest(video_id)
    count = library.replace_translated_transcript(video_id, result.lines)
    new_hash = library.transcript_digest(video_id)
    library.write_meta(video_id, {
        **meta,
        "language": result.language,
        "caption_track": result.caption_track or None,
        "audio_label": result.audio_label or None,
        "caption_kind": result.caption_kind,
        "provider": provider,
        "line_count": count,
        "replaced_at": _now(),
        "replaced_language": stored_raw,
    })
    ledger.mark_has_transcript(video_id, source=result.source, caption_kind=result.caption_kind,
                               provider=provider, content_hash=new_hash)
    ledger.record_repair(RepairRow(video_id, REPAIR_REPLACED, _now(),
                                   old_language=stored_raw, new_language=result.language,
                                   old_track=meta.get("caption_track") or stored_raw,
                                   new_track=result.caption_track, old_hash=old_hash,
                                   new_hash=new_hash, old_end_ms=old_end, new_end_ms=new_end))
    ledger.mark_note_stale(video_id)
