"""The ledger: every video ID ever seen, and what happened to it.

The ledger outlives the data. Rows are never deleted, even if a transcript is.
Otherwise the agent re-downloads tomorrow exactly what it discarded today.

``status`` is **asset state only** — what we hold, never what we decided. What
was decided, and for which topic, is in ``decisions``, because relevance is
topic-relative and a status column cannot be. One field carrying both is why a
video declined for prompt caching used to be labelled ``rejected`` outright
while being a perfectly good video about Redis.

Write ordering is non-negotiable::

    1. mark_pending(video_id)   <- BEFORE any network call
    2. fetch the transcript
    3. write files to disk
    4. mark_has_transcript(video_id)

A crash between 1 and 4 leaves the row ``pending``, which tells the next run the
job was in flight. Writing only on success loses that and causes blind
re-fetching. ``mark_has_transcript`` and friends therefore refuse to act on a row
that does not already exist.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Sequence

from ..contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
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

log = logging.getLogger(__name__)

__all__ = [
    "Ledger",
    "LedgerError",
    "VideoRow",
    "DecisionRow",
    "RunRow",
    "skip_reason",
    "normalise_topic",
    "in_library",
    "wanted_for_a_note",
    "FAIL_PERMANENT",
    "FAIL_TRANSIENT",
    "STAGE_SEARCH",
    "STAGE_METADATA",
    "STAGE_OPENING",
    "STAGE_FULL",
    "STAGE_NOTE",
]

FAIL_PERMANENT = "permanent"
FAIL_TRANSIENT = "transient"

# Which stage of the pipeline produced a verdict. Cost rises left to right;
# `search` is free and decides only whether a video is *known*, never whether
# it is good.
STAGE_SEARCH = "search"
STAGE_METADATA = "metadata"
STAGE_OPENING = "opening"
# B3. Stage 2 reads the whole transcript now, so its verdicts are recorded
# under a different name — the old ones were decided from roughly the first
# thousand words, and calling both `opening` would make the log claim evidence
# that was never read. Old rows keep their name; nothing is migrated, because
# what they say about themselves is true.
STAGE_FULL = "full"
STAGE_NOTE = "note"
_STAGES = frozenset(
    {STAGE_SEARCH, STAGE_METADATA, STAGE_OPENING, STAGE_FULL, STAGE_NOTE}
)

# Both names for stage 2 — the per-video judgement that reads the transcript.
# Anything asking "was this video declined after being read?" must ask about
# both, or B3 would silently un-reject everything B2 rejected.
_STAGE_TWO = frozenset({STAGE_OPENING, STAGE_FULL})

_EVIDENCE = frozenset({EVIDENCE_CLEAR, EVIDENCE_DEGRADED})

_VERDICTS = frozenset({VERDICT_YES, VERDICT_NO, VERDICT_MAYBE})

# `content_hash` is unused until M3. It is created now on purpose: migrating
# later is worse than one idle column.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,
  -- Asset state only: what we have, never what we decided. Relevance is
  -- topic-relative and lives in `decisions`.
  status         TEXT NOT NULL,   -- seen|pending|has_transcript|needs_asr|failed|duplicate
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
  provider       TEXT,
  note_version   TEXT,            -- the write_note prompt version behind note.md
  note_model     TEXT,            -- which model read the transcript
  noted_at       TEXT,
  channel_id     TEXT             -- B1; never backfilled, new videos only
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_hash   ON videos(content_hash);

CREATE TABLE IF NOT EXISTS decisions (
  video_id   TEXT NOT NULL,
  topic      TEXT NOT NULL,
  stage      TEXT NOT NULL,   -- search | metadata | opening | note
  verdict    TEXT NOT NULL,   -- yes | no | maybe
  reason     TEXT,
  decided_at TEXT NOT NULL,
  run_id     TEXT,            -- the discovery run, and so the brief, behind it
  model      TEXT,            -- which model said so; the provider is temporary
  evidence   TEXT,            -- CLEAR | DEGRADED; B3 writes it, nothing reads it yet
  prompt_version TEXT         -- which prompt produced this verdict
);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON decisions(topic, verdict);
CREATE INDEX IF NOT EXISTS idx_decisions_video ON decisions(video_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run   ON decisions(run_id);

CREATE TABLE IF NOT EXISTS quota (
  provider   TEXT NOT NULL,
  date       TEXT NOT NULL,   -- UTC calendar date, YYYY-MM-DD
  units_used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, date)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id          TEXT PRIMARY KEY,
  topic           TEXT NOT NULL,
  brief           TEXT NOT NULL,
  brief_version   TEXT,
  model           TEXT,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  rounds          INTEGER NOT NULL DEFAULT 0,
  candidates_seen INTEGER NOT NULL DEFAULT 0,
  videos_stored   INTEGER NOT NULL DEFAULT 0,
  requests_made   INTEGER NOT NULL DEFAULT 0,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  stopped_because TEXT,
  territory_map     TEXT,        -- B4 writes it, nothing reads it yet
  territory_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(topic);

-- Every query a run issued, and why it issued it. `origin` is the column
-- that makes "why did it search for *that*?" answerable; without it a run's
-- queries are a list with no causal story. B4 and B5 write these rows —
-- the table exists now so that neither needs a migration.
CREATE TABLE IF NOT EXISTS queries (
  run_id         TEXT NOT NULL,
  round          INTEGER NOT NULL,
  query          TEXT NOT NULL,
  filters_json   TEXT,
  origin         TEXT NOT NULL,   -- brief | territory | planner | thread
  origin_detail  TEXT,
  results_count  INTEGER,
  provider       TEXT,
  quota_units    INTEGER,
  ran_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_run ON queries(run_id, round);
"""

