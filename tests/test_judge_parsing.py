"""Turning model output into verdicts.

No model is called here. These are pure functions over text, which is exactly
what makes them worth testing hard: the model is temporary, the prompts will
change, and neither can be relied on to keep its formatting habits.

**The rule the whole file is built around:** anything unparseable becomes
MAYBE, never NO. Rejecting a video is invisible and permanent — nothing
downstream can recover a candidate the parser silently dropped — while keeping
one costs a single download that shows up in the library. A parser bug must
only ever be able to make the cheap mistake.
"""

from __future__ import annotations

import logging

import pytest

from marginalia.brain.judge import parse_batch, parse_opening, parse_queries
from marginalia.contracts import VERDICT_MAYBE, VERDICT_NO, VERDICT_YES

CLEAN = """\
1: YES - covers cache expiry and shows configuration
2: NO - product promo, brief excludes vendor marketing
3: MAYBE - title ambiguous, could be conceptual only
"""


def verdicts(parsed):
    return [verdict for verdict, _, _ in parsed]


def reasons(parsed):
    return [reason for _, reason, _ in parsed]


def flags(parsed):
    return [was_parsed for _, _, was_parsed in parsed]


# -- the correct case -------------------------------------------------------


def test_a_clean_reply_parses() -> None:
    parsed = parse_batch(CLEAN, 3)

    assert verdicts(parsed) == [VERDICT_YES, VERDICT_NO, VERDICT_MAYBE]
    assert reasons(parsed)[0] == "covers cache expiry and shows configuration"
    assert all(flags(parsed))


def test_reasons_survive_intact() -> None:
    """The reason is for a human reading the log months later, so it must not
    be mangled into a restatement of the verdict."""
    parsed = parse_batch("1: NO - it is a conference keynote about hiring\n", 1)
    assert reasons(parsed) == ["it is a conference keynote about hiring"]


@pytest.mark.parametrize(
    "line",
    [
        "1: YES - reason",
        "1. YES - reason",
        "1) YES - reason",
        "[1] YES - reason",
        "#1 YES - reason",
        "1 - YES - reason",
        "1: **YES** - reason",
        "  1:  yes  -  reason",
        "1 — YES — reason",
        "**1.** YES: reason",
    ],
)
def test_the_shapes_models_actually_write(line: str) -> None:
    """Told one format, models produce ten. Each of these was worth allowing;
    the alternative is a MAYBE and a wasted stage-2 request."""
    parsed = parse_batch(line, 1)
    assert verdicts(parsed) == [VERDICT_YES]
    assert flags(parsed) == [True]


# -- the ways it goes wrong -------------------------------------------------


def test_a_missing_entry_becomes_maybe() -> None:
    parsed = parse_batch("1: YES - good\n3: NO - bad\n", 3)

    assert verdicts(parsed) == [VERDICT_YES, VERDICT_MAYBE, VERDICT_NO]
    assert flags(parsed) == [True, False, True]
    assert reasons(parsed)[1] == "verdict could not be parsed"


def test_extra_entries_are_ignored_not_shifted() -> None:
    """A model that invents a fourth candidate must not push the third
    verdict onto a video it never saw."""
    parsed = parse_batch("1: YES - a\n2: NO - b\n3: YES - c\n4: NO - invented\n", 3)

    assert verdicts(parsed) == [VERDICT_YES, VERDICT_NO, VERDICT_YES]


def test_shuffled_order_still_binds_to_the_right_candidate() -> None:
    """Position comes from the number the model wrote, never from the order
    the lines appear in. Getting this wrong attaches every verdict to the
    wrong video, silently."""
    parsed = parse_batch("3: NO - third\n1: YES - first\n2: MAYBE - second\n", 3)

    assert verdicts(parsed) == [VERDICT_YES, VERDICT_MAYBE, VERDICT_NO]
    assert reasons(parsed) == ["first", "second", "third"]


def test_prose_instead_of_verdicts_is_all_maybe() -> None:
    prose = (
        "Looking at these candidates, the first one seems quite relevant to "
        "caching while the others are less clearly on topic."
    )
    parsed = parse_batch(prose, 3)

    assert verdicts(parsed) == [VERDICT_MAYBE] * 3
    assert not any(flags(parsed))


