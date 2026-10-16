"""Territory mapping: parsing, and the validator that decides what may execute.

Two kinds of test, and the second kind is the important one. Parsing is the
usual "does it survive a model that did not read the format" surface. Validation
is a **safety** surface: two of the four search parameters *exclude*, and a
wrongly-applied one removes videos before anything in this system has seen them,
which is the one failure mode nothing downstream can detect or recover.

So the validator's tests are written the way a rejection test should be — every
one of them asserts both halves: that the filter did not run, and that the query
still did.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date

import pytest

from marginalia.brain.territory import (
    MAP_PROMPT,
    Mapper,
    parse_map,
    user_constraints,
    validate,
    waiver_applies,
    widest_recency,
)
from marginalia.contracts import (
    FILTER_FIELD_RECENCY_WAIVER,
    FILTER_ORDER,
    FILTER_PUBLISHED_AFTER,
    FILTER_RELEVANCE_LANGUAGE,
    FILTER_VIDEO_DURATION,
    ORIGIN_BRIEF,
    ORIGIN_TERRITORY,
    RECENCY_DERIVED_WHY,
    Brief,
    PlannedQuery,
    Recency,
    SearchFilters,
)
from marginalia.llm import load_prompt, prompts_dir

TODAY = date(2026, 8, 21)

BRIEF = Brief(topic="ai agent memory", text="INTENT\nAgent memory.", version="1/1")

GOOD_MAP = """
CONCEPTS: episodic memory, tool calling loops, context compaction
VOCABULARY: ReAct, LangGraph, scratchpad, vector store
RECENCY: 12 months — the agent tooling ecosystem turns over inside a year

