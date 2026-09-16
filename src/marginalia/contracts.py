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
from pathlib import Path

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
    "FILTER_FIELD_QUERY",
    "FILTER_FIELD_RECENCY_WAIVER",
    "NoteSummary",
    "Selection",
    "SELECTION_ALL",
    "SELECTION_SELECTED",
    "SELECTION_FALLBACK",
    "RECENCY_DERIVED_WHY",
    "FilterRejection",
    "MapSnapshot",
    "TerritoryRevision",
    "TerritoryMap",
    # planning
    "RECOMMEND_CONTINUE",
    "RECOMMEND_STOP",
    "REVISE_ADD",
    "REVISE_REMOVE",
    "REVISE_CONCEPT",
    "REVISE_VOCABULARY",
    "JudgedVideo",
    "CoverageItem",
    "LibraryCoverage",
    "RoundPlan",
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
    "STORED_THIS_RUN",
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
    "STOP_REFUSED",
    "RECOVERED",
    "STILL_FAILING",
    "NO_CAPTIONS",
    "GONE",
    "STOP_THROTTLED",
    "RefetchResult",
    "RefetchRun",
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
    # run exports
    "ExportedRun",
    # doctor: one check, one counter
    "CHECK_UNTRACKED_DIR",
    "CHECK_TRANSCRIPT_MISSING",
    "CHECK_NOTE_MISSING",
    "CHECK_NOTE_UNRECORDED",
    "CHECK_NOTE_INVALID",
    "CHECK_TRANSCRIPT_MODIFIED",
    "CHECK_RUN_UNFINISHED",
    "DOCTOR_CHECKS",
    "DOCTOR_PROBLEMS",
    "DoctorFinding",
    "DoctorReport",
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

    def with_(self, name: str, value: str) -> SearchFilters:
        """The same filters with one field set. How a derived filter is applied.

        The mirror of :meth:`without`, and used in exactly one place: the recency
        window the map reasoned about, turned into a date by the code rather than
        by the model. This one *narrows*, which is why it exists only for the one
        filter the validator has already decided is justified.
        """
        attribute = _FILTER_ATTRIBUTES.get(name)
        if attribute is None:
            return self
        return SearchFilters(
            **{
                current.name: (value if current.name == attribute else getattr(self, current.name))
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
    # Every window the call returned this run, in the order sampled, including
    # the one that won. The judgement is sampled three times and the widest is
    # taken — see `widest_recency` — so a stored map has to say whether its
    # window was a consensus or a coin flip. Rule 23: the conclusion is the
    # half you cannot correct from. Empty on a map built from one sample.
    samples: tuple[int | None, ...] = ()

    @property
    def limited(self) -> bool:
        return self.months is not None

    @property
    def agreed(self) -> bool:
        """Did every sample return the same window? False when they disagreed."""
        return len(set(self.samples)) <= 1

    @property
    def samples_label(self) -> str:
        """``24/18/12 months`` — the samples as a reader would say them."""
        return "/".join("any time" if m is None else str(m) for m in self.samples)

    @property
    def label(self) -> str:
        return f"last {self.months} months" if self.months else "any time"


# The `field` a duplicate-query rejection carries. A sentinel, so the producer
# in `brain/territory.py` and every consumer that counts agree on one string
# rather than each spelling it out — rule 10 applied to a two-outcome type.
FILTER_FIELD_QUERY = "query"

# The `field` a recency *waiver* carries. A third outcome in `FilterRejection`,
# and it stays tellable apart from the other two for rule 14's reason: a refused
# filter means a search ran wider than the model asked, a skipped duplicate
# means a search did not run at all, and a waiver means a search ran wider than
# the *code* would have made it — the model asking, for one query, that the
# reasoned date window not be applied. Summed into the refusals it would read as
# the validator refusing something, which is the opposite of what happened.
FILTER_FIELD_RECENCY_WAIVER = "recency-waiver"

# The `why` a derived `publishedAfter` carries into `filters_json`. A constant
# rather than a phrase spelled out at both ends, because reading the ledger back
# turns on it: whether a cutoff was the model's proposal or the window's
# arithmetic is the difference between a judgement and a calculation.
RECENCY_DERIVED_WHY = "derived from the reasoned recency window"


@dataclass(frozen=True)
class FilterRejection:
    """A filter the code refused to execute. Counted, logged, never silent.

    Same discipline as the unparsed counters: something the model proposed was
    dropped, and a drop nobody counts is indistinguishable from a proposal that
    was never made.

    Three different outcomes travel in this one type, and they must stay
    tellable apart. A refused *filter* means the search ran wider than the model
    asked. A skipped *duplicate query* means a search did not run at all because
    an identical one already had. A *recency waiver* means the model asked, on
    one query, that the reasoned date window not be applied — a search wider
    than the code would otherwise have made it. Reporting any two of them as one
    number is rule 14's failure, and it has already happened once: a round
    announced "8 filters rejected" when all eight were duplicate-query skips and
    no filter had been refused.
    """

    query: str
    field: str
    value: str
    reason: str

    @property
    def is_duplicate_query(self) -> bool:
        return self.field == FILTER_FIELD_QUERY

    @property
    def is_recency_waiver(self) -> bool:
        return self.field == FILTER_FIELD_RECENCY_WAIVER


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

    def filters_json(self, applied: SearchFilters | None = None) -> str:
        """``{"order": {"value": "date", "why": "..."}}`` — value and reason together.

        ``applied`` is what the provider actually ran, which is not always what
        was asked for: the overflow provider takes no parameters at all. The
        `queries` row stores what ran, because B5's planner reads that history
        to decide what to try next and a row claiming an experiment that never
        happened is worse than no row.
        """
        source = self.filters if applied is None else applied
        return json.dumps(
            {
                name: {"value": value, "why": self.reasons.get(name, "")}
                for name, value in source.as_dict().items()
            },
            ensure_ascii=False,
            sort_keys=True,
        )


# What a revision does, and to which half of the map. Shared vocabulary: the
# planner writes these, the code validating them reads them, and the report
# prints them.
REVISE_ADD = "add"
REVISE_REMOVE = "remove"
REVISE_CONCEPT = "concept"
REVISE_VOCABULARY = "vocabulary"


@dataclass(frozen=True)
class TerritoryRevision:
    """One change to the map, and the evidence for it.

    **Additions and removals are not symmetric, and the type carries that.**
    An addition widens the search: a concept the map missed, a tool name that
    turned up in kept videos. It costs one query and shows up in the results.

    A removal *narrows* it, which is the excluding direction, and this project
    treats exclusion as the error nobody can see: a term removed in round two
    is a set of videos that will never be searched for again, and nothing later
    can notice. So a removal has to name the specific queries that carried the
    term and what came back — ``evidence`` is required for one and optional for
    the other, and the code enforces that rather than the prompt asking nicely.
    """

    action: str  # add | remove
    kind: str  # concept | vocabulary
    term: str
    reason: str
    # For a removal: which queries carried the term, and what they returned.
    evidence: str = ""

    @property
    def removes(self) -> bool:
        return self.action == REVISE_REMOVE

    def line(self) -> str:
        """One printable line, for the report and the log."""
        sign = "+" if self.action == REVISE_ADD else "-"
        return f"{sign} {self.kind}: {self.term} — {self.reason}"


@dataclass(frozen=True)
class MapSnapshot:
    """A map revision's term lists, kept after the revision that replaced it.

    Stored so that "both versions stay on the run" is literally true rather
    than reconstructible-in-principle. A revision you cannot see the before of
    is a change you cannot judge — which is the same argument that put the map
    on the run in the first place.
    """

    revision: int
    concepts: list[str]
    vocabulary: list[str]


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
    # B5. The map may be revised between rounds — a wrong map inherited by every
    # round is worse than no map. `revision` counts from 1, `revisions` holds
    # every change applied to reach this one, and `history` keeps the term lists
    # each earlier revision had.
    revision: int = 1
    revisions: list[TerritoryRevision] = field(default_factory=list)
    history: list[MapSnapshot] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.concepts or self.vocabulary or self.queries)

    @property
    def label(self) -> str:
        """``1/v2`` — the prompt that wrote it, and how many times it changed."""
        return f"{self.version}/v{self.revision}"

    def snapshot(self) -> MapSnapshot:
        return MapSnapshot(
            revision=self.revision,
            concepts=list(self.concepts),
            vocabulary=list(self.vocabulary),
        )

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
                "recency": {
                    "months": self.recency.months,
                    "reason": self.recency.reason,
                    "samples": list(self.recency.samples),
                },
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
                # B5, and additive on purpose: a reader written against the
                # shape B4 stored still finds every key it knew about. An
                # unrevised map carries `revision: 1` and two empty lists,
                # which is the honest description of a map nothing changed.
                "revision": self.revision,
                "revisions": [
                    {
                        "action": revision.action,
                        "kind": revision.kind,
                        "term": revision.term,
                        "reason": revision.reason,
                        "evidence": revision.evidence,
                    }
                    for revision in self.revisions
                ],
                "history": [
                    {
                        "revision": snapshot.revision,
                        "concepts": snapshot.concepts,
                        "vocabulary": snapshot.vocabulary,
                    }
                    for snapshot in self.history
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


# -- planning -----------------------------------------------------------------

# What the planner recommends the loop do next. Two words, because a third
# would be a hedge and the loop has to act on one of them.
RECOMMEND_CONTINUE = "continue"
RECOMMEND_STOP = "stop"


@dataclass(frozen=True)
class JudgedVideo:
    """One video the round decided about, as the planner is shown it.

    Deliberately thin: an id, a title, which stage decided, and the reason the
    judge gave. **No transcript, no description, no note** — the planner is
    reasoning about coverage, not re-judging, and a structure that could carry
    transcript text is one that eventually will.

    ``reason`` is the load-bearing field and also the dangerous one. It is the
    judge's account of its own decision, which makes it evidence about the
    judge and not about YouTube. The prompt says so in as many words, because
    the failure it prevents has happened here: when the judge had a duration
    bias, 12 of 25 rejections cited runtime, and a planner reading that pattern
    would have concluded that short videos were not worth searching for.
    """

    video_id: str
    title: str
    stage: str  # metadata | full
    verdict: str
    reason: str
    # How good the evidence behind that verdict was. `CLEAR` for anything stage
    # 1 decided — it never claims to have read a transcript — and whatever
    # stage 2 graded otherwise.
    #
    # This field is what stops a `DEGRADED` keeper counting toward coverage. A
    # degraded video was kept and stored, and the one thing known about it is
    # that nobody could read it: it gets no note, so `/ask` will never answer
    # from it. A planner shown it in the ordinary KEPT list would conclude a
    # part of the brief was satisfied by evidence that does not exist, and stop
    # searching for the video that would actually have satisfied it.
    evidence: str = EVIDENCE_CLEAR
    # Stored, readable, and past the note worker's ceiling — so it will never be
    # noted, and `/ask` can never answer from it. Different cause from
    # `unreadable`, identical consequence, and it needs its own flag for exactly
    # the reason rule 14 gives: a video nobody could read and a video nobody
    # will summarise are two findings, and one field holding both would report a
    # ceiling as a caption failure.
    too_long: bool = False

    @property
    def unreadable(self) -> bool:
        return self.evidence == EVIDENCE_DEGRADED

    @property
    def answerable(self) -> bool:
        """Will a note ever exist for this video?

        The question coverage actually turns on. A kept video that produces no
        note satisfies no part of the brief however good it looked.
        """
        return not self.unreadable and not self.too_long


@dataclass(frozen=True)
class CoverageItem:
    """One part of the brief, and whether anything found this round satisfies it.

    ``satisfied`` is a claim the planner makes and the report prints. ``detail``
    is what makes it checkable — for a satisfied item, which videos cover it;
    for a gap, what is missing. "More depth needed" is not a gap; "nothing
    found on cache expiry and TTL" is.
    """

    part: str
    satisfied: bool
    detail: str = ""


@dataclass(frozen=True)
class LibraryCoverage:
    """What the library already satisfied before this run searched for anything.

    The planner is otherwise blind to the library. Gate 1 drops videos that have
    notes before they can become candidates, and the fetch-step disk check skips
    stored-but-unnoted ones — so neither reaches ``RoundReport.kept``, and the
    planner's *"no kept video covers X"* means *"no video this run newly fetched
    covers X"*. Meanwhile `/ask` reads every note. Measured: a run declared
    retrieval-scoring a gap while `lgCzmVkk1W4`, noted the previous day, gave the
    same three-signal recipe the gap asked for.

    Computed once, before round one, from the notes on disk. An empty library
    yields an empty assessment, which is the honest answer for a fresh topic and
    costs nothing because the call is skipped.
    """

    items: list[CoverageItem] = field(default_factory=list)
    notes_read: int = 0
    parsed: bool = True
    problem: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0

    @property
    def satisfied(self) -> list[CoverageItem]:
        return [item for item in self.items if item.satisfied]

    def as_dict(self) -> dict:
        return {
            "notes_read": self.notes_read,
            "items": [
                {"part": item.part, "satisfied": item.satisfied, "detail": item.detail}
                for item in self.items
            ],
            "parsed": self.parsed,
            "problem": self.problem,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.latency_s, 2),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class RoundPlan:
    """What the planner concluded at the end of one round.

    The loop reads three things from this: the queries to run next, the map
    revisions to apply, and whether to keep going. Everything else is for the
    report and the ledger — but "everything else" is where the coverage
    assessment lives, and a coverage claim nobody can read is a claim nobody
    can check.

    ``recommendation`` is advisory. The planner is not the sole authority on
    stopping: every budget ceiling still applies, and a planner declaring
    satisfaction in round one is a real failure mode rather than a hypothetical
    one. What it *does* get is its own stop reason, so `coverage_satisfied` is
    never confused with running out of money.
    """

    round_number: int
    coverage: list[CoverageItem] = field(default_factory=list)
    queries: list[PlannedQuery] = field(default_factory=list)
    revisions: list[TerritoryRevision] = field(default_factory=list)
    recommendation: str = RECOMMEND_CONTINUE
    reason: str = ""
    # Revisions and filters the code refused. Counted for the same reason every
    # other refusal here is: a drop nobody counts is indistinguishable from a
    # proposal that was never made.
    rejected_filters: list[FilterRejection] = field(default_factory=list)
    refused_revisions: list[tuple[TerritoryRevision, str]] = field(default_factory=list)
    parsed: bool = True
    problem: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0

    @property
    def gaps(self) -> list[CoverageItem]:
        return [item for item in self.coverage if not item.satisfied]

    @property
    def satisfied(self) -> list[CoverageItem]:
        return [item for item in self.coverage if item.satisfied]

    @property
    def wants_to_stop(self) -> bool:
        return self.recommendation == RECOMMEND_STOP

    def as_dict(self) -> dict:
        """What `--json` carries and what the report reads."""
        return {
            "round": self.round_number,
            "recommendation": self.recommendation,
            "reason": self.reason,
            "coverage": [
                {"part": item.part, "satisfied": item.satisfied, "detail": item.detail}
                for item in self.coverage
            ],
            "queries": [
                {
                    "text": query.text,
                    "origin": query.origin,
                    "origin_detail": query.origin_detail,
                    "filters": query.filters.as_dict(),
                }
                for query in self.queries
            ],
            "revisions": [
                {
                    "action": revision.action,
                    "kind": revision.kind,
                    "term": revision.term,
                    "reason": revision.reason,
                    "evidence": revision.evidence,
                }
                for revision in self.revisions
            ],
            "refused_revisions": [
                {"term": revision.term, "action": revision.action, "why": why}
                for revision, why in self.refused_revisions
            ],
            "rejected_filters": [
                {
                    "query": rejection.query,
                    "field": rejection.field,
                    "value": rejection.value,
                    "reason": rejection.reason,
                }
                for rejection in self.rejected_filters
            ],
            "parsed": self.parsed,
            "problem": self.problem,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.latency_s, 2),
        }


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
    # The uploader's stable YouTube ID, as opposed to `channel`, which is the
    # display name and can be changed at any time. Nothing reads it yet; it is
    # carried because it arrives free with the fetch, and backfilling it later
    # would mean one network call per video already in the library.
    channel_id: str = ""


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
    # The reason made a claim about an API being out of date without carrying
    # the `[outside knowledge: ...]` marker the prompt asks for. **Not** a parse
    # failure and not a reason to change the verdict: the five lines were well
    # formed and the judgement may be perfectly right. What it fails is
    # auditability — era is the one axis a transcript cannot speak on, so an
    # unmarked currency claim is outside knowledge that cannot be counted as
    # such, and counting it is the entire reason the marker exists.
    unmarked_currency: bool = False

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
    # When the note on disk *now* was written. Regeneration moves it, which is
    # what makes it useful for "how old is this note?" and useless for "when
    # did this video become answerable?".
    noted_at: str | None = None
    # The second question, and the reason both exist. Set on the first note a
    # video ever gets and never rewritten, so a drain cannot erase the
    # distinction between new coverage and a regenerated note. NULL on every
    # row written before the column, and deliberately not backfilled from
    # `noted_at`: for a regenerated note that value is the regeneration date,
    # and a first-note date invented from it would be the fabricated
    # provenance the column exists to prevent.
    first_noted_at: str | None = None
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
    # Gate 1, run-scoped: stored by an earlier round of *this* run, so it has
    # no note yet and `in_library` cannot see it. Its own counter rather than
    # part of `known`, because it measures something only this run can fix —
    # every one of these used to be a candidate judged for the second time.
    known_this_run: int = 0
    # Gate 2: carries a NO for this exact topic from an earlier run.
    rejected_before: int = 0
    verdict_no: int = 0
    verdict_maybe: int = 0
    verdict_yes: int = 0
    # Stage 2 rejected a candidate stage 1 had called MAYBE — the routine flip,
    # and what stage 2 was built to do.
    flipped_to_no: int = 0
    # Stage 2 rejected a candidate stage 1 had called **YES**. Kept apart from
    # `flipped_to_no` deliberately: a confident metadata verdict overturned by
    # the transcript is the number that says whether stage 1 can be trusted on
    # its own, and summing it into the routine flips would hide exactly that.
    overturned: int = 0
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
    # Kept, and past the note ceiling, so it will never be noted. Counted apart
    # from `degraded`: the transcript was fine and the length was not.
    too_long_to_note: int = 0
    # Kept, but stage 2 could not read the transcript well enough to be sure.
    # Not a rejection and not a clean keep — and it suppresses the note, which
    # is a consequence nobody should have to infer from a silent counter.
    degraded: int = 0
    # Stage-2 replies that broke the five-line contract. Counted rather than
    # swallowed, the way citation parsing counts what it drops.
    unparsed: int = 0
    # Stage-2 reasons that judged an API out of date without marking the claim
    # as outside knowledge. Its own counter, not part of `unparsed`: those
    # replies were well formed, and folding the two together would report a
    # convention nobody followed as a format nobody could parse. Measured at 3
    # of 4 currency claims unmarked on the second `langchain` run, which is what
    # a rule living only in a prompt is worth.
    unmarked_currency: int = 0
    # B4. Filters the validator refused before any search ran. Its own counter,
    # like everything else here: a rejected filter is neither a failure nor a
    # rejection nor a parse problem, and a round that reported it as any of
    # those would be reporting three causes in one number.
    #
    # **Filters only.** A duplicate query the validator skipped travels in the
    # same list and is counted below instead. They were one number until a round
    # reported "8 filters rejected" where every one was a duplicate skip and no
    # filter had been refused — rule 14's failure, third occurrence, in the
    # counter the planner reads.
    filters_rejected: int = 0
    # Queries dropped because an identical text-and-filters pair had already run
    # this round. A search that did not happen because it would have repeated
    # one — not a filter refusal, and the opposite direction: a refused filter
    # widens what a search sees, a skipped duplicate removes a search entirely.
    duplicates_skipped: int = 0
    # Queries that asked, in the reply, for the reasoned date window not to be
    # applied to them. Neither of the two above: no filter was refused and no
    # search was skipped — one search ran wider than the code would have made
    # it. Its own counter because it is the only way a model can now widen past
    # a derived hard filter, and a widening nobody counts is how a window that
    # silently stopped applying looks exactly like a window that is applying.
    recency_waived: int = 0
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
    # B5. What the round actually decided about, with the judge's own reasons.
    # The counters above say how many; these say which, and the planner cannot
    # reason about coverage from an integer.
    #
    # Kept and rejected are both here, and both are shown to the planner. A
    # planner given only failures only ever learns what to avoid — it would
    # have no way to say "this part of the brief is already covered", which is
    # half of what it is for.
    kept: list[JudgedVideo] = field(default_factory=list)
    rejected: list[JudgedVideo] = field(default_factory=list)
    # The plan this round ended with, if one was made. `None` on the final
    # round, on a dry run, and whenever the budget stopped the loop first —
    # planning a round that cannot happen is a request spent on nothing.
    plan: RoundPlan | None = None

    @property
    def judged_here(self) -> int:
        """Candidates this round actually reached a stage-2 verdict on.

        Every stored video is now one of them. Stage 1 decides whether to
        *fetch*; nothing enters the library without stage 2 having read the
        transcript, so this is `stored` plus both kinds of rejection rather
        than the MAYBE branch alone.
        """
        return self.stored + self.flipped_to_no + self.overturned

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
    # What the library already satisfied before round one. Empty is a real
    # state: a fresh library, or a dry run, or an assessment that failed — and
    # empty means "assume nothing is covered", which is where the planner stood
    # before this existed.
    baseline: LibraryCoverage = field(default_factory=LibraryCoverage)

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
# Stored by *this run*, in an earlier round. Its own word rather than `KNOWN`,
# because it is a different question with a different answer: Gate 1 asks
# whether a video is in the library, and a video stored ten seconds ago is not
# — its note has not been written yet and may never be. Folding the two
# together would make "already in the library" a claim the ledger contradicts.
STORED_THIS_RUN = "stored_this_run"


@dataclass(frozen=True)
class Candidate:
    result: SearchResult
    verdict: str  # new | known | rejected | stored_this_run
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
    # What the provider actually applied, which is not always what it was given:
    # the overflow provider takes no parameters at all. Recorded separately
    # because the `queries` row stores this one, and a row saying a filter ran
    # when the search was plain is a false record of an experiment.
    filters_applied: SearchFilters = SearchFilters()
    quota_units: int = 0

    def of(self, verdict: str) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.verdict == verdict]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "found": len(self.candidates),
            KNOWN: len(self.of(KNOWN)),
            REJECTED: len(self.of(REJECTED)),
            STORED_THIS_RUN: len(self.of(STORED_THIS_RUN)),
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
# The provider refused in a way no retry addresses — no balance, a rejected key.
# Its own reason rather than `repeated_failures`, which is what it looked like
# the first time: three videos failed in a row and the run reported a streak,
# when what had happened was one refusal repeated. A streak says the work is
# going wrong; this says the work cannot start. Nothing is written about the
# video, because a billing error is not evidence about a transcript.
STOP_REFUSED = "provider_refused"


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


