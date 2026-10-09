"""M7 stage 1: which whole notes the answering call reads.

**The constraint first, because it decides everything else.** Both stages read
whole notes, never fragments. There is no embedding here, no vector, no
similarity score and no chunk — the standard approach destroys the whole-video
understanding the note format exists to produce, which is the argument that
rejected chunking transcripts at the start of the project.

**And rule 8, arriving in a new place.** A note wrongly excluded is invisible:
the answer never sees it, cites around it, and nothing reports that it existed.
So every failure below resolves toward loading more, and the tests assert the
fallback rather than the failure.
"""

from __future__ import annotations

import logging

import pytest

from marginalia.brain.selector import Selector, parse_selection
from marginalia.contracts import NoteSummary, SELECTION_SELECTED
from marginalia.llm import LlmClient, LlmConfigError

from conftest import make_llm_config

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
KNOWN = [A, B, C]


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class FakeReply:
    def __init__(self, text: str, finish: str = "stop") -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message, "finish_reason": finish})()]
        self.usage = FakeUsage()
        self.model = "test-model"


def client(item):
    def transport(**_):
        if isinstance(item, Exception):
            raise item
        return item

    return LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)


def _summaries():
    return [
        NoteSummary(video_id=v, title=f"title {v}", published="20260101", gist="a claim")
        for v in KNOWN
    ]


# -- parsing ----------------------------------------------------------------


def test_the_reply_names_what_to_drop_and_the_rest_is_kept() -> None:
    """Silence includes. The format carries rule 8, not only the prompt: a note
    the model does not name is a note that gets read."""
    selection = parse_selection(f"DROPPED: {B}\nWHY: they cover the loop", known=KNOWN)

    assert selection.video_ids == [A, C]
    assert selection.reason == "they cover the loop"
    assert selection.parsed
    assert selection.considered == 3


def test_an_id_that_was_never_offered_is_dropped_and_counted() -> None:
    """A hallucinated id here is worse than one in a citation. A citation that
    cannot resolve is a missing link somebody sees; this would silently shrink
    the selection — the model asks for a note that does not exist and the answer
    is one note thinner with nothing to show for it."""
    selection = parse_selection(f"DROPPED: {B}, zzzzzzzzzzz\nWHY: x", known=KNOWN)

    assert selection.video_ids == [A, C]
    assert "not in the library" in selection.problem
    assert selection.parsed, "one bad id does not discard a usable selection"


def test_case_is_never_repaired() -> None:
    """Rule 5: two ids differing by case are two videos. A repair here would
    load the wrong note while reporting success, which is worse than dropping
    one and saying so."""
    selection = parse_selection(f"DROPPED: {B.upper()}\nWHY: x", known=KNOWN)

    assert selection.video_ids == KNOWN, "a mis-cased id drops nothing"
    assert selection.parsed


def test_duplicates_collapse() -> None:
    selection = parse_selection(f"DROPPED: {B}, {B}, {C}\nWHY: x", known=KNOWN)

    assert selection.video_ids == [A]


def test_a_reply_with_no_selected_line_is_unparsed_not_empty() -> None:
    """The distinction the caller acts on. `parsed=False` means "fill the
    budget"; an empty *parsed* selection would mean "the model read the library
    and wanted none of it", which is a claim no reply here has ever made."""
    selection = parse_selection("I would suggest looking at the first one.", known=KNOWN)

    assert not selection.parsed
    assert selection.video_ids == []
    assert "no DROPPED" in selection.problem


def test_dropping_every_note_is_refused() -> None:
    """Answering from nothing is not an option the model gets to choose."""
    selection = parse_selection(f"DROPPED: {A}, {B}, {C}\nWHY: x", known=KNOWN)

    assert not selection.parsed
    assert "dropped every note" in selection.problem


# -- the call ---------------------------------------------------------------


def test_the_selector_names_its_own_task() -> None:
    """Effort is per task and set in config; a call with no task name silently
    takes the global default."""
    made = client(FakeReply(f"DROPPED: {A}\nWHY: x"))
    tasks: list[str] = []
    original = made.complete

    def spy(*args, **kwargs):
        tasks.append(kwargs.get("task"))
        return original(*args, **kwargs)

    made.complete = spy  # type: ignore[method-assign]
    Selector(made).select("q", _summaries(), keep=2)

    assert tasks == ["select_notes"]


def test_a_failed_call_reports_rather_than_narrowing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`parsed=False` is the caller's signal to retry and then refuse. What it
    must never be is a small selection wearing a successful one's clothes."""
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.selector"):
        selection = Selector(client(RuntimeError("boom"))).select("q", _summaries(), keep=2)

    assert not selection.parsed
    assert selection.video_ids == []
    assert "call failed" in selection.problem


def test_a_truncated_reply_is_discarded() -> None:
    """A fragment of a list reads exactly like a short list, and a short list is
    a narrow answer nobody can see."""
    selection = Selector(client(FakeReply(f"DROPPED: {A}, {B}", finish="length"))).select(
        "q", _summaries(), keep=2
    )

    assert not selection.parsed
    assert "ceiling" in selection.problem


def test_a_refusal_is_raised_not_swallowed() -> None:
    """Rule 24: every later call fails identically, so continuing would spend
    the answering call against a provider that has already said no."""

    class Refused(Exception):
        status_code = 402

    with pytest.raises(LlmConfigError):
        Selector(client(Refused())).select("q", _summaries(), keep=2)


