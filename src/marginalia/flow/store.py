"""Storing a fetched transcript: the one path every acquirer takes.

Ingest, discovery and refetch each used to carry their own copy of this — write
the transcript, hash it, write ``meta.json``, mark the ledger — and with it
their own copy of the ``meta.json`` schema. Three copies of an ordering rule
and a schema are three places for one of them to drift, and rule 25 is the
record of what happens when a guard lives in one caller and not its siblings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from ..contracts import STATUS_DUPLICATE, STATUS_HAS_TRANSCRIPT, TranscriptResult, VideoRow
from ..core.ledger import Ledger
from ..core.library import Library

log = logging.getLogger(__name__)

__all__ = ["needs_reconciling", "reconcile_stored", "store_transcript", "unreconciled"]

# Statuses a row may hold while its transcript is on disk only because a store
# was interrupted. `has_transcript` is the agreeing state, and `duplicate` has
# its own canonical copy — reconciling one would make a second claim to it.
_SETTLED = frozenset({STATUS_HAS_TRANSCRIPT, STATUS_DUPLICATE})


def store_transcript(
    library: Library,
    ledger: Ledger,
    video_id: str,
    result: TranscriptResult,
    *,
    provider: str,
    title: str | None = None,
    channel: str | None = None,
    original_url: str | None = None,
) -> tuple[int, str | None]:
    """Write ``transcript.txt`` and ``meta.json``, then the ledger row. Returns
    the line count and the transcript's digest.

    **File first, row second.** A crash between them leaves the row `pending`
    — the state the next run expects after a fetch that did not finish — while
    the other order would leave a row claiming bytes that are not there.

    The digest is read back from the file rather than computed from
    ``result.lines``, so ``content_hash`` records what landed on disk, which is
    the only thing ``marg doctor`` can later compare against.

    The `pending` that a crash leaves is not repaired by fetching again — the
    file is immutable and `write_transcript` refuses — so it is repaired by
    `reconcile_stored`, which reads the bytes already there.

    ``title`` and ``channel`` are the caller's fallbacks for a provider that
    did not report them — a search result's, typically.
    """
    count = library.write_transcript(video_id, result.lines)
    digest = library.transcript_digest(video_id)
    library.write_meta(
        video_id,
        {
            "video_id": video_id,
            "title": result.title or title or video_id,
            "channel": result.channel or channel or "",
            "channel_id": result.channel_id or None,
            "duration_s": result.duration_s,
            "language": result.language,
            "upload_date": result.upload_date,
            "source": result.source,
            "caption_kind": result.caption_kind,
            "caption_track": result.caption_track or None,
            "audio_label": result.audio_label or None,
            "provider": provider,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chapters": result.chapters,
            "original_url": original_url or f"https://www.youtube.com/watch?v={video_id}",
            "line_count": count,
        },
    )
    ledger.mark_has_transcript(
        video_id,
        source=result.source,
        caption_kind=result.caption_kind,
        provider=provider,
        content_hash=digest,
        # `None` rather than `""` so the ledger's COALESCE keeps whatever an
        # earlier store recorded: a provider that stopped reporting the channel
        # must not erase one that did.
        channel_id=result.channel_id or None,
    )
    return count, digest


def needs_reconciling(library: Library, row: VideoRow) -> bool:
    """Is a transcript on disk that this row does not record?

    The shape an interrupted `store_transcript` leaves: the file and usually
    `meta.json` written, the row still `pending`. Nothing notes a `pending`
    video, `write_transcript` refuses the re-fetch, and `refetch` only asks
    about transient failures — so without this the video is stranded for good.
    """
    return row.status not in _SETTLED and library.has_transcript(row.video_id)


def unreconciled(library: Library, rows: Sequence[VideoRow]) -> list[str]:
    return [row.video_id for row in rows if needs_reconciling(library, row)]


def reconcile_stored(library: Library, ledger: Ledger, video_id: str) -> bool:
    """Record a transcript that is already on disk. Returns whether it did.

    **The bytes are the record and are never touched**: the digest is read from
    the file as it is, exactly as `store_transcript` reads it back. Idempotent —
    a row that already agrees with the disk is left alone, so calling this
    twice, or on a healthy video, changes nothing.

    `meta.json` is written only when the interruption came before it, and then
    only with what the file itself can tell: its id and line count. Provenance
    nobody recorded is written as unknown rather than guessed.
    """
    row = ledger.get(video_id)
    if row is None or not needs_reconciling(library, row):
        return False
    digest = library.transcript_digest(video_id)
    meta = library.read_meta(video_id)
    if meta is None and library.meta_path(video_id).is_file():
        # Present and unreadable — hand-edited, or cut off mid-write. It is
        # left exactly as it is: it used to be overwritten with "unknown"
        # provenance, destroying whatever it held, by the one command whose
        # promise is that it only records what is already there.
        log.warning(
            "store: %s has a meta.json that could not be read; left untouched", video_id
        )
        meta = {}
    elif meta is None:
        meta = {
            "video_id": video_id,
            "title": video_id,
            "channel": "",
            "source": "unknown",
            "provider": "unknown",
            "line_count": _line_count(library, video_id),
            "original_url": f"https://www.youtube.com/watch?v={video_id}",
            "reconciled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        library.write_meta(video_id, meta)
    ledger.mark_has_transcript(
        video_id,
        source=str(meta.get("source") or "unknown"),
        caption_kind=meta.get("caption_kind") or None,
        provider=str(meta.get("provider") or "unknown"),
        content_hash=digest,
        channel_id=meta.get("channel_id") or None,
    )
    log.warning(
        "store: %s had a transcript on disk its row did not record (status %s); reconciled",
        video_id,
        row.status,
    )
    return True


def _line_count(library: Library, video_id: str) -> int:
    """Lines in the stored file. An integrity read, like the digest: no access
    is recorded and the text goes nowhere."""
    return library.transcript_path(video_id).read_bytes().count(b"\n")
