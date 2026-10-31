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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Sequence

from ..contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    DECISION_OUTCOMES,
    OUTCOME_CALL_FAILED,
    OUTCOME_JUDGED,
    OUTCOME_NO_VERDICT,
    OUTCOME_TOO_LARGE,
    OUTCOME_TRUNCATED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STATUS_PENDING,
    STATUS_SEEN,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    UNJUDGED_CALL_FAILED,
    UNJUDGED_NO_VERDICT,
    UNJUDGED_TOO_LARGE,
    UNJUDGED_TRUNCATED,
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
    "QueryRow",
    "PlanRow",
    "BriefRow",
    "skip_reason",
    "normalise_topic",
    "in_library",
    "wanted_for_a_note",
    "current_verdicts",
    "judged_for_topic",
    "rejection_in",
    "legacy_outcome",
    "quota_day",
    "now",
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
# Which stage read more. Used to decide, within one topic, which verdict
# is the current one: a stage that read the transcript supersedes one that
# read the title, which supersedes one that read a search result.
_STAGE_ORDER = {STAGE_SEARCH: 1, STAGE_METADATA: 2, STAGE_OPENING: 3, STAGE_FULL: 3}

_EVIDENCE = frozenset({EVIDENCE_CLEAR, EVIDENCE_DEGRADED})

_VERDICTS = frozenset({VERDICT_YES, VERDICT_NO, VERDICT_MAYBE})

# `content_hash` is the SHA-256 of `transcript.txt` as it landed on disk,
# written by every store path and read by `marg doctor` to check rule 3. It
# sat idle from M1 until the doctor arrived — created early because migrating
# later is worse than one idle column — and it is *not* a fingerprint: Gate 3
# is rejected (PLAN.md §10), and nothing compares one video's hash to another's.
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
  noted_at       TEXT,            -- when the CURRENT note.md was written
  first_noted_at TEXT,            -- when this video first had any note; never rewritten
  channel_id     TEXT,            -- B1; never backfilled, new videos only
  note_failures  INTEGER NOT NULL DEFAULT 0,  -- failed note attempts since the last note
  note_failed_at TEXT             -- when the latest of them failed
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_hash   ON videos(content_hash);