QUERY: building react loops with tool calling
FROM: vocabulary: ReAct
FILTERS: order=date (this year's patterns rank poorly by relevance)

QUERY: langgraph persistent state tutorial
FROM: vocabulary: LangGraph
FILTERS: none
"""


def _query(text="a query", **kwargs) -> PlannedQuery:
    kwargs.setdefault("origin", ORIGIN_TERRITORY)
    return PlannedQuery(text=text, **kwargs)


# -- parsing ----------------------------------------------------------------


def test_a_well_formed_map_is_read_whole() -> None:
    territory = parse_map(GOOD_MAP, topic="ai agent memory", version="1")

    assert territory.concepts == ["episodic memory", "tool calling loops", "context compaction"]
    assert territory.vocabulary == ["ReAct", "LangGraph", "scratchpad", "vector store"]
    assert territory.recency.months == 12
    assert "turns over" in territory.recency.reason
    assert [query.text for query in territory.queries] == [
        "building react loops with tool calling",
        "langgraph persistent state tutorial",
    ]
    assert territory.parsed


def test_every_query_carries_where_it_came_from() -> None:
    """`origin_detail` is what makes the phase's own claim checkable.

    "The map found a video the topic words never would have" is only inspectable
    if each query says which term produced it.
    """
    territory = parse_map(GOOD_MAP, topic="ai agent memory", version="1")

    assert territory.queries[0].origin_detail == "vocabulary: ReAct"
    assert all(query.origin == ORIGIN_TERRITORY for query in territory.queries)


def test_a_filter_carries_its_reason() -> None:
    territory = parse_map(GOOD_MAP, topic="ai agent memory", version="1")

    query = territory.queries[0]
    assert query.filters.order == "date"
    assert "relevance" in query.reasons[FILTER_ORDER]
    assert '"why"' in query.filters_json()


def test_concepts_and_vocabulary_are_kept_apart() -> None:
    """A concept falls under the brief; a vocabulary term is only a word.

    They are shown to the judge as one flat list on purpose, but they are stored
    separately — the map is the record of what the run thought, and collapsing
    the two would lose the distinction the prompt asked for.
    """
    territory = parse_map(GOOD_MAP, topic="t", version="1")

    assert "ReAct" not in territory.concepts
    assert "episodic memory" not in territory.vocabulary
    assert territory.terms[:3] == territory.concepts


def test_terms_deduplicates_across_the_two_lists() -> None:
    text = "CONCEPTS: caching\nVOCABULARY: Caching, kv cache\nRECENCY: ANY — stable\nQUERY: a b\nFROM: t"
    territory = parse_map(text, topic="t", version="1")

    assert territory.terms == ["caching", "kv cache"]


def test_markdown_decoration_survives() -> None:
    text = """
**CONCEPTS:** one, two
**VOCABULARY**: three
RECENCY: ANY — stable subject

**QUERY:** sqlite wal mode explained
FROM: topic
FILTERS: none
"""
    territory = parse_map(text, topic="t", version="1")

    assert territory.concepts == ["one", "two"]
    assert territory.queries[0].text == "sqlite wal mode explained"


def test_a_reply_with_no_queries_is_not_parsed() -> None:
    """There is nothing to search. Marked, not silently returned as an empty map.

    An empty map that claims to have parsed reads, to every later reader, as a
    subject that genuinely has no other words.
    """
    territory = parse_map("CONCEPTS: a\nVOCABULARY: b\nRECENCY: ANY — x", topic="t", version="1")

    assert not territory.parsed
    assert "no queries" in territory.problem


def test_a_sentence_instead_of_a_map_is_not_parsed() -> None:
    territory = parse_map("I'm sorry, I can't help with that.", topic="t", version="1")

    assert not territory.parsed
    assert not territory.queries


def test_an_over_long_query_is_dropped_and_named() -> None:
    text = (
        "CONCEPTS: a\nVOCABULARY: b\nRECENCY: ANY — x\n"
        "QUERY: " + " ".join(["word"] * 20) + "\nFROM: t\n"
        "QUERY: short one\nFROM: t"
    )
    territory = parse_map(text, topic="t", version="1")

    assert [query.text for query in territory.queries] == ["short one"]
    assert "too long" in territory.problem


def test_too_many_queries_are_capped() -> None:
    blocks = "\n".join(f"QUERY: query number {n}\nFROM: topic\n" for n in range(40))
    territory = parse_map(
        f"CONCEPTS: a\nVOCABULARY: b\nRECENCY: ANY — x\n{blocks}", topic="t", version="1"
    )

    assert len(territory.queries) == 24


def test_the_cap_says_what_it_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """A silent cap reads, months later, as a model that proposed only this many."""
    blocks = "\n".join(f"QUERY: query number {n}\nFROM: topic\n" for n in range(40))
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.territory"):
        parse_map(f"CONCEPTS: a\nRECENCY: ANY — x\n{blocks}", topic="t", version="1")

    assert any("40 queries proposed" in record.getMessage() for record in caplog.records)


# -- recency ----------------------------------------------------------------


def test_recency_is_a_judgement_with_a_reason() -> None:
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: 6 months — provider pricing changed three times this year\n"
        "QUERY: a b\nFROM: t",
        topic="t",
        version="1",
    )

    assert territory.recency.months == 6
    assert territory.recency.reason == "provider pricing changed three times this year"
    assert territory.recency.label == "last 6 months"


def test_any_is_a_real_answer_and_the_wider_one() -> None:
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: ANY — SQLite WAL mode has behaved this way for a decade\n"
        "QUERY: a b\nFROM: t",
        topic="t",
        version="1",
    )

    assert territory.recency.months is None
    assert not territory.recency.limited
    assert territory.recency.label == "any time"


def test_an_unreadable_recency_line_widens_rather_than_guesses() -> None:
    """A window invented from a line nobody could read would exclude on a parse bug."""
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: sometime recentish\nQUERY: a b\nFROM: t", topic="t", version="1"
    )

    assert territory.recency.months is None
    assert "recency line unreadable" in territory.problem


def test_an_absurd_window_is_not_used() -> None:
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: 9000 months — forever\nQUERY: a b\nFROM: t",
        topic="t",
        version="1",
    )

    assert territory.recency.months is None


# The parser used to take the first number and ignore its unit, so "2 years"
# derived a 2-month cutoff onto every main query — rule 16's undetectable
# exclusion, produced by a parser. A range takes its wider end.
@pytest.mark.parametrize(
    "line,months",
    [
        ("2 years — the API churned", 24),
        ("1 year — tooling moves fast", 12),
        ("2 yrs — the API churned", 24),
        ("18 months — churn", 18),
        ("6-12 months — pricing moved", 12),
        ("1-2 years — the API churned", 24),
        ("1–2 years — the API churned", 24),
        ("12 — no unit, so months as the prompt asks", 12),
        ("24m — churn", 24),
    ],
)
def test_the_recency_unit_is_read_and_a_range_takes_its_wider_end(line: str, months: int) -> None:
    territory = parse_map(
        f"CONCEPTS: a\nRECENCY: {line}\nQUERY: a b\nFROM: t", topic="t", version="1"
    )

    assert territory.recency.months == months
    assert "recency" not in territory.problem


def test_any_still_means_no_window() -> None:
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: ANY — fundamentals do not move\nQUERY: a b\nFROM: t",
        topic="t",
        version="1",
    )

    assert territory.recency.months is None
    assert territory.recency.reason == "fundamentals do not move"


@pytest.mark.parametrize("line", ["3 weeks — very fresh", "1 decade — stable", "90 days — new"])
def test_an_unrecognised_unit_is_unreadable_never_a_narrower_window(line: str) -> None:
    territory = parse_map(
        f"CONCEPTS: a\nRECENCY: {line}\nQUERY: a b\nFROM: t", topic="t", version="1"
    )

    assert territory.recency.months is None
    assert "recency line unreadable" in territory.problem


# -- the validator: unsupported and malformed --------------------------------


def test_an_unsupported_field_is_rejected_and_counted() -> None:
    """Caught at parse time, and counted the same way a validator rejection is."""
    territory = parse_map(
        "CONCEPTS: a\nRECENCY: ANY — x\nQUERY: a b\nFROM: t\n"
        "FILTERS: publishedBefore=2020-01-01 (older only)",
        topic="t",
        version="1",
    )

    assert territory.queries[0].filters.as_dict() == {}
    assert [rejection.field for rejection in territory.rejected] == ["publishedBefore"]
    assert "not a search parameter" in territory.rejected[0].reason


def test_a_malformed_published_after_is_caught_not_executed() -> None:
    """Gate 3. Models compute dates badly, and the failure is silent: a cutoff
    the API cannot read is a 400, and one in the future returns nothing at all —
    which reads exactly like a subject with no videos."""
    queries = [
        _query(filters=SearchFilters(published_after="2027-13-45"), origin=ORIGIN_TERRITORY)
    ]

    kept, rejections = validate(
        queries, recency=Recency(months=12, reason="fast"), today=TODAY
    )

    assert len(kept) == 1, "the query still runs; only the filter is dropped"
    assert rejections[0].field == FILTER_PUBLISHED_AFTER
    assert "readable" in rejections[0].reason
    # And then the window is derived onto it, which is not the rejection being
    # undone: what the model wrote is gone, and what replaces it is the date the
    # recency reasoning actually implies rather than the one it fumbled.
    assert kept[0].filters.published_after == "2025-08-21"


def test_a_future_cutoff_is_rejected() -> None:
    queries = [_query(filters=SearchFilters(published_after="2030-01-01"))]

    kept, rejections = validate(queries, recency=Recency(months=12, reason="f"), today=TODAY)

    assert "future" in rejections[0].reason
    # 2030 excluded everything; the derived window excludes twelve months.
    assert kept[0].filters.published_after == "2025-08-21"


def test_a_rejection_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Counted, logged, surfaced — the same discipline as the unparsed counters."""
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.territory"):
        validate(
            [_query(filters=SearchFilters(order="sideways"))],
            recency=Recency(),
            today=TODAY,
        )

    assert any("rejected filter" in record.message for record in caplog.records)


@pytest.mark.parametrize("value", ["sideways", "DATE", "most_viewed", ""])
def test_an_order_outside_the_allowed_set_is_rejected(value: str) -> None:
    kept, rejections = validate(
        [_query(filters=SearchFilters(order=value))], recency=Recency(), today=TODAY
    )

    assert kept[0].filters.order is None
    assert len(rejections) == 1


@pytest.mark.parametrize("value", ["relevance", "date", "viewCount", "rating", "title"])
def test_every_documented_order_is_allowed(value: str) -> None:
    """The other half. A validator that rejected everything would pass the tests
    above and quietly delete the width `order` exists to buy."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(order=value))], recency=Recency(), today=TODAY
    )

    assert kept[0].filters.order == value
    assert not rejections


@pytest.mark.parametrize("value", ["english", "en_GB", "e", "EN"])
def test_a_malformed_language_is_rejected(value: str) -> None:
    kept, rejections = validate(
        [_query(filters=SearchFilters(relevance_language=value))],
        recency=Recency(),
        today=TODAY,
    )

    assert kept[0].filters.relevance_language is None
    assert rejections[0].field == FILTER_RELEVANCE_LANGUAGE


@pytest.mark.parametrize("value", ["en", "de", "pt-BR"])
def test_a_well_formed_language_is_allowed(value: str) -> None:
    """It ranks and never excludes, so it is the safe one and stays permitted."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(relevance_language=value))],
        recency=Recency(),
        today=TODAY,
    )

    assert kept[0].filters.relevance_language == value
    assert not rejections


