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
    parse_baseline,
    PLAN_PROMPT,
    Planner,
    apply_revisions,
    parse_plan,
    render_round,
    validate_revisions,
)
from marginalia.brain.territory import validate
from marginalia.contracts import (
    CoverageItem,
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
    title, reason, *, stage="metadata", verdict="no", evidence=EVIDENCE_CLEAR,
    too_long=False,
) -> JudgedVideo:
    return JudgedVideo(
        video_id=title[:11].ljust(11, "x"),
        title=title,
        stage=stage,
        verdict=verdict,
        reason=reason,
        evidence=evidence,
        too_long=too_long,
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
    assert "KEPT SO FAR IN THIS RUN (1)" in rendered and "REJECTED (1)" in rendered


def test_a_long_round_says_how_much_it_did_not_show() -> None:
    """A truncated list read as the whole round makes a pattern look stronger
    than it is — which is exactly the failure mode this phase is guarding."""
    report = _round(rejected=[_video(f"Video {n}", "off topic") for n in range(40)])

    rendered = render_round(report)

    assert "(+15 more, not shown)" in rendered


def test_rejections_made_after_reading_the_transcript_are_shown_first() -> None:
    """The loop appends every stage-1 NO before any transcript is read, so with
    30 of them ahead of 3 stage-2 NOs the cap of 25 showed none of the three —
    the rejections that rest on reading the video. Reordered in the render only:
    `report.rejected` keeps the order the loop wrote it in."""
    metadata = [_video(f"Title only {n}", "off topic") for n in range(30)]
    full = [_video(f"Transcript read {n}", "pooling never discussed", stage="full")
            for n in range(3)]
    report = _round(rejected=[*metadata, *full])

    rendered = render_round(report)
    section = rendered.split("REJECTED (33):", 1)[1].split("\n\n", 1)[0]
    shown = [line for line in section.splitlines() if line.startswith("- ")]

    assert [line.split(" [")[0] for line in shown[:3]] == [
        "- Transcript read 0", "- Transcript read 1", "- Transcript read 2",
    ]
    assert "- Title only 0 [metadata]" in shown[3]
    assert "(+8 more, not shown)" in section
    assert report.rejected == [*metadata, *full], "the report's own order is untouched"


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

    assert "KEPT SO FAR IN THIS RUN (1)" in rendered
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
    # The key is taken from a *validated* query rather than a bare one, because
    # that is the only kind that exists in a run: `seen_queries` is filled from
    # what came back from `validate`, so every key in it already carries the
    # derived date window. A key built by hand without one describes a query
    # this system never runs.
    query = PlannedQuery(text="prompt cache ttl expiry", origin=ORIGIN_PLANNER)
    already, _ = validate([query], recency=MAP.recency, topic=BRIEF.topic)
    plan = Planner(FakeClient(FakeReply(GOOD_PLAN))).plan(
        BRIEF, MAP, _round(), already_run={already[0].key}
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

    assert "KEPT SO FAR IN THIS RUN (1)" in rendered
    assert "KEPT BUT NOT ANSWERABLE (1)" in rendered
    assert "do NOT count toward coverage" in rendered
    # The video is still named. It was found and stored, and hiding it entirely
    # would invite the planner to keep searching for a video it already has.
    assert "Garbled one" in rendered
    kept_block = rendered.split("KEPT SO FAR IN THIS RUN")[1].split("KEPT BUT NOT ANSWERABLE")[0]
    assert "Garbled one" not in kept_block
    assert "Readable one" in kept_block


def test_a_round_with_nothing_degraded_says_none() -> None:
    """The heading is always present, and empty is the normal case. A section
    that appears only when something went wrong teaches the planner to read its
    absence as reassurance, which is not what an absence means."""
    report = _round(kept=[_video("Readable one", "good", verdict="yes")])

    rendered = render_round(report)

    unreadable = rendered.split("KEPT BUT NOT ANSWERABLE")[1].split("REJECTED")[0]
    assert "(0)" in unreadable
    assert "- none" in unreadable


def test_the_planner_prompt_says_an_unreadable_video_satisfies_nothing() -> None:
    """The render splits them; the prompt has to say what the split means.
    Either half alone is a rule enforced by a coincidence."""
    text = " ".join(load_prompt(PLAN_PROMPT, directory=prompts_dir()).text.split())
    assert "KEPT BUT NOT ANSWERABLE" in text
    assert "Only videos under KEPT can satisfy anything" in text


# -- coverage is judged against the run, not the round -------------------------
#
# Run `ea18299bcea5`: round 1 marked "comparison across two or more frameworks"
# covered, citing a video literally titled "Agent Memory Explained: LangChain vs
# CrewAI Approaches Compared". Round 2, shown only its own six keepers, called
# the same item a gap. Round 3 spent five of six queries chasing it.


def test_the_planner_is_shown_every_video_the_run_kept() -> None:
    report = _round(kept=[_video("Round two find", "a new one", verdict="yes")])

    rendered = render_round(
        report,
        kept_so_far=[
            (1, _video("LangChain vs CrewAI compared", "compares two frameworks",
                       verdict="yes")),
            (2, _video("Round two find", "a new one", verdict="yes")),
        ],
    )

    assert "LangChain vs CrewAI compared" in rendered
    assert "Round two find" in rendered
    assert "KEPT SO FAR IN THIS RUN (2)" in rendered


def test_each_kept_video_says_which_round_found_it() -> None:
    """Cumulative without the marking would lose what this round contributed,
    which is the other half of what the planner reasons about."""
    rendered = render_round(
        _round(),
        kept_so_far=[
            (1, _video("Earlier", "kept in round one", verdict="yes")),
            (3, _video("Later", "kept in round three", verdict="yes")),
        ],
    )

    assert "[round 1] Earlier" in rendered
    assert "[round 3] Later" in rendered


def test_an_unreadable_keeper_from_an_earlier_round_stays_out_of_coverage() -> None:
    """The DEGRADED split has to survive going cumulative — otherwise a video
    nobody could read becomes permanent evidence of coverage."""
    rendered = render_round(
        _round(),
        kept_so_far=[
            (1, _video("Garbled", "captions unreadable", verdict="yes",
                       evidence=EVIDENCE_DEGRADED)),
            (2, _video("Clean", "walks through it", verdict="yes")),
        ],
    )

    kept = rendered.split("KEPT SO FAR IN THIS RUN")[1].split("KEPT BUT NOT ANSWERABLE")[0]
    assert "Clean" in kept and "Garbled" not in kept
    assert "KEPT BUT NOT ANSWERABLE (1)" in rendered


def test_rejections_stay_scoped_to_this_round() -> None:
    """Deliberate, and the opposite choice for the opposite reason. Rejections
    are evidence about the judge; accumulating them would make a systematic
    judge error look more and more like a fact about YouTube, which is the one
    failure this whole module is built around."""
    report = _round(rejected=[_video("This round's reject", "off topic")])

    rendered = render_round(
        report, kept_so_far=[(1, _video("Old keeper", "good", verdict="yes"))]
    )

    assert "REJECTED (1)" in rendered
    assert "This round's reject" in rendered


def test_the_oldest_keepers_are_dropped_first_when_the_list_is_capped() -> None:
    """When the cap bites, the round being reasoned about must survive it —
    and the drop is announced, like every other cap here."""
    kept = [(1, _video(f"Old {n}", "kept early", verdict="yes")) for n in range(30)]
    kept.append((4, _video("Newest", "kept just now", verdict="yes")))

    rendered = render_round(_round(number=4), kept_so_far=kept)

    assert "Newest" in rendered
    assert "kept in earlier rounds, not shown" in rendered


def test_without_a_cumulative_set_the_round_renders_alone() -> None:
    """A direct call still produces something coherent rather than an empty
    KEPT section — the fallback the loop never takes."""
    rendered = render_round(_round(kept=[_video("Only", "kept", verdict="yes")]))

    assert "Only" in rendered
    assert "KEPT SO FAR IN THIS RUN (1)" in rendered


# -- the library the planner cannot otherwise see ------------------------------
#
# Gate 1 drops noted videos before they can become candidates, and the fetch-step
# disk check skips stored-but-unnoted ones, so neither reaches `report.kept`.
# "No kept video covers X" has always meant "no video this run newly fetched
# covers X" — while `/ask` reads every note. Measured: a run called
# retrieval-scoring a gap while a note written the previous day held the formula.


def test_the_baseline_is_shown_as_covered_evidence() -> None:
    rendered = render_round(
        _round(),
        baseline=[CoverageItem("retrieval scoring", True, "the ranking video gives the formula")],
    )

    assert "ALREADY IN THE LIBRARY BEFORE THIS RUN (1)" in rendered
    assert "retrieval scoring" in rendered
    assert "the ranking video gives the formula" in rendered


def test_an_unsatisfied_baseline_item_is_not_listed_as_covered() -> None:
    """The section is what the library *has*. Listing a NO there would tell the
    planner something is covered on the strength of an assessment that said the
    opposite."""
    rendered = render_round(
        _round(), baseline=[CoverageItem("benchmarks", False, "nothing on this")]
    )

    block = rendered.split("ALREADY IN THE LIBRARY")[1].split("MENTIONED BUT")[0]
    assert "benchmarks" not in block
    assert "none" in block


def test_no_baseline_says_so_rather_than_nothing() -> None:
    """Printed even when empty, like every other counter here: an absent section
    reads as reassurance, and empty means the library was never assessed."""
    rendered = render_round(_round())

    assert "ALREADY IN THE LIBRARY BEFORE THIS RUN (0)" in rendered


def test_a_baseline_reply_parses_into_coverage() -> None:
    text = (
        "COVERAGE: shows a mechanism in code | YES | the Mem0 walkthrough shows add/search\n"
        "COVERAGE: benchmarks for the memory layer | NO | nothing evaluates one\n"
    )

    baseline = parse_baseline(text, notes_read=42)

    assert baseline.parsed and baseline.notes_read == 42
    assert [(i.part, i.satisfied) for i in baseline.items] == [
        ("shows a mechanism in code", True),
        ("benchmarks for the memory layer", False),
    ]
    assert len(baseline.satisfied) == 1


def test_a_baseline_reply_with_no_coverage_lines_is_unparsed_and_empty() -> None:
    """Empty means "assume the library covers nothing", which leaves the planner
    exactly where it stood before this call existed — more work, never less."""
    baseline = parse_baseline("I could not assess the library.")

    assert baseline.parsed is False
    assert baseline.items == []


def test_a_baseline_ignores_queries_and_recommendations() -> None:
    """This call proposes nothing. A reply carrying a plan has misunderstood the
    task, and acting on it would let an audit start issuing searches."""
    text = (
        "COVERAGE: shows code | YES | the walkthrough\n"
        "QUERY: agent memory benchmarks\n"
        "RECOMMENDATION: STOP - looks covered\n"
    )

    baseline = parse_baseline(text)

    assert [i.part for i in baseline.items] == ["shows code"]


# -- "REVISE: none" is an answer, not a defect ---------------------------------
#
# The prompt asks for a REVISE line, and a round that needs no map change is the
# common case. Refusing it filed "unreadable revision 'none'" into
# `plans.problem` on a run where nothing had gone wrong — and that column is
# what anyone later reads to decide whether the planner is healthy.

def _reply(revise: str) -> str:
    return (
        "COVERAGE: shows configuration | YES | the pgbouncer walkthrough\n"
        "QUERY: pgbouncer pool sizing\n"
        "FROM: gap - sizing\n"
        f"REVISE: {revise}\n"
        "RECOMMENDATION: CONTINUE - one item still open\n"
    )


@pytest.mark.parametrize(
    "revise",
    ["none", "None", "None.", "**none**", "no changes", "no revisions", "n/a", "nothing"],
)
def test_declining_to_revise_the_map_is_not_a_problem(revise: str) -> None:
    plan = parse_plan(_reply(revise), round_number=1)

    assert plan.revisions == []
    assert plan.parsed is True
    assert plan.problem == ""


def test_a_real_revision_still_parses() -> None:
    """The fix must not swallow the case the line exists for."""
    plan = parse_plan(_reply("add vocabulary: PgCat"), round_number=1)

    assert [(r.action, r.kind, r.term) for r in plan.revisions] == [
        (REVISE_ADD, REVISE_VOCABULARY, "PgCat")
    ]
    assert plan.problem == ""


def test_a_genuinely_unreadable_revision_is_still_reported() -> None:
    """Accepting "none" must not become accepting anything. A line that meant to
    revise and could not be read is still a defect worth recording."""
    plan = parse_plan(_reply("frobnicate the map"), round_number=1)

    assert plan.revisions == []
    assert "unreadable revision" in plan.problem


def test_no_revise_line_at_all_is_also_silent() -> None:
    """Omitting the line and writing "none" mean the same thing, and neither is
    a problem — otherwise the fix would only move the noise."""
    reply = (
        "COVERAGE: shows configuration | YES | the walkthrough\n"
        "QUERY: pgbouncer pool sizing\n"
        "RECOMMENDATION: CONTINUE - one item still open\n"
    )

    plan = parse_plan(reply, round_number=1)

    assert plan.revisions == []
    assert plan.problem == ""


def test_a_no_changes_preamble_never_swallows_a_real_revision() -> None:
    """Matching is on the whole normalised body, never a prefix.

    `no changes to concepts, add vocabulary: PgCat` starts with words that mean
    "nothing to do" and ends with a revision. A `startswith` check would drop
    the revision in silence — the one outcome worse than dropping it loudly.
    """
    plan = parse_plan(_reply("no changes to concepts, add vocabulary: PgCat"), round_number=1)

    assert plan.revisions == [] , "ambiguous, so nothing is applied"
    assert "unreadable revision" in plan.problem, "and it is reported, not silent"


def test_a_bare_hyphen_is_reported_rather_than_read_as_no_changes() -> None:
    """A model writing one hyphen to mean "no changes" is rare. A model writing
    a bulleted revision is not, so the hyphen must not carry that meaning."""
    plan = parse_plan(_reply("-"), round_number=1)

    assert plan.revisions == []
    assert "unreadable revision" in plan.problem


@pytest.mark.parametrize("bullet", ["-", "*", "•", "- ", "*  "])
def test_a_bulleted_revision_still_parses(bullet: str) -> None:
    """The case the hyphen was removed to protect. Losing this revision would be
    a widening the map never gets, reported only as a log line."""
    plan = parse_plan(_reply(f"{bullet}add vocabulary: PgCat"), round_number=1)

    assert [(r.action, r.kind, r.term) for r in plan.revisions] == [
        (REVISE_ADD, REVISE_VOCABULARY, "PgCat")
    ]
    assert plan.problem == ""


def test_a_video_too_long_to_note_is_kept_out_of_coverage() -> None:
    """Different cause from `DEGRADED`, identical consequence. Four of one run's
    24 keepers were past the note ceiling: stored, judged YES, counted as
    coverage, and unreachable by `/ask` because they would never be noted."""
    rendered = render_round(
        _round(),
        kept_so_far=[
            (1, _video("A seven-hour course", "covers the whole path", verdict="yes",
                       too_long=True)),
            (1, _video("A normal video", "covers it too", verdict="yes")),
        ],
    )

    kept = rendered.split("KEPT SO FAR IN THIS RUN")[1].split("KEPT BUT NOT ANSWERABLE")[0]
    assert "A normal video" in kept
    assert "A seven-hour course" not in kept
    assert "KEPT BUT NOT ANSWERABLE (1)" in rendered
    assert "length ceiling" in rendered


def test_the_two_reasons_a_keeper_is_unanswerable_are_both_named() -> None:
    """One heading, both causes, because the planner's action is the same for
    each — but the counters stay apart, per rule 14."""
    rendered = render_round(
        _round(),
        kept_so_far=[
            (1, _video("Garbled", "captions unreadable", verdict="yes",
                       evidence=EVIDENCE_DEGRADED)),
            (1, _video("Enormous", "past the ceiling", verdict="yes", too_long=True)),
        ],
    )

    assert "KEPT BUT NOT ANSWERABLE (2)" in rendered
    assert "too damaged to read" in rendered and "length ceiling" in rendered
