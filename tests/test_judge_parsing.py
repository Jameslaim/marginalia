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

from marginalia.brain.judge import (
    _CHAPTER_LIMIT,
    _describe,
    parse_batch,
    parse_full,
    parse_queries,
)
from marginalia.contracts import VERDICT_MAYBE, VERDICT_NO, VERDICT_YES, SearchResult

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


# -- concepts, as stage 2 reports them ------------------------------------------
#
# Threads become the next round's queries, so what counts as a concept matters.
# These used to be exercised through the opening judge's parser, which nothing
# in production called; the live path is `parse_full`'s UNEXPLAINED line.


def _full(unexplained: str) -> str:
    return (
        "VERDICT: YES\nEVIDENCE: CLEAR\nREASON: shows the cache_control breakpoints.\n"
        f"RELEVANT: 00:01:00-00:02:00\nUNEXPLAINED: {unexplained}\n"
    )


def test_none_means_no_concepts() -> None:
    assert parse_full(_full("NONE"), "dQw4w9WgXcQ").concepts == []


def test_concepts_are_capped_and_deduplicated() -> None:
    concepts = parse_full(_full("a b, a b, c d, e f, g h, i j, k l, m n"), "dQw4w9WgXcQ").concepts
    assert len(concepts) <= 5
    assert len(set(concepts)) == len(concepts)


def test_a_sentence_masquerading_as_a_concept_is_dropped() -> None:
    """Threads become search queries. A whole sentence is not a query."""
    concepts = parse_full(
        _full("kv cache, the video also discusses at length how batching interacts"),
        "dQw4w9WgXcQ",
    ).concepts
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


# -- B2: chapters reach stage 1 ---------------------------------------------


def _candidate(**overrides) -> SearchResult:
    base = dict(
        video_id="aaaaaaaaaaa",
        title="Prompt caching explained",
        channel="Chan",
        description="A video about caching.",
        duration_s=600,
        published_at="2026-01-01T00:00:00Z",
        view_count=10,
        provider="fake",
    )
    base.update(overrides)
    return SearchResult(**base)  # type: ignore[arg-type]


def test_chapters_are_shown_to_the_metadata_judge() -> None:
    """A creator's table of contents is the only structural view of a video that
    exists before anything is downloaded, and stage 1 was throwing it away."""
    described = _describe(
        1,
        _candidate(
            chapters=[
                {"start_ms": 0, "title": "Intro"},
                {"start_ms": 120_000, "title": "cache_control breakpoints"},
                {"start_ms": 400_000, "title": "The five minute TTL"},
            ]
        ),
    )

    assert "chapters: Intro | cache_control breakpoints | The five minute TTL" in described


def test_a_candidate_without_chapters_says_nothing_about_them() -> None:
    """An empty line would be noise in every batch, and a judge that sees
    "chapters:" with nothing after it will reason about the absence."""
    assert "chapters" not in _describe(1, _candidate(chapters=[]))


def test_chapters_are_capped_so_one_video_cannot_crowd_a_batch() -> None:
    """A fifty-chapter workshop must not push the other candidates out of the
    judge's attention. The same cap stage 2 uses."""
    many = [{"start_ms": i * 60_000, "title": f"Part {i}"} for i in range(40)]
    described = _describe(1, _candidate(chapters=many))

    assert described.count(" | ") == _CHAPTER_LIMIT - 1
    assert "Part 39" not in described


def test_an_untitled_chapter_is_dropped_rather_than_shown_blank() -> None:
    chapters = [
        {"start_ms": 0, "title": "Intro"},
        {"start_ms": 60_000, "title": ""},
        {"start_ms": 120_000, "title": "End"},
    ]
    assert "chapters: Intro | End" in _describe(1, _candidate(chapters=chapters))


def test_chapters_come_after_the_description() -> None:
    """What the video says it is, then how it is laid out. The order is the one
    a reader needs, and a batch of ten candidates is read quickly."""
    described = _describe(1, _candidate(chapters=[{"start_ms": 0, "title": "Intro"}]))
    assert described.index("description:") < described.index("chapters:")


