"""Decision outcomes: stored, never inferred — and history read in one place.

Stage 2 used to record "decided nothing" only implicitly: `verdict == "maybe"`
or a reason string matching one of four constants. A literal MAYBE from the
judge was then stored as `yes` (and counted as coverage), a NO on unreadable
evidence was stored as `yes`, and three historical reason shapes were never
recognised at all. These tests pin the explicit `decisions.outcome` column, the
one legacy classifier, and the migration/read-only behaviour around both, on
synthetic ledgers built in the older shape.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from marginalia.contracts import (
    OUTCOME_CALL_FAILED,
    OUTCOME_JUDGED,
    OUTCOME_NO_VERDICT,
    OUTCOME_TOO_LARGE,
    UNJUDGED_CALL_FAILED,
    UNJUDGED_NO_VERDICT,
    UNJUDGED_TOO_LARGE,
    UNJUDGED_TRUNCATED,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
)
from marginalia.core.ledger import (
    STAGE_FULL,
    STAGE_METADATA,
    STAGE_OPENING,
    STAGE_SEARCH,
    DecisionRow,
    Ledger,
    LedgerError,
    judged_for_topic,
    legacy_outcome,
    rejection_in,
)

VIDEO = "dQw4w9WgXcQ"
TOPIC = "prompt caching"


def _row(stage: str, verdict: str, reason: str | None, *, outcome=None, model=None, topic=TOPIC):
    return DecisionRow(
        VIDEO, topic, stage, verdict, reason, "2026-01-01T00:00:00+00:00",
        model=model, outcome=outcome,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _older_ledger(path: Path, *, drop: tuple[str, ...] = ("outcome", "said")) -> None:
    """A ledger in the shape an earlier version wrote: the newer decision
    columns simply are not there."""
    Ledger(path).close()
    conn = sqlite3.connect(path)
    for column in drop:
        conn.execute(f"ALTER TABLE decisions DROP COLUMN {column}")
    conn.commit()
    conn.close()


# -- the legacy classifier: every historical shape, each from git history ----


@pytest.mark.parametrize(
    ("stage", "verdict", "reason", "expected"),
    [
        # recognised before this change
        (STAGE_FULL, VERDICT_YES, UNJUDGED_CALL_FAILED, OUTCOME_CALL_FAILED),
        (STAGE_FULL, VERDICT_MAYBE, UNJUDGED_TOO_LARGE, OUTCOME_TOO_LARGE),
        (STAGE_FULL, VERDICT_YES, UNJUDGED_TRUNCATED, "truncated"),
        (STAGE_FULL, VERDICT_MAYBE, UNJUDGED_NO_VERDICT, OUTCOME_NO_VERDICT),
        # never recognised before: these read as "already judged" forever
        (STAGE_OPENING, VERDICT_YES, "opening judge failed, kept", OUTCOME_CALL_FAILED),
        (STAGE_OPENING, VERDICT_YES, "verdict could not be parsed, kept", OUTCOME_NO_VERDICT),
        (
            STAGE_FULL,
            VERDICT_YES,
            "reply broke the output contract (missing VERDICT; no readable verdict)",
            OUTCOME_NO_VERDICT,
        ),
        # stage 1's own failure shapes
        (STAGE_METADATA, VERDICT_MAYBE, "batch call failed", OUTCOME_CALL_FAILED),
        (STAGE_METADATA, VERDICT_MAYBE, "verdict could not be parsed", OUTCOME_NO_VERDICT),
        # real judgements
        (STAGE_FULL, VERDICT_YES, "walks through prefix caching with numbers", OUTCOME_JUDGED),
        (STAGE_FULL, VERDICT_NO, "a cooking video", OUTCOME_JUDGED),
        (STAGE_METADATA, VERDICT_NO, "a cooking video", OUTCOME_JUDGED),
        # a contract break that still carried a verdict is a judgement
        (
            STAGE_FULL,
            VERDICT_YES,
            "reply broke the output contract (1 line(s) outside the format)",
            OUTCOME_JUDGED,
        ),
    ],
)
def test_the_legacy_classifier_reads_every_historical_shape(stage, verdict, reason, expected) -> None:
    assert legacy_outcome(_row(stage, verdict, reason)) == expected


def test_a_stored_outcome_is_never_second_guessed_from_the_reason() -> None:
    """The reason is prose. Once a row carries an outcome, nothing reads the
    prose — a judged row whose reason happens to match an old constant is a
    judgement, and an unjudged row with ordinary words is not."""
    judged = _row(STAGE_FULL, VERDICT_YES, UNJUDGED_CALL_FAILED, outcome=OUTCOME_JUDGED)
    failed = _row(STAGE_FULL, VERDICT_MAYBE, "anything at all", outcome=OUTCOME_CALL_FAILED)

    assert judged.is_judgement is True
    assert failed.is_judgement is False


def test_a_search_row_is_never_a_judgement() -> None:
    assert _row(STAGE_SEARCH, VERDICT_NO, "already in the library").is_judgement is False
    assert _row(STAGE_SEARCH, VERDICT_NO, "x").resolved_outcome is None


# -- re-judging policy --------------------------------------------------------


def test_a_judged_maybe_is_not_asked_again() -> None:
    """It was read; a second call is a second opinion, not a retry."""
    rows = [_row(STAGE_FULL, VERDICT_MAYBE, "unsure", outcome=OUTCOME_JUDGED)]
    assert judged_for_topic(rows, TOPIC) is True


def test_a_failed_call_is_asked_again() -> None:
    rows = [_row(STAGE_FULL, VERDICT_MAYBE, "x", outcome=OUTCOME_CALL_FAILED)]
    assert judged_for_topic(rows, TOPIC) is False


def test_too_large_waits_for_a_different_model() -> None:
    rows = [_row(STAGE_FULL, VERDICT_MAYBE, "x", outcome=OUTCOME_TOO_LARGE, model="small")]
    assert judged_for_topic(rows, TOPIC, model="small") is True
    assert judged_for_topic(rows, TOPIC, model="large") is False


def test_a_legacy_opening_failure_is_asked_again() -> None:
    """Before this change it read as a verdict, and the topic never re-read it."""
    rows = [_row(STAGE_OPENING, VERDICT_YES, "opening judge failed, kept")]
    assert judged_for_topic(rows, TOPIC) is False


def test_judgements_for_another_topic_do_not_count() -> None:
    rows = [_row(STAGE_FULL, VERDICT_YES, "good", outcome=OUTCOME_JUDGED, topic="redis")]
    assert judged_for_topic(rows, TOPIC) is False


# -- Gate 2 -------------------------------------------------------------------


def test_gate_two_ignores_its_own_echo_and_unjudged_rows() -> None:
    rows = [
        _row(STAGE_SEARCH, VERDICT_NO, "no for this topic (2026-01-01)"),
        _row(STAGE_FULL, VERDICT_MAYBE, "x", outcome=OUTCOME_CALL_FAILED),
    ]
    assert rejection_in(rows, TOPIC) is None


def test_gate_two_rejects_on_the_current_judgement() -> None:
    no = _row(STAGE_METADATA, VERDICT_NO, "off topic", outcome=OUTCOME_JUDGED)
    assert rejection_in([no], TOPIC) == no
    yes_later = _row(STAGE_FULL, VERDICT_YES, "read it, on topic", outcome=OUTCOME_JUDGED)
    assert rejection_in([no, yes_later], TOPIC) is None


# -- writing ------------------------------------------------------------------


def test_an_unknown_outcome_is_refused(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger, pytest.raises(ValueError):
        ledger.log_decision(VIDEO, TOPIC, stage=STAGE_FULL, verdict=VERDICT_MAYBE, outcome="unsure")


def test_a_search_row_cannot_claim_an_outcome(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger, pytest.raises(ValueError):
        ledger.log_decision(
            VIDEO, TOPIC, stage=STAGE_SEARCH, verdict=VERDICT_NO, outcome=OUTCOME_JUDGED
        )


def test_outcome_and_said_round_trip(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        ledger.log_decision(
            VIDEO, TOPIC, stage=STAGE_FULL, verdict=VERDICT_MAYBE, evidence="DEGRADED",
            outcome=OUTCOME_JUDGED, said=VERDICT_NO,
        )
        row = ledger.decisions_for(VIDEO)[0]
    assert (row.verdict, row.said, row.outcome) == (VERDICT_MAYBE, VERDICT_NO, OUTCOME_JUDGED)


# -- migration and read-only, on ledgers in the older shape -------------------


def test_an_older_ledger_gains_the_columns_and_keeps_every_row(tmp_path: Path) -> None:
    """Additive and nothing else: the old rows read exactly as they were
    written, with NULL in the new columns, and are classified by the legacy
    rules rather than rewritten."""
    path = tmp_path / "ledger.db"
    _older_ledger(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO decisions (video_id, topic, stage, verdict, reason, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (VIDEO, TOPIC, STAGE_OPENING, VERDICT_YES, "opening judge failed, kept", "2025-09-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    with Ledger(path) as ledger:
        (row,) = ledger.decisions_for(VIDEO)

    assert (row.stage, row.verdict, row.reason) == (STAGE_OPENING, VERDICT_YES, "opening judge failed, kept")
    assert row.outcome is None and row.said is None
    assert row.resolved_outcome == OUTCOME_CALL_FAILED


def test_a_read_only_open_of_an_older_ledger_reads_it_and_changes_nothing(tmp_path: Path) -> None:
    """`marg doctor` opens read-only. Before this change any additive column it
    lacked made it refuse the ledger outright, until some write-mode command
    had migrated the file behind the user's back."""
    path = tmp_path / "ledger.db"
    _older_ledger(path, drop=("outcome", "said", "relevant"))
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO videos (video_id, status, first_seen, last_updated) VALUES (?, 'pending', 'x', 'x')",
        (VIDEO,),
    )
    conn.execute(
        "INSERT INTO decisions (video_id, topic, stage, verdict, reason, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (VIDEO, TOPIC, STAGE_FULL, VERDICT_YES, "good", "2025-09-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    before = _digest(path)

    with Ledger(path, read_only=True) as ledger:
        assert [row.video_id for row in ledger.list_by_status()] == [VIDEO]
        (decision,) = ledger.decisions_for(VIDEO)
        assert decision.outcome is None and decision.relevant is None
        assert decision.is_judgement

    assert _digest(path) == before


def test_a_read_only_open_still_refuses_real_drift(tmp_path: Path) -> None:
    """A gap in an additive column is not drift. A missing core column is."""
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE decisions DROP COLUMN reason")
    conn.commit()
    conn.close()

    with pytest.raises(LedgerError, match="missing column"):
        Ledger(path, read_only=True)


def test_a_legacy_ledger_is_refused_before_anything_is_written_to_it(tmp_path: Path) -> None:
    """The refusal used to come after the additive columns, the new tables and
    the status rename had landed — so the older version the message sends you
    back to could no longer read the file."""
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE videos (video_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute("CREATE TABLE rejections (video_id TEXT, topic TEXT)")
    conn.execute("INSERT INTO videos VALUES ('aaaaaaaaaaa', 'stored')")
    conn.commit()
    conn.close()
    before = _digest(path)

    with pytest.raises(LedgerError, match="rejections"):
        Ledger(path)

    assert _digest(path) == before


def test_a_ledger_with_real_drift_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """Independent review F5: the legacy-table case was refused first, but a
    ledger missing a core column still had additive columns and tables written
    into it before the refusal."""
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE decisions DROP COLUMN reason")
    conn.execute("ALTER TABLE decisions DROP COLUMN outcome")
    conn.execute("DROP TABLE plans")
    conn.commit()
    conn.close()
    before = _digest(path)

    with pytest.raises(LedgerError, match="missing column"):
        Ledger(path)

    assert _digest(path) == before
