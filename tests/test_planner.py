"""The round planner: parsing, the revision validator, and the anchoring rule.

Three surfaces, and they are not equally important.

Parsing is routine. The revision validator is a **safety** surface for the same
reason B4's filter validator is: removing a term from the map narrows every
later search, and a class of video that stops being searched for is one nobody
notices the absence of.

The third is the one this phase exists to get right, and it is not enforceable
by code at all. Rejection reasons come from another model's judgement. A planner
that reads a consistent rejection pattern and redirects the search around it is
learning from a bug and calling it adaptation — the historical case is a judge
with a duration bias, where twelve of twenty-five rejections cited runtime, and
a planner acting on that would have blinded the library to short videos
permanently. `test_a_poisoned_log_does_not_redirect_the_search` is the *code*
half of that defence; the prompt half is measured against a real model and
reported in PLAN.md, because a rule about intent cannot be unit-tested.
"""

from __future__ import annotations

import logging

import pytest

from marginalia.brain.planner import (
    PLAN_PROMPT,
    Planner,
    apply_revisions,
    parse_plan,
    render_round,
    validate_revisions,
)
from marginalia.contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    ORIGIN_PLANNER,
    RECOMMEND_CONTINUE,
    RECOMMEND_STOP,
    REVISE_ADD,
    REVISE_REMOVE,
    REVISE_VOCABULARY,
    Brief,
    JudgedVideo,
    PlannedQuery,
    Recency,
    RoundReport,
    SearchFilters,
    TerritoryMap,
    TerritoryRevision,
)
from marginalia.llm import load_prompt, prompts_dir

BRIEF = Brief(
    topic="prompt caching",
    text="INTENT\nHow prompt caching works.\n\nMUST CONTAIN\n- shows code\n- covers expiry",
    version="1/1",
)

MAP = TerritoryMap(
    topic="prompt caching",
    version="1",
    concepts=["prefix matching", "cache expiry"],
    vocabulary=["cache_control", "semantic cache"],
    recency=Recency(months=6, reason="pricing moved this year"),
)

GOOD_PLAN = """
COVERAGE: shows code | YES | two kept videos walk through cache_control
COVERAGE: covers expiry | NO | nothing kept mentions expiry at all

REVISE: add vocabulary: TTL
WHY: three kept videos use this term and the map does not have it

QUERY: prompt cache ttl expiry
FROM: gap - cache expiry
FILTERS: none

QUERY: anthropic caching compared
FROM: gap - provider comparison
FILTERS: order=date (provider behaviour changed this year)

RECOMMENDATION: CONTINUE - one MUST CONTAIN item still has nothing against it
"""


def _round(number=1, **kwargs) -> RoundReport:
    report = RoundReport(number=number, queries=kwargs.pop("queries", ["a query"]))
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


def _video(
    title, reason, *, stage="metadata", verdict="no", evidence=EVIDENCE_CLEAR
) -> JudgedVideo:
    return JudgedVideo(
        video_id=title[:11].ljust(11, "x"),
        title=title,
        stage=stage,
        verdict=verdict,
        reason=reason,
        evidence=evidence,
    )


# -- parsing ------------------------------------------------------------------


def test_a_well_formed_plan_is_read_whole() -> None:
    plan = parse_plan(GOOD_PLAN, round_number=1)

    assert plan.parsed, plan.problem
    assert [item.part for item in plan.coverage] == ["shows code", "covers expiry"]
    assert [item.satisfied for item in plan.coverage] == [True, False]
    assert [query.text for query in plan.queries] == [
        "prompt cache ttl expiry",
        "anthropic caching compared",
    ]
    assert plan.recommendation == RECOMMEND_CONTINUE


def test_every_planner_query_is_marked_as_the_planner_s() -> None:
    """`queries.origin` is what makes "did the planner change anything?"
    answerable from the ledger rather than from an impression."""
    plan = parse_plan(GOOD_PLAN, round_number=1)

    assert all(query.origin == ORIGIN_PLANNER for query in plan.queries)
    assert plan.queries[0].origin_detail == "gap - cache expiry"