# -- refetch -----------------------------------------------------------------
#
# What a retry of a transient failure did. `RECOVERED` is the point; the other
# three are what the run has to be able to tell apart, because "it failed again"
# and "it turns out to have no captions" and "it is gone" are three different
# findings and only one of them means try again tomorrow.
RECOVERED = "recovered"
STILL_FAILING = "still_failing"
NO_CAPTIONS = "no_captions"
GONE = "gone"

# Why a refetch run stopped. `throttled` is its own reason and not a failure
# streak: YouTube refusing a burst is the provider being authoritative, which is
# rule 24 one layer down from the model provider, and a run that keeps asking
# through it is how the 108-video backlog was created in the first place.
STOP_THROTTLED = "throttled"


@dataclass(frozen=True)
class RefetchResult:
    video_id: str
    outcome: str
    detail: str = ""
    # How many times this video has now been tried, ever. Reported, never acted
    # on: no threshold flips `transient` to permanent, because a video written
    # off is rule 8's invisible error with extra steps. Some of these are
    # genuinely gone and some are unlucky, and the count cannot tell them apart
    # — a person reading it can.
    attempts: int = 0
    wanted: bool = False


@dataclass
class RefetchRun:
    results: list[RefetchResult] = field(default_factory=list)
    considered: int = 0
    dry_run: bool = False
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
    # Citations whose video ID was mis-cased by the model and resolved against
    # the library's own spelling. Counted rather than silently fixed, for the
    # reason `unparsed` exists: a repair is a thing that happened to an answer,
    # and a repair nobody counts is how you stop noticing the model needs one.
    repaired: list[str] = field(default_factory=list)

    @property
    def shaped(self) -> int:
        return len(self.citations) + len(self.unparsed)


