"""Working out what a subject actually covers, before anything is searched.

The problem, stated once. Search "AI agent memory" and you get videos with
those words in the title. The video called *"Building ReAct loops with tool
calling"* is exactly what was wanted and never says "agent". No amount of
careful judging finds a video the search never returned — the judge only ever
sees what came back, so a gap here is invisible everywhere downstream.

So before searching, the model works out the subtopics, the alternative names,
the practitioner jargon, the tools and the adjacent concepts, and the queries
are written from **that** rather than from the words the user typed.

Two halves, and they are deliberately different kinds of thing:

* :func:`parse_map` reads the model's reply into a :class:`TerritoryMap`. It
  trusts nothing and repairs where it can, the same way `judge.parse_full` does.
* :func:`validate` decides which of the proposed filters may actually execute.
  **The model proposes; the code checks.** A filter is a claim about what may be
  excluded, and two of the four exclude silently and permanently.

Same rules as everything in `brain/`: evidence in, decision out, no I/O beyond
`llm`.

**Rejection widens, never narrows.** Every failure in this module resolves to
*run the query with fewer filters* — never to drop the query, and never to add a
filter nobody asked for. That is the search-layer form of the asymmetry the
judge follows: keeping something useless costs one download and is visible;
excluding something good costs a video nobody will ever know existed. A hard
filter applied by mistake is exactly that invisible loss, with the additional
property that it happens before anything is logged about the video at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from ..contracts import (
    FILTER_FIELD_RECENCY_WAIVER,
    FILTER_FIELDS,
    FILTER_ORDER,
    FILTER_PUBLISHED_AFTER,
    FILTER_RELEVANCE_LANGUAGE,
    FILTER_VIDEO_DURATION,
    HARD_FILTERS,
    ORDER_VALUES,
    ORIGIN_TERRITORY,
    RECENCY_DERIVED_WHY,
    VIDEO_DURATION_VALUES,
    Brief,
    FilterRejection,
    PlannedQuery,
    Recency,
    SearchFilters,
    TerritoryMap,
    Usage,
)
from ..llm import LlmClient, Prompt, load_prompt

log = logging.getLogger(__name__)

__all__ = [
    "MAP_PROMPT",
    "Mapper",
    "parse_map",
    "validate",
    "user_constraints",
    "UserConstraints",
    # Shared with `planner.py`, which parses the same filter syntax out of a
    # different reply. Public rather than duplicated: two parsers for one format
    # is how the two drift apart, and a filter read one way here and another way
    # there would make the validator's guarantees provable in only one of them.
    "parse_filters",
    "tidy",
]

# In a subdirectory for the same reason `judge/full` is: mapping is going to
# grow more prompts (B5's planner revises a map), and a flat `prompts/` where
# `map.md` and `write_note.md` sit side by side stops saying which belong to
# the same decision.
MAP_PROMPT = "territory/map"

_LABEL_RE = re.compile(
    r"^\s*\**\s*(CONCEPTS|VOCABULARY|RECENCY|QUERY|FROM|FILTERS|VARIANT[- ]OF)"
    r"\s*\**\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)

# `order=date (the ecosystem turns over)` — value and reason together, because
# a filter you cannot inspect is one you cannot correct.
_FILTER_RE = re.compile(r"^\s*([A-Za-z]+)\s*=\s*([^(]*?)\s*(?:\((.*)\))?\s*$")

# `12 months — the ecosystem turns over`, `ANY — fundamentals do not move`,
# `2 years — …`, `6-12 months — …`. The number, an optional second number making
# a range, and then whatever follows: the unit is read from that remainder.
_RECENCY_RE = re.compile(
    r"^\s*(?:(ANY)\b|(\d+)(?:\s*(?:-|–|—|to)\s*(\d+))?)\s*(.*)$",
    re.IGNORECASE,
)

# The unit after the number. Months and years are read; any other time unit is
# a window this parser cannot convert, and is refused rather than read as months
# — "2 weeks" taken as 2 months would be a guess, and "1 decade" as 1 month a
# narrowing. A word that is not a time unit is the start of the reason, and the
# number is months, as the prompt asks for.
_MONTH_UNIT_RE = re.compile(r"(?:months?|mos?|m)\b\.?", re.IGNORECASE)
_YEAR_UNIT_RE = re.compile(r"(?:years?|yrs?)\b\.?", re.IGNORECASE)
_OTHER_UNIT_RE = re.compile(
    r"(?:days?|weeks?|wks?|decades?|centur(?:y|ies)|hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"^\s*[-–—:]?\s*")

_ISO_DATE_RE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})")

# ISO 639-1, optionally with a region: `en`, `pt-BR`. A shape check rather than
# an enumeration — any list of codes written here goes stale, and this is the
# one soft parameter, so a wrong value costs ranking rather than content.
_LANGUAGE_RE = re.compile(r"\A[a-z]{2}(?:-[A-Za-z]{2,4})?\Z")

# Ceilings, so one over-enthusiastic reply cannot turn into a hundred searches
# at a hundred quota units each. Truncation is logged: a silent cap reads as
# "covered everything" when it did not.
_MAX_CONCEPTS = 20
_MAX_VOCABULARY = 30
_MAX_QUERIES = 24
_MAX_TERM_WORDS = 6
_MAX_QUERY_WORDS = 12
_MAX_REASON = 200

_MAX_RECENCY_MONTHS = 120


# -- what the user said, which outranks what the model reasoned ---------------


class UserConstraints:
    """Constraints read off the raw topic string, in the user's own words.

    Deliberately tiny, and deliberately one-directional: this can only ever
    *forbid* a hard filter, never require one. A phrase it fails to recognise
    costs nothing — the recency reasoning still applies — while a phrase it
    recognises wrongly costs one unfiltered search. Both errors point at width.

    It fires only on unambiguous phrasing, and the list is short on purpose. The
    case it exists for is real and specific: a user who asked for "the history
    of transformers" and got a `publishedAfter` filter has had the exact thing
    they asked for excluded, silently, by a model reasoning about ecosystems.
    """

    def __init__(self, *, no_date_filter: bool = False, reason: str = "") -> None:
        self.no_date_filter = no_date_filter
        self.reason = reason


# Phrases that say, in the user's own words, that age is not a disqualifier.
_TIMELESS_PHRASES = (
    "all time",
    "all-time",
    "any time",
    "anytime",
    "any age",
    "ever",
    "history of",
    "historical",
    "classic",
    "timeless",
    "foundational",
    "original paper",
    "no matter how old",
    "regardless of age",
    "old and new",
)


def user_constraints(topic: str) -> UserConstraints:
    """Read the explicit constraints out of the topic the user typed."""
    lowered = " ".join(topic.split()).lower()
    for phrase in _TIMELESS_PHRASES:
        # Whole words. As a substring "ever" is inside "every", "however" and
        # "never", and "every agent framework" is not a request for old videos.
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            return UserConstraints(
                no_date_filter=True,
                reason=f"the topic says {phrase!r}, which asks for older material",
            )
    return UserConstraints()


# -- parsing ------------------------------------------------------------------


def parse_map(text: str, *, topic: str, version: str) -> TerritoryMap:
    """Read a mapping reply into a :class:`TerritoryMap`.

    Tolerant in the same places `parse_batch` is: markdown decoration, missing
    sections, blocks in any order, prose the format did not ask for. A reply
    that yields no queries at all is a failure the caller has to handle — there
    is nothing to search — and it is marked ``parsed=False`` rather than
    silently returning an empty map that looks like a subject with no words.
    """
    concepts: list[str] = []
    vocabulary: list[str] = []
    recency = Recency()
    recency_readable = True
    queries: list[PlannedQuery] = []
    problems: list[str] = []
    rejected: list[FilterRejection] = []

    # One query block being assembled: QUERY, then its FROM / FILTERS /
    # VARIANT-OF lines, until the next QUERY.
    pending: dict[str, str] | None = None

    def close() -> None:
        nonlocal pending
        if pending is None:
            return
        filters, reasons, bad = parse_filters(pending.get("FILTERS", ""))
        # A filter caught here is the same event as one caught by `validate`,
        # and it is counted the same way. An unsupported field name that only
        # became a note on the map would be a drop nobody could count.
        rejected.extend(
            FilterRejection(pending["QUERY"], field, value, reason) for field, value, reason in bad
        )
        queries.append(
            PlannedQuery(
                text=pending["QUERY"],
                origin=ORIGIN_TERRITORY,
                origin_detail=tidy(pending.get("FROM", ""), limit=_MAX_REASON),
                filters=filters,
                variant_of=tidy(pending.get("VARIANT-OF", ""), limit=_MAX_QUERY_WORDS * 20),
                reasons=reasons,
            )
        )
        pending = None

    for raw in text.splitlines():
        match = _LABEL_RE.match(raw)
        if match is None:
            continue
        label = match.group(1).upper().replace(" ", "-")
        body = match.group(2).strip()

        if label == "QUERY":
            close()
            query = tidy(body, limit=200)
            if not query or len(query.split()) > _MAX_QUERY_WORDS:
                if query:
                    problems.append(f"query too long to be typed by a person: {query!r}")
                continue
            pending = {"QUERY": query}
        elif label in ("FROM", "FILTERS", "VARIANT-OF"):
            if pending is not None:
                pending.setdefault(label, body)
        elif label == "CONCEPTS" and not concepts:
            concepts = _terms(body, _MAX_CONCEPTS)
        elif label == "VOCABULARY" and not vocabulary:
            vocabulary = _terms(body, _MAX_VOCABULARY)
        elif label == "RECENCY" and not recency.reason and recency.months is None:
            recency, recency_problem = _parse_recency(body)
            if recency_problem:
                problems.append(recency_problem)
                recency_readable = False

    close()

    if len(queries) > _MAX_QUERIES:
        # Counted out loud. A cap that trims silently reads, months later, as a
        # model that only ever proposed this many.
        log.warning(
            "territory: %d queries proposed, keeping the first %d",
            len(queries),
            _MAX_QUERIES,
        )
        queries = queries[:_MAX_QUERIES]

    if not concepts and not vocabulary:
        problems.append("no concepts and no vocabulary")
    if not queries:
        problems.append("no queries")

    for rejection in rejected:
        log.warning(
            "territory: rejected filter %s=%r on %r — %s",
            rejection.field,
            rejection.value,
            rejection.query,
            rejection.reason,
        )

    problem = "; ".join(problems)
    if problem:
        log.warning("territory: mapping reply had problems (%s)", problem)
    return TerritoryMap(
        topic=topic,
        version=version,
        concepts=concepts,
        vocabulary=vocabulary,
        recency=recency,
        queries=queries,
        rejected=rejected,
        # A rejected filter is not a broken reply — it is the validator doing
        # its job, and it happens on perfectly good maps. `parsed` means the
        # reply had no problems at all; whether it is *usable* is a separate
        # question, answered by its content (`bool(map)`), so one bad line costs
        # that line rather than the map.
        parsed=bool(queries) and not problems,
        problem=problem,
        recency_readable=recency_readable,
    )


def _parse_recency(body: str) -> tuple[Recency, str]:
    """``12 months — reason``, ``2 years — reason`` or ``ANY — reason``.

    An unreadable recency line becomes *any time*, which is the wider answer.
    Guessing a window from a line nobody could read would be excluding content
    on the strength of a parse failure.

    **The unit is read, not assumed.** This used to take the first number and
    ignore what followed it, so "2 years — the API churned" derived a 2-month
    cutoff onto every main query. Years are twelve months; a range takes its
    wider end, because an unstable exclusion is ambiguity and rule 16 resolves
    that toward keeping; any other time unit is unreadable.
    """
    unreadable = Recency(reason="recency line unreadable; searching any time"), (
        "recency line unreadable"
    )
    match = _RECENCY_RE.match(body.strip())
    if match is None:
        return unreadable
    any_time, first, second, rest = match.groups()
    if any_time:
        return Recency(months=None, reason=tidy(_SEPARATOR_RE.sub("", rest), limit=_MAX_REASON)), ""

    months = max(int(first), int(second or first))
    unit = _YEAR_UNIT_RE.match(rest) or _MONTH_UNIT_RE.match(rest)
    if unit is not None and unit.re is _YEAR_UNIT_RE:
        months *= 12
    elif unit is None and _OTHER_UNIT_RE.match(rest):
        return unreadable
    remainder = rest[unit.end():] if unit is not None else rest
    reason = tidy(_SEPARATOR_RE.sub("", remainder), limit=_MAX_REASON)
    if not 1 <= months <= _MAX_RECENCY_MONTHS:
        return Recency(reason=reason), f"recency of {months} months is not a usable window"
    return Recency(months=months, reason=reason), ""


def parse_filters(
    body: str,
) -> tuple[SearchFilters, dict[str, str], list[tuple[str, str, str]]]:
    """``order=date (why); publishedAfter=2026-02-01 (why)`` -> a structure.

    Reads what the model wrote and nothing more. Whether a *readable* filter may
    execute is :func:`validate`'s question, and keeping the two apart is what
    lets a rejection name the exact value that was proposed.

    The third return value is ``(field, value, reason)`` for everything that
    could not be read as a filter at all — an unsupported field name, or a
    fragment that is not ``name=value``. The caller turns these into the same
    counted :class:`FilterRejection` the validator produces, because "the model
    asked for something this system does not send" is exactly as much a
    rejection as "the value was out of range".
    """
    stripped = body.strip().rstrip(".")
    if not stripped or stripped.upper() in {"NONE", "N/A", "-"}:
        return SearchFilters(), {}, []

    values: dict[str, str] = {}
    reasons: dict[str, str] = {}
    bad: list[tuple[str, str, str]] = []

    for piece in _split_filters(stripped):
        if not piece.strip():
            continue
        match = _FILTER_RE.match(piece.strip().strip("*"))
        if match is None:
            bad.append(("filter", piece.strip(), "could not be read as name=value"))
            continue
        name, value, reason = match.group(1), match.group(2).strip().strip("\"'"), match.group(3)
        canonical = _canonical_field(name)
        if canonical is None:
            # Recorded under the name the model wrote, not a canonical one:
            # the whole point is that this field is not one of ours.
            bad.append((name, value, "not a search parameter this system sends"))
            continue
        values[canonical] = value
        reasons[canonical] = tidy(reason or "", limit=_MAX_REASON)

    return (
        SearchFilters(
            published_after=values.get(FILTER_PUBLISHED_AFTER),
            order=values.get(FILTER_ORDER),
            relevance_language=values.get(FILTER_RELEVANCE_LANGUAGE),
            video_duration=values.get(FILTER_VIDEO_DURATION),
        ),
        reasons,
        bad,
    )


def _split_filters(body: str) -> list[str]:
    """Split on semicolons that separate filters, not on ones inside a reason.

    Found in a real mapping call, not imagined: the model wrote

        order=date (Mem0 changes monthly; latest tutorials reflect the API)

    and a plain ``split(";")`` cut the reason in half, leaving two fragments
    that were neither ``name=value``. Both were counted as rejections and a
    perfectly good `order` filter — one of the two that cannot exclude anything
    — was dropped.

    The failure was safe, which is the design working: rejecting widens, and it
    was logged rather than silent. It was still wrong, and a separator that the
    reason field is free to contain is a parser asking for it.
    """
    pieces: list[str] = []
    current: list[str] = []
    depth = 0
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif character == ";" and depth == 0:
            pieces.append("".join(current))
            current = []
            continue
        current.append(character)
    pieces.append("".join(current))
    return pieces


def _canonical_field(name: str) -> str | None:
    """Match a field name case- and underscore-insensitively, or not at all.

    `published_after` and `publishedafter` are the same field written by a model
    that did not copy the format exactly; `publishedBefore` is a different field
    that this system does not support, and it must not be quietly accepted.
    """
    flattened = name.replace("_", "").replace("-", "").lower()
    for field in FILTER_FIELDS:
        if field.lower() == flattened:
            return field
    return None


def _terms(body: str, limit: int) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    # Brackets separate too, not just commas. A real reply wrote one entry as
    # `vector database (Pinecone, Chroma, Weaviate, FAISS)`, and splitting on
    # commas alone left `vector database (Pinecone` and `FAISS)` — fragments
    # with dangling brackets, shown to the judge as words this subject uses.
    # Treating a bracket as another separator recovers every name in the group
    # *and* the term it qualified, which is what the model meant by writing it.
    for piece in re.split(r"[,;()]", body):
        cleaned = tidy(piece, limit=80).strip("*\"'.")
        if not cleaned or len(cleaned.split()) > _MAX_TERM_WORDS:
            continue
        if cleaned.upper() in {"NONE", "N/A", "-"}:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def tidy(raw: str, *, limit: int) -> str:
    cleaned = " ".join(raw.split()).lstrip("-–—:*").strip().strip("*")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


# -- validation ---------------------------------------------------------------


# How many times the recency judgement is sampled before a window is derived.
# Three, and measured rather than chosen — see `widest_recency`.
RECENCY_SAMPLES = 3


def widest_recency(samples: Sequence[Recency]) -> Recency:
    """The widest of several recency judgements, with all of them recorded.

    **Why the widest rather than the first, the modal, or the mean.** Rule 16
    made this call load-bearing: it derives `publishedAfter` onto every main
    query, so a judgement that moves between samples decides what a run is
    allowed to see. Measured, ten samples per topic with the brief held fixed:

        ai agent architecture   ANY x4, 24m x4, 18m x2     3 distinct
        langchain               24m x4, 18m x3, 12m x3     3 distinct

    and the real `ai agent architecture` run answered 36 months, wider than any
    of the ten. Against the 31 videos that run kept, 24 months would have
    excluded 2, 18 months 4, and 12 months 7 — and among the four is *Building
    Effective Agents with LangGraph*, which the planner named as its evidence
    that a MUST CONTAIN item was covered. A different sample does not merely
    shrink the library; it invalidates a coverage claim the run reports as fact.

    The reasons are individually correct, which is the point. *"The modern
    meaning of 'AI agent' only settled from late 2023"* and *"ReAct and
    tool-use loops are stable patterns taught the same way since 2022"* are
    both true. This is a well-posed question with several defensible answers,
    not a prompt to fix.

    **What makes the widest safe is an asymmetry in the same measurement.** The
    call got the *direction* right 10 of 10 on `langchain` — always a window,
    never `ANY` — and varied only on *how far back*. So widest-of-three keeps
    the reliable half of the judgement and neutralises the unreliable half, and
    it does not collapse to `ANY`: on `langchain` it still returns 24 months.
    The odds of landing on the narrowest observed window fall from 0.2 to 0.008
    on the first topic and 0.3 to 0.027 on the second, for two extra calls.

    This is rule 8 reaching the search layer. An unstable exclusion is
    ambiguity, and ambiguity resolves toward keeping — because a narrow window
    removes foundational material before anything downstream can see that it
    existed.

    `ANY` is the widest answer and wins outright. An empty list is `ANY`, which
    is the same default a run with no map at all gets.
    """
    if not samples:
        return Recency()
    windows = tuple(sample.months for sample in samples)
    if any(month is None for month in windows):
        winner = next(sample for sample in samples if sample.months is None)
    else:
        winner = max(samples, key=lambda sample: sample.months or 0)
    return Recency(months=winner.months, reason=winner.reason, samples=windows)


# The values a query uses to say "not this one" to the derived date window.
# Deliberately a small closed set of the ways a model writes *no limit*, and
# deliberately not a free-text field: a waiver is the one thing here that makes
# a search wider than the code decided, so it has to be unmistakable rather than
# inferred.
_WAIVER_VALUES = frozenset(
    {"any", "any time", "anytime", "all", "all time", "none", "no", "off", "unset"}
)


def _waiver_value(query: PlannedQuery) -> str:
    """The waiver this query asked for, or `""`. Read before anything is judged.

    It has to come first because otherwise the ordinary rules see it: `any` is
    not a date, so a waiver would be refused as a malformed `publishedAfter`,
    counted as a filter rejection, and then have the window derived onto it
    anyway — the exact opposite of what the query asked for, arrived at through
    two correct-looking steps.
    """
    value = (query.filters.published_after or "").strip().lower()
    return value if value in _WAIVER_VALUES else ""


def _derived_why(recency: Recency) -> str:
    """The `why` a derived cutoff carries into `filters_json`."""
    if not recency.samples or recency.agreed:
        return f"{RECENCY_DERIVED_WHY} ({recency.label})"
    return (
        f"{RECENCY_DERIVED_WHY} ({recency.label}; "
        f"{len(recency.samples)} samples returned {recency.samples_label}, widest taken)"
    )


def _derive_published_after(
    query: PlannedQuery,
    *,
    recency: Recency,
    constraints: UserConstraints,
    waiver: str,
    waiver_reason: str,
    today: date,
) -> tuple[PlannedQuery, FilterRejection | None]:
    """Turn the reasoned recency window into a date, and put it on the query.

    **The split this exists to fix.** The map answers two separate questions —
    *does age matter here, and why* (`RECENCY`) and *what filters does each
    query carry* (`FILTERS`) — and nothing joined them. Measured twice on
    `langchain`: a map reasoned *"last 18 months, LangChain's API has churned
    (LLMChain→LCEL→LangGraph)"* and then emitted twelve queries carrying
    `order=date` and no cutoff at all. Zero of 29 queries filtered by date, and
    `rejected_filters` was empty — nothing refused it, it was never proposed.
    The run before that managed 3 of 33.

    So the judgement stays with the model and the arithmetic moves to the code.
    Whether a subject's age matters is a real judgement about an ecosystem;
    *today minus eighteen months* is a calculation, and a calculation asked of a
    model is a calculation that is sometimes not done at all.

    Four things stop it, and each is one of this file's existing rules:

    - recency reasoned as *any time* — nothing justifies excluding anything,
      and `ANY` is the default and the wider answer
    - the user's own words — "the history of transformers" outranks any
      reasoning about ecosystems
    - a per-query waiver — below, and the reason it exists
    - a `variant_of` query — a variant exists to *add* a slice that a main query
      buries, so narrowing it by date takes back the width it was written for

    A `publishedAfter` the model proposed and the validator allowed is left
    exactly as it is. Derivation fills an absence; it never overrules a
    judgement that survived the rules.
    """
    if waiver:
        # Recorded, not silent, and only when it actually changed the outcome:
        # a waiver under `ANY` recency, or under a user who already forbade the
        # filter, prevented nothing and would be a rejection of nothing.
        if not recency.limited or constraints.no_date_filter:
            return query, None
        return query, FilterRejection(
            query.text,
            FILTER_FIELD_RECENCY_WAIVER,
            recency.label,
            # Taken from the query as it was *written*, not as it now stands:
            # stripping the waiver takes its reason with it, and the reason is
            # the only part of a waiver worth reading back.
            waiver_reason or "the query asked for material of any age and gave no reason",
        )
    if not recency.limited or constraints.no_date_filter:
        return query, None
    if query.variant_of or query.filters.published_after:
        return query, None

    cutoff = _months_before(today, recency.months or 0)
    return (
        PlannedQuery(
            text=query.text,
            origin=query.origin,
            origin_detail=query.origin_detail,
            filters=query.filters.with_(FILTER_PUBLISHED_AFTER, cutoff.isoformat()),
            variant_of=query.variant_of,
            reasons={
                **query.reasons,
                # The full window, with no slack. The month of slack in
                # `_filter_problem` is there to accept a date a *model*
                # computed a few days off; arithmetic done here is exact, and
                # the widest date the reasoning allows is the right one.
                #
                # Carrying every sample, not just the winner. The judgement is
                # sampled three times and the widest taken, so "was this window
                # a consensus or a coin flip?" is a question the ledger can
                # answer on any run months later — and the answer changes how
                # much a stored cutoff should be trusted.
                FILTER_PUBLISHED_AFTER: _derived_why(recency),
            },
        ),
        None,
    )


def validate(
    queries: Sequence[PlannedQuery],
    *,
    recency: Recency,
    topic: str = "",
    already_run: Iterable[tuple] = (),
    today: date | None = None,
) -> tuple[list[PlannedQuery], list[FilterRejection]]:
    """Decide which queries and filters may execute. The code checks; the model proposed.

    Every rule below either strips one filter or drops one duplicate query, and
    both outcomes are recorded as a :class:`FilterRejection`. Nothing is ever
    silently passed through, and nothing is ever silently removed.

    The rules, and what each is protecting:

    unsupported field
        A field this system does not send. Passing it through would either be
        ignored by the provider — a filter that looks applied and is not — or
        rejected by it, failing a query for a reason nobody could see.

    malformed or future ``publishedAfter``
        A date the model computed. Models are poor at date arithmetic, and the
        failure is silent: a cutoff in 2027 returns nothing at all, which reads
        exactly like a subject with no videos.

    a value outside the allowed set
        ``order``, ``videoDuration`` and ``relevanceLanguage`` each take a fixed
        vocabulary. An invalid one is a request the provider will refuse.

    ``videoDuration`` on a main query
        The trap of the four. A duration band excludes everything outside it,
        so a main query carrying one silently removes an entire class of video
        — and the class it usually removes is Shorts, which is the duration
        bias the stage-1 prompt spends four paragraphs forbidding, relocated to
        where nobody would look for it. Allowed only on a query that exists to
        *add* a slice: one that names, in ``variant_of``, a main query it
        accompanies.

    a date filter when nothing justified one
        ``publishedAfter`` is only legitimate when the recency call reasoned
        that age matters. A map that concluded *any time* and then filtered by
        date has contradicted its own reasoning, and the filter is the half
        that excludes.

    a constraint the user stated explicitly
        The user outranks the reasoning. "The history of transformers" with a
        twelve-month cutoff excludes precisely what was asked for.

    a duplicate of a query already run
        Identity is text *and* filters, so the same query by `relevance` and by
        `date` are two queries, not one — that difference is the free width
        `order` exists to buy.

    And one thing is *added* rather than refused. When the recency call reasoned
    a window, `publishedAfter` is derived from it and applied to every main
    query that does not already carry one — see `_derive_published_after`. The
    model decides whether age matters; the code does the date arithmetic, having
    twice watched the model reason a window and then attach it to nothing.

    That makes this function the only place a hard date filter can be added as
    well as the only place one can be refused, which is what keeps both
    directions inspectable from one read.
    """
    today = today or datetime.now(timezone.utc).date()
    constraints = user_constraints(topic)
    seen = {key for key in already_run}
    main_texts = {" ".join(query.text.lower().split()) for query in queries if not query.variant_of}

    kept: list[PlannedQuery] = []
    rejections: list[FilterRejection] = []

    for query in queries:
        # First, before any rule looks at it: a waiver is not a malformed date.
        waiver = _waiver_value(query)
        filters = query.filters.without(FILTER_PUBLISHED_AFTER) if waiver else query.filters
        for name, value in list(filters.as_dict().items()):
            problem = _filter_problem(
                name,
                value,
                query=query,
                recency=recency,
                constraints=constraints,
                today=today,
                main_texts=main_texts,
            )
            if problem is None:
                continue
            rejections.append(FilterRejection(query.text, name, value, problem))
            # Whether the refusal prevented an *exclusion* is the thing worth
            # knowing at a glance. A rejected `order` cost this query some
            # ranking; a rejected `publishedAfter` or `videoDuration` stopped
            # content being removed before anything in this system saw it, and
            # those two are the only ones that could have.
            log.warning(
                "territory: rejected filter %s=%r on %r%s — %s",
                name,
                value,
                query.text,
                " (would have excluded)" if name in HARD_FILTERS else "",
                problem,
            )
            filters = filters.without(name)

        candidate = PlannedQuery(
            text=query.text,
            origin=query.origin,
            origin_detail=query.origin_detail,
            filters=filters,
            variant_of=query.variant_of,
            reasons={
                name: why for name, why in query.reasons.items() if name in filters.as_dict()
            },
        )

        # After the refusals and before identity is computed. A derived cutoff
        # is part of what makes a query the query it is, so a round that
        # derives one and a round that does not are asking different questions
        # and must not collapse into a duplicate.
        candidate, waived = _derive_published_after(
            candidate,
            recency=recency,
            constraints=constraints,
            waiver=waiver,
            waiver_reason=query.reasons.get(FILTER_PUBLISHED_AFTER, ""),
            today=today,
        )
        if waived is not None:
            rejections.append(waived)
            log.warning(
                "territory: %r waived the %s date window — %s",
                waived.query,
                waived.value,
                waived.reason,
            )
        elif candidate.filters.published_after and not query.filters.published_after:
            log.info(
                "territory: derived publishedAfter=%s on %r from %s",
                candidate.filters.published_after,
                candidate.text,
                recency.label,
            )

        if candidate.key in seen:
            rejections.append(
                FilterRejection(
                    candidate.text,
                    "query",
                    _describe(candidate),
                    "identical query and filters already run this round",
                )
            )
            log.warning("territory: dropped duplicate query %r", candidate.text)
            continue

        seen.add(candidate.key)
        kept.append(candidate)

    return kept, rejections


def _filter_problem(
    name: str,
    value: str,
    *,
    query: PlannedQuery,
    recency: Recency,
    constraints: UserConstraints,
    today: date,
    main_texts: set[str],
) -> str | None:
    """Why this filter may not execute, or ``None`` if it may."""
    if name not in FILTER_FIELDS:  # pragma: no cover - parsing rejects these first
        return f"{name} is not a search parameter this system sends"

    if not value:
        return "no value"

    if name == FILTER_ORDER:
        return None if value in ORDER_VALUES else f"order must be one of {_listing(ORDER_VALUES)}"

    if name == FILTER_RELEVANCE_LANGUAGE:
        if not _LANGUAGE_RE.match(value):
            return "relevanceLanguage must be an ISO 639-1 code such as 'en'"
        return None

    if name == FILTER_VIDEO_DURATION:
        if value not in VIDEO_DURATION_VALUES:
            return f"videoDuration must be one of {_listing(VIDEO_DURATION_VALUES)}"
        target = " ".join(query.variant_of.lower().split())
        if not target:
            return (
                "videoDuration excludes, so it is never allowed on a main query; "
                "only a variant that adds a slice may carry one"
            )
        if target not in main_texts:
            return (
                f"names {query.variant_of!r} as the query it varies, and no such "
                f"main query was proposed"
            )
        return None

    # publishedAfter, the other one that excludes.
    if constraints.no_date_filter:
        return f"the user asked for material of any age — {constraints.reason}"
    if not recency.limited:
        return (
            "recency was reasoned as 'any time', so nothing justifies excluding "
            "anything by date"
        )
    cutoff = _iso_date(value)
    if cutoff is None:
        return "not a readable YYYY-MM-DD date"
    if cutoff > today:
        return f"is in the future ({cutoff.isoformat()}), which would exclude everything"
    # One month of slack, and it is doing real work. A model computing
    # "today minus twelve months" lands a few days off routinely, and rejecting
    # that would mean the reasoned window almost never survives its own
    # arithmetic. A cutoff a fortnight inside a twelve-month window is the same
    # judgement; a two-month cutoff under a twelve-month reasoning is a
    # different one, and that is what this catches.
    #
    # Slack in the widening direction only: an *earlier* cutoff excludes less
    # than the reasoning allows and is always fine.
    window = _months_before(today, max(0, (recency.months or 0) - 1))
    if cutoff > window:
        return (
            f"excludes more than the reasoned window: {cutoff.isoformat()} is well "
            f"inside {recency.label} (which reaches back to "
            f"{_months_before(today, recency.months or 0).isoformat()})"
        )
    return None


def _iso_date(value: str) -> date | None:
    """``2026-02-01`` or ``2026-02-01T00:00:00Z`` -> a date. Anything else is None."""
    match = _ISO_DATE_RE.match(value.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _months_before(today: date, months: int) -> date:
    """``today`` minus ``months`` calendar months, clamped to a valid day.

    Arithmetic rather than a dependency, and day-clamped rather than exact: the
    31st minus one month has no correct answer, and being one day wider is the
    error this file always chooses.
    """
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    day = min(today.day, _days_in_month(year, month + 1))
    return date(year, month + 1, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _listing(values: Iterable[str]) -> str:
    return ", ".join(sorted(values))


def _describe(query: PlannedQuery) -> str:
    filters = query.filters.as_dict()
    return " ".join(f"{name}={value}" for name, value in sorted(filters.items())) or "no filters"


# -- the mapper ---------------------------------------------------------------


class Mapper:
    """Owns the mapping prompt and the model client. Knows nothing about the loop.

    Deliberately separate from :class:`~marginalia.brain.judge.Judge`, which
    turns replies into verdicts. This one turns a brief into a description of a
    subject, and the two will change for entirely different reasons.
    """

    def __init__(self, client: LlmClient, *, prompts_directory=None) -> None:
        self.client = client
        self._directory = prompts_directory
        self._cache: dict[str, Prompt] = {}

    def prompt(self, name: str) -> Prompt:
        if name not in self._cache:
            self._cache[name] = load_prompt(name, directory=self._directory)
        return self._cache[name]

    def map(
        self,
        brief: Brief,
        *,
        count: int = 12,
        max_tokens: int | None = None,
        recency_samples: int = RECENCY_SAMPLES,
    ) -> TerritoryMap:
        """Sample the map `recency_samples` times; keep the first, widen the window.

        **The first usable reply is the map.** Its concepts, vocabulary and
        queries are one coherent account of a subject, and mixing three maps'
        queries together would be a different change from the one measured — so
        the later samples contribute exactly one thing: their recency judgement.

        The window is then the widest of the three, for the reasons in
        `widest_recency`. Every sample's tokens are counted, because the run's
        cost report must not under-report a call it made.

        A failure or a truncated reply returns an empty map rather than raising.
        Discovery still has the brief's own queries, so a run whose mapping call
        failed is a narrower run, not a dead one — and a sample that fails
        simply casts no vote, which leaves the window narrower than it might
        have been but never narrower than one sample alone would have made it.
        """
        samples: list[TerritoryMap] = []
        recencies: list[Recency] = []
        spent = Usage()

        for attempt in range(max(1, recency_samples)):
            sample = self._sample(brief, count=count, max_tokens=max_tokens)
            spent.add(
                Usage(
                    requests=1,
                    input_tokens=sample.input_tokens,
                    output_tokens=sample.output_tokens,
                )
            )
            samples.append(sample)
            # Usable means something came out of it. A problem with one line —
            # an over-long query, an unreadable RECENCY — costs that line, not
            # the sample's concepts, vocabulary and other queries.
            if not sample:
                log.info(
                    "territory: sample %d of %d was unusable (%s)",
                    attempt + 1,
                    recency_samples,
                    sample.problem,
                )
                continue
            if sample.recency_readable:
                recencies.append(sample.recency)
            else:
                log.info(
                    "territory: sample %d of %d cast no recency vote (%s)",
                    attempt + 1,
                    recency_samples,
                    sample.problem,
                )

        usable = [candidate for candidate in samples if candidate]
        # The first with queries, so a sample that wrote terms and no searches
        # does not displace one that did; otherwise the first usable at all.
        first = next((candidate for candidate in usable if candidate.queries), None) or (
            usable[0] if usable else None
        )

        if first is None:
            # Every sample failed. Return the last one so its problem is
            # reported, with the cost of all of them.
            return replace(
                sample, input_tokens=spent.input_tokens, output_tokens=spent.output_tokens
            )

        recency = widest_recency(recencies)
        if not recency.agreed:
            log.info(
                "territory: recency sampled %s — taking the widest (%s)",
                recency.samples_label,
                recency.label,
            )
        # Validated once, here, against the widened window — the only window
        # that will run. A `publishedAfter` the model proposed is judged against
        # it, and every main query's cutoff is derived from it.
        kept, rejections = validate(first.queries, recency=recency, topic=brief.topic)
        # The other samples' problems are kept on the stored map too (rule 14):
        # a sample that cast no vote must be visible as one. Labels only for a
        # sample that produced nothing — its problem may carry a provider error.
        others = [
            f"sample {number}: {candidate.problem if candidate else 'unusable'}"
            for number, candidate in enumerate(samples, start=1)
            if candidate is not first and (candidate.problem or not candidate)
        ]
        return replace(
            first,
            problem="; ".join(part for part in (first.problem, *others) if part),
            recency=recency,
            queries=kept,
            rejected=[*first.rejected, *rejections],
            input_tokens=spent.input_tokens,
            output_tokens=spent.output_tokens,
        )

    def _sample(
        self, brief: Brief, *, count: int, max_tokens: int | None
    ) -> TerritoryMap:
        """One mapping call, parsed. Validation happens once, in `map`."""
        prompt = self.prompt(MAP_PROMPT)
        try:
            reply = self.client.complete(
                "You map out what a subject covers and what words it uses.",
                prompt.render(topic=brief.topic, brief=brief.text, count=count),
                task="map_territory",
                # A map is a long structured document — a dozen query blocks,
                # two term lists, a recency call — and it is the one call whose
                # output decides what the search is even capable of returning.
                # On `AI agent memory` it hit the 8,192 ceiling and returned a
                # fragment, which the guard below discarded: B4's entire feature
                # contributed nothing to that run. Effort is already at `low`,
                # so the ceiling is what was left to raise.
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - a failed map must not end the run
            log.warning("territory: mapping call failed (%s) — searching from the brief alone", exc)
            return TerritoryMap(
                topic=brief.topic,
                version=prompt.version,
                parsed=False,
                problem=f"mapping call failed: {exc}",
            )

        if reply.finish_reason == "length":
            # A fragment, not a map. Parsing it would take whichever queries
            # happened to fit, and a truncated vocabulary is worse than none:
            # it looks like the subject's full extent.
            log.warning(
                "territory: mapping reply was truncated (%d output tokens) — ignoring it",
                reply.usage.output_tokens,
            )
            return TerritoryMap(
                topic=brief.topic,
                version=prompt.version,
                parsed=False,
                problem="reply hit the token ceiling before finishing",
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                latency_s=reply.latency_s,
            )

        parsed = parse_map(reply.text, topic=brief.topic, version=prompt.version)
        # **Not validated here.** `map` validates the winning sample once,
        # against the widest window. Validating each sample against its own
        # window derived that window's cutoff onto the queries, and the second
        # validation then refused the code's own date as narrower than the
        # widened window — counted in `filters_rejected` and printed as a filter
        # the map proposed that "would have excluded".
        return TerritoryMap(
            topic=parsed.topic,
            version=parsed.version,
            concepts=parsed.concepts,
            vocabulary=parsed.vocabulary,
            recency=parsed.recency,
            queries=parsed.queries,
            # What the reply asked for that this system does not send. What it
            # asked for that the rules would not allow is `map`'s to find.
            rejected=list(parsed.rejected),
            parsed=parsed.parsed,
            problem=parsed.problem,
            recency_readable=parsed.recency_readable,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            latency_s=reply.latency_s,
        )