def test_gaps_and_satisfied_are_both_available() -> None:
    plan = parse_plan(GOOD_PLAN, round_number=1)

    assert [item.part for item in plan.gaps] == ["covers expiry"]
    assert [item.part for item in plan.satisfied] == ["shows code"]


def test_a_filter_on_a_planner_query_is_read_with_its_reason() -> None:
    plan = parse_plan(GOOD_PLAN, round_number=1)

    assert plan.queries[1].filters.order == "date"
    assert "changed this year" in plan.queries[1].reasons["order"]


def test_a_stop_recommendation_is_read_with_its_reason() -> None:
    plan = parse_plan(
        "COVERAGE: a | YES | done\nRECOMMENDATION: STOP - every MUST CONTAIN item is covered",
        round_number=2,
    )

    assert plan.recommendation == RECOMMEND_STOP
    assert plan.wants_to_stop
    assert "every MUST CONTAIN" in plan.reason


def test_markdown_decoration_survives() -> None:
    plan = parse_plan(
        "**COVERAGE:** shows code | **YES** | fine\n"
        "**QUERY:** cache ttl expiry\n**FROM:** gap\n"
        "**RECOMMENDATION:** **CONTINUE** - more to find",
        round_number=1,
    )

    assert plan.coverage[0].satisfied
    assert plan.queries[0].text == "cache ttl expiry"
    assert plan.recommendation == RECOMMEND_CONTINUE


def test_a_coverage_line_with_no_verdict_is_a_problem_not_a_guess() -> None:
    """Guessing `satisfied` either way invents the one claim the line exists to
    make, and the report prints that claim as though it had been assessed."""
    plan = parse_plan(
        "COVERAGE: shows code | probably fine\nRECOMMENDATION: CONTINUE - x", round_number=1
    )

    assert not plan.coverage
    assert "unreadable coverage line" in plan.problem


def test_a_reply_with_no_coverage_is_marked_unparsed() -> None:
    plan = parse_plan("QUERY: a b c\nFROM: t\nRECOMMENDATION: CONTINUE - x", round_number=1)

    assert not plan.parsed
    assert "no coverage assessment" in plan.problem


def test_continuing_without_proposing_anything_is_noticed() -> None:
    plan = parse_plan("COVERAGE: a | NO | missing\nRECOMMENDATION: CONTINUE - x", round_number=1)

    assert not plan.parsed
    assert "without proposing a query" in plan.problem


def test_an_unreadable_reply_never_stops_the_loop() -> None:
    """The inclusion bias, at the loop level. A parser bug that recommended
    stopping would end runs early and shrink libraries invisibly, which is the
    same asymmetry every other parser here resolves the same way."""
    plan = parse_plan("I'm sorry, I can't help with that.", round_number=1)

    assert not plan.parsed
    assert plan.recommendation == RECOMMEND_CONTINUE
    assert not plan.wants_to_stop
    assert not plan.queries


def test_too_many_queries_are_capped(caplog: pytest.LogCaptureFixture) -> None:
    blocks = "\n".join(f"QUERY: query number {n}\nFROM: gap\n" for n in range(30))
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.planner"):
        plan = parse_plan(f"COVERAGE: a | NO | x\n{blocks}", round_number=1)

    assert len(plan.queries) == 12
    assert any("30 queries proposed" in record.getMessage() for record in caplog.records)


# -- revisions: additions are free ---------------------------------------------


def test_an_addition_needs_no_evidence() -> None:
    """It widens the search. The worst case is one query whose results are
    visible, which is not a cost worth a gate."""
    revision = TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "TTL", "kept videos use it")

    kept, refused = validate_revisions([revision], territory=MAP)

    assert kept == [revision]
    assert not refused


def test_adding_a_term_the_map_already_holds_is_refused() -> None:
    revision = TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "cache_control", "seen a lot")

    kept, refused = validate_revisions([revision], territory=MAP)

    assert not kept
    assert "already holds" in refused[0][1]


