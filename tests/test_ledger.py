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
    STATUS_FAILED,
    STATUS_NEEDS_ASR,
    STATUS_PENDING,
    STATUS_STORED,
    Ledger,
    LedgerError,
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


# -- the happy path ---------------------------------------------------------


def test_pending_then_stored(ledger: Ledger) -> None:
    pending = ledger.mark_pending(VIDEO_ID)
    assert pending.status == STATUS_PENDING
    assert pending.attempts == 1
    assert pending.first_seen
    assert pending.transcribed_at is None

    stored = ledger.mark_stored(VIDEO_ID, source="captions", provider="ytdlp")
    assert stored.status == STATUS_STORED
    assert stored.source == "captions"
    assert stored.provider == "ytdlp"
    assert stored.transcribed_at is not None
    assert stored.fail_kind is None
    # first_seen survives the transition; last_updated moves with it.
    assert stored.first_seen == pending.first_seen


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


# -- the rejections table exists now, though M2 is what fills it ------------


def test_rejections_table_exists(ledger: Ledger) -> None:
    names = {
        row[0]
        for row in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"videos", "rejections"} <= names