# Columns added after a table shipped, which can be introduced in place.
#
# The no-migration rule still holds for anything that changes meaning — M2a
# replaced `rejections` with `decisions` and that required a rebuild, correctly.
# But adding a *nullable* column loses nothing and invents nothing: existing
# rows read NULL, which is the honest answer to "which run produced this?" for
# a decision made before runs existed. Destroying a library of transcripts to
# add a column nobody had yet would be the more damaging choice.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("decisions", "run_id", "TEXT"),
    ("decisions", "model", "TEXT"),
    # M5. A library of transcripts that predates notes is exactly the library
    # notes are for; requiring a rebuild to add three nullable columns would
    # destroy the fragile artifact to add the regenerable one.
    ("videos", "note_version", "TEXT"),
    ("videos", "note_model", "TEXT"),
    ("videos", "noted_at", "TEXT"),
    # B1. All five read NULL on existing rows, which is the honest answer:
    # nobody recorded a channel, an evidence grade or a territory map before
    # the columns existed, and inventing one would be worse than admitting it.
    ("videos", "channel_id", "TEXT"),
    ("decisions", "evidence", "TEXT"),
    ("decisions", "prompt_version", "TEXT"),
    ("runs", "territory_map", "TEXT"),
    ("runs", "territory_version", "TEXT"),
)

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
    run_id: str | None = None
    model: str | None = None
    # B1, both NULL on every existing row and on everything written until B3.
    # `evidence` is the one that changes what a verdict *means*: a NO decided
    # from a full transcript and a NO decided from a truncated description are
    # different claims, and Gate 2 treats both as final.
    evidence: str | None = None
    prompt_version: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> DecisionRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    @property
    def decided_on(self) -> str:
        """Just the calendar date, for a report line."""
        return self.decided_at[:10]


@dataclass(frozen=True)
class RunRow:
    """One discovery run: the brief it worked from, and what it cost.

    ``brief`` is stored in full rather than summarised. When a library looks
    wrong the first question is always *what did it think I meant?*, and that
    is unanswerable from a topic string.
    """

    run_id: str
    topic: str
    brief: str
    brief_version: str | None
    model: str | None
    started_at: str
    finished_at: str | None
    rounds: int
    candidates_seen: int
    videos_stored: int
    requests_made: int
    input_tokens: int
    output_tokens: int
    stopped_because: str | None
    # B1. B4 writes these; nothing reads them yet.
    territory_map: str | None = None
    territory_version: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> RunRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})


# Every table the current schema expects, and the columns it must have. Used
# by the drift guard; kept beside the dataclasses so it cannot fall behind.
_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "videos": frozenset(field.name for field in fields(VideoRow)),
    "decisions": frozenset(field.name for field in fields(DecisionRow)),
    "quota": frozenset({"provider", "date", "units_used"}),
    "runs": frozenset(field.name for field in fields(RunRow)),
    # No dataclass yet: nothing reads `queries` until B4, and inventing a row
    # type for a table with no reader would be inventing its shape twice.
    "queries": frozenset(
        {
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
        }
    ),
}


class LedgerError(RuntimeError):
    """Raised when the ledger is used out of order, or is the wrong shape."""


