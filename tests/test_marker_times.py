"""A time difference stated inside a marker, checked against its own line.

Markers stated time differences their own lines' timestamps contradict — "four
minutes apart" of two moments 21 seconds apart, "ten seconds" of 39. Counted,
never rewritten (rule 33).
"""

from __future__ import annotations

from marginalia.enrich.marker_times import marker_time_claims


def test_the_measured_shapes_are_flagged_with_both_values() -> None:
    body = (
        "## Wrong or shaky\n\n"
        "- 01:10 and 01:31 [note: the two moments are four minutes apart]\n"
        "- 00:20 to 00:59 [note: a gap of ten seconds]\n"
    )

    claims = marker_time_claims(body)

    assert [(c.line, c.stated_s, c.nearest_s, c.holds) for c in claims] == [
        (3, 240, 21.0, False),
        (4, 10, 39.0, False),
    ]


def test_a_statement_the_line_supports_holds() -> None:
    body = "- 02:00 and 02:30 [note: thirty seconds later]\n- 00:01:00 to 00:04:10 [note: over three minutes]\n"

    assert [c.holds for c in marker_time_claims(body)] == [True, True]


def test_a_bare_duration_is_not_a_difference() -> None:
    assert marker_time_claims("- 01:00 and 05:00 [note: a 20-minute video overall]\n") == []


def test_a_line_with_one_timestamp_cannot_be_checked_and_is_not_counted() -> None:
    assert marker_time_claims("- 03:00 [note: four minutes apart from the intro]\n") == []


def test_a_difference_outside_any_marker_is_not_this_check() -> None:
    assert marker_time_claims("- 01:10 and 01:31, four minutes apart [note: a guess]\n") == []