# -- the validator: videoDuration, the trap ----------------------------------


@pytest.mark.parametrize("band", ["short", "medium", "long"])
def test_video_duration_never_survives_on_a_main_query(band: str) -> None:
    """Gate 4, and the rule this phase most needs held.

    A duration band excludes everything outside it. On a main query that is the
    duration bias the stage-1 prompt spends four paragraphs forbidding, moved
    into the search layer where nobody would look for it — and the class it
    usually removes is Shorts, which can be denser than a fifty-minute talk.
    """
    kept, rejections = validate(
        [_query("cache breakpoints", filters=SearchFilters(video_duration=band))],
        recency=Recency(),
        today=TODAY,
    )

    assert kept[0].filters.video_duration is None
    assert kept[0].text == "cache breakpoints", "the query runs; the band does not"
    assert rejections[0].field == FILTER_VIDEO_DURATION
    assert "main query" in rejections[0].reason


def test_no_planned_query_ever_reaches_a_provider_with_a_duration_band_alone() -> None:
    """The rule stated as a property over a whole map rather than one query."""
    text = (
        "CONCEPTS: a\nRECENCY: ANY — x\n"
        "QUERY: prompt caching explained\nFROM: t\nFILTERS: videoDuration=long (deep dives)\n"
        "QUERY: kv cache eviction\nFROM: t\nFILTERS: videoDuration=short (quick ones)\n"
    )
    territory = parse_map(text, topic="t", version="1")
    kept, _ = validate(territory.queries, recency=territory.recency, today=TODAY)

    for query in kept:
        assert query.variant_of or query.filters.video_duration is None


def test_a_duration_variant_of_a_real_query_is_allowed() -> None:
    """The one legitimate use: *adding* a slice rather than removing one.

    Both run — the unfiltered query and the variant — so nothing is excluded and
    a band that relevance ranking buries gets surfaced.
    """
    queries = [
        _query("cache breakpoints"),
        _query(
            "cache breakpoints",
            filters=SearchFilters(video_duration="short"),
            variant_of="cache breakpoints",
        ),
    ]

    kept, rejections = validate(queries, recency=Recency(), today=TODAY)

    assert len(kept) == 2
    assert kept[1].filters.video_duration == "short"
    assert not rejections


def test_a_variant_of_a_query_that_does_not_exist_is_rejected() -> None:
    """Otherwise `variant_of` is a password: write anything and the band runs."""
    queries = [
        _query("cache breakpoints"),
        _query(
            "something else entirely",
            filters=SearchFilters(video_duration="short"),
            variant_of="a query nobody proposed",
        ),
    ]

    kept, rejections = validate(queries, recency=Recency(), today=TODAY)

    assert kept[1].filters.video_duration is None
    assert "no such" in rejections[0].reason


def test_a_variant_may_not_name_another_variant() -> None:
    """A chain of variants would let one legitimate main query authorise many
    bands, which is the exclusion this rule exists to bound."""
    queries = [
        _query("a b", filters=SearchFilters(video_duration="short"), variant_of="c d"),
        _query("c d", filters=SearchFilters(video_duration="long"), variant_of="a b"),
    ]

    kept, _ = validate(queries, recency=Recency(), today=TODAY)

    assert all(query.filters.video_duration is None for query in kept)


@pytest.mark.parametrize("band", ["tiny", "4min", "any minutes"])
def test_a_duration_outside_the_allowed_set_is_rejected(band: str) -> None:
    """A band the provider would refuse. Stripping it leaves a variant identical
    to the query it varied, which the duplicate rule then drops — the variant
    existed only to carry the band, and without it there is nothing to run."""
    queries = [
        _query("a b"),
        _query("a b", filters=SearchFilters(video_duration=band), variant_of="a b"),
    ]

    kept, rejections = validate(queries, recency=Recency(), today=TODAY)

    assert all(query.filters.video_duration is None for query in kept)
    assert rejections[0].field == FILTER_VIDEO_DURATION
    assert "one of" in rejections[0].reason


# -- the validator: recency and the user -------------------------------------


def test_a_date_filter_needs_the_recency_call_to_justify_it() -> None:
    """A map that reasoned "any time" and then filtered by date has contradicted
    its own reasoning, and the filter is the half that excludes."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="2026-01-01"))],
        recency=Recency(months=None, reason="fundamentals do not move"),
        today=TODAY,
    )

    assert kept[0].filters.published_after is None
    assert "any time" in rejections[0].reason


def test_a_date_inside_the_reasoned_window_is_allowed() -> None:
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="2025-09-01"))],
        recency=Recency(months=12, reason="turns over"),
        today=TODAY,
    )

    assert kept[0].filters.published_after == "2025-09-01"
    assert not rejections


def test_a_cutoff_far_narrower_than_the_window_is_rejected() -> None:
    """Rejecting widens. A twelve-month judgement does not license a two-month
    exclusion, and dropping the filter falls back to the unfiltered search.

    "Far" is the operative word: a month of slack absorbs the model's date
    arithmetic, because a cutoff a fortnight off is the same judgement while a
    two-month one is a different judgement wearing the same reason."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="2026-07-01"))],
        recency=Recency(months=12, reason="turns over"),
        today=TODAY,
    )

    assert "excludes more than the reasoned window" in rejections[0].reason
    # Falling back to the *reasoned* window rather than to no filter at all.
    # Still a widening — 2026-07-01 excluded eleven months more than the
    # judgement licensed — and now it is the judgement that decides, which is
    # the whole point of deriving the date in code.
    assert kept[0].filters.published_after == "2025-08-21"


