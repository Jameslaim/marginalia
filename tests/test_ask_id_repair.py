"""A mistyped video id, repaired against the library by edit distance.

Measured in nine answers: 16 of 247 citations carried a mistyped id, each one
edit from the id it meant and ten from the next; 8 were dropped as unreadable,
and one quote was checked against the wrong videos. The repair returns the
library's own spelling (rule 5), needs a unique id within two edits and every
other id at least seven away, and is counted on its own (rule 14).
"""

from __future__ import annotations

from marginalia.ask import (
    _EDIT_MAX,
    _EDIT_NEXT_MIN,
    check_grounding,
    edit_distance,
    load_notes,
    render_answer,
)
from marginalia.contracts import Answer, IdEdit

from test_ask import _cue, build

TYPED = "aYfZN8tAQs"
ID = "aYfZN8t6AQs"
OTHER = "rGf9Db4tjUQ"


def test_edit_distance_counts_swaps_as_one_and_case_as_a_difference() -> None:
    assert edit_distance("o6vU4u_yEuE", "o6v4Uu_yEuE") == 1
    assert edit_distance("aYfZN8tAQs", "aYfZN8t6AQs") == 1
    assert edit_distance("abc", "ABC") == 3
    assert (_EDIT_MAX, _EDIT_NEXT_MIN) == (2, 7)


def test_a_quote_beside_a_mistyped_id_is_checked_against_that_video(tmp_path) -> None:
    """Before the repair the bracket named nothing, so the quote was checked
    against whatever other id sat in the window — or reported as uncited."""
    config, library = build(
        tmp_path,
        {ID: "## Claims\n\n- x\n", OTHER: "## Claims\n\n- y\n"},
        {ID: _cue("the minimum is one thousand tokens"), OTHER: _cue("nothing alike")},
    )
    text = f'They say "the minimum is one thousand tokens" [{TYPED} 00:00].'

    grounding = check_grounding(
        text, load_notes(library), [ID, OTHER],
        transcripts={ID: library.read_transcript(ID)},
    )

    assert (grounding.quotes_checked, grounding.quotes_verified) == (1, 1)
    assert grounding.unresolved_ids == (), "a repaired id is not also a missing video"
    assert grounding.uncited_quotes == ()


def test_the_answer_record_shows_each_edit_repair_with_its_distance() -> None:
    answer = Answer(question="q", text="t", version="1", model="m",
                    edited=[IdEdit(written=TYPED, video_id=ID, distance=1, next_nearest=10)])

    rendered = render_answer(answer, asked_at="2026-10-20T00:00:00Z")

    assert "1 citation id(s) repaired by edit distance" in rendered
    assert f"- repaired: {TYPED} -> {ID} (1 edit(s); next-nearest id 10 away)" in rendered


def test_the_summary_line_counts_edit_repairs_apart_from_case_repairs() -> None:
    from marginalia.cli import _ask_summary

    answer = Answer(question="q", text="t", version="1", model="m",
                    repaired=["x"], edited=[IdEdit(TYPED, ID, 1, 10)])

    line = _ask_summary(answer)

    assert "1 id(s) repaired, " in line
    assert "1 mistyped id(s) repaired by edit distance, " in line
