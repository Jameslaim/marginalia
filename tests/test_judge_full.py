"""Stage 2's parser, and the contract it enforces.

The five-line format is a guarantee, not a request. Every rule here exists
because a reply that breaks it is *usable-looking* — it parses into something,
and that something is wrong in a direction nobody notices. `/ask` once reported
2 citations out of 104 for exactly this reason: a parser that silently drops
what it cannot read looks like a system with nothing to read.

So a violation is a parse failure — counted, logged, surfaced — and every
resolution points at keeping the video, because a parser bug that resolves to NO
deletes videos from a library nobody will ever know were missing.
"""

from __future__ import annotations

import logging

import pytest

from marginalia.brain.judge import Judge, parse_full
from marginalia.contracts import JudgedVideo
from marginalia.llm import LlmClient
from marginalia.contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    RELEVANT_UNAVAILABLE,
    VERDICT_NO,
    VERDICT_MAYBE,
    VERDICT_YES,
)

VIDEO = "dQw4w9WgXcQ"


def reply(**overrides: str) -> str:
    lines = {
        "VERDICT": "YES",
        "EVIDENCE": "CLEAR",
        "REASON": "The transcript walks through cache_control breakpoints and the five-minute TTL.",
        "RELEVANT": "00:04:20-00:09:15",
        "UNEXPLAINED": "kv cache eviction",
    }
    lines.update(overrides)
    return "\n".join(f"{label}: {body}" for label, body in lines.items())


# -- the well-formed case ---------------------------------------------------


def test_a_clean_reply_parses_into_every_field() -> None:
    verdict = parse_full(reply(RELEVANT="00:04:20-00:09:15; 00:22:00-00:24:30"), VIDEO)

    assert verdict.parsed is True
    assert verdict.problem == ""
    assert verdict.video_id == VIDEO
    assert verdict.verdict == VERDICT_YES
    assert verdict.evidence == EVIDENCE_CLEAR
    assert verdict.relevant == ["00:04:20-00:09:15", "00:22:00-00:24:30"]
    assert verdict.concepts == ["kv cache eviction"]


def test_a_clean_no_parses() -> None:
    verdict = parse_full(
        reply(VERDICT="NO", REASON="Ninety minutes of vendor pricing with no mechanism described.",
              RELEVANT="NONE", UNEXPLAINED="NONE"),
        VIDEO,
    )

    assert verdict.parsed is True
    assert verdict.verdict == VERDICT_NO
    assert verdict.relevant == []
    assert verdict.concepts == []


@pytest.mark.parametrize(
    "body,expected",
    [
        ("00:04:20-00:09:15", ["00:04:20-00:09:15"]),
        ("4:20-9:15", ["4:20-9:15"]),
        ("00:04:20 - 00:09:15", ["00:04:20-00:09:15"]),
        ("00:04:20–00:09:15", ["00:04:20-00:09:15"]),
        ("00:00:00-00:01:00, 00:05:00-00:06:00", ["00:00:00-00:01:00", "00:05:00-00:06:00"]),
    ],
)
def test_the_shapes_a_model_reaches_for(body: str, expected: list[str]) -> None:
    """A comma where the format said semicolon, an en dash, a missing hour
    field. None of these is a broken judgement, and refusing them would throw
    away a good verdict over punctuation."""
    assert parse_full(reply(RELEVANT=body), VIDEO).relevant == expected


def test_a_maybe_is_kept_as_maybe_and_is_not_coverage() -> None:
    """A MAYBE used to resolve to YES — "there is no stage 3, so it has nowhere
    to go". It has somewhere now: kept, noted, and not coverage. Resolving it to
    YES is the stage-2 error rule 8 says hides — the planner counted an unsure
    reading as a satisfied part of the brief. It is still a judgement (the judge
    read the video), so the next run does not ask again."""
    verdict = parse_full(reply(VERDICT="MAYBE"), VIDEO)

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.judged is True
    assert verdict.said == VERDICT_MAYBE
    shown = JudgedVideo(VIDEO, "t", "full", verdict.verdict, verdict.reason, verdict.evidence)
    assert shown.covers is False


def test_maybe_is_flagged_as_breaking_the_format() -> None:
    """The format allows YES or NO. A MAYBE is the reply breaking it, and is
    counted in `unparsed` (rule 14)."""
    verdict = parse_full(reply(VERDICT="MAYBE"), VIDEO)

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.parsed is False
    assert "VERDICT MAYBE is not allowed at stage 2" in verdict.problem


def test_flagging_a_maybe_changes_nothing_but_the_verdict_and_the_flag() -> None:
    """Same evidence, reason, ranges and concepts as the YES it used to be
    indistinguishable from — only the verdict, `parsed` and `problem` differ."""
    maybe = parse_full(reply(VERDICT="MAYBE"), VIDEO)
    yes = parse_full(reply(VERDICT="YES"), VIDEO)

    for field in ("evidence", "reason", "relevant", "concepts",
                  "relevant_text", "unmarked_currency"):
        assert getattr(maybe, field) == getattr(yes, field), field
    assert (yes.verdict, maybe.verdict) == (VERDICT_YES, VERDICT_MAYBE)
    assert (yes.parsed, maybe.parsed) == (True, False)


def test_a_maybe_keeps_its_other_problems() -> None:
    """Problems combine; the MAYBE flag does not hide the other rules."""
    verdict = parse_full(reply(VERDICT="MAYBE", RELEVANT="somewhere in the middle"), VIDEO)

    assert "VERDICT MAYBE" in verdict.problem
    assert "relevant ranges were neither" in verdict.problem


def test_unavailable_is_not_the_same_absence_as_none() -> None:
    """NONE means nothing worth pointing at. UNAVAILABLE means there is, and the
    clock cannot be trusted. Collapsing them would make a video with unusable
    captions look like a video with nothing in it."""
    unavailable = parse_full(reply(RELEVANT="UNAVAILABLE"), VIDEO)
    none = parse_full(reply(RELEVANT="NONE", VERDICT="NO"), VIDEO)

    assert unavailable.relevant == [RELEVANT_UNAVAILABLE]
    assert unavailable.timestamps_unavailable is True
    assert none.relevant == []
    assert none.timestamps_unavailable is False


def test_unexplained_is_capped_at_five() -> None:
    verdict = parse_full(reply(UNEXPLAINED="one, two, three, four, five, six, seven"), VIDEO)
    assert len(verdict.concepts) == 5


# -- rule: NO must carry CLEAR ----------------------------------------------


def test_a_no_on_degraded_evidence_is_a_violation() -> None:
    """You cannot confidently reject evidence you could not read. NO is
    permanent — Gate 2 treats every past NO as final — so a rejection resting on
    captions the judge admits it could not read is a claim about captions
    wearing the costume of a claim about content."""
    verdict = parse_full(
        reply(VERDICT="NO", EVIDENCE="DEGRADED",
              REASON="The transcript is largely unintelligible so relevance could not be checked.",
              RELEVANT="UNAVAILABLE"),
        VIDEO,
    )

    assert verdict.parsed is False
    assert "rejected on degraded evidence" in verdict.problem


