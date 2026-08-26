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
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from ..contracts import (
    FILTER_FIELDS,
    FILTER_ORDER,
    FILTER_PUBLISHED_AFTER,
    FILTER_RELEVANCE_LANGUAGE,
    FILTER_VIDEO_DURATION,
    HARD_FILTERS,
    ORDER_VALUES,
    ORIGIN_TERRITORY,
    VIDEO_DURATION_VALUES,
    Brief,
    FilterRejection,
    PlannedQuery,
    Recency,
    SearchFilters,
    TerritoryMap,
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

# `12 months — the ecosystem turns over`, `ANY — fundamentals do not move`.
_RECENCY_RE = re.compile(r"^\s*(ANY|\d{1,3})\s*(?:months?)?\s*[-–—:]?\s*(.*)$", re.IGNORECASE)

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
        if phrase in lowered:
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
        # its job, and it happens on perfectly good maps. Only a reply that
        # could not be read as a map at all is unparsed.
        parsed=bool(queries) and not problems,
        problem=problem,
    )


def _parse_recency(body: str) -> tuple[Recency, str]:
    """``12 months — reason`` or ``ANY — reason``.

    An unreadable recency line becomes *any time*, which is the wider answer.
    Guessing a window from a line nobody could read would be excluding content
    on the strength of a parse failure.
    """
    match = _RECENCY_RE.match(body.strip())
    if match is None:
        return Recency(reason="recency line unreadable; searching any time"), (
            "recency line unreadable"
        )
    head, reason = match.group(1), tidy(match.group(2), limit=_MAX_REASON)
    if head.upper() == "ANY":
        return Recency(months=None, reason=reason), ""
    months = int(head)
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
    """
    today = today or datetime.now(timezone.utc).date()
    constraints = user_constraints(topic)
    seen = {key for key in already_run}
    main_texts = {" ".join(query.text.lower().split()) for query in queries if not query.variant_of}

    kept: list[PlannedQuery] = []
    rejections: list[FilterRejection] = []

    for query in queries:
        filters = query.filters
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
        self, brief: Brief, *, count: int = 12, max_tokens: int | None = None
    ) -> TerritoryMap:
        """One call. A failure returns an empty map rather than raising.

        Discovery still has the brief's own queries, so a run whose mapping call
        failed is a narrower run, not a dead one. Raising here would make the
        new, unproven step able to kill a loop that worked before it existed.
        """
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
        kept, rejections = validate(
            parsed.queries, recency=parsed.recency, topic=brief.topic
        )
        return TerritoryMap(
            topic=parsed.topic,
            version=parsed.version,
            concepts=parsed.concepts,
            vocabulary=parsed.vocabulary,
            recency=parsed.recency,
            queries=kept,
            # Both halves, in the order they were caught: what the reply asked
            # for that this system does not send, then what it asked for that
            # the rules would not allow.
            rejected=[*parsed.rejected, *rejections],
            parsed=parsed.parsed,
            problem=parsed.problem,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            latency_s=reply.latency_s,
        )