@dataclass(frozen=True)
class LoadedNote:
    video_id: str
    title: str
    text: str


@dataclass(frozen=True)
class NoteSummary:
    """One line about one note, for the selector to choose from.

    **Built from a note and never shown to a human.** Hard rule 1 does not stop
    at whole notes: `gist` is note text, so these lines go to the model and to
    nowhere else — not stdout, not `logs/`, not an answer file. The counts
    derived from them are what gets reported.

    Deliberately thin. The selector's job is to say *which notes to read*, and
    it does that from a title, a date and a sentence — 309 of these are about
    23k tokens against the 875k the notes themselves cost.
    """

    video_id: str
    title: str
    published: str
    gist: str

    @property
    def line(self) -> str:
        published = self.published or "undated"
        return f"{self.video_id} | {published} | {self.title} | {self.gist}"


# What the selector decided, and why. `reason` exists for rule 23: the queries
# are the conclusion and the reasoning is the half you cannot correct from, and
# "did it not know, or did it not look?" is the first question asked of a thin
# answer.
SELECTION_ALL = "all"
SELECTION_SELECTED = "selected"
SELECTION_FALLBACK = "fallback"


@dataclass(frozen=True)
class Selection:
    """Which notes to load, how that was decided, and what it left out.

    **`mode` is the field that must never be silently wrong.** `all` means the
    library fit and every note was loaded; `selected` means the model chose;
    `fallback` means the selector failed and the budget was filled anyway. The
    third is not an error state to hide — rule 8 says an unusable signal
    resolves toward including, and a fallback that looked like a selection
    would be a narrow answer wearing a confident one's clothes.
    """

    video_ids: list[str] = field(default_factory=list)
    mode: str = SELECTION_ALL
    reason: str = ""
    considered: int = 0
    # Notes the model asked for that were dropped because the budget ran out.
    # Counted, never silent: this is the one place selection can lose something
    # the model actually wanted.
    overflowed: int = 0
    # How many drops the reply named, against how many the quota allowed, and
    # how many the code therefore refused. **Three fields rather than one**,
    # because they were briefly sharing `overflowed` and that hid the number
    # that matters: a reply naming 409 drops against a quota of 159 is a model
    # thresholding rather than ranking, and it reads identically to a reply
    # naming 160 unless the request is recorded next to the allowance.
    requested_drops: int = 0
    allowed_drops: int = 0
    refused_drops: int = 0
    parsed: bool = True
    problem: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def selected(self) -> bool:
        return self.mode == SELECTION_SELECTED


