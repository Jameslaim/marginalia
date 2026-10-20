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
    _FOLD_EDIT_MAX,
    _FOLD_NEXT_MIN,
    check_grounding,
    edit_distance,
    load_notes,
    render_answer,
    scan_citations,
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


# -- the case-folded pass (F3) ----------------------------------------------
#
# A dropped character plus a case change: 3 edits case-sensitively, 1 folded.
# Neither the case repair (a full match) nor the edit repair caught it.

MEANT = "FTfLrcHMzLQ"
MIXED = "ftLrcHMzLQ"


def test_a_typo_mixing_a_case_change_and_an_edit_is_repaired_folded() -> None:
    assert edit_distance(MIXED, MEANT) == 3
    assert edit_distance(MIXED.lower(), MEANT.lower()) == 1
    assert (_FOLD_EDIT_MAX, _FOLD_NEXT_MIN) == (1, 6)

    scan = scan_citations(f"[{MIXED} 04:10]", [MEANT, OTHER])

    assert [c.video_id for c in scan.citations] == [MEANT], "the library's spelling"
    assert scan.citations[0].raw.startswith(f"[{MEANT} ")
    assert [(e.written, e.video_id, e.distance, e.case_folded) for e in scan.edited] == [
        (MIXED, MEANT, 1, True)
    ]
    assert scan.unparsed == []


def test_the_folded_pass_refuses_when_another_id_is_near_once_folded() -> None:
    near = "FTfLrcHMzQQ"  # 2 folded edits from what was written: inside the margin

    scan = scan_citations(f"[{MIXED} 04:10]", [MEANT, near])

    assert scan.citations == []
    assert scan.edited == []
    assert len(scan.unparsed) == 1


def test_the_folded_pass_allows_one_folded_edit_only() -> None:
    scan = scan_citations("[ftLrcHMzQQ 04:10]", [MEANT])  # 2 folded edits

    assert scan.citations == []


def test_the_folded_pass_never_overrules_a_case_sensitive_refusal() -> None:
    """A token the case-sensitive pass found close, and refused on its margin,
    is not handed to the folded pass, whose margin is smaller."""
    scan = scan_citations(f"[{TYPED} 11:23]", [ID, "aYfZN8tXAQz"])

    assert scan.citations == []
    assert scan.edited == []


def test_a_case_sensitive_repair_is_not_marked_folded() -> None:
    scan = scan_citations(f"[{TYPED} 11:23]", [ID])

    assert [e.case_folded for e in scan.edited] == [False]


def test_the_record_and_the_summary_count_folded_repairs_as_their_own_kind() -> None:
    from marginalia.cli import _ask_summary

    answer = Answer(question="q", text="t", version="1", model="m",
                    edited=[IdEdit(TYPED, ID, 1, 10), IdEdit(MIXED, MEANT, 1, 8, case_folded=True)])

    rendered = render_answer(answer, asked_at="2026-10-21T00:00:00Z")
    line = _ask_summary(answer)

    assert "- 1 citation id(s) repaired by edit distance against the library" in rendered
    assert "- 1 citation id(s) repaired by edit distance with case folded" in rendered
    assert (f"- repaired: {MIXED} -> {MEANT} (1 case-folded edit(s); next-nearest id 8 away,"
            " folded)") in rendered
    assert "1 mistyped id(s) repaired by edit distance, " in line
    assert "1 mistyped id(s) repaired by case-folded edit distance, " in line