@pytest.mark.parametrize(
    "topic",
    [
        "history of transformers",
        "SQLite WAL mode, all time",
        "classic compiler lectures",
    ],
)
def test_the_user_outranks_the_recency_reasoning(topic: str) -> None:
    """The case this rule exists for: a user who asked for old material and got
    it excluded, silently, by a model reasoning about ecosystems."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="2026-01-01"))],
        recency=Recency(months=12, reason="the model thought this moves fast"),
        topic=topic,
        today=TODAY,
    )

    assert kept[0].filters.published_after is None
    assert "the user asked" in rejections[0].reason


def test_a_topic_with_no_stated_constraint_leaves_the_reasoning_alone() -> None:
    assert not user_constraints("ai agent memory").no_date_filter
    assert user_constraints("history of transformers").no_date_filter


def test_a_constraint_phrase_must_be_a_whole_word() -> None:
    """"ever" was matched as a substring, so every topic containing "every",
    "however" or "never" was read as asking for material of any age."""
    for topic in ("every agent framework compared", "never lose agent state", "clever caching"):
        assert not user_constraints(topic).no_date_filter, topic
    assert user_constraints("the best agent framework ever").no_date_filter


def test_the_user_rule_only_ever_removes_a_hard_filter() -> None:
    """One-directional by design: it can forbid `publishedAfter`, never require
    one. A phrase it recognises wrongly costs one unfiltered search."""
    kept, _ = validate(
        [_query(filters=SearchFilters(order="date", relevance_language="en"))],
        recency=Recency(),
        topic="history of transformers",
        today=TODAY,
    )

    assert kept[0].filters.order == "date"
    assert kept[0].filters.relevance_language == "en"


# -- the validator: duplicates ------------------------------------------------


def test_an_identical_query_already_run_is_dropped() -> None:
    kept, rejections = validate(
        [_query("prompt caching")],
        recency=Recency(),
        already_run={_query("Prompt  Caching").key},
        today=TODAY,
    )

    assert not kept
    assert rejections[0].field == "query"


def test_the_same_text_with_different_filters_is_not_a_duplicate() -> None:
    """The same query by `relevance` and by `date` returns different slices of
    the same result set. Collapsing them throws away free width."""
    queries = [_query("prompt caching"), _query("prompt caching", filters=SearchFilters(order="date"))]

    kept, rejections = validate(queries, recency=Recency(), today=TODAY)

    assert len(kept) == 2
    assert not rejections


def test_a_query_that_becomes_a_duplicate_after_a_rejection_is_dropped() -> None:
    """Stripping a filter can make two queries identical, and the check has to
    run on what will actually be sent rather than on what was proposed."""
    queries = [
        _query("prompt caching"),
        _query("prompt caching", filters=SearchFilters(published_after="2026-01-01")),
    ]

    kept, rejections = validate(queries, recency=Recency(reason="any time"), today=TODAY)

    assert len(kept) == 1
    assert {rejection.field for rejection in rejections} == {FILTER_PUBLISHED_AFTER, "query"}


def test_rejected_filters_lose_their_reasons_too() -> None:
    """A reason for a filter that did not run would read, in the ledger, as a
    filter that did."""
    kept, _ = validate(
        [
            PlannedQuery(
                text="a b",
                origin=ORIGIN_TERRITORY,
                filters=SearchFilters(order="date", video_duration="short"),
                reasons={FILTER_ORDER: "fresh", FILTER_VIDEO_DURATION: "shorts"},
            )
        ],
        recency=Recency(),
        today=TODAY,
    )

    assert kept[0].reasons == {FILTER_ORDER: "fresh"}


# -- the prompt ---------------------------------------------------------------


def test_the_mapping_prompt_is_versioned_and_renders() -> None:
    prompt = load_prompt(MAP_PROMPT, directory=prompts_dir())

    assert prompt.version
    rendered = prompt.render(topic="t", brief="B", count=8)
    assert "{{" not in rendered


def test_the_mapping_prompt_names_no_model_family() -> None:
    """Same rule the other prompts are held to: a prompt that names a model
    family is a prompt that has to be rewritten to swap providers."""
    text = load_prompt(MAP_PROMPT, directory=prompts_dir()).text.lower()

    for family in ("claude", "gpt-", "gemini", "llama", "kimi", "deepseek", "qwen"):
        assert family not in text


def test_the_mapping_prompt_states_the_duration_rule() -> None:
    """The trap parameter needs the rule in the prompt as well as in the code.
    Code that silently rejects every proposal teaches the model nothing."""
    text = load_prompt(MAP_PROMPT, directory=prompts_dir()).text

    assert "Never put this on a main query" in text


# -- the mapper ---------------------------------------------------------------


class FakeReply:
    def __init__(self, text: str, finish_reason: str = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()
        self.latency_s = 1.5


class FakeClient:
    def __init__(self, reply=None, raises=None) -> None:
        self._reply = reply
        self._raises = raises
        self.tasks: list[str] = []

    def complete(self, system, user, *, task=None, max_tokens=None):
        self.tasks.append(task)
        if self._raises is not None:
            raise self._raises
        return self._reply


def test_the_mapper_returns_a_validated_map() -> None:
    mapper = Mapper(FakeClient(FakeReply(GOOD_MAP)))

    territory = mapper.map(BRIEF)

    assert territory.vocabulary == ["ReAct", "LangGraph", "scratchpad", "vector store"]
    # Every sample's tokens, not the first one's. The recency judgement is
    # sampled three times and a cost report that named one of the three would
    # under-report a call the run actually made.
    assert territory.input_tokens == 300
    assert territory.latency_s == 1.5


def test_a_failed_mapping_call_leaves_the_run_narrower_not_dead() -> None:
    """Discovery still has the brief's own queries. Raising here would let a new
    and unproven step kill a loop that worked before it existed."""
    mapper = Mapper(FakeClient(raises=RuntimeError("boom")))

    territory = mapper.map(BRIEF)

    assert not territory.parsed
    assert not territory.queries
    assert "RuntimeError" in territory.problem, "named by class"
    assert "boom" not in territory.problem, "an unclassified exception's text is not stored"


def test_a_truncated_reply_is_not_parsed_at_all() -> None:
    """A truncated vocabulary is worse than none: it looks like the subject's
    full extent, and the queries that would have covered the rest never run."""
    mapper = Mapper(FakeClient(FakeReply(GOOD_MAP, finish_reason="length")))

    territory = mapper.map(BRIEF)

    assert not territory.parsed
    assert not territory.queries
    assert "token ceiling" in territory.problem


def test_the_mapping_call_names_its_own_task() -> None:
    """Effort is per task and set in config; a call with no task name silently
    takes the global default."""
    client = FakeClient(FakeReply(GOOD_MAP))
    Mapper(client).map(BRIEF)

    assert client.tasks == ["map_territory"] * 3, "one task name, three samples"


def test_the_mapper_validates_before_returning() -> None:
    text = (
        "CONCEPTS: a\nRECENCY: ANY — stable\n"
        "QUERY: sqlite wal mode\nFROM: t\nFILTERS: publishedAfter=2026-01-01 (recent)\n"
    )
    territory = Mapper(FakeClient(FakeReply(text))).map(BRIEF)

    assert territory.queries[0].filters.published_after is None
    assert territory.rejected


# -- what a real mapping call actually wrote --------------------------------
#
# Both of these were found by running the map for real, not by imagining what a
# model might do. Both failures were *safe* — one rejected a filter that cannot
# exclude anything, the other produced cosmetically broken vocabulary — and both
# were counted and logged rather than silent, which is the design working. They
# are fixed anyway: a separator the reason field is free to contain is a parser
# asking for trouble.


def test_a_semicolon_inside_a_reason_does_not_split_the_filter() -> None:
    """Observed verbatim from a real mapping call for "AI agent memory".

    A plain `split(";")` cut the reason in half and left two fragments that were
    neither `name=value`, so a perfectly good `order` filter — one of the two
    that cannot exclude anything — was dropped and counted twice as a rejection.
    """
    text = (
        "CONCEPTS: a\nRECENCY: 12 months — fast\n"
        "QUERY: mem0 agent memory tutorial\nFROM: vocabulary: Mem0\n"
        "FILTERS: order=date (Mem0 changes monthly; latest tutorials reflect the API)"
    )

    territory = parse_map(text, topic="t", version="1")

    assert territory.queries[0].filters.order == "date"
    assert territory.queries[0].reasons[FILTER_ORDER].endswith("reflect the API")
    assert not territory.rejected


def test_two_filters_still_separate_on_a_semicolon() -> None:
    """The other half: the fix must not make the separator stop working."""
    text = (
        "CONCEPTS: a\nRECENCY: ANY — x\nQUERY: a b\nFROM: t\n"
        "FILTERS: order=date (fresh); relevanceLanguage=en (english ranks first)"
    )

    territory = parse_map(text, topic="t", version="1")

    assert territory.queries[0].filters.as_dict() == {"order": "date", "relevanceLanguage": "en"}


def test_a_parenthesised_list_of_names_survives_the_comma_split() -> None:
    """Also observed. The model wrote one vocabulary entry as

        vector database (Pinecone, Chroma, Weaviate, FAISS)

    and the comma split left `vector database (Pinecone` and `FAISS)` with three
    clean names between them. Nothing downstream broke — the judge is shown a
    word list — but two of the words were fragments with dangling brackets, and
    the map is meant to be readable as the record of what a run thought a
    subject was called.
    """
    text = (
        "CONCEPTS: a\n"
        "VOCABULARY: Mem0, vector database (Pinecone, Chroma, FAISS), embeddings\n"
        "RECENCY: ANY — x\nQUERY: a b\nFROM: t"
    )

    territory = parse_map(text, topic="t", version="1")

    assert territory.vocabulary == [
        "Mem0",
        "vector database",
        "Pinecone",
        "Chroma",
        "FAISS",
        "embeddings",
    ]


# -- deriving the date window from the recency reasoning ----------------------
#
# The gap these close was measured twice on `langchain`. A map reasoned "last 18
# months — LangChain's API has churned (LLMChain -> LCEL -> LangGraph)" and then
# emitted queries carrying `order=date` and no cutoff: 0 of 29 queries filtered
# by date, with `rejected_filters` empty. Nothing refused it; nothing proposed
# it. `RECENCY` and `FILTERS` were separate fields and nothing joined them.


def test_a_reasoned_window_becomes_a_date_on_every_main_query() -> None:
    kept, rejections = validate(
        [_query("one"), _query("two")],
        recency=Recency(months=18, reason="the API churned"),
        today=TODAY,
    )

    assert [query.filters.published_after for query in kept] == [
        "2025-02-21",
        "2025-02-21",
    ]
    assert not rejections, "deriving is not a rejection; nothing was refused"


def test_the_derived_filter_says_it_was_derived() -> None:
    """Which half made the judgement has to survive into the ledger.

    A cutoff the model proposed and a cutoff the code computed are different
    claims — one is a judgement to correct, the other is arithmetic — and
    `filters_json` is where anyone asks."""
    kept, _ = validate(
        [_query()], recency=Recency(months=6, reason="pricing moved"), today=TODAY
    )

    assert kept[0].reasons[FILTER_PUBLISHED_AFTER] == (
        "derived from the reasoned recency window (last 6 months)"
    )
    assert RECENCY_DERIVED_WHY in kept[0].filters_json()


def test_any_time_derives_nothing() -> None:
    """`ANY` is the default and the wider answer. Nothing changes for it."""
    kept, rejections = validate([_query()], recency=Recency(), today=TODAY)

    assert kept[0].filters.published_after is None
    assert not rejections


def test_the_user_outranks_a_derived_window_too() -> None:
    """The rule that already governed a proposed filter governs a derived one.

    Otherwise moving the arithmetic into the code would have quietly moved it
    out from under the one constraint that outranks the reasoning."""
    kept, _ = validate(
        [_query()],
        recency=Recency(months=12, reason="fast"),
        topic="history of transformers",
        today=TODAY,
    )

    assert kept[0].filters.published_after is None


def test_a_variant_keeps_its_width() -> None:
    """A `videoDuration` variant exists to *add* a slice a main query buries.

    Narrowing it by date takes back the width it was written for."""
    kept, _ = validate(
        [
            _query("a query"),
            _query("a query", filters=SearchFilters(video_duration="short"),
                   variant_of="a query"),
        ],
        recency=Recency(months=12, reason="fast"),
        today=TODAY,
    )

    assert kept[0].filters.published_after == "2025-08-21"
    assert kept[1].filters.published_after is None


def test_a_model_date_inside_the_window_survives_untouched() -> None:
    """Derivation fills an absence. It never overrules a judgement that passed."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="2025-09-01"))],
        recency=Recency(months=12, reason="fast"),
        today=TODAY,
    )

    assert kept[0].filters.published_after == "2025-09-01"
    assert not rejections