# -- revisions: removals are the excluding direction ---------------------------


def test_a_removal_without_evidence_is_refused() -> None:
    """"This term feels off topic" is an impression. A term removed on one is a
    class of video that stops being searched for, and nothing downstream can
    notice the absence."""
    revision = TerritoryRevision(
        REVISE_REMOVE, REVISE_VOCABULARY, "semantic cache", "feels off topic"
    )

    kept, refused = validate_revisions(
        [revision], territory=MAP, queries_run=["prompt caching vs semantic caching"]
    )

    assert not kept
    assert "must name the queries" in refused[0][1]


def test_a_removal_whose_evidence_names_no_real_query_is_refused() -> None:
    """The check is mechanical on purpose: it cannot know whether reasoning is
    sound, but it can know whether the claim is about searches that happened."""
    revision = TerritoryRevision(
        REVISE_REMOVE,
        REVISE_VOCABULARY,
        "semantic cache",
        "off topic",
        evidence="a search I did not run returned nothing useful",
    )

    kept, refused = validate_revisions(
        [revision], territory=MAP, queries_run=["prompt caching vs semantic caching"]
    )

    assert not kept
    assert "names no query that actually ran" in refused[0][1]


def test_a_removal_with_real_evidence_is_accepted() -> None:
    revision = TerritoryRevision(
        REVISE_REMOVE,
        REVISE_VOCABULARY,
        "semantic cache",
        "different subject entirely",
        evidence="prompt caching vs semantic caching returned 10 results, all rejected",
    )

    kept, refused = validate_revisions(
        [revision], territory=MAP, queries_run=["prompt caching vs semantic caching"]
    )

    assert kept == [revision]
    assert not refused


def test_removing_a_term_the_map_does_not_hold_is_refused() -> None:
    revision = TerritoryRevision(
        REVISE_REMOVE, REVISE_VOCABULARY, "kv cache", "unhelpful", evidence="a query"
    )

    kept, refused = validate_revisions([revision], territory=MAP, queries_run=["a query"])

    assert not kept
    assert "holds no vocabulary by that name" in refused[0][1]


def test_removals_are_capped_and_additions_are_not() -> None:
    """A single bad round must not be able to gut a map. Three is enough for a
    real correction and too few for a collapse — and the cap applies only in the
    narrowing direction, because that is the only one that loses anything."""
    territory = TerritoryMap(
        topic="t", version="1", vocabulary=["a", "b", "c", "d", "e"]
    )
    removals = [
        TerritoryRevision(REVISE_REMOVE, REVISE_VOCABULARY, term, "why", evidence="a query")
        for term in ("a", "b", "c", "d", "e")
    ]
    additions = [
        TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, term, "why")
        for term in ("f", "g", "h", "i", "j", "k")
    ]

    kept, refused = validate_revisions(
        [*removals, *additions], territory=territory, queries_run=["a query"]
    )

    assert sum(1 for revision in kept if revision.removes) == 3
    assert sum(1 for revision in kept if not revision.removes) == 6
    assert all("more than 3 removals" in why for _, why in refused)


def test_a_refusal_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.planner"):
        validate_revisions(
            [TerritoryRevision(REVISE_REMOVE, REVISE_VOCABULARY, "semantic cache", "no")],
            territory=MAP,
        )

    assert any("refused revision" in record.getMessage() for record in caplog.records)


# -- applying a revision -------------------------------------------------------


def test_a_revision_creates_a_new_version_and_keeps_the_old_one() -> None:
    """Both versions stay on the run. A change you cannot see the before of is
    a change nobody can judge, which is the argument that put the map on the
    run in the first place."""
    revised = apply_revisions(
        MAP,
        [
            TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "TTL", "used a lot"),
            TerritoryRevision(
                REVISE_REMOVE, REVISE_VOCABULARY, "semantic cache", "off topic", evidence="q"
            ),
        ],
    )

    assert revised.revision == 2
    assert revised.label == "1/v2"
    assert "TTL" in revised.vocabulary
    assert "semantic cache" not in revised.vocabulary
    assert revised.history[0].vocabulary == ["cache_control", "semantic cache"]
    assert len(revised.revisions) == 2