def test_the_judge_never_sees_a_caption_flag() -> None:
    """`has_captions` is a fact about caption tracks, not about content. It must
    never reach a relevance judgement — a video with no captions is unreadable
    today, not irrelevant."""
    described = _describe(1, _candidate(has_captions=False))
    assert "caption" not in described.lower()


# -- a stage-1 NO that guesses is MAYBE (rule 8) ----------------------------

# The five checkpoint-3 topic-1 NOs whose reason was a guess, verbatim.
_MEASURED_GUESSES = (
    "inference engine video with sponsor read, likely serving optimization",
    "build tutorial for memory chatbots, likely setup without mechanism",
    "coding agent memory tool setup, likely product/tutorial focused",
    "framework setup walkthrough, likely API tutorial without mechanism",
    "framework walkthrough, likely setup without inference mechanism",
)


@pytest.mark.parametrize("reason", _MEASURED_GUESSES)
def test_every_measured_guess_is_read_as_a_guess(reason) -> None:
    from marginalia.brain.judge import guesses

    assert guesses(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "probably a drain unblocker advert",
        "unlikely to explain why the fault happens",
        "may be a product demo with no explanation",
        "might only list saddle brands",
        "could be a sponsored review",
        "presumably a setup walkthrough",
    ],
)
def test_the_other_hedges_are_guesses_too(reason) -> None:
    from marginalia.brain.judge import guesses

    assert guesses(reason)


@pytest.mark.parametrize(
    "reason",
    [
        # The honest shapes: each names what the metadata shows.
        "product promo, brief excludes vendor marketing",
        "the title suggests a product promo",
        "appears in the description as a saddle price list",
        "seems to be about road cycling, not plumbing",
        # Subject matter, not a hedge.
        "about maximum likelihood estimation, a different subject",
        "published May 2024, a news clip with no explanation",
        "a probability lecture, unrelated to the brief",
    ],
)
def test_a_reason_that_names_the_evidence_is_not_a_guess(reason) -> None:
    """Tuned to miss rather than over-fire, though over-firing is the cheap
    direction here: `seems`, `appears` and `suggests` are how metadata is read,
    and counting them would turn honest rejections into fetches."""
    from marginalia.brain.judge import guesses

    assert not guesses(reason)


def test_a_guessed_no_resolves_to_maybe_and_remembers_the_no() -> None:
    from marginalia.brain.judge import resolve_guess

    assert resolve_guess(VERDICT_NO, _MEASURED_GUESSES[0]) == (VERDICT_MAYBE, VERDICT_NO)


def test_only_a_no_is_resolved() -> None:
    """A YES or a MAYBE that hedges has already sent the video to be read."""
    from marginalia.brain.judge import resolve_guess

    assert resolve_guess(VERDICT_YES, "likely a full explanation") == (VERDICT_YES, None)
    assert resolve_guess(VERDICT_MAYBE, "could be conceptual only") == (VERDICT_MAYBE, None)
    assert resolve_guess(VERDICT_NO, "product promo, brief excludes it") == (VERDICT_NO, None)


