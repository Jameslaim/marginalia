"""The ledger: state transitions and write ordering.

Mistakes here are silent too — a lost `pending` row causes blind re-fetching,
and a missing `last_accessed` cannot be reconstructed after the fact.

Every test uses a temporary library. Nothing here touches the real one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from marginalia.contracts import (
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STATUS_PENDING,
    STATUS_SEEN,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    VIDEO_STATUSES,
    VideoRow,
)
from marginalia.core.ledger import (
    FAIL_PERMANENT,
    FAIL_TRANSIENT,
    STAGE_METADATA,
    STAGE_NOTE,
    STAGE_OPENING,
    STAGE_SEARCH,
    Ledger,
    LedgerError,
    in_library,
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


def test_a_ledger_with_the_old_rejections_table_is_refused(tmp_path: Path) -> None:
    """`rejections` was replaced by `decisions` in M2a. Left alone it would be
    an M1 ledger quietly running M2a code — every verdict written to a table
    nothing reads, and Gate 2 permanently finding nothing."""
    import sqlite3

    path = tmp_path / "m1.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE videos (
          video_id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT,
          caption_kind TEXT, transcribed_at TEXT, content_hash TEXT,
          duplicate_of TEXT, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
          last_accessed TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          fail_kind TEXT, provider TEXT
        );
        CREATE TABLE rejections (
          video_id TEXT NOT NULL, topic TEXT NOT NULL, reason TEXT,
          rejected_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as caught:
        Ledger(path)
    message = str(caught.value)
    assert "rejections" in message
    assert "decisions" in message
    assert "re-ingest" in message


def test_a_ledger_missing_a_decisions_column_is_refused(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE videos (
          video_id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT,
          caption_kind TEXT, transcribed_at TEXT, content_hash TEXT,
          duplicate_of TEXT, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
          last_accessed TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          fail_kind TEXT, provider TEXT
        );
        CREATE TABLE decisions (
          video_id TEXT NOT NULL, topic TEXT NOT NULL, verdict TEXT NOT NULL,
          reason TEXT, decided_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError) as caught:
        Ledger(path)
    assert "stage" in str(caught.value)


# -- the happy path ---------------------------------------------------------


def test_pending_then_stored(ledger: Ledger) -> None:
    pending = ledger.mark_pending(VIDEO_ID)
    assert pending.status == STATUS_PENDING
    assert pending.attempts == 1
    assert pending.first_seen
    assert pending.transcribed_at is None
    assert pending.caption_kind is None

    stored = ledger.mark_has_transcript(
        VIDEO_ID, source="captions", caption_kind="manual", provider="ytdlp"
    )
    assert stored.status == STATUS_HAS_TRANSCRIPT
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
    row = ledger.mark_has_transcript(
        VIDEO_ID, source="captions", caption_kind=kind, provider="ytdlp"
    )
    assert row.caption_kind == kind


def test_caption_kind_is_optional(ledger: Ledger) -> None:
    """Whisper transcripts (M4) have a source but no caption kind."""
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_has_transcript(VIDEO_ID, source="whisper", provider="whisper_local")
    assert row.source == "whisper"
    assert row.caption_kind is None


def test_auto_captioned_videos_are_queryable(ledger: Ledger) -> None:
    """The reason it is a column: M3's upgrade pass must find these cheaply,
    without opening every meta.json in the library."""
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_has_transcript(VIDEO_ID, source="captions", caption_kind="auto", provider="ytdlp")
    ledger.mark_pending(OTHER_ID)
    ledger.mark_has_transcript(OTHER_ID, source="captions", caption_kind="manual", provider="ytdlp")

    upgradable = [
        row.video_id
        for row in ledger.list_by_status(STATUS_HAS_TRANSCRIPT)
        if row.caption_kind == "auto"
    ]
    assert upgradable == [VIDEO_ID]


def test_re_ingest_of_a_stored_video_is_a_no_op(ledger: Ledger) -> None:
    """The CLI reads this status to decide to skip without a network call."""
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp")

    before = ledger.get(VIDEO_ID)
    assert before is not None and before.status == STATUS_HAS_TRANSCRIPT

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
        lambda l: l.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp"),
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
    stored = ledger.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp")
    assert stored.last_accessed is None

    ledger.touch_accessed(VIDEO_ID)

    row = ledger.get(VIDEO_ID)
    assert row is not None
    assert row.last_accessed is not None
    # Reading must not disturb anything else about the row.
    assert row.status == STATUS_HAS_TRANSCRIPT
    assert row.last_updated == stored.last_updated


# -- listing ----------------------------------------------------------------


def test_list_by_status(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp")
    ledger.mark_pending(OTHER_ID)
    ledger.mark_needs_asr(OTHER_ID)

    assert [row.video_id for row in ledger.list_by_status(STATUS_HAS_TRANSCRIPT)] == [VIDEO_ID]
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


def test_has_transcript_is_skipped() -> None:
    verdict = skip_reason(row(STATUS_HAS_TRANSCRIPT), transcript_present=True)
    assert verdict == "already have the transcript"


def test_has_transcript_with_a_missing_file_is_refetched() -> None:
    """The ledger and the filesystem disagree; the filesystem wins."""
    assert skip_reason(row(STATUS_HAS_TRANSCRIPT), transcript_present=False) is None


def test_seen_is_reattempted() -> None:
    """`seen` means known to us and holding nothing, so there is work to do.

    This row replaces the old `rejected` one. That state re-attempted for a
    reason worth keeping: a rejection is topic-relative — "irrelevant to some
    brief" — which says nothing about a video the user has now handed us by
    URL. B1 made that reasoning structural rather than a special case. There
    is no rejected *state* any more; relevance lives in `decisions` against a
    topic, and what is left on the row is simply that nothing is held.
    """
    assert skip_reason(row(STATUS_SEEN), transcript_present=False) is None


def test_seen_is_reattempted_even_holding_a_stray_file() -> None:
    """`seen` says the ledger holds nothing. If a file is somehow present the
    two disagree, and as everywhere else the filesystem is not trusted to
    overrule the ledger into skipping work."""
    assert skip_reason(row(STATUS_SEEN), transcript_present=True) is None


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


def test_unknown_future_status_is_reattempted() -> None:
    """Surplus work is recoverable; silently never retrying is not."""
    assert skip_reason(row("some_b7_status"), transcript_present=False) is None


def test_every_status_has_a_skip_reason_test() -> None:
    """The table above must cover the vocabulary, not most of it.

    A state added without a policy decision silently falls through to the
    re-attempt default, which is the safe direction but not a decision anyone
    made. This fails the moment `VIDEO_STATUSES` grows."""
    covered = {
        STATUS_SEEN,
        STATUS_PENDING,
        STATUS_HAS_TRANSCRIPT,
        STATUS_NEEDS_ASR,
        STATUS_FAILED,
        STATUS_DUPLICATE,
    }
    assert covered == VIDEO_STATUSES


@pytest.mark.parametrize("status", sorted(VIDEO_STATUSES))
def test_a_skip_reason_is_short_enough_for_a_report_line(status: str) -> None:
    verdict = skip_reason(row(status), transcript_present=True)
    assert verdict is None or (len(verdict) < 40 and "\n" not in verdict)


# -- decisions --------------------------------------------------------------
#
# Every verdict is logged, not only the negative ones — a rejection log alone
# cannot answer "why did the judge keep this?" months later, and none of it can
# be reconstructed retroactively.


def test_the_schema_is_the_m2a_shape(ledger: Ledger) -> None:
    names = {
        row[0]
        for row in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"videos", "decisions", "quota"} <= names
    assert "rejections" not in names


def test_a_decision_round_trips(ledger: Ledger) -> None:
    written = ledger.log_decision(
        VIDEO_ID, "prompt caching", stage=STAGE_SEARCH, verdict=VERDICT_MAYBE,
        reason="new candidate",
    )
    assert written.stage == STAGE_SEARCH
    assert written.verdict == VERDICT_MAYBE
    assert written.decided_at

    found = ledger.latest_decision(VIDEO_ID, "prompt caching")
    assert found == written


def test_a_decision_needs_no_video_row(ledger: Ledger) -> None:
    """Search candidates have not been touched. Inserting a `videos` row for
    all 25 hits of every query would claim work was in flight that never
    started — so `decisions` is deliberately not foreign-keyed."""
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert ledger.get(VIDEO_ID) is None


def test_decision_topics_are_normalised_on_the_way_in(ledger: Ledger) -> None:
    ledger.log_decision(
        VIDEO_ID, "  Prompt   CACHING ", stage=STAGE_SEARCH, verdict=VERDICT_NO
    )
    row = ledger.latest_decision(VIDEO_ID, "prompt caching")
    assert row is not None and row.topic == "prompt caching"


def test_a_decision_is_found_however_the_topic_is_typed(ledger: Ledger) -> None:
    ledger.log_decision(VIDEO_ID, "prompt caching", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert ledger.latest_decision(VIDEO_ID, "Prompt  Caching") is not None


def test_a_decision_is_topic_relative_not_global(ledger: Ledger) -> None:
    """The whole reason the topic column exists: a video rejected for one
    enquiry must not be invisible to a different one."""
    ledger.log_decision(VIDEO_ID, "prompt caching", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert ledger.latest_decision(VIDEO_ID, "prompt caching", verdict=VERDICT_NO) is not None
    assert ledger.latest_decision(VIDEO_ID, "agent architecture", verdict=VERDICT_NO) is None


def test_a_decision_is_video_relative(ledger: Ledger) -> None:
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert ledger.latest_decision(OTHER_ID, "topic", verdict=VERDICT_NO) is None


def test_the_latest_decision_wins(ledger: Ledger) -> None:
    """A later stage overrules an earlier one; the earlier row is kept."""
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_SEARCH, verdict=VERDICT_MAYBE)
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_METADATA, verdict=VERDICT_YES)

    latest = ledger.latest_decision(VIDEO_ID, "topic")
    assert latest is not None and latest.stage == STAGE_METADATA
    assert len(ledger.decisions_for(VIDEO_ID)) == 2


def test_decisions_can_be_filtered_by_verdict(ledger: Ledger) -> None:
    """Gate 2 asks only one question: is there a `no` for this topic?"""
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_METADATA, verdict=VERDICT_YES)

    assert ledger.latest_decision(VIDEO_ID, "topic").verdict == VERDICT_YES
    no = ledger.latest_decision(VIDEO_ID, "topic", verdict=VERDICT_NO)
    assert no is not None and no.stage == STAGE_SEARCH


def test_every_stage_and_verdict_is_accepted(ledger: Ledger) -> None:
    for stage in (STAGE_SEARCH, STAGE_METADATA, STAGE_OPENING, STAGE_NOTE):
        for verdict in (VERDICT_YES, VERDICT_NO, VERDICT_MAYBE):
            ledger.log_decision(VIDEO_ID, "topic", stage=stage, verdict=verdict)
    assert len(ledger.decisions_for(VIDEO_ID)) == 12


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": "guessing", "verdict": VERDICT_NO},
        {"stage": STAGE_SEARCH, "verdict": "nope"},
        {"stage": STAGE_SEARCH, "verdict": "NO"},
    ],
)
def test_an_unknown_stage_or_verdict_is_refused(ledger: Ledger, kwargs: dict) -> None:
    """A typo'd verdict is invisible: Gate 2 looks for `no` and finds nothing,
    forever. Better to fail at the write."""
    with pytest.raises(ValueError):
        ledger.log_decision(VIDEO_ID, "topic", **kwargs)


def test_decided_on_is_a_bare_date_for_the_report_line(ledger: Ledger) -> None:
    row = ledger.log_decision(VIDEO_ID, "topic", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert len(row.decided_on) == 10
    assert row.decided_on == row.decided_at[:10]
    assert row.decided_on.count("-") == 2


def test_ids_are_not_normalised_in_decisions_either(ledger: Ledger) -> None:
    """The topic is lowercased. The video ID is never touched."""
    ledger.log_decision("dQw4w9WgXcQ", "Topic", stage=STAGE_SEARCH, verdict=VERDICT_NO)
    assert ledger.latest_decision("dQw4w9WgXcQ", "topic") is not None
    assert ledger.latest_decision("dqw4w9wgxcq", "topic") is None


# -- quota ------------------------------------------------------------------
#
# Keyed by UTC calendar date, so the daily reset is the key changing rather
# than a job anyone has to remember to run.


def test_quota_starts_at_zero(ledger: Ledger) -> None:
    assert ledger.quota_used("youtube_api") == 0


def test_quota_accumulates(ledger: Ledger) -> None:
    assert ledger.spend_quota("youtube_api", 100) == 100
    assert ledger.spend_quota("youtube_api", 1) == 101
    assert ledger.quota_used("youtube_api") == 101


def test_quota_is_per_provider(ledger: Ledger) -> None:
    ledger.spend_quota("youtube_api", 100)
    assert ledger.quota_used("some_other_api") == 0


def test_quota_resets_on_the_date_rollover(ledger: Ledger) -> None:
    """No reset job exists, and none is wanted: yesterday's spend simply lives
    under yesterday's key."""
    ledger.spend_quota("youtube_api", 9_900, date="2026-08-10")
    assert ledger.quota_used("youtube_api", date="2026-08-10") == 9_900
    assert ledger.quota_used("youtube_api", date="2026-08-11") == 0


