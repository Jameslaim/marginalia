"""Caption format parsers.

These only run on the day YouTube changes serialisation format — which is the
worst possible moment to discover a bug in them. Every format is therefore
pinned to a static fixture here: no network, no yt-dlp.

All five fixtures express the *same* captions. They must all parse to the same
``list[TranscriptLine]``:

======  ==========  ================================================
start   end         content
======  ==========  ================================================
1.500   3.000       HTML entities, incl. a double-encoded ampersand
3.000   (=start)    zero-duration cue; end is repaired from the next
4.000   7.250       a cue split over two lines
8.000   9.000       an empty cue — dropped entirely
10.000  12.000      plain text, plus markup in the VTT case
======  ==========  ================================================
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginalia.providers.base import TranscriptLine
from marginalia.providers.ytdlp import _parse_captions

FIXTURES = Path(__file__).parent / "fixtures"

# The zero-length cue's end time is repaired to the next cue's start (4000), and
# the empty cue at 8.000 is gone.
EXPECTED = [
    TranscriptLine(start_ms=1500, end_ms=3000, text="Tom & Jerry's big day"),
    TranscriptLine(start_ms=3000, end_ms=4000, text="Zero length cue"),
    TranscriptLine(start_ms=4000, end_ms=7250, text="First line second line"),
    TranscriptLine(start_ms=10000, end_ms=12000, text="Final line"),
]

FORMATS = ["json3", "srv1", "srv3", "ttml", "vtt"]


def parse(ext: str) -> list[TranscriptLine]:
    return _parse_captions(ext, (FIXTURES / f"captions.{ext}").read_bytes())


@pytest.mark.parametrize("ext", FORMATS)
def test_every_format_parses_to_the_same_lines(ext: str) -> None:
    assert parse(ext) == EXPECTED


def test_all_formats_agree_with_each_other() -> None:
    parsed = {ext: parse(ext) for ext in FORMATS}
    reference = parsed["json3"]
    for ext, lines in parsed.items():
        assert lines == reference, f"{ext} disagrees with json3"


# -- the specific hazards ---------------------------------------------------


@pytest.mark.parametrize("ext", FORMATS)
def test_html_entities_are_decoded(ext: str) -> None:
    """Including the double encoding YouTube applies inside XML payloads."""
    first = parse(ext)[0]
    assert first.text == "Tom & Jerry's big day"
    assert "&amp;" not in first.text
    assert "&#39;" not in first.text


@pytest.mark.parametrize("ext", FORMATS)
def test_multi_line_cues_join_with_a_space(ext: str) -> None:
    """Never 'First linesecond line' — that is what <br/> and itertext give."""
    assert parse(ext)[2].text == "First line second line"


@pytest.mark.parametrize("ext", FORMATS)
def test_empty_cues_are_dropped(ext: str) -> None:
    assert all(line.text.strip() for line in parse(ext))
    assert 8000 not in [line.start_ms for line in parse(ext)]


@pytest.mark.parametrize("ext", FORMATS)
def test_zero_duration_cue_gets_a_usable_end_time(ext: str) -> None:
    """A cue with start == end would render as a zero-width timestamp."""
    zero = parse(ext)[1]
    assert zero.start_ms == 3000
    assert zero.end_ms > zero.start_ms


@pytest.mark.parametrize("ext", FORMATS)
def test_no_line_contains_a_newline_or_markup(ext: str) -> None:
    """transcript.txt is line-based; a stray newline would corrupt the file."""
    for line in parse(ext):
        assert "\n" not in line.text
        assert "<" not in line.text and ">" not in line.text


@pytest.mark.parametrize("ext", FORMATS)
def test_timestamps_are_monotonic(ext: str) -> None:
    starts = [line.start_ms for line in parse(ext)]
    assert starts == sorted(starts)


# -- degenerate input -------------------------------------------------------


@pytest.mark.parametrize("ext", FORMATS)
def test_empty_payload_yields_no_lines(ext: str) -> None:
    assert _parse_captions(ext, b"") == []


@pytest.mark.parametrize("ext", FORMATS)
def test_garbage_payload_does_not_raise(ext: str) -> None:
    assert _parse_captions(ext, b"\x00not a caption file at all") == []


def test_unknown_extension_falls_back_to_sniffing() -> None:
    """An unrecognised ext must still parse a payload we can identify."""
    for ext in FORMATS:
        raw = (FIXTURES / f"captions.{ext}").read_bytes()
        assert _parse_captions("something-new", raw) == EXPECTED, ext


def test_rollup_repeats_are_collapsed() -> None:
    """Auto captions repeat the previous cue as they roll up the screen."""
    payload = (
        b'{"events":['
        b'{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"hello there"}]},'
        b'{"tStartMs":1000,"dDurationMs":1000,"segs":[{"utf8":"hello there"}]},'
        b'{"tStartMs":2000,"dDurationMs":1000,"aAppend":1,"segs":[{"utf8":"\\n"}]},'
        b'{"tStartMs":2000,"dDurationMs":1000,"segs":[{"utf8":"and goodbye"}]}]}'
    )
    lines = _parse_captions("json3", payload)
    assert [line.text for line in lines] == ["hello there", "and goodbye"]
    # The collapsed repeat extends the surviving line rather than vanishing.
    assert lines[0].end_ms == 2000