@dataclass(frozen=True)
class Opened:
    """A transcript read for one question, and why it was read."""

    video_id: str
    why: str
    text: str


@dataclass(frozen=True)
class UnverifiedQuote:
    """A quoted span the note it cites does not contain, and how badly.

    **Per claim rather than per answer.** A counter saying "5 unverified" tells
    a reader to distrust the whole answer; naming which five tells them to
    distrust five sentences and read the rest normally.

    `loose` separates the two measured failures, which one marker would
    conflate. Of 42 sentence-length cases, 5 were quotes drawn from the cited
    note's ``## Verbatim`` section and rendered imprecisely — substantively
    right, loosely copied — while 24 had no source anywhere in the note. Marking
    those identically overstates the first group by a lot.

    **`loose` is a statement about closeness, never about content.** It says a
    near passage exists; it does not carry it, name it, or locate it. Hard rule
    1 draws the line at what the note held, and closeness is on the safe side of
    it — the same line the availability measurement was run under.
    """

    text: str
    # The ids cited near the quote, joined — the check passed if *any* of them
    # held the string, so naming one would misreport what was tested.
    video_ids: str
    loose: bool = False

    def render(self) -> str:
        return f"{self.video_ids}: {self.text}"


@dataclass
class Answer:
    question: str
    text: str = ""
    version: str = ""
    model: str = ""
    notes_loaded: int = 0
    # M7. Notes the library held, against `notes_loaded`, which is how many the
    # answering call actually read. Equal until selection runs; the gap is the
    # number that says an answer was written from part of the library.
    notes_considered: int = 0
    # How that gap came about, including the selector's own account of it.
    # Rule 23: without it, "did it not know, or did it not look?" — the first
    # question asked of a thin answer — is unanswerable from disk.
    selection: Selection = field(default_factory=Selection)
    # What was about to be sent, estimated before sending it. Reported on every
    # answer, because the whole-library approach is only safe while it fits, and
    # an approach that is visible every time is one nobody has to remember to
    # check. `headroom` is None when no ceiling is configured — the honest
    # answer, since a context limit is a provider's number and this project
    # does not guess one.
    estimated_input_tokens: int = 0
    headroom: float | None = None
    opened: list[tuple[str, str]] = field(default_factory=list)  # video_id, why
    citations: list[AnswerCitation] = field(default_factory=list)
    # Citation-shaped strings the parser could not read. Reported rather than
    # discarded: a parser that drops most of its input must say so.
    unparsed: list[str] = field(default_factory=list)
    # Citations that resolved only after their video ID's casing was repaired
    # against the library. Surfaced for the same reason as `unparsed`: this is
    # the model getting an ID wrong, and a repair nobody counts is a defect
    # nobody sees.
    repaired: list[str] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    # video_id -> a short digest of the note text this answer actually read.
    #
    # A note is overwritten in place, so "which notes did this answer read?"
    # decays into "which notes exist now?" the moment one is regenerated.
    # Measured: 43 of 55 stored answers cannot be re-scored, and for the August
    # ones **646 of 646 notes on disk were written after the answer was**. The
    # ids alone did not save them, because the text behind an id changed.
    #
    # A digest makes a later re-score able to say "all 252 unchanged, this score
    # is valid" or "14 rewritten, treat it with suspicion" instead of scoring
    # against text that did not exist at the time and reporting a number.
    # Cheap now and impossible retroactively, which is the whole argument.
    #
    # **A digest, not the text.** Hard rule 1 does not stop at the answer file.
    note_digests: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    truncated: bool = False
    # What the answer asserts that its own sources do not support. Three
    # outcomes, three lists, per rule 14: an id naming no video, a quote
    # attributed to a video whose note does not contain it, and a quote
    # attributed to nothing are three failures with three fixes.
    #
    # **Reported, never used to suppress.** The answer is written whether or
    # not a quote verifies — same discipline as `unparsed` and
    # `unmarked_currency`. These will carry false positives, because a
    # paraphrase inside quote marks flags, and that is the price of a check
    # simple enough to be trustworthy.
    # `unverified_quotes` was split in two rather than narrowed: a sentence
    # attributed to a video whose note lacks it, and a phrase in the same
    # position, are different claims and one number over-reported fabrication
    # by roughly 3x. Never summed. See `ask._SPEECH_CHARS` for the boundary.
    unresolved_ids: list[str] = field(default_factory=list)
    unverified_speech: list[UnverifiedQuote] = field(default_factory=list)
    unverified_phrases: list[UnverifiedQuote] = field(default_factory=list)
    # Quotes the cited note contains **outside** its `## Verbatim` section: the
    # note-writer's words presented as a speaker's. Its own outcome rather than
    # part of `unverified`, because a legitimate quote can sit inline in
    # `## Claims` and a Verbatim-only test would call that a failure. Until this
    # existed these verified silently, so "verified" meant "somewhere in eleven
    # thousand characters of note", most of which nobody said.
    unspoken_quotes: list[UnverifiedQuote] = field(default_factory=list)
    uncited_quotes: list[str] = field(default_factory=list)

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