def in_library(row: VideoRow | None) -> bool:
    """Gate 1: do we already have this video, for library purposes?

    **A video is in the library if it has a note.** Notes are what `/ask` loads,
    so note existence *is* membership — nothing else is visible to a question.

    This deliberately is not "has a transcript". Since B1 a video can hold a
    transcript and not be in the library: a candidate judged NO at stage 2 keeps
    whatever was fetched, and a freshly ingested video has a transcript for as
    long as it takes the note worker to reach it. Treating those as members
    would hide them from every future search while contributing nothing to any
    answer — present in the ledger, absent from the library, invisible to both.

    ``noted_at`` rather than ``note_is_current``: a note written by last month's
    prompt is still a note, and `/ask` still loads it. Staleness is the note
    worker's question, not this one.

    ``duplicate`` counts because the canonical copy is the membership that
    matters; fetching the same content under a second ID adds nothing.
    """
    if row is None:
        return False
    if row.status == STATUS_DUPLICATE:
        return True
    return row.noted_at is not None


def skip_reason(row: VideoRow | None, *, transcript_present: bool) -> str | None:
    """Decide whether an ingest request needs work, and say why if it does not.

    Returns ``None`` to (re-)attempt, or a short phrase to report as the skip
    reason. This is policy, so it lives here rather than in the CLI:

    ==================  ===========  =====================================
    status              re-attempt?  why
    ==================  ===========  =====================================
    (not in ledger)     yes          never seen
    ``seen``            yes          known, but nothing is held
    ``pending``         yes          was in flight; a crash left it here
    ``has_transcript``  no           unless the transcript file is missing
    ``needs_asr``       yes          captions can appear later
    ``failed`` perm.    no           deleted, private, blocked
    ``failed`` trans.   yes          network or rate limit
    ``duplicate``       no           the canonical copy is already held
    ==================  ===========  =====================================

    **``rejected`` is gone as a state**, and nothing replaced it here. It used
    to re-attempt on purpose, with the reasoning that a rejection is
    topic-relative — "irrelevant to some brief" says nothing about a video the
    user has now named directly. That reasoning was right and is now structural
    rather than a special case: relevance never was asset state, so a video
    declined for one topic is simply ``seen``, and ``seen`` re-attempts because
    nothing is held. "Has a NO for this topic" is Gate 2's question, and Gate 2
    is not consulted when a user names a video directly.

    ``transcript_present`` is passed in rather than looked up so this stays a
    pure function of ledger state; the library owns the filesystem.
    """
    if row is None:
        return None

    if row.status == STATUS_HAS_TRANSCRIPT:
        return "already have the transcript" if transcript_present else None
    if row.status == STATUS_DUPLICATE:
        return f"duplicate of {row.duplicate_of}" if row.duplicate_of else "duplicate"
    if row.status == STATUS_FAILED and row.fail_kind == FAIL_PERMANENT:
        return "previously failed (permanent)"

    # seen, pending, needs_asr, failed/transient, and anything a later milestone
    # adds: re-attempt. Doing surplus work is recoverable; silently never
    # retrying is not.
    return None


# A stage-2 verdict that let a video through. MAYBE counts: stage 2 keeps on
# MAYBE by the same asymmetry every parser here follows — an unclear answer
# must never cost a video nobody will know existed.
_KEPT = frozenset({VERDICT_YES, VERDICT_MAYBE})