def test_the_diff_and_the_reason_survive_into_the_stored_map() -> None:
    import json

    revised = apply_revisions(
        MAP, [TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "TTL", "kept videos use it")]
    )
    stored = json.loads(revised.as_json())

    assert stored["revision"] == 2
    assert stored["revisions"][0]["term"] == "TTL"
    assert stored["revisions"][0]["reason"] == "kept videos use it"
    assert stored["history"][0]["vocabulary"] == ["cache_control", "semantic cache"]


def test_no_revisions_means_no_new_version() -> None:
    """A version bump with nothing behind it is a diff nobody can read."""
    assert apply_revisions(MAP, []) is MAP


# -- what the planner is shown -------------------------------------------------


def test_the_planner_sees_what_was_kept_as_well_as_what_was_rejected() -> None:
    """A planner given only failures only ever learns to avoid. Coverage is a
    claim about what the library now holds, and it cannot be made from
    rejections."""
    report = _round(
        kept=[_video("Caching deep dive", "walks through cache_control", verdict="yes")],
        rejected=[_video("Some promo", "vendor marketing")],
    )

    rendered = render_round(report)

    assert "Caching deep dive" in rendered
    assert "Some promo" in rendered
    assert "KEPT (1)" in rendered and "REJECTED (1)" in rendered


def test_a_long_round_says_how_much_it_did_not_show() -> None:
    """A truncated list read as the whole round makes a pattern look stronger
    than it is — which is exactly the failure mode this phase is guarding."""
    report = _round(rejected=[_video(f"Video {n}", "off topic") for n in range(40)])

    rendered = render_round(report)

    assert "(+15 more, not shown)" in rendered


def test_the_evidence_carries_no_transcript_text() -> None:
    """Hard rule 1, structurally: `JudgedVideo` has a title and a reason and
    nowhere to put anything larger."""
    report = _round(kept=[_video("A video", "the transcript established X", verdict="yes")])

    rendered = render_round(report)

    assert "the transcript established X" in rendered
    assert len(rendered) < 4000


def test_quota_is_shown_when_there_is_a_ceiling() -> None:
    assert "SEARCH QUOTA REMAINING: 400" in render_round(_round(), quota_remaining=400)
    assert "QUOTA" not in render_round(_round())


# -- the anchoring rule, in code -----------------------------------------------


POISONED = _round(
    kept=[_video("A long talk", "covers cache_control in depth", verdict="yes")],
    rejected=[
        _video(f"Short clip {n}", "too short to contain real implementation detail")
        for n in range(20)
    ]
    + [_video(f"Other {n}", "different subject") for n in range(5)],
)


def test_a_poisoned_log_cannot_produce_a_duration_filter() -> None:
    """The structural half of the anchoring defence.

    The prompt asks the model not to redirect around a rejection pattern, and
    that is measured against a real model rather than asserted here. What *is*
    assertable is the worst case: even a planner that decided short videos were
    the problem cannot act on it, because a planner query is a main query by
    construction and B4's validator refuses `videoDuration` on one.
    """
    plan = parse_plan(
        "COVERAGE: shows code | NO | nothing\n"
        "QUERY: prompt caching deep dive\n"
        "FROM: the short videos keep getting rejected\n"
        "FILTERS: videoDuration=long (stop surfacing short clips)\n"
        "RECOMMENDATION: CONTINUE - x",
        round_number=1,
    )

    from marginalia.brain.territory import validate

    kept, rejected = validate(plan.queries, recency=MAP.recency, topic=BRIEF.topic)

    assert kept[0].filters.video_duration is None, "the band must never survive"
    assert kept[0].text == "prompt caching deep dive", "the query itself still runs"
    assert rejected[0].field == "videoDuration"