# -- run exports -------------------------------------------------------------


@dataclass(frozen=True)
class ExportedRun:
    """What ``marg run export`` wrote, and how much of the run it found.

    The counts are here so the command can report what it rendered rather than
    only where it put it. A run that exports with zero queries and zero
    decisions is a real and readable answer — it usually means the run died
    before it judged anything — and an export that said only "written to
    exports/abc123" would hide it behind a success message.
    """

    run_id: str
    directory: Path
    files: list[Path]
    rounds: int
    queries: int
    decisions: int


# -- doctor -----------------------------------------------------------------
#
# `marg doctor` compares the library on disk against the ledger. Rule 2 cannot
# be enforced from inside a program — only the OS could stop another process
# writing into the folder, and it does not — so what the rule gets instead is
# detection after the fact. These are the seven disagreements it looks for, and
# every one is its own counter (rule 14): a directory nobody recorded and a
# note nobody recorded are both "something wrote without the CLI", and summing
# them would hide which surface it came through.

CHECK_UNTRACKED_DIR = "untracked_dir"  # videos/<id>/ exists, no `videos` row
CHECK_TRANSCRIPT_MISSING = "transcript_missing"  # status claims one, disk has none
CHECK_NOTE_MISSING = "note_missing"  # noted_at set, note.md gone
CHECK_NOTE_UNRECORDED = "note_unrecorded"  # note.md present, noted_at NULL
CHECK_NOTE_INVALID = "note_invalid"  # stored note fails validate_note
CHECK_TRANSCRIPT_MODIFIED = "transcript_modified"  # content_hash disagrees: rule 3
CHECK_RUN_UNFINISHED = "run_unfinished"  # a run with no finished_at: rule 6's signal

