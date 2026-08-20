"""Topics are lowercased. Video IDs are not. This file exists to keep the two
from being "made consistent" by someone tidying up later.

They look alike — both short strings the ledger stores — and the temptation to
normalise both the same way is real. It is also the single most destructive
edit available in this codebase:

* a topic is a *search string*. "Prompt Caching" and "prompt  caching" name the
  same enquiry, and Gate 2 must recognise that or a rejection silently stops
  applying the moment someone capitalises differently.
* a video ID is an *identifier*, case-sensitive by construction.
  ``dQw4w9WgXcQ`` and ``dqw4w9wgxcq`` are two different videos, so lowercasing
  one collides it with a video nobody asked for.

The asymmetry is deliberate, and the failure it prevents is invisible.
"""

from __future__ import annotations

import pytest

from marginalia.core.ids import extract_video_id, is_video_id
from marginalia.core.ledger import normalise_topic


# -- topics: case and whitespace collapse -----------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("prompt caching", "prompt caching"),
        ("Prompt Caching", "prompt caching"),
        ("PROMPT CACHING", "prompt caching"),
        ("  prompt caching  ", "prompt caching"),
        ("prompt   caching", "prompt caching"),
        ("prompt\tcaching", "prompt caching"),
        ("prompt\ncaching", "prompt caching"),
        ("\n  Prompt \t Caching \n", "prompt caching"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_topics_are_lowercased_and_collapsed(raw: str, expected: str) -> None:
    assert normalise_topic(raw) == expected


def test_topic_normalisation_is_idempotent() -> None:
    once = normalise_topic("  Agent  Cost   Control ")
    assert normalise_topic(once) == once


def test_differently_typed_topics_are_the_same_topic() -> None:
    """Gate 2 matches on this string. If these diverged, a rejection would
    stop applying the moment someone typed the topic with a capital."""
    assert normalise_topic("Agent Cost Control") == normalise_topic("agent  cost control")


# -- video IDs: emphatically not ------------------------------------------


def test_video_ids_are_not_lowercased() -> None:
    assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_case_variants_of_an_id_are_different_videos() -> None:
    upper = extract_video_id("dQw4w9WgXcQ")
    lower = extract_video_id("dqw4w9wgxcq")
    assert upper is not None and lower is not None
    assert upper != lower


def test_both_case_variants_are_valid_ids() -> None:
    """Neither is a typo of the other — YouTube can issue both."""
    assert is_video_id("dQw4w9WgXcQ")
    assert is_video_id("dqw4w9wgxcq")


def test_applying_topic_normalisation_to_an_id_would_break_it() -> None:
    """The explicit statement of the rule, as an assertion.

    If someone ever routes an ID through normalise_topic, this is the test
    that says why the result is wrong rather than merely different.
    """
    video_id = "dQw4w9WgXcQ"
    assert normalise_topic(video_id) != video_id
    assert extract_video_id(normalise_topic(video_id)) != video_id