def test_a_refusal_is_all_maybe_never_no() -> None:
    """The single most important line in this file. A refusal means the model
    judged nothing; treating that as rejection would delete three videos on
    the strength of a safety filter."""
    parsed = parse_batch("I'm sorry, but I can't help with that request.", 3)

    assert verdicts(parsed) == [VERDICT_MAYBE] * 3
    assert VERDICT_NO not in verdicts(parsed)


def test_an_empty_reply_is_all_maybe() -> None:
    assert verdicts(parse_batch("", 4)) == [VERDICT_MAYBE] * 4


def test_preamble_and_summary_are_skipped() -> None:
    reply = (
        "Here are my evaluations:\n\n"
        "1: YES - relevant\n"
        "2: NO - off topic\n\n"
        "In summary, one candidate is worth keeping.\n"
    )
    assert verdicts(parse_batch(reply, 2)) == [VERDICT_YES, VERDICT_NO]


def test_a_restated_verdict_cannot_overturn_the_first() -> None:
    """Models sometimes repeat themselves in a closing summary. First mention
    wins, so a summary cannot quietly flip an answer."""
    parsed = parse_batch("1: YES - relevant\n\nRecap:\n1: NO - actually not\n", 1)
    assert verdicts(parsed) == [VERDICT_YES]


def test_a_verdict_word_in_prose_is_not_a_verdict() -> None:
    """`YES` inside a sentence must not be mistaken for an answer to
    candidate 1 — there is no number in front of it."""
    parsed = parse_batch("The answer is YES for most of these.\n", 2)
    assert verdicts(parsed) == [VERDICT_MAYBE, VERDICT_MAYBE]


def test_a_missing_reason_still_parses() -> None:
    parsed = parse_batch("1: YES\n", 1)
    assert verdicts(parsed) == [VERDICT_YES]
    assert reasons(parsed) == ["no reason given"]


def test_an_overlong_reason_is_truncated_not_dropped() -> None:
    parsed = parse_batch("1: NO - " + "verbose " * 60, 1)
    assert verdicts(parsed) == [VERDICT_NO]
    assert len(reasons(parsed)[0]) <= 120