def test_a_no_on_degraded_evidence_keeps_the_video() -> None:
    """And honours the DEGRADED claim. The model said it could not read the
    transcript; believing that costs a note, which is cheap and reversible.
    Overriding it to CLEAR would write a note from text known to be noise."""
    verdict = parse_full(
        reply(VERDICT="NO", EVIDENCE="DEGRADED",
              REASON="Captions are machine-translated noise throughout.",
              RELEVANT="UNAVAILABLE"),
        VIDEO,
    )

    # Kept as MAYBE, not YES: nobody read the content, so it is neither a
    # rejection nor coverage. It was stored as `yes`, so the ledger claimed the
    # judge accepted a video it had refused; `said` keeps the judge's word.
    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.said == VERDICT_NO
    assert verdict.evidence == EVIDENCE_DEGRADED
    assert verdict.judged is True


# -- rule: DEGRADED is about evidence, not uncertainty ----------------------


def test_degraded_must_blame_the_evidence() -> None:
    """Ordinary uncertainty about a readable video is a judgement stage 2 is
    equipped to make, and its answer is YES. A DEGRADED that talks about
    relevance is that judgement, declining to be made."""
    verdict = parse_full(
        reply(EVIDENCE="DEGRADED", REASON="It is hard to say whether this really matches the brief."),
        VIDEO,
    )

    assert verdict.parsed is False
    assert "degraded without naming an evidence problem" in verdict.problem


def test_an_unsupported_degraded_does_not_take_the_video_out_of_the_library() -> None:
    """DEGRADED is the one label that removes a video: `wanted_for_a_note`
    refuses it a note for every topic. A DEGRADED its own reason does not back
    was counted as a violation and then honoured anyway, so a format slip
    removed the video. It now resolves to CLEAR, toward keeping."""
    verdict = parse_full(
        reply(EVIDENCE="DEGRADED", REASON="It is hard to say whether this really matches the brief."),
        VIDEO,
    )

    assert verdict.verdict == VERDICT_YES
    assert verdict.evidence == EVIDENCE_CLEAR


def test_a_no_on_an_unsupported_degraded_is_kept_on_clear_evidence() -> None:
    """The NO rule still keeps the video, and the label it rested on is the
    unsupported one, so the grade is CLEAR rather than DEGRADED.

    Kept as MAYBE. It used to become YES on CLEAR evidence — a reply whose own
    reason was "no bearing on the brief" counted as coverage of the brief."""
    verdict = parse_full(
        reply(VERDICT="NO", EVIDENCE="DEGRADED", REASON="A cooking tutorial with no bearing on the brief."),
        VIDEO,
    )

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.said == VERDICT_NO
    assert verdict.evidence == EVIDENCE_CLEAR
    assert "rejected on degraded evidence" in verdict.problem
    assert "degraded without naming an evidence problem" in verdict.problem


@pytest.mark.parametrize(
    "reason",
    [
        "The transcript is unreadable for most of the runtime.",
        "Captions are auto-translated and largely gibberish.",
        "Audio quality prevented any reliable reading of the content.",
        "The stored text is truncated after ninety seconds.",
    ],
)
def test_a_real_evidence_problem_passes(reason: str) -> None:
    verdict = parse_full(reply(EVIDENCE="DEGRADED", REASON=reason, RELEVANT="UNAVAILABLE"), VIDEO)

    assert verdict.parsed is True, verdict.problem
    assert verdict.evidence == EVIDENCE_DEGRADED


# -- the damage proxy reads the evidence, not the subject -------------------
#
# Its second over-fire. Bare `quality`, `noise`, `empty`, `truncated` and
# `corrupt` matched subject matter, so a content NO became a DEGRADED keeper —
# and DEGRADED denies the video a note for every topic. On a Postgres topic
# `TRUNCATE` and data corruption are the subject.


@pytest.mark.parametrize(
    "reason",
    [
        # the measured case from rule 13
        "the transcript is garbled and contains no substantive content",
        "the captions are machine-translated noise",
        "transcript quality too poor to verify any claim",
        "the transcript is empty after the first minute",
    ],
)
def test_a_no_that_says_the_transcript_is_damaged_is_still_kept_degraded(reason: str) -> None:
    verdict = parse_full(reply(VERDICT="NO", REASON=reason, RELEVANT="NONE"), VIDEO)

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.said == VERDICT_NO
    assert verdict.evidence == EVIDENCE_DEGRADED
    assert "rejected while calling the transcript unreadable" in verdict.problem


@pytest.mark.parametrize(
    "reason",
    [
        "covers TRUNCATE and VACUUM, not connection pooling",
        "discusses data corruption recovery, unrelated to pooling",
        "a code quality and linting tutorial",
        "covers data quality checks in dbt pipelines",
        "discusses truncated backpropagation through time",
        "an empty sales pitch with no technical content",
        "mostly background noise about cloud pricing, nothing on pooling",
        # naming the transcript does not make a subject word into damage
        "the transcript covers data corruption recovery, not pooling",
        "the transcript is about truncated backpropagation, not retrieval",
    ],
)
def test_a_no_about_subject_matter_stays_a_no(reason: str) -> None:
    verdict = parse_full(reply(VERDICT="NO", REASON=reason, RELEVANT="NONE"), VIDEO)

    assert verdict.verdict == VERDICT_NO
    assert verdict.evidence == EVIDENCE_CLEAR
    assert verdict.parsed is True, verdict.problem


@pytest.mark.parametrize(
    "reason",
    [
        "the stored text is truncated after two minutes",
        "the transcript text is empty",
        "caption text quality too poor to verify any claim",
    ],
)
def test_damage_said_of_the_transcript_text_is_still_damage(reason: str) -> None:
    verdict = parse_full(reply(VERDICT="NO", REASON=reason, RELEVANT="NONE"), VIDEO)

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.evidence == EVIDENCE_DEGRADED


# Bare `text` is a subject as well as the evidence — a Postgres column type —
# so it counts as the transcript only in a compound that says so.
@pytest.mark.parametrize(
    "reason",
    [
        "covers how empty text values are handled, not pooling",
        "explains that long text is truncated at the column limit",
        "compares text and varchar storage quality, nothing on pooling",
        "covers full-text search, unrelated to connection pooling",
    ],
)
def test_bare_text_as_a_subject_is_not_the_transcript(reason: str) -> None:
    verdict = parse_full(reply(VERDICT="NO", REASON=reason, RELEVANT="NONE"), VIDEO)

    assert verdict.verdict == VERDICT_NO
    assert verdict.evidence == EVIDENCE_CLEAR
    assert verdict.parsed is True, verdict.problem


@pytest.mark.parametrize(
    "reason",
    [
        "Mostly about data quality, hard to say whether it matches.",
        "An empty sales pitch, unclear whether it fits the brief.",
    ],
)
def test_degraded_on_a_subject_word_still_names_no_evidence_problem(reason: str) -> None:
    """The proxy's other caller. DEGRADED resting on a subject word is still
    DEGRADED standing in for "I am not sure"."""
    verdict = parse_full(reply(EVIDENCE="DEGRADED", REASON=reason, RELEVANT="UNAVAILABLE"), VIDEO)

    assert "degraded without naming an evidence problem" in verdict.problem


