"""Storing a fetched transcript: the one path every acquirer takes.

Ingest, discovery and refetch each used to carry their own copy of this — write
the transcript, hash it, write ``meta.json``, mark the ledger — and with it
their own copy of the ``meta.json`` schema. Three copies of an ordering rule
and a schema are three places for one of them to drift, and rule 25 is the
record of what happens when a guard lives in one caller and not its siblings.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..contracts import TranscriptResult
from ..core.ledger import Ledger
from ..core.library import Library

__all__ = ["store_transcript"]


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
