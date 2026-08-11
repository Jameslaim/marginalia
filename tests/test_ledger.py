"""The ledger: state transitions and write ordering.

Mistakes here are silent too — a lost `pending` row causes blind re-fetching,
and a missing `last_accessed` cannot be reconstructed after the fact.

Every test uses a temporary library. Nothing here touches the real one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from marginalia.ledger import (
    FAIL_PERMANENT,
    FAIL_TRANSIENT,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_NEEDS_ASR,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_STORED,
    Ledger,
    LedgerError,
    VideoRow,
    skip_reason,
)

VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "aB_cD-eFgH1"


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "library" / "ledger.db") as instance:
        yield instance


def test_creates_its_own_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "library" / "ledger.db"
    with Ledger(path):
        pass
    assert path.is_file()


def test_unknown_video_is_none(ledger: Ledger) -> None:
    assert ledger.get(VIDEO_ID) is None


def test_an_older_ledger_fails_with_a_clear_message(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS leaves an old table alone, so a ledger from
    before a column was added would otherwise die on an opaque IndexError."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE videos (
          video_id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT,
          transcribed_at TEXT, content_hash TEXT, duplicate_of TEXT,
          first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
          last_accessed TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          fail_kind TEXT, provider TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as caught:
        Ledger(path)
    assert "caption_kind" in str(caught.value)
    assert "re-ingest" in str(caught.value)


# -- the happy path ---------------------------------------------------------


def test_pending_then_stored(ledger: Ledger) -> None:
    pending = ledger.mark_pending(VIDEO_ID)
    assert pending.status == STATUS_PENDING
    assert pending.attempts == 1
    assert pending.first_seen
    assert pending.transcribed_at is None
    assert pending.caption_kind is None

    stored = ledger.mark_stored(
        VIDEO_ID, source="captions", caption_kind="manual", provider="ytdlp"
    )
    assert stored.status == STATUS_STORED
    assert stored.source == "captions"
    assert stored.caption_kind == "manual"
    assert stored.provider == "ytdlp"
    assert stored.transcribed_at is not None
    assert stored.fail_kind is None
    # first_seen survives the transition; last_updated moves with it.
    assert stored.first_seen == pending.first_seen


# -- caption_kind: queryable, beside source -------------------------------


@pytest.mark.parametrize("kind", ["manual", "auto"])
def test_caption_kind_is_recorded(ledger: Ledger, kind: str) -> None:
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_stored(
        VIDEO_ID, source="captions", caption_kind=kind, provider="ytdlp"
    )
    assert row.caption_kind == kind


def test_caption_kind_is_optional(ledger: Ledger) -> None:
    """Whisper transcripts (M4) have a source but no caption kind."""
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_stored(VIDEO_ID, source="whisper", provider="whisper_local")
    assert row.source == "whisper"
    assert row.caption_kind is None


def test_auto_captioned_videos_are_queryable(ledger: Ledger) -> None:
    """The reason it is a column: M3's upgrade pass must find these cheaply,
    without opening every meta.json in the library."""
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_stored(VIDEO_ID, source="captions", caption_kind="auto", provider="ytdlp")
    ledger.mark_pending(OTHER_ID)
    ledger.mark_stored(OTHER_ID, source="captions", caption_kind="manual", provider="ytdlp")

    upgradable = [
        row.video_id
        for row in ledger.list_by_status(STATUS_STORED)
        if row.caption_kind == "auto"
    ]
    assert upgradable == [VIDEO_ID]


def test_re_ingest_of_a_stored_video_is_a_no_op(ledger: Ledger) -> None:
    """The CLI reads this status to decide to skip without a network call."""
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_stored(VIDEO_ID, source="captions", provider="ytdlp")

    before = ledger.get(VIDEO_ID)
    assert before is not None and before.status == STATUS_STORED

    # A second ingest sees `stored` and stops here — no mark_pending, no fetch.
    after = ledger.get(VIDEO_ID)
    assert after == before
    assert after.attempts == 1


# -- crash recovery ---------------------------------------------------------


def test_a_pending_row_is_reattemptable(ledger: Ledger) -> None:
    """A crash between fetch and store leaves `pending`. Re-running resumes."""
    first = ledger.mark_pending(VIDEO_ID)
    assert first.attempts == 1

    second = ledger.mark_pending(VIDEO_ID)
    assert second.status == STATUS_PENDING
    assert second.attempts == 2
    assert second.first_seen == first.first_seen


def test_re_attempt_clears_a_previous_failure(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_failed(VIDEO_ID, FAIL_TRANSIENT)

    retried = ledger.mark_pending(VIDEO_ID)
    assert retried.status == STATUS_PENDING
    assert retried.fail_kind is None
    assert retried.attempts == 2


# -- write ordering is enforced, not merely documented ----------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda l: l.mark_stored(VIDEO_ID, source="captions", provider="ytdlp"),
        lambda l: l.mark_needs_asr(VIDEO_ID),
        lambda l: l.mark_failed(VIDEO_ID, FAIL_PERMANENT),
        lambda l: l.touch_accessed(VIDEO_ID),
    ],
)
def test_writes_require_an_existing_pending_row(ledger: Ledger, call) -> None:
    with pytest.raises(LedgerError):
        call(ledger)


# -- the other routes -------------------------------------------------------


def test_no_captions_is_needs_asr_not_a_failure(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_needs_asr(VIDEO_ID, provider="ytdlp")
    assert row.status == STATUS_NEEDS_ASR
    assert row.fail_kind is None
    assert row.provider == "ytdlp"


def test_mark_failed_records_the_kind(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_failed(VIDEO_ID, FAIL_PERMANENT, provider="ytdlp")
    assert row.status == STATUS_FAILED
    assert row.fail_kind == FAIL_PERMANENT


def test_mark_failed_rejects_an_unknown_kind(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    with pytest.raises(ValueError):
        ledger.mark_failed(VIDEO_ID, "sideways")


# -- last_accessed ----------------------------------------------------------


def test_last_accessed_updates_on_read(ledger: Ledger) -> None:
    """Cannot be reconstructed retroactively; the deletion policy needs it."""
    ledger.mark_pending(VIDEO_ID)
    stored = ledger.mark_stored(VIDEO_ID, source="captions", provider="ytdlp")
    assert stored.last_accessed is None

    ledger.touch_accessed(VIDEO_ID)

    row = ledger.get(VIDEO_ID)
    assert row is not None
    assert row.last_accessed is not None
    # Reading must not disturb anything else about the row.
    assert row.status == STATUS_STORED
    assert row.last_updated == stored.last_updated


# -- listing ----------------------------------------------------------------


def test_list_by_status(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_stored(VIDEO_ID, source="captions", provider="ytdlp")
    ledger.mark_pending(OTHER_ID)
    ledger.mark_needs_asr(OTHER_ID)

    assert [row.video_id for row in ledger.list_by_status(STATUS_STORED)] == [VIDEO_ID]
    assert [row.video_id for row in ledger.list_by_status(STATUS_NEEDS_ASR)] == [OTHER_ID]
    assert ledger.list_by_status(STATUS_FAILED) == []
    assert {row.video_id for row in ledger.list_by_status()} == {VIDEO_ID, OTHER_ID}


def test_ids_are_stored_verbatim(ledger: Ledger) -> None:
    """The ledger must not normalise case either: two rows, not one."""
    ledger.mark_pending("dQw4w9WgXcQ")
    ledger.mark_pending("dqw4w9wgxcq")

    assert len(ledger.list_by_status(STATUS_PENDING)) == 2
    row = ledger.get("dQw4w9WgXcQ")
    assert row is not None and row.video_id == "dQw4w9WgXcQ"


# -- re-attempt policy ------------------------------------------------------
#
# This decides what a second `marg ingest` on the same URL does. It used to
# live in cli.py, which has no tests, and it is policy rather than plumbing.


def row(status: str, **overrides: object) -> VideoRow:
    base = dict(
        video_id=VIDEO_ID,
        status=status,
        source=None,
        caption_kind=None,
        transcribed_at=None,
        content_hash=None,
        duplicate_of=None,
        first_seen="2026-01-01T00:00:00+00:00",
        last_updated="2026-01-01T00:00:00+00:00",
        last_accessed=None,
        attempts=1,
        fail_kind=None,
        provider=None,
    )
    base.update(overrides)
    return VideoRow(**base)  # type: ignore[arg-type]


def test_unseen_video_is_ingested() -> None:
    assert skip_reason(None, transcript_present=False) is None


def test_stored_video_is_skipped() -> None:
    assert skip_reason(row(STATUS_STORED), transcript_present=True) == "already stored"


def test_stored_video_with_a_missing_transcript_is_refetched() -> None:
    """The ledger and the filesystem disagree; the filesystem wins."""
    assert skip_reason(row(STATUS_STORED), transcript_present=False) is None


def test_pending_is_reattempted() -> None:
    """A crash left it in flight."""
    assert skip_reason(row(STATUS_PENDING), transcript_present=False) is None


def test_needs_asr_is_reattempted() -> None:
    """Captions can appear on a video that had none."""
    assert skip_reason(row(STATUS_NEEDS_ASR), transcript_present=False) is None


def test_permanent_failure_is_not_retried() -> None:
    verdict = skip_reason(
        row(STATUS_FAILED, fail_kind=FAIL_PERMANENT), transcript_present=False
    )
    assert verdict == "previously failed (permanent)"


def test_transient_failure_is_retried() -> None:
    assert (
        skip_reason(row(STATUS_FAILED, fail_kind=FAIL_TRANSIENT), transcript_present=False)
        is None
    )


def test_duplicate_is_skipped_and_names_the_canonical_video() -> None:
    verdict = skip_reason(
        row(STATUS_DUPLICATE, duplicate_of=OTHER_ID), transcript_present=False
    )
    assert verdict == f"duplicate of {OTHER_ID}"


def test_duplicate_without_a_canonical_id_still_skips() -> None:
    assert skip_reason(row(STATUS_DUPLICATE), transcript_present=False) == "duplicate"


def test_rejected_is_reattempted_when_named_directly() -> None:
    """A rejection is topic-relative: "irrelevant to some brief". That says
    nothing about a video the user has now handed us by URL, so a direct
    request outranks the earlier judgement."""
    assert skip_reason(row(STATUS_REJECTED), transcript_present=False) is None


def test_unknown_future_status_is_reattempted() -> None:
    """Surplus work is recoverable; silently never retrying is not."""
    assert skip_reason(row("some_m5_status"), transcript_present=False) is None


@pytest.mark.parametrize(
    "status", [STATUS_PENDING, STATUS_NEEDS_ASR, STATUS_REJECTED, STATUS_STORED]
)
def test_a_skip_reason_is_short_enough_for_a_report_line(status: str) -> None:
    verdict = skip_reason(row(status), transcript_present=True)
    assert verdict is None or (len(verdict) < 40 and "\n" not in verdict)


# -- the rejections table exists now, though M2 is what fills it ------------


def test_rejections_table_exists(ledger: Ledger) -> None:
    names = {
        row[0]
        for row in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"videos", "rejections"} <= names
