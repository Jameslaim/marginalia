"""The description chapter parser, against fixtures rather than live text.

Fixtures on purpose: a live description is a moving target, and the cases worth
defending are the ones that *look* like chapter lists and are not. Every string
here is either a real description shape or a deliberate near-miss.

The bias under test is that the parser refuses when unsure. A missing table of
contents costs stage 1 some context it never had before B2; a fabricated one is
a confident lie about a video's structure, and stage 1 would judge on it.
"""

from __future__ import annotations

import pytest

from marginalia.providers.chapters import (
    MIN_CHAPTERS,
    NEAR_ZERO_MS,
    parse_description_chapters,
)

# -- fixtures ----------------------------------------------------------------

NO_CHAPTERS = """\
In this video I explain how prompt caching works and why it cuts your bill.

Links:
My newsletter: https://example.invalid/news
Discord: https://example.invalid/chat

#promptcaching #llm #anthropic
"""

REAL_LIST = """\
Everything you need to know about prompt caching.

00:00 Intro
01:45 What a cache hit actually costs
04:20 Setting up your key
14:20 cache_control breakpoints
21:07 The five minute TTL trap
28:33 Wrapping up

Subscribe for more.
"""

ONE_INCIDENTAL_TIMESTAMP = """\
I built an agent that reads your codebase.

The bit everyone asks about is at 4:20 — that is where I show the diff.

Links below.
"""

STARTS_MID_VIDEO = """\
A few moments worth jumping to:

04:20 Setting up your key
14:20 cache_control breakpoints
21:07 The five minute TTL trap
28:33 The part I got wrong
"""

HOURS_AND_BRACKETS = """\
Full workshop recording.

[0:00] - Welcome
[12:30] - Prompt caching from scratch
[45:02] - Break
[1:02:33] - Q&A
[1:48:15] - Closing
"""

DASHES_AND_EM_DASHES = """\
Chapters:
0:00 — Intro
2:15 – What we are building
9:40 - The caching layer
17:02 · Deploying it
"""


# -- the four cases the phase named -----------------------------------------


def test_a_description_with_no_chapters_yields_none() -> None:
    assert parse_description_chapters(NO_CHAPTERS) == []


def test_a_real_chapter_list_is_parsed() -> None:
    chapters = parse_description_chapters(REAL_LIST)

    assert [c["title"] for c in chapters] == [
        "Intro",
        "What a cache hit actually costs",
        "Setting up your key",
        "cache_control breakpoints",
        "The five minute TTL trap",
        "Wrapping up",
    ]
    assert [c["start_ms"] for c in chapters] == [
        0,
        105_000,
        260_000,
        860_000,
        1_267_000,
        1_713_000,
    ]


def test_one_incidental_timestamp_is_not_a_chapter_list() -> None:
    """A mention of a moment is not a description of the whole. One line can
    never be a listing however well-formed it looks."""
    assert parse_description_chapters(ONE_INCIDENTAL_TIMESTAMP) == []


def test_a_list_that_starts_mid_video_is_refused() -> None:
    """Four ascending timestamps with titles — every rule satisfied except the
    one that matters. A table of contents begins at the beginning; this is a set
    of bookmarks, and reading it as structure would misdescribe the video."""
    assert parse_description_chapters(STARTS_MID_VIDEO) == []


# -- format variation --------------------------------------------------------


def test_brackets_hours_and_dash_separators() -> None:
    chapters = parse_description_chapters(HOURS_AND_BRACKETS)

    assert [c["title"] for c in chapters] == [
        "Welcome",
        "Prompt caching from scratch",
        "Break",
        "Q&A",
        "Closing",
    ]
    assert chapters[3]["start_ms"] == (1 * 3600 + 2 * 60 + 33) * 1000


def test_every_dash_a_creator_reaches_for() -> None:
    chapters = parse_description_chapters(DASHES_AND_EM_DASHES)

    assert [c["title"] for c in chapters] == [
        "Intro",
        "What we are building",
        "The caching layer",
        "Deploying it",
    ]