def test_yesterdays_spend_is_kept(ledger: Ledger) -> None:
    """It costs nothing and records what a day actually cost."""
    ledger.spend_quota("youtube_api", 300, date="2026-08-10")
    ledger.spend_quota("youtube_api", 100, date="2026-08-11")
    assert ledger.quota_used("youtube_api", date="2026-08-10") == 300


def test_negative_spend_is_refused(ledger: Ledger) -> None:
    with pytest.raises(ValueError):
        ledger.spend_quota("youtube_api", -100)


# -- B1: status is asset state, and the vocabulary migrated -----------------


def _legacy_ledger(path: Path) -> None:
    """A ledger as it looked before B1: `stored` and `rejected` statuses, and
    none of the columns B1 added.

    Hand-built rather than checked in, because the point is the *shape*, and a
    fixture file would drift out of sync with the thing it is supposed to
    predate.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE videos (
          video_id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT,
          caption_kind TEXT, transcribed_at TEXT, content_hash TEXT,
          duplicate_of TEXT, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
          last_accessed TEXT, attempts INTEGER NOT NULL DEFAULT 0, fail_kind TEXT,
          provider TEXT, note_version TEXT, note_model TEXT, noted_at TEXT
        );
        CREATE TABLE decisions (
          video_id TEXT NOT NULL, topic TEXT NOT NULL, stage TEXT NOT NULL,
          verdict TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL,
          run_id TEXT, model TEXT
        );
        CREATE TABLE quota (
          provider TEXT NOT NULL, date TEXT NOT NULL,
          units_used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (provider, date)
        );
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY, topic TEXT NOT NULL, brief TEXT NOT NULL,
          brief_version TEXT, model TEXT, started_at TEXT NOT NULL, finished_at TEXT,
          rounds INTEGER NOT NULL DEFAULT 0, candidates_seen INTEGER NOT NULL DEFAULT 0,
          videos_stored INTEGER NOT NULL DEFAULT 0, requests_made INTEGER NOT NULL DEFAULT 0,
          input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          stopped_because TEXT
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO videos (video_id, status, first_seen, last_updated, attempts)"
        " VALUES (?, ?, ?, ?, 1)",
        [
            ("aaaaaaaaaaa", "stored", now, now),
            ("bbbbbbbbbbb", "stored", now, now),
            ("ccccccccccc", "rejected", now, now),
            ("ddddddddddd", "needs_asr", now, now),
            ("eeeeeeeeeee", "failed", now, now),
            ("fffffffffff", "duplicate", now, now),
        ],
    )
    conn.execute(
        "INSERT INTO decisions (video_id, topic, stage, verdict, reason, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("ccccccccccc", "prompt caching", "metadata", "no", "off topic", now),
    )
    conn.commit()
    conn.close()


def test_the_old_vocabulary_migrates_in_place(tmp_path: Path) -> None:
    """`stored` -> `has_transcript`, `rejected` -> `seen`. Nothing else moves.

    A value rename, not a change of meaning, which is why it migrates rather
    than forcing the rebuild the no-migration rule demands elsewhere. The NO
    verdicts that made those rows `rejected` are already in `decisions` with
    their topic, so dropping the label loses nothing.
    """
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        by_id = {row.video_id: row.status for row in ledger.list_by_status()}

    assert by_id == {
        "aaaaaaaaaaa": STATUS_HAS_TRANSCRIPT,
        "bbbbbbbbbbb": STATUS_HAS_TRANSCRIPT,
        "ccccccccccc": STATUS_SEEN,
        "ddddddddddd": STATUS_NEEDS_ASR,
        "eeeeeeeeeee": STATUS_FAILED,
        "fffffffffff": STATUS_DUPLICATE,
    }


def test_the_migration_loses_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        assert len(ledger.list_by_status()) == 6
        assert len(ledger.decisions_for("ccccccccccc")) == 1


def test_the_rejection_survives_the_label_being_dropped(tmp_path: Path) -> None:
    """The whole argument for dropping the status. The video is no longer
    globally `rejected`, but it is still a NO *for prompt caching*, and Gate 2
    still finds it — which is what a rejection actually meant all along."""
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        row = ledger.get("ccccccccccc")
        assert row is not None and row.status == STATUS_SEEN
        previous = ledger.latest_decision("ccccccccccc", "prompt caching", verdict=VERDICT_NO)
        assert previous is not None and previous.reason == "off topic"


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    """It runs on every open, so running twice must be indistinguishable from
    running once — otherwise a second session would rewrite live data."""
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        first = {row.video_id: row.status for row in ledger.list_by_status()}
    with Ledger(path) as ledger:
        second = {row.video_id: row.status for row in ledger.list_by_status()}

    assert first == second


def test_an_older_ledger_gains_the_b1_columns(tmp_path: Path) -> None:
    """All five read NULL, which is the honest answer: nobody recorded a
    channel or an evidence grade before the column existed."""
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        row = ledger.get("aaaaaaaaaaa")
        assert row is not None and row.channel_id is None
        decision = ledger.decisions_for("ccccccccccc")[0]
        assert decision.evidence is None and decision.prompt_version is None


def test_an_older_ledger_gains_the_queries_table(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    _legacy_ledger(path)

    with Ledger(path) as ledger:
        columns = [
            row["name"] for row in ledger._conn.execute("PRAGMA table_info(queries)")
        ]

    assert columns == [
        "run_id",
        "round",
        "query",
        "filters_json",
        "origin",
        "origin_detail",
        "results_count",
        "provider",
        "quota_units",
        "ran_at",
    ]


def test_every_mark_lands_on_a_known_status(ledger: Ledger) -> None:
    """A status the vocabulary does not contain is a state nothing has a policy
    for — `skip_reason` would fall through to its default and no decision would
    have been made anywhere."""
    ledger.mark_pending(VIDEO_ID)
    assert ledger.get(VIDEO_ID).status in VIDEO_STATUSES

    for mark in (
        lambda: ledger.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp"),
        lambda: ledger.mark_needs_asr(VIDEO_ID),
        lambda: ledger.mark_failed(VIDEO_ID, FAIL_TRANSIENT),
        lambda: ledger.mark_seen(VIDEO_ID),
    ):
        assert mark().status in VIDEO_STATUSES


def test_marking_seen_clears_a_pending_row(ledger: Ledger) -> None:
    """`pending` means an interrupted fetch, so a candidate that was fetched and
    then declined must not be left there — it would read as a phantom crash."""
    ledger.mark_pending(VIDEO_ID)
    assert ledger.get(VIDEO_ID).status == STATUS_PENDING

    assert ledger.mark_seen(VIDEO_ID).status == STATUS_SEEN


def test_a_channel_id_is_recorded_when_offered(ledger: Ledger) -> None:
    ledger.mark_pending(VIDEO_ID)
    row = ledger.mark_has_transcript(
        VIDEO_ID, source="captions", provider="ytdlp", channel_id="UC_example"
    )
    assert row.channel_id == "UC_example"


def test_a_channel_id_is_not_clobbered_by_a_later_store(ledger: Ledger) -> None:
    """COALESCE, like content_hash: a re-store that does not know the channel
    must not erase one that was recorded."""
    ledger.mark_pending(VIDEO_ID)
    ledger.mark_has_transcript(
        VIDEO_ID, source="captions", provider="ytdlp", channel_id="UC_example"
    )
    row = ledger.mark_has_transcript(VIDEO_ID, source="captions", provider="ytdlp")
    assert row.channel_id == "UC_example"


# -- B1: in_library ---------------------------------------------------------


def test_in_library_is_false_for_an_unknown_video() -> None:
    assert in_library(None) is False


def test_in_library_needs_a_note_not_a_transcript() -> None:
    """The split B1 exists for. Notes are what `/ask` loads, so a video holding
    only a transcript contributes nothing to any answer yet."""
    assert in_library(row(STATUS_HAS_TRANSCRIPT)) is False
    assert in_library(row(STATUS_HAS_TRANSCRIPT, noted_at="2026-01-01T00:00:00+00:00")) is True


def test_in_library_accepts_a_stale_note() -> None:
    """`noted_at`, not `note_is_current`: `/ask` loads a note written by last
    month's prompt just the same."""
    assert (
        in_library(
            row(
                STATUS_HAS_TRANSCRIPT,
                noted_at="2026-01-01T00:00:00+00:00",
                note_version="0-ancient",
            )
        )
        is True
    )


def test_a_duplicate_is_in_the_library_without_a_note() -> None:
    """The canonical copy is the membership that matters; fetching the same
    content under a second ID adds nothing to answer from."""
    assert in_library(row(STATUS_DUPLICATE)) is True