def test_the_planner_is_shown_the_kept_video_alongside_the_pattern() -> None:
    """The evidence half. Twenty rejections citing duration is only misleading
    if the round looks like nothing but rejections."""
    rendered = render_round(POISONED)

    assert "KEPT (1)" in rendered
    assert "covers cache_control in depth" in rendered


# -- the prompt ----------------------------------------------------------------


def test_the_prompt_is_versioned_and_renders() -> None:
    prompt = load_prompt(PLAN_PROMPT, directory=prompts_dir())

    assert prompt.version
    rendered = prompt.render(
        brief="B", territory="T", round="R", rounds_left=2, count=6
    )
    assert "{{" not in rendered


def test_the_prompt_names_no_model_family() -> None:
    text = load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.lower()

    for family in ("claude", "gpt-", "gemini", "llama", "kimi", "deepseek", "qwen"):
        assert family not in text


def test_the_prompt_states_the_anchoring_rule() -> None:
    """The rule that carries this phase, and it goes in as a rule with the
    historical failure attached — a note would not survive a rewrite."""
    text = " ".join(load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.split())

    assert "The brief is the authority" in text
    assert "It does not say what exists on YouTube" in text
    assert "twelve of twenty-five rejections in one round cited runtime" in text
    assert "Consistency is what a systematic error looks like from the outside" in text


def test_the_prompt_forbids_the_duration_filter_to_the_planner() -> None:
    text = " ".join(load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.split())

    assert "videoDuration` is not available to you" in text


def test_the_prompt_demands_evidence_for_a_removal() -> None:
    text = " ".join(load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.split())

    assert "Additions are cheap" in text
    assert "A removal without that evidence will be refused" in text


# -- the planner ----------------------------------------------------------------


