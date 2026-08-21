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

import json
import sqlite3
from dataclasses import dataclass, field, fields

__all__ = [
    # asset state
    "STATUS_SEEN",
    "STATUS_PENDING",
    "STATUS_HAS_TRANSCRIPT",
    "STATUS_NEEDS_ASR",
    "STATUS_FAILED",
    "STATUS_DUPLICATE",
    "VIDEO_STATUSES",
    # judgement evidence
    "EVIDENCE_CLEAR",
    "EVIDENCE_DEGRADED",
    # query provenance
    "ORIGIN_BRIEF",
    "ORIGIN_TERRITORY",
    "ORIGIN_PLANNER",
    "ORIGIN_THREAD",
    # search filters
    "FILTER_PUBLISHED_AFTER",
    "FILTER_ORDER",
    "FILTER_RELEVANCE_LANGUAGE",
    "FILTER_VIDEO_DURATION",
    "FILTER_FIELDS",
    "HARD_FILTERS",
    "ORDER_VALUES",
    "VIDEO_DURATION_VALUES",
    "SearchFilters",
    # territory
    "Recency",
    "PlannedQuery",
    "FilterRejection",
    "TerritoryMap",
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
    "FullVerdict",
    "RELEVANT_UNAVAILABLE",
    "RELEVANT_NONE",
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


# -- asset state ------------------------------------------------------------

# What we *have*, and nothing else. Relevance is not a status: a video rejected
# for prompt caching is still a perfectly good video about Redis, and one field
# carrying both meanings is why it used to be labelled `rejected` globally. What
# was decided, and for which topic, lives in `decisions`.
STATUS_SEEN = "seen"
STATUS_PENDING = "pending"
STATUS_HAS_TRANSCRIPT = "has_transcript"
STATUS_NEEDS_ASR = "needs_asr"
STATUS_FAILED = "failed"
STATUS_DUPLICATE = "duplicate"

# `seen` and `pending` stay distinct, and the distinction is the whole payoff of
# the write-before-fetch rule. `pending` means *something was interrupted* —
# a row written before a network call that never came back. Collapsing it into
# `seen` would turn every candidate we merely looked at into a phantom crash,
# and the one signal that says "a fetch died here" would be gone.
VIDEO_STATUSES = frozenset(
    {
        STATUS_SEEN,
        STATUS_PENDING,
        STATUS_HAS_TRANSCRIPT,
        STATUS_NEEDS_ASR,
        STATUS_FAILED,
        STATUS_DUPLICATE,
    }
)


# -- judgement evidence -------------------------------------------------------

# How good the evidence behind a verdict was. B3 writes these; the column exists
# now so that no later migration is needed, and nothing reads it yet.
#
# The reason it is worth a column at all: a NO decided from a full transcript and
# a NO decided from a truncated description are not the same claim, and today
# they are indistinguishable in the log. Gate 2 treats every past NO as final,
# so a DEGRADED one is a video permanently excluded on evidence nobody can see.
EVIDENCE_CLEAR = "CLEAR"
EVIDENCE_DEGRADED = "DEGRADED"

# What stage 2 says about *where* the value is. Two different absences, kept
# apart on purpose: NONE means there is nothing worth pointing at, UNAVAILABLE
# means there is and the timestamps cannot be trusted. Collapsing them would
# make a video with unusable captions look like a video with nothing in it.
RELEVANT_NONE = "NONE"
RELEVANT_UNAVAILABLE = "UNAVAILABLE"


# -- query provenance ---------------------------------------------------------

# Where a search query came from. B4 and B5 write these; nothing reads them yet.
# This is the column that makes "why did it search for *that*?" answerable at
# all — without it a run's queries are a list with no causal story.
ORIGIN_BRIEF = "brief"
ORIGIN_TERRITORY = "territory"
ORIGIN_PLANNER = "planner"
ORIGIN_THREAD = "thread"


# -- search filters -----------------------------------------------------------

# The four search parameters a territory map may propose, named exactly as the
# model writes them and the validator reads them. Shared vocabulary, so rule 10
# puts them here rather than in `brain/` or `providers/`.
#
# **They are not equivalent, and the difference is the whole reason this is a
# validated structure rather than a dict.** Two of them exclude and two do not:
#
# ============================  ==========================================
# `publishedAfter`              hard — removes everything older, silently
# `videoDuration`               hard — removes everything outside the band
# `order`                       sorts only; a different slice, never a
#                               smaller universe
# `relevanceLanguage`           soft — ranks; a highly relevant result in
#                               another language still comes back
# ============================  ==========================================
#
# A hard filter applied without a reason excludes content permanently and
# invisibly, which is the same failure shape as a false-negative verdict: you
# never find out what it removed. `videoDuration` is the trap of the four — a
# rule that drops Shorts is the duration bias the stage-1 prompt spends four
# paragraphs forbidding, moved into the search layer where nobody would see it.
FILTER_PUBLISHED_AFTER = "publishedAfter"
FILTER_ORDER = "order"
FILTER_RELEVANCE_LANGUAGE = "relevanceLanguage"
FILTER_VIDEO_DURATION = "videoDuration"

FILTER_FIELDS = frozenset(
    {
        FILTER_PUBLISHED_AFTER,
        FILTER_ORDER,
        FILTER_RELEVANCE_LANGUAGE,
        FILTER_VIDEO_DURATION,
    }
)

# The two that exclude. Named as a set because the rules that matter are the
# ones about these two, and a reader should not have to remember which is which.
HARD_FILTERS = frozenset({FILTER_PUBLISHED_AFTER, FILTER_VIDEO_DURATION})

ORDER_VALUES = frozenset({"relevance", "date", "viewCount", "rating", "title"})
VIDEO_DURATION_VALUES = frozenset({"any", "short", "medium", "long"})

# `relevanceLanguage` deliberately has no value set. It takes an ISO 639-1 code
# and any list written here would go stale; more to the point it is the one soft
# parameter, so a wrong value costs ranking rather than content. Its shape is
# checked instead — see `brain/territory.py`.


@dataclass(frozen=True)
class SearchFilters:
    """What was asked of the search provider beyond the query string.

    Every field optional and unset by default, because the default search — no
    filters at all — is the widest one, and width is what discovery is short of.
    A filter is something a reason had to justify.
    """

    published_after: str | None = None
    order: str | None = None
    relevance_language: str | None = None
    video_duration: str | None = None

    def __bool__(self) -> bool:
        return bool(self.as_dict())

    def as_dict(self) -> dict[str, str]:
        """Only what is set, keyed by the wire name. Empty when nothing is."""
        pairs = (
            (FILTER_PUBLISHED_AFTER, self.published_after),
            (FILTER_ORDER, self.order),
            (FILTER_RELEVANCE_LANGUAGE, self.relevance_language),
            (FILTER_VIDEO_DURATION, self.video_duration),
        )
        return {name: value for name, value in pairs if value is not None}

    def without(self, name: str) -> SearchFilters:
        """The same filters with one field cleared. How a rejection is applied.

        A rejected filter never drops the query — it drops the *filter*, which
        widens the search. Rejecting toward width is the same asymmetry the
        judge follows: an unjustified exclusion is invisible and permanent, and
        one extra unfiltered query is neither.
        """
        attribute = _FILTER_ATTRIBUTES.get(name)
        if attribute is None:
            return self
        return SearchFilters(
            **{
                current.name: (None if current.name == attribute else getattr(self, current.name))
                for current in fields(self)
            }
        )


_FILTER_ATTRIBUTES = {
    FILTER_PUBLISHED_AFTER: "published_after",
    FILTER_ORDER: "order",
    FILTER_RELEVANCE_LANGUAGE: "relevance_language",
    FILTER_VIDEO_DURATION: "video_duration",
}


# -- territory ----------------------------------------------------------------


@dataclass(frozen=True)
class Recency:
    """How far back this subject is worth searching, and why.

    ``months`` of ``None`` means *any time*, which is the default and the wider
    answer. The reason is stored with the decision because a recency call you
    cannot inspect is one you cannot correct — "SQLite WAL mode" and "prompt
    caching pricing" want opposite answers and no rule produces both.
    """

    months: int | None = None
    reason: str = ""

    @property
    def limited(self) -> bool:
        return self.months is not None

    @property
    def label(self) -> str:
        return f"last {self.months} months" if self.months else "any time"


@dataclass(frozen=True)
class FilterRejection:
    """A filter the code refused to execute. Counted, logged, never silent.

    Same discipline as the unparsed counters: something the model proposed was
    dropped, and a drop nobody counts is indistinguishable from a proposal that
    was never made.
    """

    query: str
    field: str
    value: str
    reason: str


@dataclass(frozen=True)
class PlannedQuery:
    """One search to run, where it came from, and what was asked alongside it.

    ``origin_detail`` is the half of provenance that ``origin`` cannot carry: a
    territory query is only interesting if you can see *which* map term produced
    it, which is exactly the question asked when a round finds something the
    topic words never would have.
    """

    text: str
    origin: str
    origin_detail: str = ""
    filters: SearchFilters = SearchFilters()
    # Set when this query exists only to *add* a slice another query excludes —
    # the one circumstance in which `videoDuration` is allowed at all.
    variant_of: str = ""
    # Per filter, in the model's words. Kept beside the value in `filters_json`
    # so that reading the ledger answers "why this filter?" without a rerun.
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        """Identity for duplicate detection: the text *and* the filters.

        Not the text alone. The same query ordered by `relevance` and by `date`
        returns different slices of the same result set — free width, and the
        reason `order` is worth proposing — so collapsing them as duplicates
        would throw away the thing that makes the parameter useful.
        """
        return (
            " ".join(self.text.lower().split()),
            tuple(sorted(self.filters.as_dict().items())),
        )

    def filters_json(self) -> str:
        """``{"order": {"value": "date", "why": "..."}}`` — value and reason together."""
        return json.dumps(
            {
                name: {"value": value, "why": self.reasons.get(name, "")}
                for name, value in self.filters.as_dict().items()
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True)
class TerritoryMap:
    """What a subject actually covers, worked out before anything is searched.

    The problem it exists for: search "AI agent memory" and you get videos with
    those words in the title. The video called "Building ReAct loops with tool
    calling" is exactly what was wanted and never says "agent" — and no amount
    of careful judging finds a video the search never returned.

    ``concepts`` and ``vocabulary`` are separate on purpose. A concept is
    something that falls *under* the brief; a vocabulary term is only a word the
    subject uses. The distinction is load-bearing at judging time, where the map
    is shown as **the words this subject uses and nothing more** — B3 measured
    this judge confabulating from suggestive context, and a territory map is a
    list of suggestive context.
    """

    topic: str
    version: str
    concepts: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)
    recency: Recency = Recency()
    queries: list[PlannedQuery] = field(default_factory=list)
    # Filters the validator refused, in the order they were caught.
    rejected: list[FilterRejection] = field(default_factory=list)
    parsed: bool = True
    problem: str = ""
    # What the mapping call cost. One call per run, so the report can name it
    # exactly rather than folding it into a total.
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.concepts or self.vocabulary or self.queries)

    @property
    def terms(self) -> list[str]:
        """Concepts and vocabulary together, deduplicated, in order.

        What the judge is shown. It is deliberately *not* the queries or the
        filters: those are instructions to a search engine, and showing them to
        a judge would be handing it the reasoning it is supposed to check.
        """
        seen: set[str] = set()
        terms: list[str] = []
        for term in [*self.concepts, *self.vocabulary]:
            key = " ".join(term.lower().split())
            if key and key not in seen:
                seen.add(key)
                terms.append(term)
        return terms

    def as_json(self) -> str:
        """What goes in ``runs.territory_map``.

        Stored whole, for the same reason the brief is: when results look wrong
        the first question is *what did it think this topic covered?*, and that
        is unanswerable from a list of queries.
        """
        return json.dumps(
            {
                "topic": self.topic,
                "version": self.version,
                "concepts": self.concepts,
                "vocabulary": self.vocabulary,
                "recency": {"months": self.recency.months, "reason": self.recency.reason},
                "queries": [
                    {
                        "text": query.text,
                        "origin_detail": query.origin_detail,
                        "filters": query.filters.as_dict(),
                        "reasons": query.reasons,
                        "variant_of": query.variant_of,
                    }
                    for query in self.queries
                ],
                "rejected": [
                    {
                        "query": rejection.query,
                        "field": rejection.field,
                        "value": rejection.value,
                        "reason": rejection.reason,
                    }
                    for rejection in self.rejected
                ],
                "parsed": self.parsed,
                "problem": self.problem,
            },
            ensure_ascii=False,
            indent=2,
        )


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
    # B2. A table of contents parsed out of the description, in the same shape
    # as `TranscriptResult.chapters` — so stage 1 and stage 2 read chapters
    # identically and neither learns which produced them. Empty when the
    # description has none, or has something the parser would not vouch for.
    chapters: list[dict] = field(default_factory=list)
    # B2. Does YouTube know of a caption track? **Tri-state, and the third
    # state is the point.** `None` means nobody asked — yt-dlp search does not
    # report it, and the API omits it when quota ran out before `videos.list`.
    # Only an explicit `False` may skip a fetch; `None` must behave exactly as
    # it did before this field existed, or a provider that cannot answer would
    # silently route every video to ASR.
    #
    # A hint, never a verdict: it reflects the tracks YouTube admits to, and
    # yt-dlp finds auto-captions where the API reports none. And it is a
    # caption fact, never a content fact — it must never reject a video.
    has_captions: bool | None = None


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
    """Stage 2 before B3, when it read only the opening.

    Kept because `prompts/judge_opening.md` and its parser are still here and
    still tested: the opening judge is the fallback if reading whole
    transcripts turns out to cost more than it is worth, and deleting the
    working version of a thing while replacing it is how a rollback stops
    being possible. Nothing in the live path constructs one.
    """

    video_id: str
    verdict: str
    reason: str
    concepts: list[str] = field(default_factory=list)
    parsed: bool = True


