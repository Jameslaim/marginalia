"""Retry the transcript fetches that failed transiently.

**Why this exists at all.** The system already recorded these videos as
retryable — that is what rule 6's write-before-fetch ordering buys — and then
nothing ever read the state back. 119 of them accumulated: 108 on a single day,
in two sustained bursts of YouTube throttling, at five to eight failures a
minute for a quarter of an hour. They are not a missing capability, they are an
unread flag.

**The thing that will bite, and the shape of the defence.** A loop over 119
videos against a throttling provider is exactly how the backlog was created.
So the run keeps discovery's fetch delay, and it stops on a streak of transient
failures rather than walking the rest collecting refusals — because a burst of
transient failures *is* the provider being authoritative about rate, which is
rule 24 one layer below the model provider. A run that dies at video four and
says so is worth more than one that turns 119 retryable videos into 119
retryable videos with higher attempt counts.

**Nothing here condemns a video.** No threshold promotes `transient` to
`permanent`. A video written off is rule 8's invisible error with extra steps,
and an attempt count cannot tell a deleted video from an unlucky one — it is
reported so a person can look.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ..config import Config
from ..contracts import (
    GONE,
    NO_CAPTIONS,
    RECOVERED,
    STILL_FAILING,
    STOP_DONE,
    STOP_LIMIT,
    STOP_THROTTLED,
    RefetchResult,
    RefetchRun,
    SearchResult,
)
from ..core.ledger import Ledger
from ..core.library import Library
from ..providers import get_provider
from ..providers.base import PermanentError, TransientError

log = logging.getLogger(__name__)

__all__ = ["refetch"]

# Consecutive transient failures before the run gives up. Three, matching the
# note drain, and for a sharper reason: the failure being retried here is the
# one that arrives in bursts, so a streak is the signature of the condition
# rather than bad luck about one video.
_FAILURE_STREAK = 3


def refetch(
    config: Config,
    ledger: Ledger,
    library: Library,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> RefetchRun:
    """Walk the transient failures, known-wanted first, and try again."""
    started = time.monotonic()
    rows = ledger.videos_to_refetch(limit=limit)
    run = RefetchRun(considered=len(rows), dry_run=dry_run)

    if dry_run:
        # Touches nothing: no ledger write, no network call, no attempt count.
        for row in rows:
            run.results.append(
                RefetchResult(
                    video_id=row.video_id,
                    outcome=STILL_FAILING,
                    detail="would retry",
                    attempts=row.attempts,
                    wanted=ledger.was_wanted(row.video_id),
                )
            )
        run.elapsed_s = time.monotonic() - started
        return run

    pause = sleep or time.sleep
    provider = get_provider(config.transcript_order[0])
    delay = config.discover.fetch_delay_s
    streak = 0

    try:
        for index, row in enumerate(rows):
            if index:
                pause(delay)
            wanted = ledger.was_wanted(row.video_id)
            result, outcome, detail = _try(ledger, provider, row.video_id)

            if outcome == RECOVERED and result is not None:
                _store(library, ledger, config, provider, row.video_id, result)

            run.results.append(
                RefetchResult(
                    video_id=row.video_id,
                    outcome=outcome,
                    detail=detail,
                    # After the attempt, so the number is what the video has
                    # actually cost so far.
                    attempts=(ledger.get(row.video_id) or row).attempts,
                    wanted=wanted,
                )
            )

            streak = streak + 1 if outcome == STILL_FAILING else 0
            if streak >= _FAILURE_STREAK:
                log.warning(
                    "refetch: %d transient failures in a row — stopping. Every "
                    "video is still retryable, and this is what created the "
                    "backlog in the first place",
                    streak,
                )
                run.stopped_because = STOP_THROTTLED
                break
        else:
            run.stopped_because = STOP_LIMIT if limit is not None else STOP_DONE
    finally:
        run.elapsed_s = time.monotonic() - started
    return run


def _try(ledger: Ledger, provider, video_id: str):
    """One fetch, with the ledger row written before it. Rule 6."""
    ledger.mark_pending(video_id)
    try:
        result = provider.fetch(video_id)
    except PermanentError as exc:
        # The only place this run may condemn a video, and it is the provider's
        # claim rather than ours: deleted, private, region-locked.
        log.info("refetch: %s is permanently unavailable (%s)", video_id, exc)
        ledger.mark_failed(video_id, "permanent", provider=provider.name)
        return None, GONE, str(exc)[:120]
    except TransientError as exc:
        log.info("refetch: %s failed again (%s)", video_id, exc)
        ledger.mark_failed(video_id, "transient", provider=provider.name)
        return None, STILL_FAILING, str(exc)[:120]

    if result is None:
        # Not a failure — a route. The fetch worked and the video has no
        # captions, which is M4's queue rather than this one's.
        ledger.mark_needs_asr(video_id, provider=provider.name)
        return None, NO_CAPTIONS, "no caption track"
    return result, RECOVERED, ""


def _store(library: Library, ledger: Ledger, config: Config, provider, video_id: str, result):
    """Write the transcript and the metadata, then the ledger. File first.

    The same ordering `flow/discover.py` uses, for the same reason: a crash
    between them costs one fetch, while a ledger row claiming a transcript that
    was never written costs a video nothing will ever revisit.
    """
    from datetime import datetime, timezone

    count = library.write_transcript(video_id, result.lines)
    library.write_meta(
        video_id,
        {
            "video_id": video_id,
            "title": result.title or video_id,
            "channel": result.channel or "",
            "channel_id": result.channel_id or None,
            "duration_s": result.duration_s,
            "language": result.language,
            "upload_date": result.upload_date,
            "source": result.source,
            "caption_kind": result.caption_kind,
            "provider": config.transcript_order[0],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chapters": result.chapters,
            "original_url": f"https://www.youtube.com/watch?v={video_id}",
            "line_count": count,
        },
    )
    ledger.mark_has_transcript(
        video_id,
        source=result.source,
        caption_kind=result.caption_kind,
        provider=config.transcript_order[0],
        channel_id=result.channel_id or None,
    )
