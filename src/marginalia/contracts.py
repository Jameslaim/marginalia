"""Every structure that crosses a module boundary.

One place, so that a boundary is a thing you can read rather than infer. If a
type is defined where it is produced, then `flow/` importing `brain/` to name a
verdict and `brain/` importing `providers/` to name a search result is how a
layered package quietly becomes a knot — each import individually reasonable,
the graph as a whole cyclic.

Everything here is data. Behaviour lives with the module that owns it, and the
methods that did come along are the ones that read a structure rather than act
on one: `Brief.intent` picks a line out of text it already holds,
`VideoRow.from_sqlite` and `note_is_current` read a row.

**Protocols and exception types stay where they are.** `TranscriptProvider`,
`SearchProvider`, `PermanentError` and `TransientError` remain in
`providers/base.py`, which imports its data types from here. A Protocol is the
shape of an implementation, not a structure in flight, and it belongs beside the
implementations it constrains.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, fields

__all__ = [
    # verdict vocabulary
    "VERDICT_YES",
    "VERDICT_NO",
    "VERDICT_MAYBE",
    # transcripts and search
    "TranscriptLine",
    "TranscriptResult",
    "SearchResult",
    # judgement
    "Brief",
    "MetadataVerdict",
    "OpeningVerdict",
    # ledger rows
    "VideoRow",
    # discovery runs
    "RoundReport",
    "DiscoverRun",
    # search runs
    "NEW",
    "KNOWN",
    "REJECTED",
    "Candidate",
    "SearchRun",
    # note runs
    "NOTED",
    "SKIPPED",
    "FAILED",
    "STOP_DONE",
    "STOP_MAX_COST",
    "STOP_LIMIT",
    "STOP_REPEATED_FAILURES",
    "NoteResult",
    "NoteRun",
    # answers
    "AnswerCitation",
    "CitationScan",
    "LoadedNote",
    "Opened",
    "Answer",
    # timestamp snapping
    "TranscriptIndex",
    "NoteCitation",
    "Change",
    "SnapResult",
    # model calls
    "Usage",
]


# -- verdicts ---------------------------------------------------------------

# Three-way on purpose: forcing yes/no on thin evidence makes the judge guess.
#
# Here rather than in `core/ledger.py` because these three strings are the
# vocabulary brain, flow and core all speak. Importing them from the ledger
# pulled sqlite3 into every process that imported the judge — a reasoning
# module reaching a database module to learn the word "maybe".
VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_MAYBE = "maybe"


# -- transcripts ------------------------------------------------------------


@dataclass
class TranscriptLine:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptResult:
    lines: list[TranscriptLine]
    language: str
    source: str  # "captions"
    title: str
    channel: str
    duration_s: int
    upload_date: str | None
    chapters: list[dict] = field(default_factory=list)  # [{"start_ms": int, "title": str}]
    # Which caption track was used: "manual" (author-written) or "auto"
    # (machine-generated). Recorded because manual captions are materially
    # better and a later upgrade pass needs to know which it has.
    caption_kind: str = "manual"


# -- search -----------------------------------------------------------------


@dataclass
class SearchResult:
    """One candidate video, as a search provider described it.

    Everything here is cheap metadata — the material stage 1 of relevance
    judging reads (M2b). No transcript text ever passes through this type.

    ``duration_s``, ``published_at`` and ``view_count`` are optional because
    providers differ in what they return: YouTube's ``search.list`` omits all
    three, and yt-dlp's flat extraction omits some.
    """

    video_id: str
    title: str
    channel: str
    description: str
    duration_s: int | None
    published_at: str | None
    view_count: int | None
    # Which provider produced this result. Set by the provider itself; the
    # report names it, and a mixed-provider result set must stay attributable.
    provider: str = ""


# -- judgement --------------------------------------------------------------


@dataclass(frozen=True)
class Brief:
    """The explicit statement of what is wanted, and the prompts behind it."""

    topic: str
    text: str
    version: str

    @property
    def intent(self) -> str:
        """The INTENT section on one line, for the report."""
        lines = self.text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().upper().startswith("INTENT"):
                body = line.partition(":")[2].strip()
                if body:
                    return " ".join(body.split())
                for follower in lines[index + 1 :]:
                    if follower.strip():
                        return " ".join(follower.split())
        first = next((line for line in lines if line.strip()), "")
        return " ".join(first.split())


@dataclass(frozen=True)
class MetadataVerdict:
    video_id: str
    verdict: str
    reason: str
    parsed: bool = True


@dataclass(frozen=True)
class OpeningVerdict:
    video_id: str
    verdict: str
    reason: str
    concepts: list[str] = field(default_factory=list)
    parsed: bool = True


# -- ledger rows ------------------------------------------------------------


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
    note_version: str | None = None
    note_model: str | None = None
    noted_at: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> VideoRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    def note_is_current(self, version: str) -> bool:
        """Does this video carry a note written by the current prompt?

        Both halves matter. ``noted_at`` alone says a note was written once;
        ``note_version`` says which prompt wrote it, and that is what turns
        "which notes are stale?" into a query rather than a list somebody
        maintains by hand.
        """
        return self.noted_at is not None and self.note_version == version


# -- discovery runs ---------------------------------------------------------


@dataclass
class RoundReport:
    number: int
    queries: list[str]
    candidates: int = 0
    known: int = 0
    rejected_before: int = 0
    verdict_no: int = 0
    verdict_maybe: int = 0
    verdict_yes: int = 0
    flipped_to_no: int = 0
    stored: int = 0
    failed: int = 0
    concepts: list[str] = field(default_factory=list)
    from_threads: bool = False


@dataclass
class DiscoverRun:
    run_id: str
    topic: str
    brief: Brief
    model: str
    rounds: list[RoundReport] = field(default_factory=list)
    stored_ids: list[str] = field(default_factory=list)
    stopped_because: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    dry_run: bool = False

    cost_usd: float = 0.0

    @property
    def candidates_seen(self) -> int:
        return sum(report.candidates for report in self.rounds)

    @property
    def judged(self) -> int:
        return sum(
            report.verdict_no + report.verdict_maybe + report.verdict_yes
            for report in self.rounds
        )


# -- search runs -------------------------------------------------------------

# What the report prints in its first column, and what `--json` carries.
NEW = "new"
KNOWN = "known"
REJECTED = "rejected"


@dataclass(frozen=True)
class Candidate:
    result: SearchResult
    verdict: str  # new | known | rejected
    reason: str  # short, printable, stable — a skill reads it

    @property
    def video_id(self) -> str:
        return self.result.video_id


@dataclass(frozen=True)
class SearchRun:
    query: str
    topic: str
    provider: str | None
    candidates: list[Candidate]
    # (provider, why) for each provider that was tried and fell through, in
    # order. The report names these: a silent fallback looks like a quota that
    # never runs out.
    fallbacks: list[tuple[str, str]] = field(default_factory=list)

    def of(self, verdict: str) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.verdict == verdict]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "found": len(self.candidates),
            KNOWN: len(self.of(KNOWN)),
            REJECTED: len(self.of(REJECTED)),
            NEW: len(self.of(NEW)),
        }


# -- note runs ---------------------------------------------------------------

NOTED = "noted"
SKIPPED = "skipped"
FAILED = "failed"

# Why a drain stopped early. Same vocabulary as discovery's, same purpose: a
# run that stops for an unknown reason cannot be tuned.
STOP_DONE = "done"
STOP_MAX_COST = "max_cost"
STOP_LIMIT = "limit"
STOP_REPEATED_FAILURES = "repeated_failures"


@dataclass(frozen=True)
class NoteResult:
    video_id: str
    outcome: str
    detail: str
    tokens: int = 0
    version: str | None = None


@dataclass
class NoteRun:
    version: str
    model: str
    results: list[NoteResult] = field(default_factory=list)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    stopped_because: str = STOP_DONE

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)


# -- answers -----------------------------------------------------------------


@dataclass(frozen=True)
class AnswerCitation:
    """A citation inside an *answer*: which video, and how far in.

    Not to be confused with :class:`NoteCitation`, which is a timestamp token
    inside a note. Both were called `Citation` until one file defined each and
    nothing could tell you which one a function returned.
    """

    video_id: str
    seconds: int
    raw: str


@dataclass(frozen=True)
class CitationScan:
    """What the parser found, and what it could not read.

    ``unparsed`` is the whole point. Three times in this project something was
    dropped silently and looked like success — a hardcoded temperature, an
    ASCII-only word class, and this parser. A count that says "104 shaped, 2
    unparsed" turns the fourth occurrence into a visible line instead of an
    absence nobody thinks to look for.
    """

    citations: list[AnswerCitation]
    unparsed: list[str]

    @property
    def shaped(self) -> int:
        return len(self.citations) + len(self.unparsed)


@dataclass(frozen=True)
class LoadedNote:
    video_id: str
    title: str
    text: str


@dataclass(frozen=True)
class Opened:
    """A transcript read for one question, and why it was read."""

    video_id: str
    why: str
    text: str


@dataclass
class Answer:
    question: str
    text: str = ""
    version: str = ""
    model: str = ""
    notes_loaded: int = 0
    opened: list[tuple[str, str]] = field(default_factory=list)  # video_id, why
    citations: list[AnswerCitation] = field(default_factory=list)
    # Citation-shaped strings the parser could not read. Reported rather than
    # discarded: a parser that drops most of its input must say so.
    unparsed: list[str] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    truncated: bool = False

    @property
    def escalated(self) -> bool:
        return bool(self.opened)


# -- timestamp snapping ------------------------------------------------------


@dataclass(frozen=True)
class TranscriptIndex:
    """Transcript words in order, each with the start of the cue it came from.

    ``freq`` is what makes matching safe: it is per-transcript, so "cache" is a
    common word in a caching video and a rare one in a SQLite video, and the
    anchors adapt without anyone maintaining a stop-word list.
    """

    words: tuple[str, ...]
    times_ms: tuple[int, ...]
    freq: dict[str, int]
    positions: dict[str, tuple[int, ...]]

    def __bool__(self) -> bool:
        return bool(self.words)


@dataclass(frozen=True)
class NoteCitation:
    """One timestamp token in a note, and where it sits in the text.

    A note's citation, as opposed to :class:`AnswerCitation`, which is an
    answer's. This one carries character offsets because it gets rewritten in
    place; that one carries a video ID because it gets turned into a link.
    """

    start: int
    end: int
    raw: str
    ms: int
    parts: int
    has_millis: bool


@dataclass(frozen=True)
class Change:
    section: str
    before: str
    after: str
    delta_ms: int
    preview: str
    # Which line of the note moved. Only used to recognise the same citation
    # across passes, so a two-step move is reported as one net correction.
    line: int = -1


@dataclass
class SnapResult:
    video_id: str
    changed: bool = False
    snapped: list[Change] = field(default_factory=list)
    left: list[tuple[str, str, str]] = field(default_factory=list)  # section, raw, reason
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.snapped) + len(self.left)


# -- model calls -------------------------------------------------------------


@dataclass
class Usage:
    """What a run cost. Requests are the scarce resource here, not dollars.

    Here rather than in `llm.py` because four modules outside it read these
    fields off `client.usage` and `reply.usage` to fill in their own reports.
    Nobody outside constructs one, but reading a structure across a boundary is
    still crossing it.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