def test_the_quota_is_exact_and_excess_drops_are_refused() -> None:
    """Measured, not hypothetical. On a real run the model was told it could
    drop at most 88 of 309 and dropped 297; the answer that followed cited 2
    videos where the same question over the whole library cited 7. Nothing
    downstream could see it, which is rule 8's invisible error — so the code
    caps what the prompt only asks for."""
    selection = parse_selection(f"DROPPED: {A}, {B}, {C}\nWHY: x", known=KNOWN, most=1)

    assert selection.video_ids == [B, C], "only the first drop ran"
    assert selection.requested_drops == 3
    assert selection.allowed_drops == 1
    assert selection.refused_drops == 2
    assert selection.overflowed == 0, "the budget trimmed nothing; the quota refused"
    assert "over the cap" in selection.problem
    assert selection.parsed, "a capped selection is still a usable one"


def test_a_selection_inside_the_cap_is_untouched() -> None:
    selection = parse_selection(f"DROPPED: {A}\nWHY: x", known=KNOWN, most=2)

    assert selection.video_ids == [B, C]
    assert selection.requested_drops == 1
    assert selection.refused_drops == 0
    assert selection.problem == ""


def test_the_reply_is_asked_for_exactly_the_number_that_must_go() -> None:
    """A quota, not a ceiling. Asked for *at most* twice the requirement, the
    model took the whole allowance: 318 drops where 159 were needed, which
    under-filled the budget by 60% and made a list long enough to truncate."""
    made = client(FakeReply(f"DROPPED: {A}\nWHY: x"))
    sent: list[str] = []
    original = made.complete

    def spy(system, user, **kwargs):
        sent.append(user)
        return original(system, user, **kwargs)

    made.complete = spy  # type: ignore[method-assign]
    Selector(made).select("q", _summaries(), keep=2)

    assert "Room for 2" in sent[0]
    assert "exactly 1 have" in sent[0]
    assert "one slice of a larger library" in sent[0]
    assert "contribute least" in sent[0], "ranking, not thresholding"


# -- batching ---------------------------------------------------------------
#
# Structural rather than a sixth attempt at wording. Five measurements across
# four framings, and the model never once handled a list of three hundred: a
# 3,351-id loop, 297 of 309 dropped, two truncations at 318 ids, and quotas
# ignored at 409 and 371 of 422.


def _many(n: int):
    return [
        NoteSummary(video_id=f"{i:011d}", title=f"title {i}", published="20260101", gist="a claim")
        for i in range(n)
    ]


def test_quotas_split_proportionally_and_sum_exactly() -> None:
    """Largest-remainder, because rounding each batch independently loses or
    gains notes against the budget — and the caller would then trim the
    difference by position, which is the arbitrary exclusion this avoids."""
    from marginalia.brain.selector import _quotas

    quotas = _quotas(159, [60, 60, 60, 60, 60, 60, 62])

    assert sum(quotas) == 159
    assert max(quotas) - min(quotas) <= 1, "spread evenly, not dumped on one batch"


def test_a_quota_never_exceeds_its_batch() -> None:
    from marginalia.brain.selector import _quotas

    assert _quotas(10, [2, 2]) == [2, 2]


def test_the_library_is_split_into_batches_of_sixty() -> None:
    replies = iter([FakeReply(f"DROPPED: {i:011d}\nWHY: lowest") for i in range(0, 200, 60)])
    calls: list[int] = []

    def transport(**kwargs):
        calls.append(len(kwargs["messages"][1]["content"]))
        return next(replies)

    made = LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)
    summaries = _many(180)

    selection = Selector(made).select("q", summaries, keep=177)

    assert len(calls) == 3, "180 notes is three batches of sixty"
    assert selection.parsed
    assert len(selection.video_ids) == 177


def test_a_failed_batch_keeps_its_notes_rather_than_the_question(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 8 at the level the failure now happens. One unreadable reply costs
    its batch's share of the drops, not the whole answer."""
    replies = iter([
        FakeReply(f"{0:011d}"),                        # batch 1: no DROPPED line
        FakeReply(f"DROPPED: {60:011d}\nWHY: lowest"), # batch 2: fine
        FakeReply(f"DROPPED: {120:011d}\nWHY: lowest"),
    ])

    def transport(**_):
        return next(replies)

    made = LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.selector"):
        selection = Selector(made).select("q", _many(180), keep=177)

    assert selection.parsed, "two good batches still make a usable selection"
    assert f"{0:011d}" in selection.video_ids, "the failed batch kept everything"
    assert f"{60:011d}" not in selection.video_ids, "the good batches still dropped"
    assert "1 of 3 batches" in selection.problem


def test_every_batch_failing_is_a_failed_selection() -> None:
    def transport(**_):
        return FakeReply("no list here")

    made = LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)

    selection = Selector(made).select("q", _many(120), keep=118)

    assert not selection.parsed
    assert "every batch failed" in selection.problem


def test_a_batch_needing_no_drops_is_never_asked() -> None:
    """A quota of zero has nothing to ask about, and asking would spend a call
    to be told to drop nothing."""
    calls = []

    def transport(**kwargs):
        calls.append(1)
        return FakeReply(f"DROPPED: {0:011d}\nWHY: lowest")

    made = LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)

    Selector(made).select("q", _many(120), keep=119)

    assert len(calls) == 1, "one drop falls in one batch; the other is untouched"