def test_a_query_may_waive_the_window_and_it_is_recorded() -> None:
    """The escape hatch, and the reason it is not silent.

    A query deliberately about earlier practice is exactly what the window
    hides, so there has to be a way to say so — and it is the only remaining
    way to make a search wider than the code decided, which is why it lands on
    its own counter rather than among the refusals."""
    kept, rejections = validate(
        [
            _query("how it used to be done",
                   filters=SearchFilters(published_after="any"),
                   reasons={FILTER_PUBLISHED_AFTER: "the gap is about earlier practice"}),
            _query("current practice"),
        ],
        recency=Recency(months=12, reason="fast"),
        today=TODAY,
    )

    assert kept[0].filters.published_after is None, "waived, not derived"
    assert kept[1].filters.published_after == "2025-08-21", "and only this one query"

    waiver = rejections[0]
    assert waiver.is_recency_waiver
    assert not waiver.is_duplicate_query, "three outcomes, never two"
    assert waiver.reason == "the gap is about earlier practice"


def test_a_waiver_is_not_read_as_a_malformed_date() -> None:
    """Order matters here, and getting it wrong is a silent inversion.

    `any` is not a date, so a waiver read by the ordinary rules is refused as
    malformed, counted as a filter rejection, and then has the window derived
    onto it — the exact opposite of what the query asked for, reached through
    two steps that each look right."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="any"))],
        recency=Recency(months=12, reason="fast"),
        today=TODAY,
    )

    assert kept[0].filters.published_after is None
    assert [item.field for item in rejections] == [FILTER_FIELD_RECENCY_WAIVER]


def test_a_waiver_under_any_time_recency_is_not_recorded() -> None:
    """It prevented nothing, so there is nothing to report.

    A counter that also counts the no-ops is a counter nobody can read."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="any"))],
        recency=Recency(),
        today=TODAY,
    )

    assert kept[0].filters.published_after is None
    assert not rejections