def wanted_for_a_note(row: VideoRow, decisions: Sequence[DecisionRow]) -> bool:
    """Should the note worker spend a model call on this video?

    B2 made this a real question. Before it, holding a transcript meant some
    topic had accepted the video, so `has_transcript` was a good enough proxy
    for "we chose this". Now a video declined at stage 2 keeps its transcript
    too — that is the point of keeping it — and writing a note for a video no
    topic has ever wanted is money spent on something `/ask` will then load.

    The discriminator is exact and already recorded: **a stage-`opening` NO**.
    Nothing else in the system writes one. Checked against the live library at
    the time of writing, it identified all fifteen rejections and nothing else.

    Three things override it, and each is a case where "we never wanted this"
    is simply false:

    ``noted_at``
        The video is already in the library. Regeneration must be able to reach
        every note that exists, or `--regenerate --all` would quietly stop
        maintaining some of them — and a note nobody can rewrite is a note
        frozen at whichever prompt happened to produce it.

    an acceptance elsewhere
        A stage-2 YES or MAYBE, or a stage-1 YES, means some brief wanted this
        video. A NO for one topic and a YES for another is not a contradiction;
        it is the whole reason relevance left the status column in B1.

    no judgement at all
        `marg ingest <url>` logs no decision, because there is no topic and no
        brief — the user simply asked for this video. A rule keyed on verdicts
        must not exclude the one path that never produces any.

    Since B3 there is a fourth answer that overrides all three: **degraded
    evidence is never noted**. That is not a judgement about relevance that a
    later topic might overturn — it is a statement that the transcript could
    not be read, and the transcript does not change between topics.

    Pure, and takes the decisions rather than fetching them, for the same reason
    `skip_reason` takes ``transcript_present``: policy is testable without a
    database and the ledger owns the queries.
    """
    # B3, and it goes first — ahead of every other answer including an existing
    # note. Evidence the judge could not read cannot produce a note worth
    # having: `/ask` loads notes and has no way to know the text behind one was
    # noise, so the failure mode is a confident wrong answer, which is the thing
    # a library exists to not do.
    #
    # Ahead of ``noted_at`` deliberately. That override exists so regeneration
    # can reach every note that exists — but regenerating a note from a
    # transcript now known to be garbled spends a model call to produce the same
    # bad note again, more expensively. The stale note stays on disk; it simply
    # stops being refreshed. Removing it is a real question and belongs to
    # whatever phase owns deletion.
    #
    # Unlike a rejection, this is not a claim about relevance that another topic
    # might overturn. It is a claim about the transcript, and the transcript is
    # the same for every topic that ever looks at this video.
    if any(
        decision.stage in _STAGE_TWO and decision.evidence == EVIDENCE_DEGRADED
        for decision in decisions
    ):
        return False

    if row.noted_at is not None:
        return True

    declined = False
    for decision in decisions:
        if decision.stage in _STAGE_TWO and decision.verdict == VERDICT_NO:
            declined = True
        elif decision.stage in _STAGE_TWO and decision.verdict in _KEPT:
            return True
        elif decision.stage == STAGE_METADATA and decision.verdict == VERDICT_YES:
            return True

    return not declined



