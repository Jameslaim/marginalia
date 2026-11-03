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
    ACQUIRE_FAILED_PERMANENT,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRE_TRANSLATED_ONLY,
    GONE,
    NO_CAPTIONS,
    TRANSLATED_ONLY,
    RECOVERED,
    STILL_FAILING,
    STOP_DONE,
    STOP_LIMIT,
    STOP_THROTTLED,
    RefetchResult,
    RefetchRun,
)
from ..core.ledger import Ledger
from ..core.library import Library
from .acquire import FAILURE_STREAK, acquire, transcript_providers
from .store import reconcile_stored, store_transcript

log = logging.getLogger(__name__)

__all__ = ["refetch"]

# Consecutive transient failures before the run gives up: `acquire.FAILURE_STREAK`,
# which discovery reads too.
_FAILURE_STREAK = FAILURE_STREAK


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
    # The whole configured chain, as ingest and discovery use it. This used to
    # be `transcript_order[0]` alone, so a second provider added for exactly
    # these failures was never asked.
    providers = transcript_providers(config)
    delay = config.discover.fetch_delay_s
    streak = 0

    try:
        for index, row in enumerate(rows):
            if index:
                pause(delay)
            wanted = ledger.was_wanted(row.video_id)
            if reconcile_stored(library, ledger, row.video_id):
                # The bytes arrived on an earlier attempt and the row never
                # heard. Fetching again could only hit the immutable file.
                result, outcome, detail = None, RECOVERED, "already on disk; row recorded"
            else:
                acquisition = acquire(ledger, row.video_id, providers)
                outcome, detail = _outcome(acquisition)
                if acquisition.acquired:
                    assert acquisition.result is not None
                    store_transcript(
                        library, ledger, row.video_id, acquisition.result,
                        provider=acquisition.provider or "unknown",
                    )

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


def _outcome(acquisition) -> tuple[str, str]:
    """Refetch's four words for the shared acquisition outcomes.

    The only place this run may condemn a video is a provider's own permanent
    claim — deleted, private, region-locked — and that is `acquire`'s call.
    No captions is a route (M4's queue), not a failure.
    """
    if acquisition.acquired:
        return RECOVERED, ""
    if acquisition.outcome == ACQUIRE_FAILED_PERMANENT:
        return GONE, acquisition.detail[:120]
    if acquisition.outcome == ACQUIRE_NO_CAPTIONS:
        return NO_CAPTIONS, "no caption track"
    if acquisition.outcome == ACQUIRE_TRANSLATED_ONLY:
        return TRANSLATED_ONLY, acquisition.detail[:120]
    return STILL_FAILING, acquisition.detail[:120]