# -- rule: CLEAR + YES must name where the value is -------------------------


def test_keeping_on_clear_evidence_without_a_range_is_a_violation() -> None:
    """Keeping a video while pointing at nothing in it is the shape of a
    judgement that did not happen."""
    verdict = parse_full(reply(RELEVANT="NONE"), VIDEO)

    assert verdict.parsed is False
    assert "named no relevant range" in verdict.problem
    assert verdict.verdict == VERDICT_YES


def test_unavailable_satisfies_the_rule() -> None:
    """That is what UNAVAILABLE is for: value is there, the clock is not."""
    assert parse_full(reply(RELEVANT="UNAVAILABLE"), VIDEO).parsed is True


def test_a_degraded_keep_needs_no_range() -> None:
    """The rule is CLEAR + YES. A judge that could not read the transcript
    cannot be asked to point into it."""
    verdict = parse_full(
        reply(EVIDENCE="DEGRADED", RELEVANT="NONE",
              REASON="The transcript is empty apart from music cues."),
        VIDEO,
    )
    assert verdict.parsed is True, verdict.problem


def test_a_no_needs_no_range() -> None:
    verdict = parse_full(
        reply(VERDICT="NO", RELEVANT="NONE", REASON="Entirely about a different subject."), VIDEO
    )
    assert verdict.parsed is True, verdict.problem


# -- rule: exactly five lines, nothing else ---------------------------------


def test_a_preamble_is_a_violation() -> None:
    verdict = parse_full("Here is my assessment:\n\n" + reply(), VIDEO)

    assert verdict.parsed is False
    assert "outside the format" in verdict.problem


def test_trailing_commentary_is_a_violation() -> None:
    verdict = parse_full(reply() + "\n\nHope that helps!", VIDEO)

    assert verdict.parsed is False
    assert "outside the format" in verdict.problem


def test_shown_reasoning_is_a_violation() -> None:
    body = "Let me work through the brief step by step.\nFirst, the exclusions.\n" + reply()
    assert parse_full(body, VIDEO).parsed is False


@pytest.mark.parametrize("missing", ["VERDICT", "EVIDENCE", "REASON", "RELEVANT", "UNEXPLAINED"])
def test_every_missing_line_is_a_violation(missing: str) -> None:
    """"Always emit all five lines" is the rule a parser cannot recover from
    being broken: a missing line and a broken line are indistinguishable, which
    is precisely how a silent drop happens."""
    lines = [line for line in reply().splitlines() if not line.startswith(missing)]
    verdict = parse_full("\n".join(lines), VIDEO)

    assert verdict.parsed is False
    assert missing in verdict.problem


def test_a_blank_line_between_fields_is_not_a_violation() -> None:
    """Formatting, not content. Refusing this would fail good judgements over
    whitespace."""
    assert parse_full(reply().replace("\n", "\n\n"), VIDEO).parsed is True


def test_markdown_bold_around_labels_survives() -> None:
    body = "\n".join(f"**{line.split(':', 1)[0]}:**{line.split(':', 1)[1]}"
                     for line in reply().splitlines())
    assert parse_full(body, VIDEO).parsed is True


# -- total failure ----------------------------------------------------------


def test_an_unreadable_reply_keeps_the_video() -> None:
    """Kept — never NO — and no longer a YES: a reply with no verdict word is
    no judgement, and a YES is what the planner counts as coverage."""
    verdict = parse_full("I'm sorry, I can't help with that.", VIDEO)

    assert verdict.parsed is False
    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.judged is False
    assert verdict.evidence == EVIDENCE_CLEAR


def test_an_empty_reply_keeps_the_video() -> None:
    """The shape a reasoning model returns when it spends its whole budget
    thinking. It must never read as a rejection."""
    verdict = parse_full("", VIDEO)

    assert verdict.parsed is False
    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.judged is False


def test_an_unreadable_reply_is_not_marked_degraded() -> None:
    """A judge that malfunctioned says nothing about the transcript. Marking it
    DEGRADED would suppress the note over a *parser* problem, and the transcript
    might be perfect."""
    assert parse_full("garbage", VIDEO).evidence == EVIDENCE_CLEAR


# -- the reply is surfaced, never swallowed ---------------------------------


