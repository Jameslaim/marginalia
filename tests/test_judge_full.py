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

from marginalia.brain.judge import parse_full
from marginalia.contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    RELEVANT_UNAVAILABLE,
    VERDICT_NO,
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


def test_maybe_resolves_to_keeping() -> None:
    """There is no stage 3. A MAYBE has nowhere to route to, so it resolves the
    way the asymmetry points."""
    assert parse_full(reply(VERDICT="MAYBE"), VIDEO).verdict == VERDICT_YES


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

    assert verdict.verdict == VERDICT_YES
    assert verdict.evidence == EVIDENCE_DEGRADED


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
    verdict = parse_full("I'm sorry, I can't help with that.", VIDEO)

    assert verdict.parsed is False
    assert verdict.verdict == VERDICT_YES
    assert verdict.evidence == EVIDENCE_CLEAR


def test_an_empty_reply_keeps_the_video() -> None:
    """The shape a reasoning model returns when it spends its whole budget
    thinking. It must never read as a rejection."""
    verdict = parse_full("", VIDEO)

    assert verdict.parsed is False
    assert verdict.verdict == VERDICT_YES


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


def test_the_raw_reply_goes_to_debug_and_not_higher(caplog) -> None:
    """The raw text is the only way to diagnose a format the parser cannot read,
    and it may contain transcript fragments the model quoted back. DEBUG is
    where it belongs: available on request, never in the report."""
    broken = "VERDICT: YES\nand then some free text nobody asked for"

    with caplog.at_level(logging.DEBUG, logger="marginalia.brain.judge"):
        parse_full(broken, VIDEO)
    assert any(record.levelno == logging.DEBUG for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="marginalia.brain.judge"):
        parse_full(broken, VIDEO)
    assert all(
        "free text nobody asked for" not in (record.getMessage())
        for record in caplog.records
    )


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