class Ledger:
    """SQLite-backed ledger. One instance owns one connection."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # Migration first: _SCHEMA indexes columns that an older ledger does
        # not have yet, so CREATE INDEX would fail before the guard could
        # produce a readable message.
        self._add_missing_columns()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_status_vocabulary()
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

    def mark_has_transcript(
        self,
        video_id: str,
        *,
        source: str,
        provider: str,
        caption_kind: str | None = None,
        content_hash: str | None = None,
        channel_id: str | None = None,
    ) -> VideoRow:
        """Record a stored transcript.

        Named for the state it sets rather than the act. `stored` used to mean
        both "the file is on disk" and "this is in the library", and B1 split
        those: the file being on disk is all this claims.

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
                content_hash   = COALESCE(?, content_hash),
                channel_id     = COALESCE(?, channel_id)
            WHERE video_id = ?
            """,
            (
                STATUS_HAS_TRANSCRIPT,
                source,
                caption_kind,
                provider,
                _utcnow(),
                _utcnow(),
                content_hash,
                channel_id,
                video_id,
            ),
        )
        log.info("ledger: %s -> has_transcript", video_id)
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

    def mark_seen(self, video_id: str, *, provider: str | None = None) -> VideoRow:
        """Known to us, holding nothing. The resting state for a non-keeper.

        This used to be ``mark_rejected`` and set a status of ``rejected``,
        which put a judgement in the asset column — the video was globally
        labelled rejected when what actually happened was that one brief did
        not want it. The verdict is logged in ``decisions`` against its topic,
        where it can be true of one question and false of the next; the row
        here records only that nothing is held.

        Also the correct landing place for an interrupted fetch that resolved
        to nothing: ``pending`` must not outlive the attempt, because a stale
        ``pending`` reads as a crash.
        """
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
            (STATUS_SEEN, provider, _utcnow(), video_id),
        )
        log.info("ledger: %s -> seen", video_id)
        return self._require(video_id)

    # -- notes -------------------------------------------------------------
    #
    # Note state lives on the video row rather than in a table of its own:
    # there is exactly one note per video, and it is regenerable. The three
    # columns answer one question between them — *does this video carry a note
    # written by the prompt currently on disk?* — which is what makes
    # regeneration a query instead of a hand-maintained list.

    def mark_noted(self, video_id: str, *, version: str, model: str | None) -> VideoRow:
        """Record that ``note.md`` was written. Call it *after* the file lands.

        The opposite ordering to ``mark_pending``, and deliberately so. A crash
        before this leaves a video the next run picks up again, which costs one
        model call. A crash after a row claiming a note that was never written
        leaves a video nothing will ever revisit — the invisible failure, and
        the one worth designing against. Notes are cheap; being wrong about
        having one is not.
        """
        self._update(
            video_id,
            """
            UPDATE videos SET
                note_version = ?,
                note_model   = ?,
                noted_at     = ?,
                last_updated = ?
            WHERE video_id = ?
            """,
            (version, model, _utcnow(), _utcnow(), video_id),
        )
        log.info("ledger: %s noted (%s, %s)", video_id, version, model)
        return self._require(video_id)

    def videos_needing_note(
        self, *, version: str, stale_only: bool = True, limit: int | None = None
    ) -> list[VideoRow]:
        """Videos holding a transcript, wanted, with no note from this prompt.

        Keyed on ``has_transcript`` because a note is written *from* the
        transcript — this is the one place where holding the file, rather than
        library membership, is the right question. It is also the job that
        turns the former into the latter.

        Filtered by :func:`wanted_for_a_note` since B2, because holding a
        transcript stopped implying that anyone chose the video. A stage-2
        rejection keeps its bytes and must not then be handed a note.

        ``stale_only=False`` returns every such video instead — what
        ``--regenerate --all`` means. Ordered by ``first_seen`` so a ``--limit``
        run is a stable prefix rather than a random sample: reading three notes,
        revising the prompt and running again should show you the same three.
        """
        rows = [
            row
            for row in self.list_by_status(STATUS_HAS_TRANSCRIPT)
            if wanted_for_a_note(row, self.decisions_for(row.video_id))
        ]
        if stale_only:
            rows = [row for row in rows if not row.note_is_current(version)]
        return rows[:limit] if limit is not None else rows

    def note_counts(self, *, version: str) -> dict[str, int]:
        """Of the videos holding a transcript: current, stale, or never noted.

        ``missing`` is the interesting number since B1 — those are videos with
        a transcript and no note, which means they are *not in the library*
        and no question can reach them.

        ``unwanted`` is B2's: transcripts kept from a stage-2 rejection, which
        are deliberately unnoted. Counted separately rather than folded into
        ``missing``, because they are not work waiting to be done.
        """
        counts = {"has_transcript": 0, "current": 0, "stale": 0, "missing": 0, "unwanted": 0}
        for row in self.list_by_status(STATUS_HAS_TRANSCRIPT):
            counts["has_transcript"] += 1
            if not wanted_for_a_note(row, self.decisions_for(row.video_id)):
                # Declined by every topic that judged it. Not a missing note —
                # a note deliberately not written, and counting it as missing
                # would report permanent work that will never be done.
                counts["unwanted"] += 1
                continue
            if row.noted_at is None:
                counts["missing"] += 1
            elif row.note_version == version:
                counts["current"] += 1
            else:
                counts["stale"] += 1
        return counts

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

    def log_decision(  # noqa: PLR0913 - one row, one argument each
        self,
        video_id: str,
        topic: str,
        *,
        stage: str,
        verdict: str,
        reason: str | None = None,
        run_id: str | None = None,
        model: str | None = None,
        evidence: str | None = None,
        prompt_version: str | None = None,
    ) -> DecisionRow:
        """Record one verdict. ``topic`` is normalised on the way in.

        ``model`` is recorded because the provider is temporary. When it
        changes and judging quality shifts, the only way to work out why is to
        know which model produced which verdict.

        ``evidence`` and ``prompt_version`` are B1 columns that B3 finally
        fills. Between them they answer *how good was the evidence, and which
        prompt read it?* — the two questions a surprising verdict raises, and
        neither is reconstructible afterwards. `DEGRADED` also suppresses the
        note, which makes it the one field here that changes what happens next
        rather than only explaining it.
        """
        if stage not in _STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(_STAGES)}")
        if verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}; expected one of {sorted(_VERDICTS)}")
        if evidence is not None and evidence not in _EVIDENCE:
            raise ValueError(
                f"unknown evidence {evidence!r}; expected one of {sorted(_EVIDENCE)}"
            )

        row = DecisionRow(
            video_id=video_id,
            topic=normalise_topic(topic),
            stage=stage,
            verdict=verdict,
            reason=reason,
            decided_at=_utcnow(),
            run_id=run_id,
            model=model,
            evidence=evidence,
            prompt_version=prompt_version,
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO decisions
                    (video_id, topic, stage, verdict, reason, decided_at, run_id,
                     model, evidence, prompt_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.video_id,
                    row.topic,
                    row.stage,
                    row.verdict,
                    row.reason,
                    row.decided_at,
                    row.run_id,
                    row.model,
                    row.evidence,
                    row.prompt_version,
                ),
            )
        log.info("decision: %s %s/%s for %r", video_id, stage, verdict, row.topic)
        return row

    def decisions_for_run(self, run_id: str) -> list[DecisionRow]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [DecisionRow.from_sqlite(row) for row in rows]

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        topic: str,
        *,
        brief: str,
        brief_version: str | None = None,
        model: str | None = None,
    ) -> RunRow:
        """Open a run. Written before the first model call, always.

        Same ordering rule as ``mark_pending``: a run with no ``finished_at``
        is a run that crashed, and that is diagnosable. Recording only on
        success leaves no trace that anything was ever attempted.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, topic, brief, brief_version, model, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, normalise_topic(topic), brief, brief_version, model, _utcnow()),
            )
        log.info("run %s started for %r", run_id, topic)
        return self.get_run(run_id)  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: str,
        *,
        rounds: int,
        candidates_seen: int,
        videos_stored: int,
        requests_made: int,
        input_tokens: int,
        output_tokens: int,
        stopped_because: str,
    ) -> RunRow:
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE runs SET
                    finished_at     = ?,
                    rounds          = ?,
                    candidates_seen = ?,
                    videos_stored   = ?,
                    requests_made   = ?,
                    input_tokens    = ?,
                    output_tokens   = ?,
                    stopped_because = ?
                WHERE run_id = ?
                """,
                (
                    _utcnow(),
                    rounds,
                    candidates_seen,
                    videos_stored,
                    requests_made,
                    input_tokens,
                    output_tokens,
                    stopped_because,
                    run_id,
                ),
            )
        if cursor.rowcount == 0:
            raise LedgerError(f"no run row for {run_id!r}: start_run() must be called first")
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunRow.from_sqlite(row) if row is not None else None

    def list_runs(self, topic: str | None = None) -> list[RunRow]:
        if topic is None:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE topic = ? ORDER BY started_at", (normalise_topic(topic),)
            ).fetchall()
        return [RunRow.from_sqlite(row) for row in rows]

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

    # B1 renamed two status values in place. `stored` described an asset and
    # `rejected` described a judgement, and one column carrying both is why a
    # video declined for prompt caching was globally labelled rejected while
    # being a perfectly good video about Redis.
    #
    # A value rename, not a schema change, so it migrates rather than forcing a
    # rebuild — the no-migration rule guards against a column changing *meaning*
    # under existing data, and here the data means what it always did. The NO
    # verdicts that made those rows `rejected` already live in `decisions`, in
    # full, with their topic; dropping the label loses nothing.
    #
    # Idempotent: after the first run no row matches either WHERE.
    _STATUS_RENAMES: tuple[tuple[str, str], ...] = (
        ("stored", STATUS_HAS_TRANSCRIPT),
        ("rejected", STATUS_SEEN),
    )

    def _migrate_status_vocabulary(self) -> None:
        if "videos" not in self._tables():
            return
        for old, new in self._STATUS_RENAMES:
            with self._conn:
                changed = self._conn.execute(
                    "UPDATE videos SET status = ? WHERE status = ?", (new, old)
                ).rowcount
            if changed:
                log.info("ledger: migrated %d row(s) from %r to %r", changed, old, new)

    def _tables(self) -> set[str]:
        return {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    def _add_missing_columns(self) -> None:
        """Introduce nullable columns added after a table shipped.

        Runs before the drift guard, so a ledger that only lacks additive
        columns is repaired rather than rejected. Anything the guard still
        objects to after this genuinely cannot be migrated.
        """
        for table, column, declaration in _ADDITIVE_COLUMNS:
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if not present or column in present:
                continue
            log.info("ledger: adding %s.%s to an older ledger", table, column)
            with self._conn:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _check_schema(self) -> None:
        """Fail loudly on a ledger written by an older version.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone,
        so a ledger predating a schema change would otherwise fail much later
        with an opaque ``IndexError`` from row unpacking, or — worse — keep
        writing verdicts into a table nothing reads. There is no migration
        path yet, so the message must say exactly what to do instead.
        """
        tables = self._tables()

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