def test_a_blank_line_does_not_break_a_listing() -> None:
    """Creators space their chapters out. A blank line is formatting; a line of
    prose is a boundary."""
    chapters = parse_description_chapters(
        "0:00 Intro\n\n2:00 Middle\n\n4:00 Later\n\n6:00 End\n"
    )
    assert len(chapters) == 4


def test_prose_between_two_short_runs_keeps_them_apart() -> None:
    """Two runs of three, separated by a sentence, are not one run of six."""
    body = (
        "0:00 A\n1:00 B\n2:00 C\n"
        "Then I talk about something else for a while.\n"
        "3:00 D\n4:00 E\n5:00 F\n"
    )
    assert parse_description_chapters(body) == []


def test_a_range_keeps_the_title_and_the_start() -> None:
    chapters = parse_description_chapters(
        "00:00 - 01:45 Intro\n01:45 - 04:20 Setup\n04:20 - 09:00 Caching\n09:00 - 12:00 End\n"
    )
    assert [c["title"] for c in chapters] == ["Intro", "Setup", "Caching", "End"]
    assert [c["start_ms"] for c in chapters] == [0, 105_000, 260_000, 540_000]


# -- the refusals ------------------------------------------------------------


def test_a_numbered_list_is_not_a_chapter_list() -> None:
    """"Lines that merely start with a number are not chapters." The colon is
    what does the work here."""
    body = "1. Introduction\n2. Setting up\n3. Caching\n4. Wrapping up\n"
    assert parse_description_chapters(body) == []


def test_a_timestamp_with_no_title_is_not_a_chapter() -> None:
    body = "0:00\n1:00\n2:00\n3:00\n"
    assert parse_description_chapters(body) == []


def test_descending_timestamps_are_refused() -> None:
    """A table of contents goes forwards. Anything else is a coincidence of
    numbers — a scoreline, a set of durations, a countdown."""
    body = "0:00 First\n9:00 Second\n4:00 Third\n12:00 Fourth\n"
    assert parse_description_chapters(body) == []


def test_a_repeated_timestamp_is_refused() -> None:
    """Strictly ascending: two chapters cannot start at the same moment, and a
    run that repeats is a list of durations rather than of starts."""
    body = "0:00 First\n2:00 Second\n2:00 Third\n5:00 Fourth\n"
    assert parse_description_chapters(body) == []


def test_impossible_seconds_are_not_timestamps() -> None:
    """`3:75` is not a time. Scorelines and version numbers live in this shape."""
    body = "0:00 First\n1:75 Second\n2:80 Third\n3:90 Fourth\n"
    assert parse_description_chapters(body) == []


@pytest.mark.parametrize("count", range(1, MIN_CHAPTERS))
def test_fewer_than_the_floor_is_never_a_listing(count: int) -> None:
    body = "".join(f"{minute}:00 Chapter {minute}\n" for minute in range(count))
    assert parse_description_chapters(body) == []


def test_exactly_the_floor_is_a_listing() -> None:
    body = "".join(f"{minute}:00 Chapter {minute}\n" for minute in range(MIN_CHAPTERS))
    assert len(parse_description_chapters(body)) == MIN_CHAPTERS


def test_near_zero_is_allowed_but_mid_video_is_not() -> None:
    """Creators write `00:01 Intro`. YouTube demands exactly 0:00; being a
    little looser than YouTube admits real lists without admitting bookmarks."""
    near = "0:0{}\n".format(NEAR_ZERO_MS // 1000)
    assert parse_description_chapters(
        f"00:01 A\n2:00 B\n4:00 C\n6:00 D\n"
    ), "a list starting a second in is still a list"
    assert parse_description_chapters(
        f"{NEAR_ZERO_MS // 1000 + 30}:00 A\n40:00 B\n50:00 C\n60:00 D\n"
    ) == [], near


# -- the empty cases ---------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   \n\n  "])
def test_nothing_in_nothing_out(value: str | None) -> None:
    assert parse_description_chapters(value) == []
