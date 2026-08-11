"""The ledger: every video ID ever seen, and what happened to it.

The ledger outlives the data. Rows are never deleted, even if a transcript is.
Otherwise the agent re-downloads tomorrow exactly what it discarded today.

Write ordering is non-negotiable::

    1. mark_pending(video_id)   <- BEFORE any network call
    2. fetch the transcript
    3. write files to disk
    4. mark_stored(video_id)

A crash between 1 and 4 leaves the row ``pending``, which tells the next run the
job was in flight. Writing only on success loses that and causes blind
re-fetching. ``mark_stored`` and friends therefore refuse to act on a row that
does not already exist.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

__all__ = [
    "Ledger",
    "LedgerError",
    "VideoRow",
    "DecisionRow",
    "skip_reason",
    "normalise_topic",
    "STATUS_PENDING",
    "STATUS_NEEDS_ASR",
    "STATUS_STORED",
    "STATUS_REJECTED",
    "STATUS_FAILED",
    "STATUS_DUPLICATE",
    "FAIL_PERMANENT",
    "FAIL_TRANSIENT",
    "STAGE_SEARCH",
    "STAGE_METADATA",
    "STAGE_OPENING",
    "STAGE_NOTE",
    "VERDICT_YES",
    "VERDICT_NO",
    "VERDICT_MAYBE",
    "KNOWN_STATUSES",
]

STATUS_PENDING = "pending"
STATUS_NEEDS_ASR = "needs_asr"
STATUS_STORED = "stored"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_DUPLICATE = "duplicate"

FAIL_PERMANENT = "permanent"
FAIL_TRANSIENT = "transient"

# Which stage of the pipeline produced a verdict. Cost rises left to right;
# `search` is free and decides only whether a video is *known*, never whether
# it is good.
STAGE_SEARCH = "search"
STAGE_METADATA = "metadata"
STAGE_OPENING = "opening"
STAGE_NOTE = "note"
_STAGES = frozenset({STAGE_SEARCH, STAGE_METADATA, STAGE_OPENING, STAGE_NOTE})

# Three-way on purpose: forcing yes/no on thin evidence makes the judge guess.
VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_MAYBE = "maybe"
_VERDICTS = frozenset({VERDICT_YES, VERDICT_NO, VERDICT_MAYBE})

# Gate 1: statuses meaning "we already have this video's content". Not
# `failed` or `needs_asr` — those are unfinished work, and a later search
# should surface them again.
KNOWN_STATUSES = frozenset({STATUS_STORED, STATUS_DUPLICATE})

# `content_hash` is unused until M3. It is created now on purpose: migrating
# later is worse than one idle column.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,
  status         TEXT NOT NULL,   -- pending|needs_asr|stored|rejected|failed|duplicate
  source         TEXT,            -- captions|whisper
  caption_kind   TEXT,            -- manual|auto
  transcribed_at TEXT,
  content_hash   TEXT,
  duplicate_of   TEXT,
  first_seen     TEXT NOT NULL,
  last_updated   TEXT NOT NULL,
  last_accessed  TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  fail_kind      TEXT,            -- permanent|transient
  provider       TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_hash   ON videos(content_hash);

CREATE TABLE IF NOT EXISTS decisions (
  video_id   TEXT NOT NULL,
  topic      TEXT NOT NULL,
  stage      TEXT NOT NULL,   -- search | metadata | opening | note
  verdict    TEXT NOT NULL,   -- yes | no | maybe
  reason     TEXT,
  decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON decisions(topic, verdict);
CREATE INDEX IF NOT EXISTS idx_decisions_video ON decisions(video_id);

CREATE TABLE IF NOT EXISTS quota (
  provider   TEXT NOT NULL,
  date       TEXT NOT NULL,   -- UTC calendar date, YYYY-MM-DD
  units_used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, date)
);
"""