def test_the_metadata_judge_keeps_a_guessed_no_as_maybe(caplog) -> None:
    """Through the real `judge_metadata`: the prompt says a guessed NO is
    MAYBE, and this is the half that does not depend on the model listening."""
    from conftest import make_llm_config
    from marginalia.brain.judge import Judge
    from marginalia.contracts import OUTCOME_JUDGED, Brief
    from marginalia.llm import LlmClient

    class Reply:
        def __init__(self, text):
            message = type("M", (), {"content": text})()
            self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]
            self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            self.model = "m"

    text = "1: NO - build tutorial, likely setup without explanation\n2: NO - off topic"
    judge = Judge(LlmClient(make_llm_config(), transport=lambda **_: Reply(text), sleep=lambda _: None))
    brief = Brief(
        topic="t",
        text="INTENT\nx\nREQUIRED OF EVERY VIDEO\n- x\nPARTS TO COVER\n- x\nEXCLUDED\n- y",
        version="1/1",
    )
    candidates = [
        SearchResult(f"vid{index:08d}", "title", "chan", "desc", 600, None, None)
        for index in range(2)
    ]

    with caplog.at_level(logging.INFO, logger="marginalia"):
        guessed, refused = judge.judge_metadata(brief, candidates, batch_size=10)

    assert (guessed.verdict, guessed.said, guessed.outcome) == (VERDICT_MAYBE, VERDICT_NO, OUTCOME_JUDGED)
    assert guessed.reason.startswith("build tutorial"), "the judge's own reason is kept"
    assert (refused.verdict, refused.said) == (VERDICT_NO, None)
    assert any("rested on a guess" in record.getMessage() for record in caplog.records)


# -- a stage-1 NO resting on what the metadata did not show is MAYBE (rule 8) --

# Checkpoint 4's stage-1 NOs that rest on absence of evidence plus a runtime,
# verbatim: the metadata failed to show the video was on the brief, and showed
# nothing that put it off.
_MEASURED_ABSENCES = (
    "one-minute clip, no evidence of substantive mechanism explanation",
    "59-second short, no mechanism evidence in metadata",
    "short-form clip, no substantive explanation indicated",
    "41-second clip, tool-as-function claim without mechanism evidence",
    "36-second clip too thin to substantively explain retrieval mechanism",
    "50-second definition clip, no substantive mechanism explanation",
)


@pytest.mark.parametrize("reason", _MEASURED_ABSENCES)
def test_every_measured_absence_is_read_as_absence(reason) -> None:
    from marginalia.brain.judge import rests_on_absence

    assert rests_on_absence(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "45-second clip, no evidence of technique in the metadata",
        "a short, nothing in the title shows a repair",
        "two-minute video, no explanation evident",
        "brief reel, no named tool",
    ],
)
def test_other_absences_are_read_as_absence(reason) -> None:
    from marginalia.brain.judge import rests_on_absence

    assert rests_on_absence(reason)


@pytest.mark.parametrize(
    "reason",
    [
        # Off-subject, and it says so; the length is incidental.
        "a meme clip, unrelated",
        "30-second meme clip, unrelated, no evidence of a repair",
        "prank clip, no mechanism explanation",
        "product promo, no explanation evident",
        "news-style story, no evidence of a technique",
        "short clip about road cycling, a different subject, nothing shows plumbing",
        "settings tutorial, no mechanism explanation",
        "drain cleaner advert, no repair shown",
        "excluded: brief excludes product reviews",
    ],
)
def test_a_reason_that_names_what_the_metadata_shows_keeps_its_no(reason) -> None:
    from marginalia.brain.judge import rests_on_absence, resolve_stage1_no

    assert not rests_on_absence(reason)
    assert resolve_stage1_no(VERDICT_NO, reason) == (VERDICT_NO, None, "")


def test_an_absent_evidence_no_resolves_to_maybe_on_its_own_ground() -> None:
    from marginalia.brain.judge import RESOLVED_GUESS, RESOLVED_UNSHOWN, resolve_stage1_no

    assert resolve_stage1_no(VERDICT_NO, _MEASURED_ABSENCES[0]) == (
        VERDICT_MAYBE, VERDICT_NO, RESOLVED_UNSHOWN,
    )
    assert resolve_stage1_no(VERDICT_NO, _MEASURED_GUESSES[0]) == (
        VERDICT_MAYBE, VERDICT_NO, RESOLVED_GUESS,
    )
    # Toward keeping only: a YES or a MAYBE is never touched.
    assert resolve_stage1_no(VERDICT_YES, _MEASURED_ABSENCES[0]) == (VERDICT_YES, None, "")
    assert resolve_stage1_no(VERDICT_MAYBE, _MEASURED_ABSENCES[0]) == (VERDICT_MAYBE, None, "")