def test_a_violation_is_logged_with_the_reason(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        parse_full(reply(RELEVANT="NONE"), VIDEO)

    assert any(VIDEO in record.getMessage() for record in caplog.records)


def test_the_raw_reply_reaches_no_log_at_any_level(caplog) -> None:
    """This used to send the raw reply to DEBUG as "available on request". It
    is not: the CLI attaches a DEBUG file handler on every command, so DEBUG is
    `logs/`, and the replies that land here are the ones that broke the format
    — the likeliest to have quoted the transcript back. What is logged is the
    reply's shape, which is enough to see that it broke and how."""
    broken = "VERDICT: YES\nand then some free text nobody asked for"

    with caplog.at_level(logging.DEBUG, logger="marginalia.brain.judge"):
        parse_full(broken, VIDEO)

    assert any(
        record.levelno == logging.DEBUG and "2 line(s)" in record.getMessage()
        for record in caplog.records
    )
    assert all("free text nobody asked for" not in r.getMessage() for r in caplog.records)


# -- B4: the map reaches the judge, and only as a word list -----------------


def test_the_map_renders_as_a_flat_word_list() -> None:
    from marginalia.brain.judge import territory_lines
    from marginalia.contracts import TerritoryMap

    territory = TerritoryMap(
        topic="t",
        version="1",
        concepts=["episodic memory"],
        vocabulary=["ReAct", "LangGraph"],
    )

    line = territory_lines(territory)
    assert "episodic memory" in line and "ReAct" in line
    assert "TERRITORY" in line


def test_the_judge_is_never_shown_the_queries_or_the_filters() -> None:
    """The judge is being told what words this subject uses, not what the map
    concluded. Showing it the queries would be handing it the reasoning it is
    supposed to check independently."""
    from marginalia.brain.judge import territory_lines
    from marginalia.contracts import (
        ORIGIN_TERRITORY,
        PlannedQuery,
        SearchFilters,
        TerritoryMap,
    )

    territory = TerritoryMap(
        topic="t",
        version="1",
        vocabulary=["ReAct"],
        queries=[
            PlannedQuery(
                text="a very distinctive query string",
                origin=ORIGIN_TERRITORY,
                filters=SearchFilters(order="date"),
            )
        ],
    )

    line = territory_lines(territory)
    assert "a very distinctive query string" not in line
    assert "order" not in line


def test_an_absent_map_says_so_rather_than_rendering_blank() -> None:
    """`Prompt.render` refuses unfilled placeholders, and a blank section reads
    to a model like a subject with no vocabulary rather than a run with no map."""
    from marginalia.brain.judge import territory_lines
    from marginalia.contracts import TerritoryMap

    assert "none mapped" in territory_lines(None)
    assert "none mapped" in territory_lines(TerritoryMap(topic="t", version="1"))


def test_both_judging_prompts_take_the_map() -> None:
    from marginalia.brain.judge import FULL_PROMPT
    from marginalia.llm import load_prompt, prompts_dir

    for name in ("judge_metadata", FULL_PROMPT):
        assert "{{ territory }}" in load_prompt(name, directory=prompts_dir()).text


def test_both_judging_prompts_state_the_vocabulary_rule() -> None:
    """The rule is load-bearing and it goes in as a rule, not a note.

    B3 measured this judge confabulating from suggestive context, and a
    vocabulary list is a page of suggestive context. Each prompt has to say, in
    as many words, that a term appearing is never on its own a reason to keep —
    and show the LangChain-document-loader case that makes it concrete.
    """
    from marginalia.brain.judge import FULL_PROMPT
    from marginalia.llm import load_prompt, prompts_dir

    for name in ("judge_metadata", FULL_PROMPT):
        # Whitespace-collapsed: these prompts are hard-wrapped, and a rule that
        # only holds while nobody reflows a paragraph is not being enforced.
        text = " ".join(load_prompt(name, directory=prompts_dir()).text.split())
        assert "never, on its own, a reason to keep" in text
        assert "The brief decides what counts as relevant" in text
        assert "LangChain" in text, "the rule needs its worked example"


def _full_prompt_text() -> str:
    from marginalia.brain.judge import FULL_PROMPT
    from marginalia.llm import load_prompt, prompts_dir

    return load_prompt(FULL_PROMPT, directory=prompts_dir()).text


def test_the_prompt_permits_marked_outside_knowledge_about_currency() -> None:
    """Stage 2 rejects videos for deprecated APIs by recognising import paths —
    correct, useful, and outside knowledge no transcript can supply. Marked
    rather than banned, so `grep` over `decisions.reason` can count it."""
    text = _full_prompt_text()

    assert "[outside knowledge:" in text
    assert "current" in text.lower()


def test_the_prompt_still_forbids_unmarked_invention() -> None:
    """The exception is scoped to currency. An unknown remains an absence."""
    text = _full_prompt_text().lower()

    assert "reason must come from the transcript above" in text


# -- the outside-knowledge marker -------------------------------------------
#
# PLAN.md §7 allows exactly one exception to "use only the evidence supplied":
# currency. No transcript states that its own import path has been superseded,
# and none can, so era judgement is outside knowledge by construction — allowed,
# and required to be marked so that it can be *counted*.
#
# It was asked for in the prompt and mostly not done. On the second `langchain`
# run, four of fifty stage-2 reasons rested on currency and one carried the
# marker; the three that did not are the strings tested below, verbatim. Same
# lesson as the five-line contract: a rule living only in a prompt is a rule
# enforced by a model.


def test_an_unmarked_currency_claim_is_counted_not_refused() -> None:
    """Counted, and nothing else. The verdict stands and the reply still parses.

    Dating an API is usually the right call; what is missing is the attribution,
    and turning that into a parse failure would report a convention nobody
    followed as a format nobody could read."""
    verdict = parse_full(
        reply(
            VERDICT="NO",
            REASON=(
                "The transcript shows a complete RAG pipeline with LangChain, but uses "
                "deprecated APIs (langchain.llms, langchain.chains, RetrievalQA)."
            ),
            RELEVANT="NONE",
        ),
        VIDEO,
    )

    assert verdict.unmarked_currency is True
    assert verdict.parsed is True, "the five lines were fine"
    assert verdict.problem == ""
    assert verdict.verdict == VERDICT_NO, "the judgement is not overruled by a proxy"


def test_the_marker_satisfies_the_check() -> None:
    verdict = parse_full(
        reply(
            VERDICT="NO",
            REASON=(
                "Teaches a pre-0.1 API surface [outside knowledge: these import paths "
                "were superseded]."
            ),
            RELEVANT="NONE",
        ),
        VIDEO,
    )

    assert verdict.unmarked_currency is False


def test_reporting_what_the_video_says_about_currency_is_not_a_claim() -> None:
    """The false positive this has to avoid, and it is a real reply.

    `Zbf5Hyv2DSY` was rejected as a lecture "comparing legacy chains to LCEL" —
    the video's subject matter, described. A reason that reports what the
    transcript says about currency is not the judge asserting it, and marking it
    would be marking the transcript. Same trap as "transcript" firing on every
    honest rejection that named its evidence."""
    verdict = parse_full(
        reply(
            VERDICT="NO",
            REASON=(
                "A conceptual lecture in which the speaker compares legacy chains to "
                "LCEL and defines runnables, but never shows code."
            ),
            RELEVANT="NONE",
        ),
        VIDEO,
    )

    assert verdict.unmarked_currency is False


def test_an_ordinary_rejection_does_not_trip_it() -> None:
    verdict = parse_full(
        reply(VERDICT="NO", REASON="A product review with no implementation detail.",
              RELEVANT="NONE"),
        VIDEO,
    )

    assert verdict.unmarked_currency is False


def test_a_keep_can_carry_an_unmarked_claim_too() -> None:
    """The rate on keeps is half the measurement.

    A marker that appears only on rejections says era judgement is being used to
    exclude and nothing else — which is the bias worth catching — and that is
    unreadable if the check only ever runs on a NO."""
    verdict = parse_full(
        reply(
            REASON=(
                "Builds a working agent, though the tool-calling decorator it uses is "
                "no longer the current one."
            )
        ),
        VIDEO,
    )

    assert verdict.unmarked_currency is True
    assert verdict.verdict == VERDICT_YES


def test_an_unmarked_claim_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Counted, logged, surfaced — the same discipline as everything else here."""
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        parse_full(
            reply(VERDICT="NO", RELEVANT="NONE",
                  REASON="Uses the deprecated initialize_agent entry point."),
            VIDEO,
        )

    assert "[outside knowledge" in caplog.text


# -- a refusal is not a blip ------------------------------------------------
#
# Rule 8 sends an unanswered call toward keeping, because the usual cause is
# transient and the alternative deletes videos invisibly. A refusal inverts the
# arithmetic: every later call fails identically, so "keep on failure" walks the
# entire candidate set through as MAYBE, fetches it, and stores it on judgements
# that never happened. Keeping on a blip is caution; keeping on a refusal is
# storing an unjudged library.


class Http402(Exception):
    def __init__(self) -> None:
        super().__init__("Insufficient Balance")
        self.status_code = 402


def _judge_with(exc: Exception, tmp_path):
    from marginalia.brain.judge import Judge
    from marginalia.llm import LlmClient
    from tests.conftest import make_config

    def transport(**kwargs):
        raise exc

    config = make_config(tmp_path)
    return Judge(LlmClient(config.llm, transport=transport, sleep=lambda _: None))


def test_a_refusal_propagates_instead_of_keeping(tmp_path) -> None:
    from marginalia.contracts import Brief, SearchResult
    from marginalia.llm import LlmConfigError

    judge = _judge_with(Http402(), tmp_path)
    candidate = SearchResult(
        video_id=VIDEO, title="t", channel="c", description="", duration_s=60,
        published_at="20260101", view_count=None,
    )
    brief = Brief(topic="prompt caching", text="INTENT\nCaching.", version="1/1")

    with pytest.raises(LlmConfigError):
        judge.judge_full(brief, candidate, transcript="hello world")


class _ContextOverflow(Exception):
    status_code = 400

    def __init__(self) -> None:
        super().__init__("Error code: 400 - This model's maximum context length is 131072 tokens.")


def test_a_transcript_over_the_window_keeps_the_video_and_the_run(tmp_path) -> None:
    """One long video used to end the discovery run here, because every 400 was
    a config error. The transcript is already stored; nothing read it, so it is
    kept unjudged — MAYBE, not the YES that counted as coverage — and named for
    what happened."""
    from marginalia.contracts import Brief, SearchResult

    judge = _judge_with(_ContextOverflow(), tmp_path)
    candidate = SearchResult(
        video_id=VIDEO, title="t", channel="c", description="", duration_s=60,
        published_at="20260101", view_count=None,
    )
    brief = Brief(topic="prompt caching", text="INTENT\nCaching.", version="1/1")

    verdict = judge.judge_full(brief, candidate, transcript="hello world")

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.parsed is False and verdict.judged is False
    assert "context window" in verdict.problem


def test_an_ordinary_call_failure_still_keeps(tmp_path) -> None:
    """The rule it must not have broken. A network blip is still a blip: the
    video is kept (never NO), and recorded as unjudged rather than as a YES."""
    from marginalia.contracts import Brief, SearchResult

    judge = _judge_with(ConnectionError("connection refused"), tmp_path)
    candidate = SearchResult(
        video_id=VIDEO, title="t", channel="c", description="", duration_s=60,
        published_at="20260101", view_count=None,
    )
    brief = Brief(topic="prompt caching", text="INTENT\nCaching.", version="1/1")

    verdict = judge.judge_full(brief, candidate, transcript="hello world")

    assert verdict.verdict == VERDICT_MAYBE
    assert verdict.parsed is False and verdict.judged is False


# -- the cap must not cut the marker ----------------------------------------


def test_a_long_reason_keeps_its_marker_whole() -> None:
    """Observed on a real reason, where the claim survived and the audit did not:
    `[outside knowledge: `initialize_agent` and `AgentType` we…`.

    A truncated marker still counts — the detector matches the opening bracket —
    so the rate stays honest either way. What it loses is the only thing it was
    written to carry: *which* outside knowledge was used."""
    marker = "[outside knowledge: initialize_agent and AgentType were superseded in 0.3]"
    long_prose = (
        "The video demonstrates a complete agent build with tool calling, memory "
        "and streaming, walking through every import and running the result on "
        "screen, but it teaches an API surface that predates the current one and "
        "never says so anywhere in the transcript. " * 2
    )
    verdict = parse_full(
        reply(VERDICT="NO", RELEVANT="NONE", REASON=f"{long_prose} {marker}"), VIDEO
    )

    assert verdict.reason.endswith(marker), "the marker survives the cap intact"
    assert "…" in verdict.reason, "and the prose is what was trimmed"
    assert verdict.unmarked_currency is False


def test_an_oversized_marker_does_not_eat_the_whole_reason() -> None:
    """A reason that is all attribution and no claim is not an improvement, so
    below a floor of usable prose the ordinary cap applies and says so."""
    marker = "[outside knowledge: " + "x" * 380 + "]"
    verdict = parse_full(
        reply(VERDICT="NO", RELEVANT="NONE", REASON=f"A short claim. {marker}"), VIDEO
    )

    assert verdict.reason.startswith("A short claim.")
    assert verdict.reason.endswith("…")


def test_a_short_reason_is_untouched() -> None:
    marker = "[outside knowledge: this import path was superseded]"
    verdict = parse_full(
        reply(VERDICT="NO", RELEVANT="NONE", REASON=f"Teaches a pre-0.1 API. {marker}"),
        VIDEO,
    )

    assert verdict.reason == f"Teaches a pre-0.1 API. {marker}"


# -- RELEVANT, as stored on the decision row -----------------------------------
#
# Stage 2 names where the relevant material is. The ranges were parsed and then
# discarded; they are now stored so a verdict can be checked against the
# transcript it points at. What is stored is what the parser matched — never
# the model's own words.


def test_the_ranges_are_stored_as_parsed() -> None:
    verdict = parse_full(reply(RELEVANT="00:12:30-00:18:45; 00:25:00-00:27:10"), VIDEO)

    assert verdict.relevant_text == "00:12:30-00:18:45; 00:25:00-00:27:10"


def test_none_is_stored_as_the_sentinel() -> None:
    from marginalia.contracts import RELEVANT_NONE

    verdict = parse_full(reply(VERDICT="NO", RELEVANT="NONE"), VIDEO)

    assert verdict.relevant_text == RELEVANT_NONE


def test_unavailable_is_stored_as_the_sentinel() -> None:
    verdict = parse_full(reply(RELEVANT="UNAVAILABLE"), VIDEO)

    assert verdict.relevant_text == RELEVANT_UNAVAILABLE


def test_prose_beside_the_ranges_never_reaches_the_stored_value() -> None:
    """Hard rule 1. A model that quotes the video inside RELEVANT must not turn
    the decision log into a place transcript text lives."""
    phrase = "the sentinel phrase the speaker said aloud"
    verdict = parse_full(
        reply(RELEVANT=f"00:12:30-00:18:45 ({phrase}); 00:25:00-00:27:10 where {phrase}"),
        VIDEO,
    )

    assert verdict.relevant_text == "00:12:30-00:18:45; 00:25:00-00:27:10"
    assert "sentinel" not in (verdict.relevant_text or "")


def test_a_missing_or_blank_line_is_not_recorded_as_none() -> None:
    """An empty range list also comes from a line nobody wrote. Storing NONE
    for that would record a claim the judge never made."""
    lines = reply().splitlines()
    missing = "\n".join(line for line in lines if not line.startswith("RELEVANT"))

    assert parse_full(missing, VIDEO).relevant_text is None
    assert parse_full(reply(RELEVANT=""), VIDEO).relevant_text is None


def test_an_unreadable_line_stores_nothing() -> None:
    verdict = parse_full(reply(RELEVANT="somewhere in the middle"), VIDEO)

    assert verdict.parsed is False
    assert verdict.relevant_text is None


def test_storing_the_ranges_changes_no_verdict() -> None:
    """Instrumentation only. The same reply yields the same verdict, evidence
    and working `relevant` list it always did."""
    verdict = parse_full(reply(RELEVANT="00:04:20-00:09:15"), VIDEO)

    assert verdict.verdict == VERDICT_YES
    assert verdict.evidence == EVIDENCE_CLEAR
    assert verdict.relevant == ["00:04:20-00:09:15"]


# -- the brief: the review replaces the draft only if it is a whole brief ----

_DRAFT = (
    "INTENT\nLearn how to diagnose and fix common household plumbing faults.\n\n"
    "REQUIRED OF EVERY VIDEO\n- explains why the fault happens, not only the fix\n\n"
    "PARTS TO COVER\n- leaking taps\n- blocked drains\n\n"
    "ALSO WANTED\n- names the parts and tools by their trade names\n\n"
    "EXCLUDED\n- product adverts for drain cleaners\n\n"
    "EDGE CASES\n- trade-school lectures count if they explain a real repair\n"
)
TOPIC = "home plumbing repairs"


class _BriefResponse:
    def __init__(self, text: str, finish_reason: str = "stop") -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message, "finish_reason": finish_reason})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 10})()
        self.model = "test-model"


def _brief_judge(*replies, tmp_path):
    from conftest import make_config

    script = list(replies)

    def transport(**_):
        return script.pop(0)

    config = make_config(tmp_path)
    return Judge(LlmClient(config.llm, transport=transport, sleep=lambda _: None))


def test_a_review_cut_off_by_the_ceiling_does_not_replace_the_draft(tmp_path) -> None:
    """It used to be enough to be over eighty characters. A review truncated
    after INTENT became the brief every verdict in the run was checked against,
    with no EXCLUDED section to check near misses against."""
    cut = "INTENT\nLearn how to diagnose household plumbing faults, and" + " more" * 40
    judge = _brief_judge(
        _BriefResponse(_DRAFT), _BriefResponse(cut, finish_reason="length"), tmp_path=tmp_path
    )

    brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()


def test_a_critique_instead_of_a_brief_does_not_replace_the_draft(tmp_path) -> None:
    critique = (
        "This brief is mostly sound. I would sharpen the intent, add near misses "
        "about drain-cleaner adverts, and state whether lectures count."
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(critique), tmp_path=tmp_path)

    assert judge.write_brief(TOPIC).text == _DRAFT.strip()


def test_a_whole_review_replaces_the_draft_however_its_headings_are_written(tmp_path) -> None:
    revised = (
        "## Intent: fix the faults a householder meets most often.\n"
        "**REQUIRED OF EVERY VIDEO:**\n- explains the cause of the fault\n"
        "### Parts to cover\n- leaking taps\n"
        "EXCLUDED\n- drain-cleaner adverts\n"
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(revised), tmp_path=tmp_path)

    assert judge.write_brief(TOPIC).text == revised.strip()


@pytest.mark.parametrize(
    "revised",
    [
        # Markdown headings, sentence case.
        "## Intent\nfix taps\n## Required of every video\n- explains the cause\n"
        "## Parts to cover\n- leaking taps\n## Also wanted\n- trade names\n"
        "## Excluded\n- adverts\n## Edge cases\n- lectures count\n",
        # Bold with a colon, and the body on the same line.
        "**INTENT:** fix taps\n**REQUIRED OF EVERY VIDEO:** explains the cause\n"
        "**PARTS TO COVER:** leaking taps\n**ALSO WANTED:** trade names\n"
        "**EXCLUDED:** adverts\n**EDGE CASES:** lectures count\n",
        # A dash separator, extra spaces between the words, and a bracketed note.
        "INTENT — fix taps\nREQUIRED  OF  EVERY  VIDEO (all of them) — explains the cause\n"
        "PARTS   TO COVER (at least one) - leaking taps\nALSO WANTED - trade names\n"
        "EXCLUDED - adverts\nEDGE CASES - lectures count\n",
    ],
    ids=["markdown-headings", "bold-colon-inline", "dash-separator-and-note"],
)
def test_the_six_headings_are_read_however_they_are_decorated(revised, tmp_path) -> None:
    """All six sections are recognised through the markdown and separators a
    model adds, so a decorated review is a whole brief rather than a refusal."""
    from marginalia.brain.judge import _brief_sections

    sections = _brief_sections(revised)

    assert set(sections) == {
        "INTENT", "REQUIRED OF EVERY VIDEO", "PARTS TO COVER",
        "ALSO WANTED", "EXCLUDED", "EDGE CASES",
    }
    assert "explains the cause" in sections["REQUIRED OF EVERY VIDEO"]
    assert "leaking taps" in sections["PARTS TO COVER"]
    # A body ends where the next heading begins, so no section swallows another.
    assert "leaking taps" not in sections["REQUIRED OF EVERY VIDEO"]
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(revised), tmp_path=tmp_path)
    assert judge.write_brief(TOPIC).text == revised.strip()


@pytest.mark.parametrize(
    "missing, review",
    [
        (
            "REQUIRED OF EVERY VIDEO",
            "INTENT\nfix taps\n\nPARTS TO COVER\n- leaking taps\n\nEXCLUDED\n- adverts\n",
        ),
        (
            "PARTS TO COVER",
            "INTENT\nfix taps\n\nREQUIRED OF EVERY VIDEO\n- explains the cause\n\n"
            "EXCLUDED\n- adverts\n",
        ),
    ],
    ids=["no-required", "no-parts"],
)
def test_a_review_missing_either_new_section_is_refused_and_named(
    missing, review, tmp_path, caplog
) -> None:
    """Relevance is every REQUIRED item and at least one PART. A brief without
    one of the two cannot be judged by that rule, so the draft stands."""
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(review), tmp_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()
    assert f"no {missing} section" in caplog.text


@pytest.mark.parametrize("empty", ["REQUIRED OF EVERY VIDEO", "PARTS TO COVER"])
def test_a_review_with_either_new_section_empty_is_refused_and_named(
    empty, tmp_path, caplog
) -> None:
    bodies = {
        "INTENT": "fix taps",
        "REQUIRED OF EVERY VIDEO": "- explains the cause",
        "PARTS TO COVER": "- leaking taps",
        "EXCLUDED": "- adverts",
    }
    bodies[empty] = "- "
    review = "".join(f"{name}\n{body}\n\n" for name, body in bodies.items())
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(review), tmp_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()
    assert f"empty {empty} section" in caplog.text


def test_a_review_under_the_old_headings_is_no_longer_a_whole_brief(tmp_path, caplog) -> None:
    """A reply that carries the single old required-items heading has neither
    new section. It is refused rather than read as one of them, because the
    old heading is exactly the ambiguity the split removed."""
    old_heading = "MUST " + "CONTAIN"
    old = (
        f"INTENT\nfix taps\n\n{old_heading}\n- leaking taps\n- explains the cause\n\n"
        "EXCLUDED\n- adverts\n"
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(old), tmp_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()
    assert "no REQUIRED OF EVERY VIDEO, PARTS TO COVER section" in caplog.text


# -- a skeleton or a critique never replaces a draft ---------------------------
#
# The section check that replaced the eighty-character guard asked only whether
# each required heading started a line. Twenty-eight characters of bare
# headings then became the brief every verdict in the run was measured against,
# and so did a critique whose sentences happened to begin with the heading words.


@pytest.mark.parametrize(
    "review",
    [
        "INTENT\nREQUIRED OF EVERY VIDEO\nPARTS TO COVER\nEXCLUDED",
        "INTENT:\nREQUIRED OF EVERY VIDEO:\nPARTS TO COVER:\nEXCLUDED:\n",
        "## Intent\n\n## Required of every video\n- \n## Parts to cover\n*\n## Excluded\n**",
    ],
    ids=["bare-headings", "colon-skeleton", "decorated-skeleton"],
)
def test_headings_with_nothing_under_them_do_not_replace_the_draft(review, tmp_path) -> None:
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(review), tmp_path=tmp_path)

    assert judge.write_brief(TOPIC).text == _DRAFT.strip()


def test_a_critique_whose_lines_open_with_heading_words_does_not_replace_the_draft(tmp_path) -> None:
    critique = (
        "Intent is stated but too vague to judge against.\n"
        "Required of every video reads like a list of parts.\n"
        "Parts to cover should name blocked drains.\n"
        "Excluded items look reasonable.\n"
        "Overall this brief needs another pass."
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(critique), tmp_path=tmp_path)

    assert judge.write_brief(TOPIC).text == _DRAFT.strip()


def test_a_review_with_one_empty_required_section_is_refused_and_named(tmp_path, caplog) -> None:
    review = (
        "INTENT\nFix the faults a householder meets.\n\n"
        "REQUIRED OF EVERY VIDEO\n- explains the cause\n\n"
        "PARTS TO COVER\n- leaking taps\n\nEXCLUDED\n\nEDGE CASES\n- lectures\n"
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(review), tmp_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()
    assert "empty EXCLUDED section" in caplog.text


def test_a_terse_but_whole_review_still_replaces_the_draft(tmp_path) -> None:
    """The guard refuses empty sections, not short ones."""
    review = (
        "INTENT: fix taps\nREQUIRED OF EVERY VIDEO (all of them): explains the cause\n"
        "PARTS TO COVER (at least one): leaking taps\nEXCLUDED: adverts\n"
    )
    judge = _brief_judge(_BriefResponse(_DRAFT), _BriefResponse(review), tmp_path=tmp_path)

    assert judge.write_brief(TOPIC).text == review.strip()


# -- a stage-2 call that produced no verdict is not a YES -----------------------


def test_a_reply_cut_off_before_its_verdict_is_kept_unjudged(tmp_path) -> None:
    from marginalia.contracts import Brief, SearchResult

    judge = _brief_judge(_BriefResponse("VERDICT: Y", finish_reason="length"), tmp_path=tmp_path)
    candidate = SearchResult(
        video_id=VIDEO, title="t", channel="c", description="", duration_s=60,
        published_at="20260101", view_count=None,
    )
    brief = Brief(topic="prompt caching", text="INTENT\nCaching.", version="1/1")

    verdict = judge.judge_full(brief, candidate, transcript="hello world")

    assert verdict.verdict != VERDICT_NO, "kept, never rejected"
    assert verdict.verdict != VERDICT_YES, "and not a YES the planner would count"
    assert getattr(verdict, "judged", True) is False


def test_a_failed_stage_two_call_logs_no_provider_text(tmp_path, caplog) -> None:
    from marginalia.contracts import Brief, SearchResult

    marker = "ZZ-provider-body-ZZ"
    judge = _judge_with(RuntimeError(f"reset by peer; echoed {marker}"), tmp_path)
    candidate = SearchResult(
        video_id=VIDEO, title="t", channel="c", description="", duration_s=60,
        published_at="20260101", view_count=None,
    )
    brief = Brief(topic="prompt caching", text="INTENT\nCaching.", version="1/1")

    with caplog.at_level(logging.DEBUG, logger="marginalia"):
        verdict = judge.judge_full(brief, candidate, transcript="hello world")

    assert marker not in caplog.text
    assert marker not in verdict.reason
    assert "RuntimeError" in caplog.text, "the classification is still logged"


# -- template-shaped source text reaches the model, unchanged -------------------
#
# The renderer used to scan inserted values for `{{ name }}` and refuse the
# request. Every stage that renders source text is checked here: stage 2 (the
# transcript), stage 1 (a description), the brief (the topic) and query
# expansion (the brief).

_SOURCE = r'uses {{ zqsourcetokenqz }} and {"json": {{ 1 }}} with \1 and \g<0>'


def _recording_judge(*texts):
    from conftest import make_llm_config

    script = list(texts)
    sent: list[str] = []

    def transport(**kwargs):
        sent.append(kwargs["messages"][1]["content"])
        return _BriefResponse(script.pop(0))

    return Judge(LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)), sent


def _candidate(description: str = ""):
    from marginalia.contracts import SearchResult

    return SearchResult(
        video_id=VIDEO, title="t", channel="c", description=description, duration_s=60,
        published_at="20260101", view_count=None,
    )


def _brief():
    from marginalia.contracts import Brief

    return Brief(topic="prompt caching", text=_DRAFT, version="1/1")


def test_stage_two_judges_a_transcript_that_quotes_template_syntax(caplog) -> None:
    judge, sent = _recording_judge(reply())

    with caplog.at_level(logging.DEBUG, logger="marginalia"):
        verdict = judge.judge_full(_brief(), _candidate(), transcript=f"[00:00:01] {_SOURCE}")

    assert verdict.judged is True and verdict.verdict == VERDICT_YES
    assert len(sent) == 1 and _SOURCE in sent[0], "sent to the model exactly as stored"
    assert "zqsourcetokenqz" not in caplog.text


def test_stage_one_judges_a_description_that_quotes_template_syntax(caplog) -> None:
    judge, sent = _recording_judge("1: YES - on topic")

    with caplog.at_level(logging.DEBUG, logger="marginalia"):
        verdicts = judge.judge_metadata(_brief(), [_candidate(_SOURCE)], batch_size=10)

    assert [v.verdict for v in verdicts] == [VERDICT_YES], "the model's verdict, not a failed batch"
    assert len(sent) == 1 and _SOURCE in sent[0]
    assert "zqsourcetokenqz" not in caplog.text


def test_a_brief_is_written_for_a_topic_that_quotes_template_syntax(caplog) -> None:
    judge, sent = _recording_judge(_DRAFT, _DRAFT)

    with caplog.at_level(logging.DEBUG, logger="marginalia"):
        brief = judge.write_brief(_SOURCE)

    assert brief.text == _DRAFT.strip()
    assert len(sent) == 2 and all(_SOURCE in request for request in sent)
    assert "zqsourcetokenqz" not in caplog.text


def test_queries_are_expanded_from_a_brief_that_quotes_template_syntax(caplog) -> None:
    from marginalia.contracts import Brief

    judge, sent = _recording_judge("first query\nsecond query")
    brief = Brief(topic="t", text=f"{_DRAFT}\n{_SOURCE}", version="1/1")

    with caplog.at_level(logging.DEBUG, logger="marginalia"):
        queries = judge.expand_queries(brief, count=2, concepts=[_SOURCE])

    assert queries == ["first query", "second query"]
    assert len(sent) == 1 and sent[0].count(_SOURCE) == 2, "the brief and the concept, once each"
    assert "zqsourcetokenqz" not in caplog.text


# -- a draft is checked before it is reviewed ---------------------------------
#
# Measured: 2 of 12 drafts with the current prompt reasoned to the token
# ceiling and returned 0 characters. The empty draft went to the review, which
# wrote a brief from the topic alone, and the result was recorded as an
# ordinary reviewed brief — no warning, no field, no counter.

_WHOLE_REVIEW = (
    "INTENT\nFix the faults a householder meets most often.\n\n"
    "REQUIRED OF EVERY VIDEO\n- explains why the fault happens\n\n"
    "PARTS TO COVER\n- leaking taps\n- blocked drains\n\n"
    "EXCLUDED\n- drain-cleaner adverts\n"
)


def _sending_brief_judge(*replies, tmp_path):
    """A brief judge whose transport also records the user message of each call."""
    from conftest import make_config

    script = list(replies)
    sent: list[str] = []

    def transport(**kwargs):
        sent.append(kwargs["messages"][-1]["content"])
        return script.pop(0)

    judge = Judge(LlmClient(make_config(tmp_path).llm, transport=transport, sleep=lambda _: None))
    return judge, sent


def test_an_empty_draft_cut_off_at_the_ceiling_is_drawn_again_not_reviewed(
    tmp_path, caplog
) -> None:
    """The measured failure: `finish_reason=length` and nothing in the text."""
    judge, sent = _sending_brief_judge(
        _BriefResponse("", finish_reason="length"),
        _BriefResponse(_DRAFT),
        _BriefResponse(_WHOLE_REVIEW),
        tmp_path=tmp_path,
    )

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        brief = judge.write_brief(TOPIC)

    assert len(sent) == 3, "draft, draft again, review"
    assert _DRAFT.strip() in sent[2], "the review was given the second draft"
    assert brief.text == _WHOLE_REVIEW.strip()
    assert brief.draft_attempts == 2
    assert len(brief.draft_problems) == 1
    assert brief.draft_problems[0].startswith("cut off at the token ceiling")
    assert "0 characters" in brief.draft_problems[0]
    assert brief.final_is == "review"
    assert "brief draft 1 of 2 unusable" in caplog.text


def test_a_cut_draft_with_partial_text_never_reaches_the_review(tmp_path) -> None:
    """A fragment whose last section is half-written reads as a whole brief to
    the review. It is drawn again, and its text is sent nowhere."""
    fragment = (
        "INTENT\nA half-written account of pipe repair.\n\n"
        "REQUIRED OF EVERY VIDEO\n- explains the cause\n\nPARTS TO COVER\n- leaking ta"
    )
    judge, sent = _sending_brief_judge(
        _BriefResponse(fragment, finish_reason="length"),
        _BriefResponse(_DRAFT),
        _BriefResponse(_WHOLE_REVIEW),
        tmp_path=tmp_path,
    )

    brief = judge.write_brief(TOPIC)

    assert "half-written account" not in sent[2], "the fragment was sent nowhere"
    assert _DRAFT.strip() in sent[2]
    assert brief.draft_attempts == 2
    assert f"{len(fragment.strip())} characters" in brief.draft_problems[0]


@pytest.mark.parametrize(
    "bad, problem",
    [("", "empty"), ("   \n ", "empty"), ("I cannot help with that.", "no brief section")],
    ids=["empty", "whitespace", "no-sections"],
)
def test_a_finished_draft_that_is_not_a_draft_is_drawn_again(bad, problem, tmp_path) -> None:
    judge, sent = _sending_brief_judge(
        _BriefResponse(bad), _BriefResponse(_DRAFT), _BriefResponse(_WHOLE_REVIEW),
        tmp_path=tmp_path,
    )

    brief = judge.write_brief(TOPIC)

    assert brief.draft_problems == (problem,)
    assert _DRAFT.strip() in sent[2]


def test_a_finished_draft_missing_a_section_is_still_reviewed(tmp_path) -> None:
    """Closing a draft's gaps is the review's job — a missing EXCLUDED is its
    most common repair — so only a draft with no brief at all is drawn again."""
    gappy = "INTENT\nFix taps.\n\nPARTS TO COVER\n- leaking taps\n"
    judge, sent = _sending_brief_judge(
        _BriefResponse(gappy), _BriefResponse(_WHOLE_REVIEW), tmp_path=tmp_path
    )

    brief = judge.write_brief(TOPIC)

    assert len(sent) == 2
    assert brief.draft_attempts == 1 and brief.draft_problems == ()
    assert brief.text == _WHOLE_REVIEW.strip()


def test_no_usable_draft_stops_before_the_review_and_says_why(tmp_path) -> None:
    """Two unusable drafts: the run stops before it starts. Nothing is reviewed,
    and the message names each attempt and what to change."""
    from marginalia.brain.judge import BriefUnusable

    judge, sent = _sending_brief_judge(
        _BriefResponse("", finish_reason="length"),
        _BriefResponse("partial INTENT", finish_reason="length"),
        tmp_path=tmp_path,
    )

    with pytest.raises(BriefUnusable) as caught:
        judge.write_brief(TOPIC)

    assert len(sent) == 2, "never reviewed"
    message = str(caught.value)
    assert "attempt 1: cut off" in message and "attempt 2: cut off" in message
    assert "brief_max_tokens" in message and "Nothing was searched or recorded" in message
    assert len(caught.value.problems) == 2


def test_an_ordinary_brief_records_one_draft_reviewed(tmp_path) -> None:
    judge, _ = _sending_brief_judge(
        _BriefResponse(_DRAFT), _BriefResponse(_WHOLE_REVIEW), tmp_path=tmp_path
    )

    brief = judge.write_brief(TOPIC)

    assert brief.outcome == {
        "draft_attempts": 1, "draft_problems": [], "review_problem": "", "final_is": "review",
    }


def test_an_unusable_review_is_recorded_on_the_brief(tmp_path) -> None:
    """The fallback to the unreviewed draft used to be one log line. It is now
    a field, so the run records it."""
    judge, _ = _sending_brief_judge(
        _BriefResponse(_DRAFT), _BriefResponse("", finish_reason="length"), tmp_path=tmp_path
    )

    brief = judge.write_brief(TOPIC)

    assert brief.text == _DRAFT.strip()
    assert brief.final_is == "draft"
    assert brief.review_problem == "cut off at the token ceiling"
    assert brief.outcome["final_is"] == "draft"


def test_a_cut_query_expansion_drops_its_last_line(tmp_path, caplog) -> None:
    """Rule 25's sibling: the line being written at the ceiling may be half a
    query, and a fragment is still a valid-looking search. The finished lines
    stand alone, so they are kept."""
    from marginalia.contracts import Brief

    judge, _ = _sending_brief_judge(
        _BriefResponse("fix a dripping tap\nunblock a kitchen sink\nreplace a tap wa",
                       finish_reason="length"),
        tmp_path=tmp_path,
    )
    brief = Brief(topic=TOPIC, text=_DRAFT, version="1/1")

    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        queries = judge.expand_queries(brief, count=9)

    assert queries == ["fix a dripping tap", "unblock a kitchen sink"]
    assert "cut off at the token ceiling" in caplog.text