# Tables an older marginalia created that no longer exist. Their presence is
# proof the ledger predates a schema change, which `CREATE TABLE IF NOT
# EXISTS` would otherwise hide.
_LEGACY_TABLES: dict[str, str] = {
    "rejections": "replaced by `decisions`, which logs every verdict and its stage",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utctoday() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalise_topic(topic: str) -> str:
    """Lowercase and collapse whitespace.

    A topic is a *search string*, not an identifier — "Prompt Caching" and
    "prompt  caching" name the same enquiry, and Gate 2 must recognise that.
    This is the one place in marginalia where lowercasing is correct.

    It is emphatically **not** what happens to a video ID: ``dQw4w9WgXcQ`` and
    ``dqw4w9wgxcq`` are two different videos, and ``ids.py`` never touches
    case. See ``tests/test_topics.py``, which exists to keep someone from
    "fixing" one to match the other.
    """
    return " ".join(topic.split()).lower()


@dataclass(frozen=True)
class VideoRow:
    video_id: str
    status: str
    source: str | None
    caption_kind: str | None
    transcribed_at: str | None
    content_hash: str | None
    duplicate_of: str | None
    first_seen: str
    last_updated: str
    last_accessed: str | None
    attempts: int
    fail_kind: str | None
    provider: str | None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> VideoRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})


@dataclass(frozen=True)
class DecisionRow:
    """One verdict, and which stage produced it.

    Every judgement is logged, not only the negative ones. A rejection log
    alone cannot answer "why did the judge keep this?" months later, and the
    log cannot be reconstructed retroactively.
    """

    video_id: str
    topic: str
    stage: str
    verdict: str
    reason: str | None
    decided_at: str

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> DecisionRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    @property
    def decided_on(self) -> str:
        """Just the calendar date, for a report line."""
        return self.decided_at[:10]


# Every table the current schema expects, and the columns it must have. Used
# by the drift guard; kept beside the dataclasses so it cannot fall behind.
_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "videos": frozenset(field.name for field in fields(VideoRow)),
    "decisions": frozenset(field.name for field in fields(DecisionRow)),
    "quota": frozenset({"provider", "date", "units_used"}),
}


class LedgerError(RuntimeError):
    """Raised when the ledger is used out of order, or is the wrong shape."""


def skip_reason(row: VideoRow | None, *, transcript_present: bool) -> str | None:
    """Decide whether an ingest request needs work, and say why if it does not.

    Returns ``None`` to (re-)attempt, or a short phrase to report as the skip
    reason. This is policy, so it lives here rather than in the CLI:

    ==================  ===========  =====================================
    status              re-attempt?  why
    ==================  ===========  =====================================
    (not in ledger)     yes          never seen
    ``pending``         yes          was in flight; a crash left it here
    ``stored``          no           unless the transcript file is missing
    ``needs_asr``       yes          captions can appear later
    ``failed`` perm.    no           deleted, private, blocked
    ``failed`` trans.   yes          network or rate limit
    ``duplicate``       no           the canonical copy is already stored
    ``rejected``        yes          see below
    ==================  ===========  =====================================

    ``rejected`` re-attempts on purpose. A rejection is topic-relative — it
    means "irrelevant to some brief", which says nothing about a video the
    user has now named directly. A direct request outranks a past judgement
    made for a different purpose.

    ``transcript_present`` is passed in rather than looked up so this stays a
    pure function of ledger state; the library owns the filesystem.
    """
    if row is None:
        return None

    if row.status == STATUS_STORED:
        return "already stored" if transcript_present else None
    if row.status == STATUS_DUPLICATE:
        return f"duplicate of {row.duplicate_of}" if row.duplicate_of else "duplicate"
    if row.status == STATUS_FAILED and row.fail_kind == FAIL_PERMANENT:
        return "previously failed (permanent)"

    # pending, needs_asr, failed/transient, rejected, and anything a later
    # milestone adds: re-attempt. Doing surplus work is recoverable; silently
    # never retrying is not.
    return None