# -- widest of three --------------------------------------------------------
#
# Rule 16 made this call load-bearing, and then it was measured: ten samples on
# one brief returned three distinct windows per topic — `ai agent architecture`
# ANY/24/18, `langchain` 24/18/12 — and the real run answered 36, wider than any
# of the ten. Against the 31 videos that run kept, 24 months would have excluded
# 2, 18 months 4, and 12 months 7, and one of the four is the video the planner
# named as its evidence that one of the brief's required items was covered.
#
# What makes the widest safe rather than merely wide is the other half of the
# measurement: the call got the *direction* right 10 of 10 on `langchain`, so
# widening neutralises the unreliable half of the judgement and keeps the
# reliable one. This is rule 8 at the search layer — an unstable exclusion is
# ambiguity, and ambiguity resolves toward keeping.


def test_the_widest_of_three_windows_wins() -> None:
    chosen = widest_recency(
        [Recency(12, "a year of churn"), Recency(24, "two years"), Recency(18, "eighteen")]
    )

    assert chosen.months == 24
    assert chosen.reason == "two years", "the winning sample's reasoning travels with it"
    assert chosen.samples == (12, 24, 18), "and all three are recorded"
    assert not chosen.agreed


def test_any_is_widest_and_wins_outright() -> None:
    """`ANY` is not a large number, it is the absence of a cutoff, so it cannot
    lose to one. The `ai agent architecture` samples split ANY/24/18 — the case
    this exists for."""
    chosen = widest_recency(
        [Recency(18, "eighteen"), Recency(None, "patterns are stable"), Recency(24, "two years")]
    )

    assert chosen.months is None
    assert not chosen.limited
    assert chosen.reason == "patterns are stable"
    assert chosen.samples == (18, None, 24)


def test_agreement_is_recorded_as_agreement() -> None:
    chosen = widest_recency([Recency(12, "a"), Recency(12, "b"), Recency(12, "c")])

    assert chosen.months == 12
    assert chosen.agreed, "three samples, one answer — a consensus, and it says so"


def test_widening_does_not_collapse_to_any() -> None:
    """The measured `langchain` distribution: always a window, never ANY. Taking
    the widest must still return a window, or the fix would throw away the half
    of the judgement that was reliable."""
    chosen = widest_recency([Recency(24, "a"), Recency(18, "b"), Recency(12, "c")])

    assert chosen.months == 24
    assert chosen.limited


def test_no_samples_is_any() -> None:
    assert widest_recency([]).months is None


def test_the_derived_reason_names_every_sample() -> None:
    """"Was this window a consensus or a coin flip?" has to be answerable from a
    stored run, not from a rerun. Rule 23: the conclusion is the half you cannot
    correct from."""
    kept, _ = validate(
        [_query()],
        recency=Recency(24, "two years", samples=(24, 18, 12)),
        today=TODAY,
    )

    why = kept[0].reasons[FILTER_PUBLISHED_AFTER]
    assert RECENCY_DERIVED_WHY in why
    assert "24/18/12" in why and "widest taken" in why
    assert "24/18/12" in kept[0].filters_json()


def test_a_widened_window_still_loses_to_the_user() -> None:
    """Widest-of-three is a floor on the *model's* judgement, never on the
    user's. Rule 16 is explicit that the user's own words outrank the reasoning,
    and widening must not become a route around a constraint they set."""
    kept, _ = validate(
        [_query()],
        recency=Recency(24, "two years", samples=(24, 18, 12)),
        topic="history of transformers",
        today=TODAY,
    )

    assert kept[0].filters.published_after is None