CREATE TABLE IF NOT EXISTS decisions (
  video_id   TEXT NOT NULL,
  topic      TEXT NOT NULL,
  stage      TEXT NOT NULL,   -- search | metadata | opening | full
  verdict    TEXT NOT NULL,   -- yes | no | maybe: the resolved verdict
  reason     TEXT,
  decided_at TEXT NOT NULL,
  run_id     TEXT,            -- the discovery run, and so the brief, behind it
  model      TEXT,            -- which model said so; the provider is temporary
  evidence   TEXT,            -- CLEAR | DEGRADED; DEGRADED suppresses the note
  prompt_version TEXT,        -- which prompt produced this verdict
  relevant   TEXT,            -- stage 2's RELEVANT ranges or sentinel; read by nothing
  outcome    TEXT,            -- judged | call_failed | too_large | truncated | no_verdict;
                              -- NULL on search rows and on rows written before it existed
  said       TEXT,            -- the judge's own verdict word, before any resolution
  item       TEXT             -- stage 1: the coverage item it was tagged with, as `state: tag`
);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON decisions(topic, verdict);
CREATE INDEX IF NOT EXISTS idx_decisions_video ON decisions(video_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run   ON decisions(run_id);

CREATE TABLE IF NOT EXISTS quota (
  provider   TEXT NOT NULL,
  date       TEXT NOT NULL,   -- the API's own quota day (Pacific), YYYY-MM-DD
  units_used INTEGER NOT NULL DEFAULT 0,
  -- When the API itself said the day was spent. Authoritative over
  -- `units_used`: the local count is an estimate, and Google's refusal is a
  -- fact. Set once, and every later call that day refuses before sending.
  exhausted_at TEXT,
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
  territory_map     TEXT,        -- the map B4 worked from, stored whole
  territory_version TEXT,
  library_baseline  TEXT,        -- what the library already covered, before round 1
  -- how the brief was made: draft retries, review fallback
  brief_outcome     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(topic);

-- Every query a run issued, and why it issued it. `origin` is the column
-- that makes "why did it search for *that*?" answerable; without it a run's
-- queries are a list with no causal story. Created empty in B1 and given its
-- writer in B4, so that adding provenance needed no migration.
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

-- What the planner concluded at the end of each round, stored whole. The
-- `queries` table records what the plan *produced*; this records the reasoning
-- that produced it — the coverage assessment, the recommendation and its
-- reason, the revisions, and everything the code refused. Same argument as
-- storing the brief and the map: the queries are the conclusion, and a
-- conclusion whose reasoning was thrown away cannot be corrected.
--
-- One row per round rather than a column on `runs`, because there is one plan
-- per round and a run has many. Versioned by `round`, which is what a plan is
-- identified by; `prompt_version` records which prompt wrote it.
CREATE TABLE IF NOT EXISTS plans (
  run_id         TEXT NOT NULL,
  round          INTEGER NOT NULL,
  plan_json      TEXT NOT NULL,
  prompt_version TEXT,
  planned_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_run ON plans(run_id, round);

CREATE TABLE IF NOT EXISTS briefs (
  topic          TEXT NOT NULL,     -- normalised, as `runs.topic` and `decisions.topic`
  version        INTEGER NOT NULL,  -- 1, 2, ...: one per approval that changed the text
  sha256         TEXT NOT NULL,     -- of the approved text, as stored
  brief          TEXT NOT NULL,
  prompt_version TEXT,              -- the write/review prompts the pending brief came from
  approved_at    TEXT NOT NULL,
  PRIMARY KEY (topic, version)
);
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
    # Set once, on the first note a video ever gets, and never touched again.
    # `noted_at` answers "how old is this note?" and regeneration rewrites it,
    # which is right — but it means the library cannot answer "when did this
    # video become answerable?" after a single drain. Measured cost: a
    # before/after audit could not classify 8 of 33 videos as new coverage
    # versus regenerated, because two drains had overwritten every `noted_at`
    # in the library. Rule 23's argument, one layer down: the output was kept
    # and the history that explains it was not.
    #
    # NULL on every row that predates the column, which is the honest answer —
    # it is not backfilled from `noted_at`, because for a regenerated note that
    # value is the regeneration date and inventing a first-note date from it
    # would be exactly the fabricated provenance this exists to avoid.
    ("videos", "first_noted_at", "TEXT"),
    ("decisions", "evidence", "TEXT"),
    ("decisions", "prompt_version", "TEXT"),
    # Stage 2's RELEVANT line: the ranges it parsed, or NONE / UNAVAILABLE. NULL
    # on every earlier row and on every stage that is not stage 2 — the honest
    # answer, since the ranges were produced and then discarded until now.
    ("decisions", "relevant", "TEXT"),
    # Whether a judge stage decided anything, and what the judge itself said.
    # NULL on every earlier row, which `legacy_outcome` reads by the rules that
    # were in force when the row was written — the one place that still has to
    # recognise historical reason text, so nothing else ever does.
    ("decisions", "outcome", "TEXT"),
    ("decisions", "said", "TEXT"),
    # The coverage item stage 1 said a candidate would serve, with the state it
    # matched, so "why was this fetched before that?" is answerable from the
    # ledger (rule 23). NULL on every earlier row: nothing tagged them.
    ("decisions", "item", "TEXT"),
    # Note failures, so the drain stops meeting the same failing videos first.
    # Zero on every existing row, which is the honest answer: nothing recorded
    # a failure before this, and treating unrecorded as "never failed" puts
    # those videos exactly where they were in the queue.
    ("videos", "note_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("videos", "note_failed_at", "TEXT"),
    ("runs", "territory_map", "TEXT"),
    ("runs", "territory_version", "TEXT"),
    # What the library already satisfied before this run searched. NULL on
    # every run made before the planner had a coverage baseline, which is
    # the honest answer: nobody asked.
    ("runs", "library_baseline", "TEXT"),
    # How the brief was made: how many drafts were drawn and why each unusable
    # one was, and whether the review was used. NULL on every earlier run,
    # which is the honest answer — nothing checked a draft before this, and a
    # cut one went to the review unrecorded.
    ("runs", "brief_outcome", "TEXT"),
    ("quota", "exhausted_at", "TEXT"),
)

# Tables every supported ledger has had since it was created. M2 (95993ef)
# created all three, and is the first schema without the legacy `rejections`
# table, so a ledger without one of them is damaged, not old. `runs`, `queries`
# and `plans` arrived later (M2b, B1, B6): an older ledger can genuinely lack
# them, and a write-mode open adds them (second independent review, C).
_CORE_TABLES: frozenset[str] = frozenset({"videos", "decisions", "quota"})

# Tables an older marginalia created that no longer exist. Their presence is
# proof the ledger predates a schema change, which `CREATE TABLE IF NOT
# EXISTS` would otherwise hide.
_LEGACY_TABLES: dict[str, str] = {
    "rejections": "replaced by `decisions`, which logs every verdict and its stage",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now() -> str:
    """The ledger's clock, for a caller stamping a row it will write later."""
    return _utcnow()


# The YouTube Data API's daily quota resets at **midnight Pacific**, not at
# midnight UTC. Keying the quota table to UTC meant that for the ~7-8 hours
# between 00:00 UTC and the Pacific rollover, the local counter reported a fresh
# allowance Google had not yet granted.
#
# Measured on run `7da933b180fd`, which began 05:51 UTC — 22:51 the previous day
# in Pacific: the local ledger read 3,308 of 10,000 used while Google returned
# HTTP 429 "Quota exceeded for ... 'Search Queries per day'". 25 of 33 queries
# fell through to the unfiltered overflow provider, and the map's correctly
# reasoned 12-month recency window went unapplied on all of them.
#
# **Derived from the rule, not from a fixed offset**, so the eight weeks a year
# when Pacific is UTC-8 rather than UTC-7 are handled. `zoneinfo` would be
# better, but Windows ships no IANA database and `tzdata` is a fourth dependency
# this project does not have. US Pacific has observed the same rule since 2007:
# daylight time from the second Sunday in March to the first Sunday in November.
# If Congress ever settles the perennial bill to abolish the switch, this is the
# function to change and `tests/test_ledger.py` pins the boundaries.
_PACIFIC_STANDARD_OFFSET = timedelta(hours=-8)
_PACIFIC_DAYLIGHT_OFFSET = timedelta(hours=-7)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> datetime:
    """The ``nth`` ``weekday`` of a month, as a naive UTC-less datetime."""
    first = datetime(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _pacific_is_daylight(moment: datetime) -> bool:
    """Is this UTC instant inside US Pacific daylight time?

    The switch happens at 02:00 *local*, which is 10:00 UTC in March (from
    standard time) and 09:00 UTC in November (from daylight time).
    """
    year = moment.year
    start = _nth_weekday(year, 3, 6, 2) + timedelta(hours=10)
    end = _nth_weekday(year, 11, 6, 1) + timedelta(hours=9)
    naive = moment.replace(tzinfo=None)
    return start <= naive < end


def quota_day(now: datetime | None = None) -> str:
    """The calendar date the API is currently counting against, ``YYYY-MM-DD``.

    Public because it is the answer to "which row am I spending from?", and that
    question is asked from outside the ledger whenever a report wants to say how
    much of today's allowance is gone.
    """
    moment = now or datetime.now(timezone.utc)
    offset = _PACIFIC_DAYLIGHT_OFFSET if _pacific_is_daylight(moment) else _PACIFIC_STANDARD_OFFSET
    return (moment + offset).strftime("%Y-%m-%d")


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


def _values(cls: type, row: sqlite3.Row) -> dict[str, object]:
    """A row's fields by name. A read-only ledger is never migrated, so a column
    added after it was written is absent rather than NULL — and NULL is what it
    would have held, so that is what it reads as."""
    present = set(row.keys())
    return {field.name: row[field.name] if field.name in present else None for field in fields(cls)}


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
    # Stage 2 only: where the judge said the relevant material is. Stored so a
    # verdict can be checked against the transcript; nothing decides on it.
    relevant: str | None = None
    # Whether this stage decided anything (`contracts.DECISION_OUTCOMES`), and
    # the judge's own verdict word. NULL on search rows and on rows written
    # before the columns existed; read through `resolved_outcome`, never raw.
    outcome: str | None = None
    said: str | None = None
    # Stage 1 only: `state: tag` — the coverage item the judge named and the
    # state it matched (`untagged`, or `unmatched: tag` for one matching none).
    item: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> DecisionRow:
        return cls(**_values(cls, row))

    @property
    def decided_on(self) -> str:
        """Just the calendar date, for a report line."""
        return self.decided_at[:10]

    @property
    def resolved_outcome(self) -> str | None:
        """What this row says happened: `None` for a search-stage row, which
        records a gate and not a judgement; the stored outcome when there is
        one; otherwise the legacy reading of a row written before it existed."""
        if self.stage == STAGE_SEARCH:
            return None
        if self.outcome is not None:
            return self.outcome
        return legacy_outcome(self)

    @property
    def is_judgement(self) -> bool:
        """Did a judge actually decide here? The one question every reader of
        relevance asks first, answered in one place."""
        return self.resolved_outcome == OUTCOME_JUDGED


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
    library_baseline: str | None = None
    # JSON: draft_attempts, draft_problems, review_problem, final_is.
    brief_outcome: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> RunRow:
        return cls(**_values(cls, row))


@dataclass(frozen=True)
class QueryRow:
    """One search a run issued, and why it issued it.

    ``origin`` is the column the table exists for. Without it a run's queries
    are a list with no causal story, and *"why did it search for that?"* — the
    first question anyone asks when a round goes wrong — has no answer at all.
    ``origin_detail`` carries the half ``origin`` cannot: which territory term,
    or which unexplained concept, produced this particular string.

    ``filters_json`` holds each filter's value **and the reason given for it**,
    because a filter you cannot inspect is one you cannot correct. Two of the
    four exclude content, so this is the record of what a run chose not to see.
    """

    run_id: str
    round: int
    query: str
    filters_json: str | None
    origin: str
    origin_detail: str | None
    results_count: int | None
    provider: str | None
    quota_units: int | None
    ran_at: str

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> QueryRow:
        return cls(**_values(cls, row))


@dataclass(frozen=True)
class BriefRow:
    """One approved version of a topic's brief.

    The owner confirms a topic's brief once and every later run reuses it, so
    coverage across runs of one topic is measured against one target rather
    than against whichever brief each run happened to draw. A new approval of
    a changed text is a new version; the earlier ones are kept, and each run
    records the version it used in `runs.brief_outcome` (rule 23).
    """

    topic: str
    version: int
    sha256: str
    brief: str
    prompt_version: str | None
    approved_at: str

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> BriefRow:
        return cls(**_values(cls, row))


@dataclass(frozen=True)
class PlanRow:
    """One round's plan, as the planner returned it.

    ``plan_json`` is ``RoundPlan.as_dict()`` — the coverage assessment, the
    queries, the revisions, the refusals, the recommendation and its reason.
    Stored whole rather than split into columns because nothing queries inside
    it: it is read back in one piece, by a person or by the export renderer.

    Before this existed, a plan survived only as the short gap label attached to
    each query it produced. That answers *what* the planner asked for and not
    *why*, which is the question storing runs was meant to make answerable.
    """

    run_id: str
    round: int
    plan_json: str
    prompt_version: str | None
    planned_at: str

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> PlanRow:
        return cls(**_values(cls, row))


# Every table the current schema expects, and the columns it must have. Used
# by the drift guard; kept beside the dataclasses so it cannot fall behind.
_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "videos": frozenset(field.name for field in fields(VideoRow)),
    "decisions": frozenset(field.name for field in fields(DecisionRow)),
    "quota": frozenset({"provider", "date", "units_used", "exhausted_at"}),
    "runs": frozenset(field.name for field in fields(RunRow)),
    # B4 gave the table a writer and a reader, so it gets a dataclass like the
    # rest. Until then the columns were listed by hand here, deliberately: a row
    # type for a table nothing read would have been inventing its shape twice.
    "queries": frozenset(field.name for field in fields(QueryRow)),
    "plans": frozenset(field.name for field in fields(PlanRow)),
    "briefs": frozenset(field.name for field in fields(BriefRow)),
}



# A note whose marking pass failed is recorded under its prompt version plus
# this suffix. Kept — the note is the call's whole cost and stands as written —
# but never current, so the next `marg note` writes it again, and counted on
# its own in `note --status`. In an outage one note's pass failed, it was
# recorded current, and ask read the writer's unmarked inference as the video's
# words (rule 26).
UNMARKED_SUFFIX = "+unmarked"

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


# Reason strings that older code wrote, verbatim, for a judge stage that decided
# nothing. Each was a literal in the code that wrote it — see `git log -S` on
# each — so an exact match is right *here*, for history, and nowhere else: rows
# written now carry `outcome` and are never read by their prose.
#
# The last four were missing from the set discovery used to recognise, so a
# video whose B2-era opening call failed, or whose pre-2026-10 stage-2 reply had
# no verdict word, read as "already judged for this topic" for good.
_LEGACY_UNJUDGED_REASONS: dict[str, str] = {
    UNJUDGED_CALL_FAILED: OUTCOME_CALL_FAILED,
    UNJUDGED_TOO_LARGE: OUTCOME_TOO_LARGE,
    UNJUDGED_TRUNCATED: OUTCOME_TRUNCATED,
    UNJUDGED_NO_VERDICT: OUTCOME_NO_VERDICT,
    "opening judge failed, kept": OUTCOME_CALL_FAILED,  # stage `opening`, before B3
    "verdict could not be parsed, kept": OUTCOME_NO_VERDICT,  # `parse_opening`, before B3
    "batch call failed": OUTCOME_CALL_FAILED,  # stage 1, a failed batch
    "verdict could not be parsed": OUTCOME_NO_VERDICT,  # stage 1, a missing position
}
# Before 2026-10 a stage-2 reply with no verdict word was stored as `yes`, with
# this prefix when it also had no REASON line. One with a REASON line kept the
# model's own words and is indistinguishable from a real YES; that residue is
# documented rather than guessed at.
_LEGACY_CONTRACT_BREAK = "reply broke the output contract ("
_LEGACY_NO_VERDICT_PROBLEM = "no readable verdict"


def legacy_outcome(decision: DecisionRow) -> str:
    """How a judge row written before `outcome` existed should be read.

    The only function that interprets historical reason text. Everything else
    asks `DecisionRow.is_judgement`, which calls this only when the row has no
    stored outcome.
    """
    reason = decision.reason or ""
    if reason in _LEGACY_UNJUDGED_REASONS:
        return _LEGACY_UNJUDGED_REASONS[reason]
    if decision.stage in _STAGE_TWO:
        if decision.verdict == VERDICT_MAYBE:
            # Before outcomes were stored, stage 2 wrote `maybe` for one thing
            # only: a call that decided nothing (the judge's own MAYBE was
            # resolved to `yes` then).
            return OUTCOME_NO_VERDICT
        if reason.startswith(_LEGACY_CONTRACT_BREAK) and _LEGACY_NO_VERDICT_PROBLEM in reason:
            return OUTCOME_NO_VERDICT
    return OUTCOME_JUDGED


def judged_for_topic(
    decisions: Sequence[DecisionRow], topic: str, *, model: str | None = None
) -> bool:
    """Has stage 2 already given this video a verdict for this topic?

    A verdict, not a call. A stage-2 call that failed, was cut off or carried
    no verdict is recorded too, and counting that row as a judgement meant one
    provider blip stopped the topic from ever reading the video again.

    The one non-judgement that does stand is a transcript too large for the
    model that refused it: asking the same model again is refused again, so it
    waits for a different model rather than spending a call every run.
    """
    normalised = normalise_topic(topic)
    for decision in decisions:
        if decision.stage not in _STAGE_TWO or decision.topic != normalised:
            continue
        outcome = decision.resolved_outcome
        if outcome == OUTCOME_JUDGED:
            return True
        if outcome == OUTCOME_TOO_LARGE and (not model or decision.model == model):
            return True
    return False


def rejection_in(decisions: Sequence[DecisionRow], topic: str) -> DecisionRow | None:
    """Gate 2: the judgement that currently declines this video for this topic.

    Read through `current_verdicts`, so it is a **judgement** — a search-stage
    row is the gate's own bookkeeping and an unjudged stage-2 row decided
    nothing — and the latest stage that read the video wins. Gate 2 used to take
    any `no` row for the topic, which included the `no` rows Gate 1 and Gate 2
    write themselves.
    """
    normalised = normalise_topic(topic)
    current = current_verdicts([d for d in decisions if d.topic == normalised]).get(normalised)
    if current is not None and current.verdict == VERDICT_NO:
        return current
    return None


def current_verdicts(decisions: Sequence[DecisionRow]) -> dict[str, DecisionRow]:
    """Per topic, the verdict of the latest stage that judged.

    **For one video and one topic, a later stage supersedes an earlier one.**
    Stage 2 read the transcript; stage 1 read the title. When they disagree the
    one that read wins — rule 13's `overturned`, expressed as a query.

    This is what a verdict *means*, and it is correct: 69 notes existed because
    a stage-1 YES was counted as an acceptance after the same run's stage 2 had
    overturned it seconds later on the same topic. Discovery and planning read
    relevance through here.

    It no longer gates note-writing. That was the error in the pass that added
    it: `x6akeHQ5f-M` was declined for *ai agent memory* because it missed that
    brief, and then cited by "what do these videos get wrong?", where a mediocre
    video is excellent evidence. Both judgements were right; they answer
    different questions, and only one of them is about whether a transcript can
    be read.

    **Judgements only.** A search-stage row records a gate, not a verdict, and a
    stage-2 row whose call failed decided nothing — letting either supersede a
    real verdict would let bookkeeping, or a provider blip, overrule a judge.
    """
    latest: dict[str, DecisionRow] = {}
    for decision in decisions:
        if not decision.is_judgement:
            continue
        rank = _STAGE_ORDER.get(decision.stage, 0)
        seen = latest.get(decision.topic)
        if seen is None or rank >= _STAGE_ORDER.get(seen.stage, 0):
            latest[decision.topic] = decision
    return latest


def wanted_for_a_note(row: VideoRow, decisions: Sequence[DecisionRow]) -> bool:
    """Can a useful note be written from what we hold? Not: is it relevant.

    **Note-writing consults the transcript, never the verdict.** Two questions
    have been sharing this function and they are not the same one:

    * *is this video relevant to topic T?* — lives in `decisions`, read by
      discovery and planning through `current_verdicts`
    * *can we write a useful note from what we hold?* — lives in the transcript,
      and is the only question here

    The measurement that forced the split: of the 69 videos a stage 2 declined
    and note-writing kept anyway, **37 are cited in stored answers and one in
    nine of them**. Stage 2 asks whether a video satisfies a brief; `/ask` asks
    whether a note helps answer a question, and a video that missed the first
    can be strong evidence for the second.

    Rule 13's concern never applied here either. It exists to stop *"a video
    stored because of its title"* — but rule 12 keeps the transcript whatever
    stage 2 said, so the title only ever decided whether to **fetch**. A note is
    written from the transcript, so a note for a declined video is a faithful
    account of real content.

    Two things still refuse, and both are about the text rather than the topic:

    ``EVIDENCE: DEGRADED``
        The judge said it could not read the transcript. `/ask` loads notes and
        has no way to know the text behind one was noise, so the failure mode is
        a confident wrong answer. Ahead of ``noted_at``, because regenerating
        from a transcript known to be garbled spends a model call to make the
        same bad note again. Unlike a rejection this is not a claim about
        relevance that another topic might overturn — it is a claim about the
        transcript, and the transcript is the same for every topic.

    below the length floor
        Enforced in the worker, where the transcript is in hand: this function
        is pure and takes decisions rather than fetching bytes. See
        `[note] min_transcript_chars`.

    **A cost that changed is why this is re-derived rather than inherited.**
    When the old rule was written, more notes meant a worse answer, because a
    library over budget filled the context by sorted video id and an extra note
    displaced a better one at random. The selector now ranks and fills to a
    budget, so an extra note is cheap in a way it was not.
    """
    if any(
        decision.stage in _STAGE_TWO and decision.evidence == EVIDENCE_DEGRADED
        for decision in decisions
    ):
        return False
    return True


class Ledger:
    """SQLite-backed ledger. One instance owns one connection."""

    def __init__(self, db_path: Path | str, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        if read_only:
            # For `marg doctor`, whose promise is that it changes nothing. The
            # ordinary path below migrates an older ledger in place before it
            # checks it — additive columns, new tables, the status rename — so a
            # doctor opened that way would alter the file it was diagnosing.
            # SQLite enforces this one: a write raises instead of landing.
            self._conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            try:
                self._check_schema(allow_additive_gaps=True)
            except LedgerError:
                # Closed, so the file the message says to copy aside can be moved.
                self._conn.close()
                raise
            # A later table an older ledger lacks reads as empty, not as an
            # OperationalError on the first query (see `list_runs`).
            self._absent = frozenset(_EXPECTED_COLUMNS) - self._tables()
            return
        self._absent = frozenset()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # A ledger this version will refuse must be refused *before* anything
        # below writes to it. The refusal used to come last, after the additive
        # columns, the new tables and the status rename had already landed — so
        # an older version could no longer read the file it was told to go back
        # to, which is the one recovery the message recommends.
        # The whole check, in its read-only form (missing additive columns and
        # missing tables are what the steps below add): a legacy table *or* a
        # missing core column is refused with the file untouched.
        try:
            self._check_schema(allow_additive_gaps=True, fresh_ok=True)
        except LedgerError:
            self._conn.close()
            raise
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

        ``content_hash`` is ``Library.transcript_digest`` of the file *after*
        it was written — every store path passes it, so ``marg doctor`` can
        tell a transcript that changed from one that was never hashed. A
        ``None`` keeps whatever an earlier store recorded (COALESCE), which is
        what a restored-from-archive row looks like: unrecorded, not wrong.
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
        # `first_noted_at` uses COALESCE rather than a read-then-write: the
        # existing value wins whenever there is one, so regeneration cannot
        # move it and no caller has to remember not to.
        self._update(
            video_id,
            """
            UPDATE videos SET
                note_version   = ?,
                note_model     = ?,
                noted_at       = ?,
                first_noted_at = COALESCE(first_noted_at, ?),
                note_failures  = 0,
                note_failed_at = NULL,
                last_updated   = ?
            WHERE video_id = ?
            """,
            (version, model, _utcnow(), _utcnow(), _utcnow(), video_id),
        )
        log.info("ledger: %s noted (%s, %s)", video_id, version, model)
        return self._require(video_id)

    def mark_note_failed(self, video_id: str) -> VideoRow:
        """Record a note attempt that failed — a fragment, an invalid note, a
        transient error. **Not** called for a refusal: a billing error says
        nothing about the video (rule 24).

        The video stays in the queue; this only moves it behind the ones not yet
        tried, so a drain is never again spent on the same failures first.
        """
        self._update(
            video_id,
            """
            UPDATE videos SET
                note_failures  = COALESCE(note_failures, 0) + 1,
                note_failed_at = ?
            WHERE video_id = ?
            """,
            (_utcnow(), video_id),
        )
        log.info("ledger: %s note attempt failed", video_id)
        return self._require(video_id)

    def videos_needing_note(
        self,
        *,
        version: str,
        stale_only: bool = True,
        missing_only: bool = False,
        limit: int | None = None,
    ) -> list[VideoRow]:
        """Videos holding a transcript, wanted, with no note from this prompt.

        Keyed on ``has_transcript`` because a note is written *from* the
        transcript — this is the one place where holding the file, rather than
        library membership, is the right question. It is also the job that
        turns the former into the latter.

        Filtered by :func:`wanted_for_a_note` since B2, because holding a
        transcript stopped implying that anyone chose the video. A stage-2
        rejection keeps its bytes and must not then be handed a note.

        Three widths, and the third exists because a prompt version is a blunt
        instrument. ``stale_only`` is the default — anything not written by this
        prompt. ``stale_only=False`` is every wanted video, which is what
        ``--regenerate --all`` means. **``missing_only`` is videos with no note
        at all**, ignoring version entirely.

        That last one is what makes a prompt bump affordable. `note_is_current`
        is exact string equality, so v6 marks all 422 existing notes stale and a
        plain run would queue every one — thirteen hours and $2.30. When the
        change clarifies a rare failure path rather than adding content, a v5
        and a v6 note of the same video are nearly identical, `ask.py` never
        looks at versions, and a mixed library reads fine. Regenerating is then
        a choice rather than a consequence.

        Ordered by ``first_seen`` so a ``--limit`` run is a stable prefix rather
        than a random sample: reading three notes, revising the prompt and
        running again should show you the same three.
        """
        rows = [
            row
            for row in self.list_by_status(STATUS_HAS_TRANSCRIPT)
            if wanted_for_a_note(row, self.decisions_for(row.video_id))
        ]
        if missing_only:
            rows = [row for row in rows if row.noted_at is None]
        elif stale_only:
            rows = [row for row in rows if not row.note_is_current(version)]
        # Untried first (by `first_seen`, a stable sort), then fewest failures,
        # then the oldest failure — so a video that keeps failing sinks behind
        # the ones that failed once. Without this the same three failing videos
        # took the first three attempts of every drain and stopped it, and
        # nothing behind them was ever reached; ordering failed videos by
        # `first_seen` alone brought the same three back once the untried ran
        # out (independent review, F7).
        rows.sort(key=lambda row: (row.note_failures or 0, row.note_failed_at or ""))
        return rows[:limit] if limit is not None else rows

    def videos_to_refetch(
        self, *, limit: int | None = None, stale_after_s: float = 600.0
    ) -> list[VideoRow]:
        """Transient failures, the known-wanted ones first.

        **Ordering is the whole of the policy here.** A video some run judged
        YES is one this library was told to want and does not have; the rest are
        speculative, and a `--limit` run should spend itself on the first group.
        Within each group the oldest failure comes first, so a limited run is a
        stable prefix rather than a random sample — the same reason
        `videos_needing_note` orders by `first_seen`.

        Only `transient` is returned. A permanent failure is a claim that the
        video is gone, and re-asking about it is how a rule-8 error gets made
        twice: nothing here promotes a transient failure to permanent, so this
        list can only shrink by videos actually coming back.

        **And `pending` rows older than ``stale_after_s``.** `pending` means a
        fetch started and did not finish (rule 6) — an interrupt, a crash, a
        killed process. That is the most retryable state there is, and nothing
        read it back: refetch took only `failed/transient`, so an interrupted
        fetch was stranded until someone re-ingested the video by hand. The age
        floor keeps a refetch from racing a fetch another process is making
        right now.
        """
        rows = [row for row in self.list_by_status(STATUS_FAILED) if row.fail_kind == FAIL_TRANSIENT]
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)).isoformat(
            timespec="seconds"
        )
        rows += [row for row in self.list_by_status(STATUS_PENDING) if row.last_updated <= cutoff]
        wanted = {
            row.video_id
            for row in rows
            if any(d.verdict == VERDICT_YES for d in self.decisions_for(row.video_id))
        }
        rows.sort(key=lambda row: (row.video_id not in wanted, row.last_updated))
        return rows[:limit] if limit is not None else rows

    def was_wanted(self, video_id: str) -> bool:
        """Did any run judge this video YES? Reported beside each attempt."""
        return any(d.verdict == VERDICT_YES for d in self.decisions_for(video_id))

    def note_counts(self, *, version: str) -> dict[str, int]:
        """Of the videos holding a transcript: current, stale, or never noted.

        ``missing`` is the interesting number since B1 — those are videos with
        a transcript and no note, which means they are *not in the library*
        and no question can reach them.

        ``unreadable`` is what is left of B2's ``unwanted``: transcripts a stage
        2 marked ``EVIDENCE: DEGRADED``. Counted separately rather than folded
        into ``missing``, because they are not work waiting to be done.

        **Renamed because the word stopped being true.** ``unwanted`` meant
        "declined for relevance" and covered 229 videos; once note-writing
        stopped consulting the verdict it covered 3, and they are a different
        claim — the judge could not *read* the text. A count that shrinks by two
        orders of magnitude under an unchanged name is one nobody can compare
        across two days of ``--status``.
        """
        counts = {
            "has_transcript": 0, "current": 0, "stale": 0, "missing": 0, "unreadable": 0,
            "unmarked": 0,
        }
        for row in self.list_by_status(STATUS_HAS_TRANSCRIPT):
            counts["has_transcript"] += 1
            if not wanted_for_a_note(row, self.decisions_for(row.video_id)):
                # The judge said it could not read the transcript. Not a missing
                # note — a note deliberately not written, and counting it as
                # missing would report permanent work that will never be done.
                counts["unreadable"] += 1
                continue
            if row.noted_at is None:
                counts["missing"] += 1
            elif (row.note_version or "").endswith(UNMARKED_SUFFIX):
                # Written, but its marking pass failed: kept, and still due (its
                # version is never the current one). Its own bucket, not stale —
                # nothing about the prompt changed.
                counts["unmarked"] += 1
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
        relevant: str | None = None,
        outcome: str | None = None,
        said: str | None = None,
        item: str | None = None,
    ) -> DecisionRow:
        """Record one verdict. ``topic`` is normalised on the way in.

        ``outcome`` says whether a judge stage decided anything, and ``said`` is
        the judge's own verdict word when the stored one is a resolution of it.
        Every judge stage in production passes ``outcome``; ``None`` is read as
        a row from before the column existed (`legacy_outcome`).

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
        if outcome is not None and outcome not in DECISION_OUTCOMES:
            raise ValueError(
                f"unknown outcome {outcome!r}; expected one of {sorted(DECISION_OUTCOMES)}"
            )
        if outcome is not None and stage == STAGE_SEARCH:
            raise ValueError("a search-stage row records a gate, not a judgement outcome")
        if said is not None and said not in _VERDICTS:
            raise ValueError(f"unknown verdict word {said!r}; expected one of {sorted(_VERDICTS)}")

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
            relevant=relevant,
            outcome=outcome,
            said=said,
            item=item,
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO decisions
                    (video_id, topic, stage, verdict, reason, decided_at, run_id,
                     model, evidence, prompt_version, relevant, outcome, said, item)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row.relevant,
                    row.outcome,
                    row.said,
                    row.item,
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
        brief_outcome: str | None = None,
    ) -> RunRow:
        """Open a run. Written before the first model call, always.

        Same ordering rule as ``mark_pending``: a run with no ``finished_at``
        is a run that crashed, and that is diagnosable. Recording only on
        success leaves no trace that anything was ever attempted.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, topic, brief, brief_version, model, started_at, brief_outcome
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, normalise_topic(topic), brief, brief_version, model,
                    _utcnow(), brief_outcome,
                ),
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

    def save_territory(self, run_id: str, territory_map: str, version: str) -> RunRow:
        """Store the map this run worked from, in full.

        Same argument as storing the brief. When results look wrong the first
        question is *what did it think this topic covered?*, and a list of the
        queries that ran does not answer it — the queries are the conclusion,
        and the map is the reasoning that produced them.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET territory_map = ?, territory_version = ? WHERE run_id = ?",
                (territory_map, version, run_id),
            )
        if cursor.rowcount == 0:
            raise LedgerError(f"no run row for {run_id!r}: start_run() must be called first")
        log.info("run %s stored a territory map (version %s)", run_id, version)
        return self.get_run(run_id)  # type: ignore[return-value]

    def save_baseline(self, run_id: str, baseline: str) -> RunRow:
        """Store what the library already covered when this run started.

        On the run rather than in `plans`, because it is a property of the run's
        starting conditions — like the brief and the map — not a decision made
        during a round. It is also the only one of the three that describes
        something the run did *not* do: the videos it names were never fetched,
        judged or counted anywhere else.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET library_baseline = ? WHERE run_id = ?", (baseline, run_id)
            )
        if cursor.rowcount == 0:
            raise LedgerError(f"no run row for {run_id!r}: start_run() must be called first")
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunRow.from_sqlite(row) if row is not None else None

    def list_runs(self, topic: str | None = None) -> list[RunRow]:
        if "runs" in self._absent:
            # A read-only open of a ledger older than M2b: no run was recorded.
            return []
        if topic is None:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE topic = ? ORDER BY started_at", (normalise_topic(topic),)
            ).fetchall()
        return [RunRow.from_sqlite(row) for row in rows]

    # -- queries -----------------------------------------------------------

    def log_query(
        self,
        run_id: str,
        *,
        round: int,
        query: str,
        origin: str,
        origin_detail: str | None = None,
        filters_json: str | None = None,
        results_count: int | None = None,
        provider: str | None = None,
        quota_units: int | None = None,
        ran_at: str | None = None,
    ) -> QueryRow:
        """Record one search, with where it came from and what it cost.

        ``ran_at`` is when the search *started*, passed in by a caller that
        knows. The row is written after the search, and the search writes its
        candidates' gate decisions as it goes, so a timestamp taken here can
        sort after them — and the run export, which attributes decisions to the
        round whose queries had started, then filed a round's first decisions
        under the round before.

        Written after the search rather than before it, unlike ``mark_pending``,
        and the difference is deliberate: a fetch that dies mid-flight leaves a
        file half-written and a state nobody can infer, while a search that dies
        leaves nothing at all. What this row is for is the *result* — how many
        candidates a query surfaced, and at what quota cost — and a row written
        beforehand could carry neither.
        """
        row = QueryRow(
            run_id=run_id,
            round=round,
            query=query,
            filters_json=filters_json,
            origin=origin,
            origin_detail=origin_detail,
            results_count=results_count,
            provider=provider,
            quota_units=quota_units,
            ran_at=ran_at or _utcnow(),
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO queries (
                    run_id, round, query, filters_json, origin, origin_detail,
                    results_count, provider, quota_units, ran_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.run_id,
                    row.round,
                    row.query,
                    row.filters_json,
                    row.origin,
                    row.origin_detail,
                    row.results_count,
                    row.provider,
                    row.quota_units,
                    row.ran_at,
                ),
            )
        log.info("query: %s round %d %s/%r", run_id, round, origin, query)
        return row

    def queries_for_run(self, run_id: str) -> list[QueryRow]:
        rows = self._conn.execute(
            "SELECT * FROM queries WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [QueryRow.from_sqlite(row) for row in rows]

    # -- plans -------------------------------------------------------------

    def save_plan(
        self,
        run_id: str,
        *,
        round: int,
        plan_json: str,
        prompt_version: str | None = None,
    ) -> PlanRow:
        """Store one round's plan, whole.

        Requires the run row, like everything else written against a run: a
        plan belonging to no run is a plan nothing can find again.
        """
        if self.get_run(run_id) is None:
            raise LedgerError(
                f"no run row for {run_id!r}: start_run() must be called first"
            )
        planned_at = _utcnow()
        with self._conn:
            self._conn.execute(
                "INSERT INTO plans (run_id, round, plan_json, prompt_version, planned_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, round, plan_json, prompt_version, planned_at),
            )
        log.info("run %s stored the plan for round %d", run_id, round)
        return PlanRow(
            run_id=run_id,
            round=round,
            plan_json=plan_json,
            prompt_version=prompt_version,
            planned_at=planned_at,
        )

    # -- approved briefs ----------------------------------------------------

    def approved_brief(self, topic: str) -> BriefRow | None:
        """The latest approved brief for this topic, or ``None``.

        ``topic`` is normalised here, as everywhere a topic is a key, so
        "Prompt  Caching" finds the brief approved for "prompt caching".
        """
        if "briefs" in self._absent:
            # A read-only open of a ledger older than the table: nothing approved.
            return None
        row = self._conn.execute(
            "SELECT * FROM briefs WHERE topic = ? ORDER BY version DESC LIMIT 1",
            (normalise_topic(topic),),
        ).fetchone()
        return BriefRow.from_sqlite(row) if row is not None else None

    def approved_briefs(self, topic: str) -> list[BriefRow]:
        if "briefs" in self._absent:
            return []
        rows = self._conn.execute(
            "SELECT * FROM briefs WHERE topic = ? ORDER BY version",
            (normalise_topic(topic),),
        ).fetchall()
        return [BriefRow.from_sqlite(row) for row in rows]

    def approve_brief(
        self, topic: str, brief: str, sha256: str, *, prompt_version: str | None = None
    ) -> BriefRow:
        """Store a new approved version: one more than the latest.

        Checking the text is the caller's job and is done before this is
        reached (`flow/brief.approve`); this only numbers and stores it.
        """
        key = normalise_topic(topic)
        latest = self.approved_brief(key)
        version = 1 if latest is None else latest.version + 1
        approved_at = _utcnow()
        with self._conn:
            self._conn.execute(
                "INSERT INTO briefs (topic, version, sha256, brief, prompt_version, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, version, sha256, brief, prompt_version, approved_at),
            )
        log.info("brief v%d approved for %r", version, key)
        return BriefRow(key, version, sha256, brief, prompt_version, approved_at)

    def plans_for_run(self, run_id: str) -> list[PlanRow]:
        rows = self._conn.execute(
            "SELECT * FROM plans WHERE run_id = ? ORDER BY round, rowid", (run_id,)
        ).fetchall()
        return [PlanRow.from_sqlite(row) for row in rows]

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

    def topic_rejection(self, video_id: str, topic: str) -> DecisionRow | None:
        """Gate 2, read from the ledger. See `rejection_in`."""
        normalised = normalise_topic(topic)
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE video_id = ? AND topic = ? "
            "ORDER BY decided_at, rowid",
            (video_id, normalised),
        ).fetchall()
        return rejection_in([DecisionRow.from_sqlite(row) for row in rows], normalised)

    def decisions_for(self, video_id: str) -> list[DecisionRow]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE video_id = ? ORDER BY decided_at, rowid",
            (video_id,),
        ).fetchall()
        return [DecisionRow.from_sqlite(row) for row in rows]

    # -- quota -------------------------------------------------------------
    #
    # Keyed by the API's own calendar date — Pacific, via `quota_day()`, because
    # a UTC key here once reported an allowance Google had not yet granted — so
    # the rollover is the key changing rather than a reset anyone has to
    # remember to run. Old rows are kept: they cost nothing and record what a
    # day actually spent.

    def quota_used(self, provider: str, *, date: str | None = None) -> int:
        row = self._conn.execute(
            "SELECT units_used FROM quota WHERE provider = ? AND date = ?",
            (provider, date or quota_day()),
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
        today = date or quota_day()
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

    def mark_quota_exhausted(self, provider: str, *, date: str | None = None) -> None:
        """Record that the API itself refused for the rest of its quota day.

        **Google's refusal outranks the local counter.** `units_used` is an
        estimate built from published per-call costs; a 429 naming the daily
        limit is the service stating a fact. When the two disagree the estimate
        is wrong, whatever it reads — as on run `7da933b180fd`, where the ledger
        said 3,308 of 10,000 while every request was being refused.

        Recording it stops the run paying for refusals. Charging before a
        request is right (rule 6's ordering, so a crash over-counts rather than
        under-counts), but continuing to charge *after* the service has said no
        is spending against a budget that is not there: that run burned ~3,300
        units on requests that returned nothing.
        """
        today = date or quota_day()
        when = _utcnow()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO quota (provider, date, units_used, exhausted_at) VALUES (?, ?, 0, ?)
                ON CONFLICT(provider, date) DO UPDATE SET
                    exhausted_at = COALESCE(quota.exhausted_at, excluded.exhausted_at)
                """,
                (provider, today, when),
            )
        log.warning(
            "quota: %s reported its daily quota exhausted; no further metered "
            "calls will be sent until %s rolls over",
            provider,
            today,
        )

    def quota_exhausted(self, provider: str, *, date: str | None = None) -> bool:
        """Has the API already refused today? Checked before spending anything."""
        row = self._conn.execute(
            "SELECT exhausted_at FROM quota WHERE provider = ? AND date = ?",
            (provider, date or quota_day()),
        ).fetchone()
        return bool(row and row["exhausted_at"])

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

    def _check_schema(self, *, allow_additive_gaps: bool = False, fresh_ok: bool = False) -> None:
        """Fail loudly on a ledger written by an older version.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone,
        so a ledger predating a schema change would otherwise fail much later
        with an opaque ``IndexError`` from row unpacking, or — worse — keep
        writing verdicts into a table nothing reads. There is no migration
        path yet, so the message must say exactly what to do instead.

        ``allow_additive_gaps`` is the read-only path. A write-mode open adds a
        missing additive column before checking; a read-only one may not, and
        refusing then made `marg doctor` unusable on exactly the ledger a new
        version had not yet touched. An additive column reads as NULL when it is
        absent, so its absence is not drift. Anything else still is.

        **A missing table is a gap only if it is a later one.** Skipping every
        absent table let a ledger that had lost `videos` open read-only and
        fail on its first query, and let a write-mode open create an empty
        `videos` in its place — a library whose ledger reported no videos at
        all (second independent review, C). `_CORE_TABLES` are refused before
        anything is written. ``fresh_ok`` is the write-mode open of a file with
        no marginalia table at all, which is a new ledger, not a damaged one;
        read-only, the same file is not a ledger to diagnose.
        """
        tables = self._tables()

        for name in sorted(_LEGACY_TABLES):
            if name in tables:
                raise self._stale(f"it still has the `{name}` table ({_LEGACY_TABLES[name]})")

        if not tables & set(_EXPECTED_COLUMNS):
            if fresh_ok:
                return
            raise LedgerError(
                f"{self.db_path} is not an initialized marginalia ledger: it has none "
                f"of the ledger's tables. Nothing was changed."
            )

        additive = {(table, column) for table, column, _ in _ADDITIVE_COLUMNS}
        for table, expected in _EXPECTED_COLUMNS.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if allow_additive_gaps and not present and table not in _CORE_TABLES:
                # A table a later version created. A read-only open cannot create
                # it; its one reader on that path (`list_runs`, for the doctor)
                # answers empty instead.
                continue
            if not present and table in _CORE_TABLES:
                raise self._damaged(table)
            missing = sorted(
                column
                for column in expected - present
                if not (allow_additive_gaps and (table, column) in additive)
            )
            if missing:
                raise self._stale(
                    f"table `{table}` is missing column(s): {', '.join(missing)}"
                )

    def _damaged(self, table: str) -> LedgerError:
        return LedgerError(
            f"{self.db_path} has no `{table}` table. Every marginalia ledger has had "
            f"one since it was created, so the file is damaged rather than old, and "
            f"it was refused before anything was written to it. There is no automatic "
            f"repair. Do not delete the library: its transcripts are the part that "
            f"cannot be cheaply recreated. Copy ledger.db somewhere safe, then restore "
            f"an earlier copy of it, or recreate the table by hand against the schema "
            f"in core/ledger.py."
        )

    def _stale(self, detail: str) -> LedgerError:
        return LedgerError(
            f"{self.db_path} was written by an older version of marginalia: {detail}. "
            # Not "delete the library and re-ingest", which this said until
            # 2026-10: transcripts are the one artifact rule 3 calls fragile to
            # reacquire, and a schema mismatch is a fact about ledger.db alone.
            f"There is no automatic migration. Do not delete the library: its "
            f"transcripts are the part that cannot be cheaply recreated. Copy "
            f"ledger.db somewhere safe, then use the marginalia version that wrote "
            f"it, or add what is missing by hand against the schema in "
            f"core/ledger.py."
        )

    def _require(self, video_id: str) -> VideoRow:
        row = self.get(video_id)
        if row is None:  # pragma: no cover - only reachable if the row vanished
            raise LedgerError(f"ledger row for {video_id!r} disappeared")
        return row