@dataclass(frozen=True)
class FullVerdict:
    """Stage 2's answer, having read the whole transcript.

    Replaces `OpeningVerdict`, which was named for evidence stage 2 no longer
    uses. Reading only the opening could not tell a video that delivers from one
    that opens well and never arrives — the two are identical for a thousand
    words — so the name had to go with the evidence.

    ``evidence`` is the field that earns its place. A NO decided from a full
    clean transcript and a NO decided from garbled captions are different
    claims, and Gate 2 treats every past NO as final. `DEGRADED` also suppresses
    the note: a note written from unreadable text is worse than no note, because
    `/ask` cannot tell that it was.

    ``relevant`` is where the value is, as timestamp ranges. Empty means the
    model said NONE; `UNAVAILABLE` means there is value but the timestamps
    cannot be trusted, which is a different claim and must not read as absence.
    """

    video_id: str
    verdict: str
    evidence: str = EVIDENCE_CLEAR
    reason: str = ""
    relevant: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    parsed: bool = True
    # Set when a consistency rule fired, for the log line. The verdict is still
    # usable — a violation resolves toward keeping — but it was not the reply
    # the contract asked for, and silently accepting it is how a broken output
    # format survives for months.
    problem: str = ""

    @property
    def timestamps_unavailable(self) -> bool:
        return self.relevant == [RELEVANT_UNAVAILABLE]


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
    # B1. Deliberately not backfilled: 64 existing rows would have meant 64
    # network calls for a column no current feature reads. New videos populate
    # it, and NULL is the honest answer for a video seen before it existed.
    channel_id: str | None = None

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
    """What one round did, with every outcome on its own counter.

    B3 separated three meanings that had been sharing two buckets. The
    principle is already written into `cli.py`, after a real run where
    thirteen transient fetch failures left no trace in the report at all: a
    round that says nothing about an outcome is indistinguishable from a round
    where it did not happen. B5's planner reads these numbers to decide what
    to do next, and three causes summed into one integer cannot be planned on.
    """

    number: int
    queries: list[str]
    candidates: int = 0
    # Gate 1: already in the library, so never a candidate at all.
    known: int = 0
    # Gate 2: carries a NO for this exact topic from an earlier run.
    rejected_before: int = 0
    verdict_no: int = 0
    verdict_maybe: int = 0
    verdict_yes: int = 0
    flipped_to_no: int = 0
    stored: int = 0
    # A fetch was attempted and did not produce a transcript. **Only** that —
    # it used to absorb the two below, which meant a round where nothing went
    # wrong could report failures.
    failed: int = 0
    # The transcript was already on disk from an earlier topic. Not a failure
    # and not a Gate 1 hit: the video *is* a candidate, it just costs nothing.
    already_held: int = 0
    # The provider said there is no caption track, so no fetch was attempted.
    # Nothing failed; the video was routed to ASR without being tried.
    no_captions: int = 0
    # Kept, but stage 2 could not read the transcript well enough to be sure.
    # Not a rejection and not a clean keep — and it suppresses the note, which
    # is a consequence nobody should have to infer from a silent counter.
    degraded: int = 0
    # Stage-2 replies that broke the five-line contract. Counted rather than
    # swallowed, the way citation parsing counts what it drops.
    unparsed: int = 0
    # B4. Filters the validator refused before any search ran. Its own counter,
    # like everything else here: a rejected filter is neither a failure nor a
    # rejection nor a parse problem, and a round that reported it as any of
    # those would be reporting three causes in one number.
    filters_rejected: int = 0
    concepts: list[str] = field(default_factory=list)
    from_threads: bool = False
    # B4. The queries as planned, with their origins and filters. `queries`
    # above stays a list of strings: it is what the report prints and what
    # `--json` has always carried, and a reader of either should not have to
    # learn a structure to see what was searched for.
    planned: list[PlannedQuery] = field(default_factory=list)
    # video_id -> the origins whose queries returned it. *Every* origin, not the
    # first: "would the old brief expansion have found this?" is a question
    # about all the queries that surfaced a video, and recording only the first
    # would answer it with whichever set happened to run earlier.
    surfaced_by: dict[str, set[str]] = field(default_factory=dict)

    @property
    def judged_here(self) -> int:
        """Candidates this round actually reached a stage-2 verdict on."""
        return self.stored + self.flipped_to_no

    def only_from(self, origin: str) -> list[str]:
        """Videos no other origin's queries returned. The phase's value, counted.

        A territory query that surfaces a video the brief's own expansion also
        surfaced has proved nothing. This is the set that would not have been
        seen at all — and since a video the search never returns is invisible to
        every stage after it, that set is the entire difference the map makes.
        """
        return [
            video_id
            for video_id, origins in self.surfaced_by.items()
            if origins == {origin}
        ]


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

    # B4. What this run understood the subject to cover, and every filter the
    # validator refused across all rounds. An empty map is a legitimate state:
    # a mapping call that failed leaves discovery narrower, not broken.
    territory: TerritoryMap = field(default_factory=lambda: TerritoryMap(topic="", version=""))
    rejected_filters: list[FilterRejection] = field(default_factory=list)

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
    # What was asked alongside the query string, and what the answer cost in
    # the provider's own units. Both go into the `queries` row: "why did it
    # search for that?" is only half the question, and "what did that buy?" is
    # the other half.
    filters: SearchFilters = SearchFilters()
    quota_units: int = 0

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