class Ledger:
    """SQLite-backed ledger. One instance owns one connection."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._check_schema()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- reads -------------------------------------------------------------

    def get(self, video_id: str) -> VideoRow | None:
        row = self._conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return VideoRow.from_sqlite(row) if row is not None else None

    def list_by_status(self, status: str | None = None) -> list[VideoRow]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM videos ORDER BY first_seen, video_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM videos WHERE status = ? ORDER BY first_seen, video_id",
                (status,),
            ).fetchall()
        return [VideoRow.from_sqlite(row) for row in rows]

    # -- writes ------------------------------------------------------------

    def mark_pending(self, video_id: str) -> VideoRow:
        """Record an in-flight attempt. Call this BEFORE the network call.

        Inserts the row on first sight, and on re-attempt bumps ``attempts``
        and clears any previous failure classification.
        """
        now = _utcnow()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO videos (video_id, status, first_seen, last_updated, attempts)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(video_id) DO UPDATE SET
                    status       = excluded.status,
                    last_updated = excluded.last_updated,
                    attempts     = videos.attempts + 1,
                    fail_kind    = NULL
                """,
                (video_id, STATUS_PENDING, now, now),
            )
        log.info("ledger: %s -> pending", video_id)
        return self._require(video_id)

    def mark_stored(
        self,
        video_id: str,
        *,
        source: str,
        provider: str,
        caption_kind: str | None = None,
        content_hash: str | None = None,
    ) -> VideoRow:
        """Record a stored transcript.

        ``caption_kind`` sits beside ``source`` because they answer the same
        question at the same granularity — how good is this transcript, and is
        it a candidate for replacement later? Both are also written to
        ``meta.json``; the ledger copy is the queryable one.
        """
        self._update(
            video_id,
            """
            UPDATE videos SET
                status         = ?,
                source         = ?,
                caption_kind   = ?,
                provider       = ?,
                transcribed_at = ?,
                last_updated   = ?,
                fail_kind      = NULL,
                content_hash   = COALESCE(?, content_hash)
            WHERE video_id = ?
            """,
            (
                STATUS_STORED,
                source,
                caption_kind,
                provider,
                _utcnow(),
                _utcnow(),
                content_hash,
                video_id,
            ),
        )
        log.info("ledger: %s -> stored", video_id)
        return self._require(video_id)

    def mark_needs_asr(self, video_id: str, *, provider: str | None = None) -> VideoRow:
        self._update(
            video_id,
            """
            UPDATE videos SET
                status       = ?,
                provider     = COALESCE(?, provider),
                last_updated = ?,
                fail_kind    = NULL
            WHERE video_id = ?
            """,
            (STATUS_NEEDS_ASR, provider, _utcnow(), video_id),
        )
        log.info("ledger: %s -> needs_asr", video_id)
        return self._require(video_id)

    def mark_failed(
        self, video_id: str, fail_kind: str, *, provider: str | None = None
    ) -> VideoRow:
        if fail_kind not in (FAIL_PERMANENT, FAIL_TRANSIENT):
            raise ValueError(f"fail_kind must be permanent or transient, got {fail_kind!r}")
        self._update(
            video_id,
            """
            UPDATE videos SET
                status       = ?,
                fail_kind    = ?,
                provider     = COALESCE(?, provider),
                last_updated = ?
            WHERE video_id = ?
            """,
            (STATUS_FAILED, fail_kind, provider, _utcnow(), video_id),
        )
        log.info("ledger: %s -> failed (%s)", video_id, fail_kind)
        return self._require(video_id)

    def touch_accessed(self, video_id: str) -> None:
        """Record that this video's stored text was read.

        Must be called on every transcript read. It cannot be reconstructed
        retroactively and the future deletion policy depends on it.
        """
        self._update(
            video_id,
            "UPDATE videos SET last_accessed = ? WHERE video_id = ?",
            (_utcnow(), video_id),
        )

    # -- decisions ---------------------------------------------------------
    #
    # Deliberately not foreign-keyed to `videos`. A search candidate has not
    # been touched yet — inserting a `videos` row for all 25 hits of every
    # query would fill the ledger with videos nobody ever decided to fetch,
    # and would say "in flight" about work that never started.

    def log_decision(
        self,
        video_id: str,
        topic: str,
        *,
        stage: str,
        verdict: str,
        reason: str | None = None,
    ) -> DecisionRow:
        """Record one verdict. ``topic`` is normalised on the way in."""
        if stage not in _STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(_STAGES)}")
        if verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {sorted(_VERDICTS)}")

        row = DecisionRow(
            video_id=video_id,
            topic=normalise_topic(topic),
            stage=stage,
            verdict=verdict,
            reason=reason,
            decided_at=_utcnow(),
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO decisions (video_id, topic, stage, verdict, reason, decided_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row.video_id, row.topic, row.stage, row.verdict, row.reason, row.decided_at),
            )
        log.info("decision: %s %s/%s for %r", video_id, stage, verdict, row.topic)
        return row

    def latest_decision(
        self, video_id: str, topic: str, *, verdict: str | None = None
    ) -> DecisionRow | None:
        """The most recent verdict for this video on this exact topic.

        Matching is on the normalised topic string and nothing else. Fuzzy
        topic similarity is deliberately absent — see ``PLAN.md`` §6.
        """
        sql = "SELECT * FROM decisions WHERE video_id = ? AND topic = ?"
        params: list[object] = [video_id, normalise_topic(topic)]
        if verdict is not None:
            sql += " AND verdict = ?"
            params.append(verdict)
        sql += " ORDER BY decided_at DESC, rowid DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return DecisionRow.from_sqlite(row) if row is not None else None

    def decisions_for(self, video_id: str) -> list[DecisionRow]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE video_id = ? ORDER BY decided_at, rowid",
            (video_id,),
        ).fetchall()
        return [DecisionRow.from_sqlite(row) for row in rows]

    # -- quota -------------------------------------------------------------
    #
    # Keyed by UTC calendar date, so the rollover is the key changing rather
    # than a reset anyone has to remember to run. Old rows are kept: they cost
    # nothing and record what a day actually spent.

    def quota_used(self, provider: str, *, date: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT units_used FROM quota WHERE provider = ? AND date = ?",
            (provider, date or _utctoday()),
        ).fetchone()
        return int(row["units_used"]) if row is not None else 0

    def spend_quota(self, provider: str, units: int, *, date: str | None = None) -> int:
        """Add ``units`` to today's spend and return the new total.

        Called *before* the request, for the same reason ``mark_pending`` is:
        a crash mid-request must over-count rather than under-count. Losing a
        few units is recoverable; believing you have quota you already spent
        earns an opaque 403.
        """
        if units < 0:
            raise ValueError(f"units must not be negative, got {units}")
        today = date or _utctoday()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO quota (provider, date, units_used) VALUES (?, ?, ?)
                ON CONFLICT(provider, date) DO UPDATE SET
                    units_used = quota.units_used + excluded.units_used
                """,
                (provider, today, units),
            )
        total = self.quota_used(provider, date=today)
        log.info("quota: %s spent %d, %d used on %s", provider, units, total, today)
        return total

    # -- internals ---------------------------------------------------------

    def _update(self, video_id: str, sql: str, params: tuple[object, ...]) -> None:
        with self._conn:
            cursor = self._conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise LedgerError(
                f"no ledger row for {video_id!r}: mark_pending() must be called first"
            )

    def _check_schema(self) -> None:
        """Fail loudly on a ledger written by an older version.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone,
        so a ledger predating a schema change would otherwise fail much later
        with an opaque ``IndexError`` from row unpacking, or — worse — keep
        writing verdicts into a table nothing reads. There is no migration
        path yet, so the message must say exactly what to do instead.
        """
        tables = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

        for name in sorted(_LEGACY_TABLES):
            if name in tables:
                raise self._stale(f"it still has the `{name}` table ({_LEGACY_TABLES[name]})")

        for table, expected in _EXPECTED_COLUMNS.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(expected - present)
            if missing:
                raise self._stale(
                    f"table `{table}` is missing column(s): {', '.join(missing)}"
                )

    def _stale(self, detail: str) -> LedgerError:
        return LedgerError(
            f"{self.db_path} was written by an older version of marginalia: {detail}. "
            f"There is no migration yet — delete the library and re-ingest."
        )

    def _require(self, video_id: str) -> VideoRow:
        row = self.get(video_id)
        if row is None:  # pragma: no cover - only reachable if the row vanished
            raise LedgerError(f"ledger row for {video_id!r} disappeared")
        return row