def test_unparseable_entries_are_logged_with_their_positions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parse failure absorbed in silence is a judge nobody can diagnose."""
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.judge"):
        parse_batch("1: YES - fine\n", 3)

    assert "unparseable" in caplog.text
    assert "[2, 3]" in caplog.text
    assert "never NO" in caplog.text


# -- batch size 1 is the escape hatch --------------------------------------


def test_batch_of_one_behaves_identically() -> None:
    """If per-request credits turn out not to be the binding constraint, the
    fix is judge_batch_size = 1 and nothing else."""
    for verdict, expected in (("YES", VERDICT_YES), ("NO", VERDICT_NO), ("MAYBE", VERDICT_MAYBE)):
        parsed = parse_batch(f"1: {verdict} - reason", 1)
        assert verdicts(parsed) == [expected]
        assert flags(parsed) == [True]


def test_batch_of_one_still_defaults_to_maybe() -> None:
    assert verdicts(parse_batch("no idea", 1)) == [VERDICT_MAYBE]


# -- stage 2 ----------------------------------------------------------------


OPENING = """\
VERDICT: YES
REASON: explains cache expiry with a worked example
CONCEPTS: kv cache eviction, prefix matching, speculative decoding
"""


def test_an_opening_reply_parses() -> None:
    verdict, reason, concepts, parsed = parse_opening(OPENING)

    assert verdict == VERDICT_YES
    assert reason == "explains cache expiry with a worked example"
    assert concepts == ["kv cache eviction", "prefix matching", "speculative decoding"]
    assert parsed


def test_an_opening_no_parses() -> None:
    verdict, reason, _, parsed = parse_opening("VERDICT: NO\nREASON: it is a product demo\n")
    assert verdict == VERDICT_NO
    assert reason == "it is a product demo"
    assert parsed


def test_none_means_no_concepts() -> None:
    _, _, concepts, _ = parse_opening("VERDICT: YES\nREASON: r\nCONCEPTS: NONE\n")
    assert concepts == []


def test_an_unparseable_opening_keeps_the_video() -> None:
    """There is no stage 3, so a MAYBE has nowhere to go. The asymmetry
    resolves the other way here: a parser bug must not be able to reject."""
    verdict, reason, _, parsed = parse_opening("I think this one is probably fine?")

    assert verdict == VERDICT_YES
    assert not parsed
    assert "could not be parsed" in reason


def test_a_stage_two_maybe_resolves_to_keeping() -> None:
    verdict, _, _, _ = parse_opening("VERDICT: MAYBE\nREASON: hard to say\n")
    assert verdict == VERDICT_YES


def test_markdown_decoration_survives() -> None:
    reply = "**VERDICT:** NO\n**REASON:** unrelated\n**CONCEPTS:** NONE"
    verdict, reason, _, parsed = parse_opening(reply)
    assert verdict == VERDICT_NO
    assert reason == "unrelated"
    assert parsed


def test_concepts_are_capped_and_deduplicated() -> None:
    body = "VERDICT: YES\nREASON: r\nCONCEPTS: a b, a b, c d, e f, g h, i j, k l, m n\n"
    _, _, concepts, _ = parse_opening(body)
    assert len(concepts) <= 6
    assert len(set(concepts)) == len(concepts)


def test_a_sentence_masquerading_as_a_concept_is_dropped() -> None:
    """Threads become search queries. A whole sentence is not a query."""
    body = (
        "VERDICT: YES\nREASON: r\n"
        "CONCEPTS: kv cache, the video also discusses at length how batching interacts\n"
    )
    _, _, concepts, _ = parse_opening(body)
    assert concepts == ["kv cache"]


# -- query expansion --------------------------------------------------------


def test_queries_parse_one_per_line() -> None:
    reply = "prompt caching\ncache expiry llm\nkv cache explained\n"
    assert parse_queries(reply, 9) == ["prompt caching", "cache expiry llm", "kv cache explained"]


def test_query_list_markers_are_stripped() -> None:
    reply = "1. prompt caching\n- cache expiry\n* kv cache\n• prefix matching\n"
    assert parse_queries(reply, 9) == [
        "prompt caching",
        "cache expiry",
        "kv cache",
        "prefix matching",
    ]


def test_query_headings_and_prose_are_dropped() -> None:
    reply = "Queries:\nprompt caching\nHere is a much longer sentence that no one would ever type into a search box\n"
    assert parse_queries(reply, 9) == ["prompt caching"]


def test_duplicate_queries_collapse() -> None:
    assert parse_queries("prompt caching\nPrompt  Caching\ncache expiry\n", 9) == [
        "prompt caching",
        "cache expiry",
    ]


def test_queries_respect_the_limit() -> None:
    reply = "\n".join(f"query number {n}" for n in range(20))
    assert len(parse_queries(reply, 9)) == 9


# -- how a candidate is described to the judge ------------------------------


def test_a_short_video_is_not_described_as_zero_duration() -> None:
    """Observed in a real run: integer minutes rendered a 59-second short as
    "0 min", and the judge rejected it citing "zero duration". The judge was
    reasoning correctly from a number we got wrong."""
    from marginalia.brain.judge import _describe
    from marginalia.providers.base import SearchResult

    short = SearchResult(
        video_id="6zyzxRQYmtM", title="Prompt caching in 60 seconds", channel="c",
        description="", duration_s=59, published_at=None, view_count=None,
    )
    described = _describe(1, short)

    assert "0 min" not in described
    assert "59 sec" in described


def test_a_normal_video_is_described_in_minutes() -> None:
    from marginalia.brain.judge import _describe
    from marginalia.providers.base import SearchResult

    normal = SearchResult(
        video_id="aaaaaaaaaaa", title="t", channel="c", description="",
        duration_s=1112, published_at=None, view_count=None,
    )
    assert "18 min" in _describe(1, normal)


def test_a_described_candidate_carries_no_transcript_text() -> None:
    """Stage 1 is metadata only. If caption text could reach this string it
    would reach the model, the logs, and the cost curve."""
    from dataclasses import fields

    from marginalia.providers.base import SearchResult

    assert not any(f.name in {"transcript", "lines", "text"} for f in fields(SearchResult))