def test_a_duration_variant_stays_wide_under_a_widened_window() -> None:
    """Unchanged by this pass, and checked because the interaction is easy to
    break: a variant exists to *add* a slice a main query buries."""
    kept, _ = validate(
        [
            _query("a query"),
            _query("a query", filters=SearchFilters(video_duration="short"),
                   variant_of="a query"),
        ],
        recency=Recency(24, "two years", samples=(24, 18, 12)),
        today=TODAY,
    )

    assert kept[0].filters.published_after == "2024-08-21"
    assert kept[1].filters.published_after is None


def test_a_waiver_still_wins_against_a_widened_window() -> None:
    """The per-query waiver is untouched, and the two mitigations cover
    different runs: the waiver fires when the map notices the conflict — it did
    on 10 of 20 measured samples — and widening covers the runs where it does
    not. `recency_waived` keeps its own counter; rule 14."""
    kept, rejections = validate(
        [_query(filters=SearchFilters(published_after="any"))],
        recency=Recency(24, "two years", samples=(24, 18, 12)),
        today=TODAY,
    )

    assert kept[0].filters.published_after is None
    assert rejections[0].is_recency_waiver
    assert not rejections[0].is_duplicate_query


# -- one bad line costs that line, not the sample ------------------------------

_LONG = " ".join(["word"] * 13)


def test_an_over_long_query_keeps_the_rest_of_the_sample() -> None:
    reply = (
        "CONCEPTS: pooling\nVOCABULARY: PgBouncer\nRECENCY: ANY — stable\n"
        f"QUERY: {_LONG}\nFROM: t\nQUERY: pgbouncer transaction mode\nFROM: t"
    )
    territory = Mapper(FakeClient(FakeReply(reply))).map(BRIEF)

    assert [query.text for query in territory.queries] == ["pgbouncer transaction mode"]
    assert territory.concepts == ["pooling"] and territory.vocabulary == ["PgBouncer"]
    assert "too long" in territory.problem


def test_an_unreadable_recency_line_keeps_the_sample_but_casts_no_vote() -> None:
    reply = "CONCEPTS: pooling\nVOCABULARY: PgBouncer\nRECENCY: Last 12 months\nQUERY: a b\nFROM: t"
    territory = Mapper(FakeClient(FakeReply(reply))).map(BRIEF)

    assert territory.vocabulary == ["PgBouncer"]
    assert [query.text for query in territory.queries] == ["a b"]
    assert territory.recency.samples == ()
    assert territory.recency.months is None
    assert "recency line unreadable" in territory.problem


def test_a_sample_with_nothing_usable_is_still_unusable() -> None:
    territory = Mapper(FakeClient(FakeReply("I'm sorry, I can't help with that."))).map(BRIEF)

    assert not territory
    assert not territory.parsed


class _SequenceClient(FakeClient):
    """One reply per call, in order — three samples that disagree."""

    def __init__(self, replies) -> None:
        super().__init__()
        self._replies = list(replies)

    def complete(self, system, user, *, task=None, max_tokens=None):
        self.tasks.append(task)
        return self._replies[min(len(self.tasks) - 1, len(self._replies) - 1)]


def _sample_reply(months: str) -> FakeReply:
    return FakeReply(
        "CONCEPTS: agent memory\nVOCABULARY: scratchpad\n"
        f"RECENCY: {months} months — the APIs moved\n"
        "QUERY: agent memory tutorial\nFROM: concept: agent memory\nFILTERS: none\n"
    )


def test_widening_does_not_count_the_codes_own_cutoff_as_a_rejection() -> None:
    """Each sample derived its own cutoff onto its queries, and the winner was
    then re-validated against the widest window — where the first sample's
    derived date "excludes more than the reasoned window" and is refused. That
    is the code rejecting its own arithmetic, counted in `filters_rejected` and
    printed as a filter the map proposed that would have excluded content."""
    mapper = Mapper(_SequenceClient([_sample_reply("12"), _sample_reply("24"), _sample_reply("18")]))

    territory = mapper.map(BRIEF)

    assert territory.recency.months == 24
    assert territory.rejected == []
    derived = territory.queries[0].filters.published_after
    assert derived is not None
    assert RECENCY_DERIVED_WHY in territory.queries[0].reasons[FILTER_PUBLISHED_AFTER]


# -- validate is idempotent ----------------------------------------------------
#
# Every query meets `validate` at least twice: where it was proposed (inside
# `Mapper.map`, inside `Planner.plan`) and again before its round runs. It was
# not idempotent: the first pass dropped a waiver's `publishedAfter=any` marker,
# so the second saw an ordinary query and derived the window onto it. Measured
# at checkpoint 2 on 3 of 12 maps, on exactly the queries the map had exempted
# because their material is older than the window.

_WINDOW = Recency(months=12, reason="the fittings changed this year")

_WAIVED = _query(
    "how taps were fitted before",
    filters=SearchFilters(published_after="any", order="date"),
    reasons={FILTER_PUBLISHED_AFTER: "older fittings are only in older videos",
             FILTER_ORDER: "newest first"},
)
_PLAIN = _query("tap washer replacement")
_REFUSED = _query(
    "leaking tap fix",
    filters=SearchFilters(order="loudest", published_after="2031-01-01"),
)
_MAIN = _query("drain snake use")
_VARIANT = _query(
    "drain snake use",
    filters=SearchFilters(video_duration="short"),
    variant_of="drain snake use",
    reasons={FILTER_VIDEO_DURATION: "adds the short clips relevance buries"},
)
_DUPLICATE = _query("Drain  snake USE")

_CASES = {
    "waiver": [_WAIVED],
    "derived": [_PLAIN],
    "refused": [_REFUSED],
    "variant": [_MAIN, _VARIANT],
    "duplicate": [_MAIN, _DUPLICATE],
    "all-together": [_WAIVED, _PLAIN, _REFUSED, _MAIN, _VARIANT, _DUPLICATE],
}


@pytest.mark.parametrize("recency", [_WINDOW, Recency()], ids=["window", "any-time"])
@pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
def test_validating_twice_changes_nothing_and_records_nothing(case, recency) -> None:
    """`validate(validate(x).kept)` is `validate(x)`: the same queries, the same
    filters, the same waivers — and no second record of anything."""
    first, first_rejections = validate(_CASES[case], recency=recency, today=TODAY)
    second, second_rejections = validate(first, recency=recency, today=TODAY)
    third, third_rejections = validate(second, recency=recency, today=TODAY)

    assert second == first
    assert third == first
    assert second_rejections == [] and third_rejections == []


