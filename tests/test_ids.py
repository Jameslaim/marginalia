"""Canonical ID extraction.

Mistakes here are silent: a mis-parsed URL becomes a second ledger row for a
video already stored, and a lowercased ID becomes a different video entirely.
"""

from __future__ import annotations

import pytest

from marginalia.core.ids import extract_video_id, is_video_id

VIDEO_ID = "dQw4w9WgXcQ"

URL_FORMS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&t=90s&list=PLxxxx",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=90",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
]


@pytest.mark.parametrize("url", URL_FORMS)
def test_every_url_form_collapses_to_the_same_id(url: str) -> None:
    assert extract_video_id(url) == VIDEO_ID


def test_all_url_forms_agree() -> None:
    assert {extract_video_id(url) for url in URL_FORMS} == {VIDEO_ID}


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "www.youtube.com/watch?v=dQw4w9WgXcQ",
        "youtu.be/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/v/dQw4w9WgXcQ",
        "  https://youtu.be/dQw4w9WgXcQ  ",
        "https://www.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ&feature=share",
    ],
)
def test_additional_url_shapes(url: str) -> None:
    assert extract_video_id(url) == VIDEO_ID


# -- the rule that breaks people who normalise IDs like text ----------------


def test_ids_are_case_sensitive() -> None:
    """dqw4w9wgxcq is a DIFFERENT video from dQw4w9WgXcQ. Never lowercase."""
    upper = extract_video_id("https://youtu.be/dQw4w9WgXcQ")
    lower = extract_video_id("https://youtu.be/dqw4w9wgxcq")

    assert upper == "dQw4w9WgXcQ"
    assert lower == "dqw4w9wgxcq"
    assert upper != lower


def test_case_is_preserved_for_every_url_form() -> None:
    mixed = "aB_cD-eFgH1"
    for template in (
        "https://www.youtube.com/watch?v={}",
        "https://youtu.be/{}",
        "https://www.youtube.com/embed/{}",
        "https://www.youtube.com/shorts/{}",
        "https://www.youtube.com/live/{}",
        "{}",
    ):
        assert extract_video_id(template.format(mixed)) == mixed


# -- rejections --------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "hello",
        "not a url",
        "https://vimeo.com/123456789",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQtoolong",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/playlist?list=PLxxxxxxxxxxx",
        "https://youtu.be/",
        "dQw4w9WgXc",  # 10 characters
        "dQw4w9WgXcQQ",  # 12 characters
        "dQw4w9WgXc!",  # invalid character
    ],
)
def test_invalid_input_returns_none(value: str) -> None:
    assert extract_video_id(value) is None


@pytest.mark.parametrize("value", [None, 42, b"dQw4w9WgXcQ", ["dQw4w9WgXcQ"]])
def test_non_strings_return_none_and_never_raise(value: object) -> None:
    assert extract_video_id(value) is None  # type: ignore[arg-type]


def test_is_video_id() -> None:
    assert is_video_id(VIDEO_ID)
    assert is_video_id("aB_cD-eFgH1")
    assert not is_video_id("short")
    assert not is_video_id("https://youtu.be/dQw4w9WgXcQ")