DOCTOR_CHECKS: tuple[str, ...] = (
    CHECK_UNTRACKED_DIR,
    CHECK_TRANSCRIPT_MISSING,
    CHECK_NOTE_MISSING,
    CHECK_NOTE_UNRECORDED,
    CHECK_NOTE_INVALID,
    CHECK_TRANSCRIPT_MODIFIED,
    CHECK_RUN_UNFINISHED,
)

# The checks that make the library *wrong*. An unfinished run is deliberately
# not among them: it is the signal rule 6 writes the ledger row early in order
# to leave, and a run that is still going looks exactly the same. Reported,
# never an error, never a reason for a non-zero exit.
DOCTOR_PROBLEMS: frozenset[str] = frozenset(DOCTOR_CHECKS) - {CHECK_RUN_UNFINISHED}


@dataclass(frozen=True)
class DoctorFinding:
    """One disagreement between disk and ledger.

    ``subject`` is a video id or a run id. ``detail`` is a short label — a
    status, a section name, a byte count — and never content: rule 1 applies to
    a diagnostic exactly as it applies to an answer, and a doctor that printed
    "the offending section" would be the leak the rule exists to stop.
    """

    check: str
    subject: str
    detail: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Everything ``marg doctor`` found, and how much it looked at.

    ``hashes_unrecorded`` is its own number rather than folded into
    ``hashes_verified``: a row with no ``content_hash`` to compare against is
    neither verified nor a mismatch, and reporting it as either would be a
    counter measuring something narrower than its name.
    """

    findings: tuple[DoctorFinding, ...]
    videos_checked: int
    dirs_checked: int
    runs_checked: int
    hashes_verified: int
    hashes_unrecorded: int

    def count(self, check: str) -> int:
        return sum(1 for finding in self.findings if finding.check == check)

    @property
    def problems(self) -> int:
        return sum(1 for finding in self.findings if finding.check in DOCTOR_PROBLEMS)

    @property
    def clean(self) -> bool:
        return self.problems == 0