def test_the_metadata_judge_keeps_an_absent_evidence_no_as_maybe(caplog) -> None:
    from conftest import make_llm_config
    from marginalia.brain.judge import RESOLVED_UNSHOWN, Judge
    from marginalia.contracts import Brief
    from marginalia.llm import LlmClient

    class Reply:
        def __init__(self, text):
            message = type("M", (), {"content": text})()
            self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]
            self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            self.model = "m"

    text = (
        "1: NO - 45-second clip, no evidence of technique in the metadata\n"
        "2: NO - a meme clip, unrelated"
    )
    judge = Judge(LlmClient(make_llm_config(), transport=lambda **_: Reply(text), sleep=lambda _: None))
    brief = Brief(
        topic="t",
        text="INTENT\nx\nREQUIRED OF EVERY VIDEO\n- x\nPARTS TO COVER\n- x\nEXCLUDED\n- y",
        version="1/1",
    )
    candidates = [
        SearchResult(f"vid{index:08d}", "title", "chan", "desc", 45, None, None)
        for index in range(2)
    ]

    with caplog.at_level(logging.INFO, logger="marginalia"):
        unshown, refused = judge.judge_metadata(brief, candidates, batch_size=10)

    assert (unshown.verdict, unshown.said, unshown.resolved) == (
        VERDICT_MAYBE, VERDICT_NO, RESOLVED_UNSHOWN,
    )
    assert (refused.verdict, refused.said, refused.resolved) == (VERDICT_NO, None, "")
    assert any("did not show" in record.getMessage() for record in caplog.records)


# -- stage 2: a NO names its ground; an absent PART is not one ----------------

# Checkpoint 4's two wrong stage-2 NOs, verbatim. Each covers at least one of
# the brief's PARTS and was refused for the PARTS it did not cover.
_MEASURED_ABSENT_PART_NOS = (
    "explains next-token prediction and autoregressive generation conceptually "
    "… never addresses the context window, caching … so it fails most REQUIRED "
    "items and covers no PART beyond a bare next-token framing",
    "explains tokenization, context window composition, and next-token prediction "
    "conceptually, but it never addresses caching … cross-session memory, the harness",
)


@pytest.mark.parametrize("reason", _MEASURED_ABSENT_PART_NOS)
def test_the_five_line_contract_still_reads_the_measured_reasons(reason) -> None:
    """The reason's wording is the prompt's to change; the parser must read
    both the old shape and the new one, verdict unaltered."""
    reply = (
        f"VERDICT: NO\nEVIDENCE: CLEAR\nREASON: {reason}\n"
        "RELEVANT: NONE\nUNEXPLAINED: NONE"
    )
    verdict = parse_full(reply, "dQw4w9WgXcQ")

    assert verdict.parsed and verdict.verdict == VERDICT_NO


@pytest.mark.parametrize(
    "verdict_line, reason",
    [
        ("YES", "covers the PART blocked drains: explains why grease and hair build a plug"),
        ("NO", "fails REQUIRED: explains why the fault happens — the transcript lists tools only"),
        ("NO", "hits EXCLUDED: product adverts for drain cleaners — a sponsored demo throughout"),
        ("NO", "covers no PART substantively — names taps, drains and pressure in a minute"),
    ],
)
def test_the_new_reason_shapes_parse_under_the_same_contract(verdict_line, reason) -> None:
    relevant = "00:01:00-00:03:00" if verdict_line == "YES" else "NONE"
    reply = (
        f"VERDICT: {verdict_line}\nEVIDENCE: CLEAR\nREASON: {reason}\n"
        f"RELEVANT: {relevant}\nUNEXPLAINED: NONE"
    )
    verdict = parse_full(reply, "dQw4w9WgXcQ")

    assert verdict.parsed
    assert verdict.verdict == (VERDICT_YES if verdict_line == "YES" else VERDICT_NO)
    assert verdict.reason == reason