def test_a_variant_whose_main_query_already_ran_is_kept_on_every_pass() -> None:
    """A variant may accompany a main query run earlier — in a previous round,
    or dropped as a duplicate of one. The first pass allows its band; a second
    pass over the kept list must not refuse it for a main query no longer there."""
    already = {key for key in (validate([_MAIN], recency=_WINDOW, today=TODAY)[0][0].key,)}

    first, _ = validate([_MAIN, _VARIANT], recency=_WINDOW, today=TODAY, already_run=already)
    second, second_rejections = validate(first, recency=_WINDOW, today=TODAY, already_run=already)

    assert [query.filters.video_duration for query in first] == ["short"]
    assert second == first
    assert second_rejections == []


def test_a_waiver_survives_any_number_of_passes_and_is_recorded_once(caplog) -> None:
    queries = [_WAIVED, _PLAIN]
    records = []
    with caplog.at_level(logging.INFO, logger="marginalia.brain.territory"):
        for _ in range(5):
            queries, rejections = validate(queries, recency=_WINDOW, today=TODAY)
            records.extend(rejections)

    waived = queries[0]
    assert waived.filters.published_after is None, "no date is ever derived onto it"
    assert waived.filters.order == "date", "its other filters are untouched"
    assert waived.waiver == "older fittings are only in older videos"
    assert queries[1].filters.published_after == "2025-08-21", "the window still applies elsewhere"

    assert [record.field for record in records] == [FILTER_FIELD_RECENCY_WAIVER], "one record"
    messages = [record.getMessage() for record in caplog.records]
    assert sum("waived the" in message for message in messages) == 1
    assert not any(
        "derived publishedAfter" in message and "how taps were fitted" in message
        for message in messages
    ), "no pass claims to have dated the waived query"
    assert sum("derived publishedAfter" in message for message in messages) == 1


def test_a_waiver_never_stays_in_the_filters_sent_to_the_search() -> None:
    """`any` is not a value the search provider accepts, and `filters` is
    exactly what is sent. Every spelling of the marker leaves validate as
    state on the query, on every pass."""
    markers = ["any", "ANY", "any time", "anytime", "all", "none", "off", "unset"]
    queries = [
        _query(f"pipe fitting {number}", filters=SearchFilters(published_after=marker))
        for number, marker in enumerate(markers)
    ]
    for recency in (_WINDOW, Recency()):
        kept = queries
        for _ in range(3):
            kept, _ = validate(kept, recency=recency, today=TODAY)
            assert all(query.filters.published_after is None for query in kept)
            assert all(query.waiver for query in kept)


def test_the_ledger_record_of_a_waived_query_says_so_and_why() -> None:
    (waived,), _ = validate([_WAIVED], recency=_WINDOW, today=TODAY)

    recorded = json.loads(waived.filters_json())

    assert recorded[FILTER_FIELD_RECENCY_WAIVER] == {
        "value": "any",
        "why": "older fittings are only in older videos",
    }
    assert FILTER_PUBLISHED_AFTER not in recorded, "a waiver is not a filter that ran"
    assert recorded[FILTER_ORDER]["value"] == "date"


def test_a_waiver_with_no_reason_still_says_it_was_asked_for() -> None:
    (waived,), rejections = validate(
        [_query("cast iron pipe repair", filters=SearchFilters(published_after="any"))],
        recency=_WINDOW,
        today=TODAY,
    )

    assert waived.waiver == "the query asked for material of any age and gave no reason"
    assert rejections[0].reason == waived.waiver


@pytest.mark.parametrize(
    "query, recency, topic, applies",
    [
        (replace(_PLAIN, waiver="older fittings"), _WINDOW, "", True),
        (replace(_PLAIN, waiver="older fittings"), Recency(), "", False),
        (replace(_PLAIN, waiver="older fittings"), _WINDOW, "the history of plumbing", False),
        (replace(_VARIANT, waiver="older fittings"), _WINDOW, "", False),
        (_PLAIN, _WINDOW, "", False),
    ],
    ids=["window", "any-time", "user-forbade-dates", "variant", "not-waived"],
)
def test_a_waiver_counts_only_when_it_kept_a_window_off(query, recency, topic, applies) -> None:
    """One definition for the record and the round's counter, so they agree.
    A waiver that prevented nothing — no window, a user who already forbade
    dates, a variant that is never dated — is not counted."""
    assert waiver_applies(query, recency=recency, topic=topic) is applies


def test_a_map_waiver_reaches_round_one_without_a_date() -> None:
    """The path checkpoint 2 measured, end to end in `brain/`: `Mapper.map`
    validates its own queries, and the round validates them again with the
    expansion's in front, exactly as `_plan_round` and `_loop` do."""
    reply = (
        "CONCEPTS: tap repair\nVOCABULARY: ceramic cartridge\n"
        "RECENCY: 12 months — the fittings changed this year\n\n"
        "QUERY: compression tap washer replacement\nFROM: part: older taps\n"
        "FILTERS: publishedAfter=any (compression taps are only in older videos)\n\n"
        "QUERY: ceramic cartridge tap repair\nFROM: vocabulary: ceramic cartridge\n"
        "FILTERS: none\n"
    )
    brief = Brief(topic="home plumbing repairs", text="INTENT\nFix taps.", version="1/1")

    territory = Mapper(FakeClient(FakeReply(reply))).map(brief)
    expansion = [PlannedQuery("fix a dripping tap", ORIGIN_BRIEF, brief.topic)]
    round_one, rejections = validate(
        [*expansion, *territory.queries], recency=territory.recency, topic=brief.topic,
        already_run=set(),
    )

    by_text = {query.text: query for query in round_one}
    waived = by_text["compression tap washer replacement"]
    assert waived.filters.published_after is None
    assert waived.waiver == "compression taps are only in older videos"
    assert by_text["ceramic cartridge tap repair"].filters.published_after is not None
    assert [item.field for item in territory.rejected] == [FILTER_FIELD_RECENCY_WAIVER]
    assert not any(item.is_recency_waiver for item in rejections), "recorded once, by the map"
