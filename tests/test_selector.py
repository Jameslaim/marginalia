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


def test_a_failed_call_falls_through_rather_than_narrowing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="marginalia.brain.selector"):
        selection = Selector(client(RuntimeError("boom"))).select("q", _summaries(), keep=2)

    assert not selection.parsed
    assert selection.video_ids == []
    assert "filling the budget" in caplog.text


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


def test_dropping_more_than_the_cap_is_refused_and_re_included() -> None:
    """Measured, not hypothetical. On a real run the model was told it could
    drop at most 88 of 309 and dropped 297; the answer that followed cited 2
    videos where the same question over the whole library cited 7. Nothing
    downstream could see it, which is rule 8's invisible error — so the code
    caps what the prompt only asks for."""
    selection = parse_selection(f"DROPPED: {A}, {B}, {C}\nWHY: x", known=KNOWN, most=1)

    assert selection.video_ids == [B, C], "only the first drop ran"
    assert selection.overflowed == 2
    assert "over the cap" in selection.problem
    assert selection.parsed, "a capped selection is still a usable one"


def test_a_selection_inside_the_cap_is_untouched() -> None:
    selection = parse_selection(f"DROPPED: {A}\nWHY: x", known=KNOWN, most=2)

    assert selection.video_ids == [B, C]
    assert selection.overflowed == 0
    assert selection.problem == ""