class FakeReply:
    def __init__(self, text: str, finish_reason: str = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.usage = type("U", (), {"input_tokens": 900, "output_tokens": 300})()
        self.latency_s = 2.5


class FakeClient:
    def __init__(self, reply=None, raises=None) -> None:
        self._reply = reply
        self._raises = raises
        self.tasks: list[str] = []
        self.prompts: list[str] = []

    def complete(self, system, user, *, task=None, max_tokens=None):
        self.tasks.append(task)
        self.prompts.append(user)
        if self._raises is not None:
            raise self._raises
        return self._reply


def test_the_planner_returns_a_validated_plan() -> None:
    plan = Planner(FakeClient(FakeReply(GOOD_PLAN))).plan(BRIEF, MAP, _round())

    assert [query.text for query in plan.queries] == [
        "prompt cache ttl expiry",
        "anthropic caching compared",
    ]
    assert plan.revisions[0].term == "TTL"
    assert plan.input_tokens == 900
    assert plan.latency_s == 2.5


def test_a_failed_planning_call_leaves_the_round_as_it_was() -> None:
    """The loop still has expansion, threads and every budget ceiling. A new
    and unproven step must not be able to kill a loop that worked without it."""
    plan = Planner(FakeClient(raises=RuntimeError("boom"))).plan(BRIEF, MAP, _round())

    assert not plan.parsed
    assert not plan.queries
    assert not plan.wants_to_stop
    assert "boom" in plan.problem


def test_a_truncated_reply_is_not_parsed_at_all() -> None:
    """A half-read coverage assessment is worse than none: it reads as a
    complete account of what the brief is missing."""
    plan = Planner(FakeClient(FakeReply(GOOD_PLAN, finish_reason="length"))).plan(
        BRIEF, MAP, _round()
    )

    assert not plan.parsed
    assert not plan.coverage
    assert "token ceiling" in plan.problem


def test_the_planning_call_names_its_own_task() -> None:
    client = FakeClient(FakeReply(GOOD_PLAN))
    Planner(client).plan(BRIEF, MAP, _round())

    assert client.tasks == ["plan_round"]


def test_a_query_already_run_is_dropped() -> None:
    query = PlannedQuery(text="prompt cache ttl expiry", origin=ORIGIN_PLANNER)
    plan = Planner(FakeClient(FakeReply(GOOD_PLAN))).plan(
        BRIEF, MAP, _round(), already_run={query.key}
    )

    assert [item.text for item in plan.queries] == ["anthropic caching compared"]
    assert plan.rejected_filters[0].field == "query"


def test_the_planner_sees_the_brief_the_map_and_the_round() -> None:
    client = FakeClient(FakeReply(GOOD_PLAN))
    Planner(client).plan(
        BRIEF,
        MAP,
        _round(kept=[_video("A kept video", "good", verdict="yes")]),
        searched=[(PlannedQuery("a query", ORIGIN_PLANNER), 7)],
    )

    sent = client.prompts[0]
    assert "How prompt caching works" in sent
    assert "cache_control" in sent
    assert "A kept video" in sent
    assert "a query — 7 results" in sent


def test_a_removal_proposed_against_a_query_that_ran_is_accepted_end_to_end() -> None:
    reply = (
        "COVERAGE: shows code | NO | nothing\n"
        "REVISE: remove vocabulary: semantic cache\n"
        "WHY: a different subject entirely\n"
        "EVIDENCE: prompt caching vs semantic caching returned 10 results, all rejected\n"
        "QUERY: prompt cache ttl\nFROM: gap\nFILTERS: none\n"
        "RECOMMENDATION: CONTINUE - x"
    )
    plan = Planner(FakeClient(FakeReply(reply))).plan(
        BRIEF,
        MAP,
        _round(),
        searched=[(PlannedQuery("prompt caching vs semantic caching", ORIGIN_PLANNER), 10)],
    )

    assert [revision.term for revision in plan.revisions] == ["semantic cache"]
    assert not plan.refused_revisions


# -- a degraded keeper is not coverage ---------------------------------------


def test_an_unreadable_keeper_is_listed_apart_from_the_keepers() -> None:
    """`DEGRADED` means stage 2 could not read the transcript. The video is
    stored and gets no note, so nothing will ever be answered from it — and a
    planner reading it inside KEPT would count it as coverage and stop looking
    for the video that would actually have covered that part of the brief.

    Split rather than annotated: an adjective inside a list is something a model
    may or may not weigh, and a heading that says *these do not count* is not.
    """
    report = _round(
        kept=[
            _video("Readable one", "walks through the config", verdict="yes"),
            _video(
                "Garbled one",
                "captions are unreadable",
                verdict="yes",
                evidence=EVIDENCE_DEGRADED,
            ),
        ]
    )

    rendered = render_round(report)

    assert "KEPT (1):" in rendered
    assert "KEPT BUT UNREADABLE (1)" in rendered
    assert "do NOT count toward coverage" in rendered
    # The video is still named. It was found and stored, and hiding it entirely
    # would invite the planner to keep searching for a video it already has.
    assert "Garbled one" in rendered
    kept_block = rendered.split("KEPT (1):")[1].split("KEPT BUT UNREADABLE")[0]
    assert "Garbled one" not in kept_block
    assert "Readable one" in kept_block


def test_a_round_with_nothing_degraded_says_none() -> None:
    """The heading is always present, and empty is the normal case. A section
    that appears only when something went wrong teaches the planner to read its
    absence as reassurance, which is not what an absence means."""
    report = _round(kept=[_video("Readable one", "good", verdict="yes")])

    rendered = render_round(report)

    unreadable = rendered.split("KEPT BUT UNREADABLE")[1].split("REJECTED")[0]
    assert "(0)" in unreadable
    assert "- none" in unreadable


def test_the_planner_prompt_says_an_unreadable_video_satisfies_nothing() -> None:
    """The render splits them; the prompt has to say what the split means.
    Either half alone is a rule enforced by a coincidence."""
    text = " ".join(load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.split())
    assert "KEPT BUT UNREADABLE" in text
    assert "Only videos under KEPT can satisfy anything" in text
