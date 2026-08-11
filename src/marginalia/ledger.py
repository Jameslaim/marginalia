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
    "VideoRow",
    "STATUS_PENDING",
    "STATUS_NEEDS_ASR",
    "STATUS_STORED",
    "STATUS_REJECTED",
    "STATUS_FAILED",
    "STATUS_DUPLICATE",
    "FAIL_PERMANENT",
    "FAIL_TRANSIENT",
]

STATUS_PENDING = "pending"
STATUS_NEEDS_ASR = "needs_asr"
STATUS_STORED = "stored"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_DUPLICATE = "duplicate"

FAIL_PERMANENT = "permanent"
FAIL_TRANSIENT = "transient"

# `rejections` is unused until M2 and `content_hash` until M3. Both are created
# now on purpose: migrating later is worse than a few idle columns.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,
  status         TEXT NOT NULL,   -- pending|needs_asr|stored|rejected|failed|duplicate
  source         TEXT,            -- captions|whisper
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

CREATE TABLE IF NOT EXISTS rejections (
  video_id    TEXT NOT NULL,
  topic       TEXT NOT NULL,
  reason      TEXT,
  rejected_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class VideoRow:
    video_id: str
    status: str
    source: str | None
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


class LedgerError(RuntimeError):
    """Raised when the ledger is used out of order."""


class Ledger:
    """SQLite-backed ledger. One instance owns one connection."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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
        content_hash: str | None = None,
    ) -> VideoRow:
        self._update(
            video_id,
            """
            UPDATE videos SET
                status         = ?,
                source         = ?,
                provider       = ?,
                transcribed_at = ?,
                last_updated   = ?,
                fail_kind      = NULL,
                content_hash   = COALESCE(?, content_hash)
            WHERE video_id = ?
            """,
            (STATUS_STORED, source, provider, _utcnow(), _utcnow(), content_hash, video_id),
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

    # -- internals ---------------------------------------------------------

    def _update(self, video_id: str, sql: str, params: tuple[object, ...]) -> None:
        with self._conn:
            cursor = self._conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise LedgerError(
                f"no ledger row for {video_id!r}: mark_pending() must be called first"
            )

    def _require(self, video_id: str) -> VideoRow:
        row = self.get(video_id)
        if row is None:  # pragma: no cover - only reachable if the row vanished
            raise LedgerError(f"ledger row for {video_id!r} disappeared")
        return row
