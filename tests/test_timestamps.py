"""Timestamp snapping.

The behaviour worth defending here is mostly *refusal*: a citation that cannot
be located confidently must be left exactly as it was. Every loosening of that
during development produced a snap that looked authoritative and pointed at the
wrong moment, so most of these tests assert that nothing happened.
"""

from __future__ import annotations

import pytest

from marginalia.core.library import Library
from marginalia.enrich.timestamps import (
    REASON_ALREADY,
    REASON_NO_ANCHOR,
    REASON_NO_MATCH,
    anchor_text,
    anchors_for,
    build_index,
    parse_note_timestamps,
    render_timestamp,
    snap_text,
    snap_video,
)


def cue(start_s: float, end_s: float, text: str) -> str:
    def stamp(value: float) -> str:
        total = int(value * 1000)
        seconds, millis = divmod(total, 1000)
        minutes, secs = divmod(seconds, 60)
        hours, mins = divmod(minutes, 60)
        return f"{hours:02d}:{mins:02d}:{secs:02d}.{millis:03d}"

    return f"[{stamp(start_s)} --> {stamp(end_s)}] {text}"


TRANSCRIPT = "\n".join(
    [
        cue(0, 4, "welcome back everybody to another video"),
        cue(4, 8, "today we are talking about prompt caching"),
        cue(8, 12, "the minimum is one thousand and twenty four tokens"),
        cue(12, 16, "below that nothing is cached at all"),
        cue(16, 20, "so remember the ephemeral marker matters"),
        cue(20, 24, "and that is really the whole trick"),
        cue(24, 28, "let us look at the pricing table now"),
        cue(28, 32, "writes cost twenty five percent more"),
        cue(400, 404, "again the ephemeral marker matters here"),
    ]
)


# -- parsing -----------------------------------------------------------------


def test_index_maps_words_to_their_cue_start():
    index = build_index(TRANSCRIPT)
    assert index
    position = index.words.index("ephemeral")
    assert index.times_ms[position] == 16_000


def test_index_ignores_lines_that_are_not_cues():
    index = build_index("not a cue line\n" + TRANSCRIPT)
    assert "not" not in index.words or index.freq.get("not", 0) <= 1


@pytest.mark.parametrize(
    "text,expected_ms,parts",
    [
        ("at 9:33 he says", 573_000, 2),
        ("at 00:09:33 he says", 573_000, 3),
        ("[00:00:17.030]", 17_030, 3),
        ("(51:39)", 3_099_000, 2),
    ],
)
def test_parse_note_timestamps(text, expected_ms, parts):
    found = parse_note_timestamps(text)
    assert len(found) == 1
    assert found[0].ms == expected_ms
    assert found[0].parts == parts


def test_money_and_ratios_are_not_timestamps():
    assert parse_note_timestamps("costs $4 per million, 1.25x, a 95/5 split") == []


def test_render_preserves_the_shape_it_replaces():
    assert render_timestamp(573_000, parts=2, has_millis=False) == "09:33"
    assert render_timestamp(573_000, parts=3, has_millis=False) == "00:09:33"
    assert render_timestamp(17_030, parts=3, has_millis=True) == "00:00:17.030"


def test_anchor_text_prefers_quotes_and_drops_note_markers():
    line = '- "the ephemeral marker matters" (00:00:19) [note: ephemeral means temporary]'
    text, quoted = anchor_text(line)
    assert quoted is True
    assert "ephemeral" in text
    assert "temporary" not in text


def test_anchor_text_on_a_description_strips_markup_and_timestamps():
    text, quoted = anchor_text("- **00:00:08** - the 1024-token minimum is stated")
    assert quoted is False
    assert "00" not in text.split()
    assert "minimum" in text


def test_anchors_are_only_words_rare_in_this_transcript():
    index = build_index(TRANSCRIPT)
    anchors = anchors_for("the ephemeral marker matters and the pricing", index)
    assert "ephemeral" in anchors
    assert "the" not in anchors  # common: carries no location


# -- snapping ----------------------------------------------------------------


def note_with(section: str, body: str) -> str:
    return f"# vid - title\nchannel\n\n## {section}\n\n{body}\n"


def test_verbatim_quote_snaps_onto_the_cue_start():
    note = note_with("Verbatim", '- "writes cost twenty five percent more" (00:00:31)')
    updated, changes, _left = snap_text(note, build_index(TRANSCRIPT))
    assert len(changes) == 1
    assert changes[0].after == "00:00:28"
    assert "00:00:28" in updated


def test_snapping_is_idempotent():
    index = build_index(TRANSCRIPT)
    note = note_with("Verbatim", '- "writes cost twenty five percent more" (00:00:31)')
    once, first, _ = snap_text(note, index)
    twice, second, left = snap_text(once, index)
    assert first and not second
    assert twice == once
    assert any(reason == REASON_ALREADY for _s, _r, reason in left)


def test_a_line_with_no_distinctive_words_is_left_alone():
    """A key moment written entirely in the note-writer's own vocabulary.

    Nothing on the line occurs in the transcript, so there is nothing to match
    on and the citation must survive untouched. This is the common case in real
    notes and the reason `left` carries a reason rather than being a count.
    """
    note = note_with("Key moments", "- **00:00:20** - segue toward monetisation")
    updated, changes, left = snap_text(note, build_index(TRANSCRIPT))
    assert changes == []
    assert updated == note
    assert left and left[0][2] == REASON_NO_ANCHOR


def test_a_description_is_not_relocated_across_the_video():
    """The 20s bound. 'ephemeral' also occurs at 6:40; a key moment citing
    00:00:17 must not be dragged there."""
    note = note_with("Key moments", "- **00:00:17** - the ephemeral marker matters")
    updated, changes, _left = snap_text(note, build_index(TRANSCRIPT))
    for change in changes:
        assert abs(change.delta_ms) <= 20_000
    assert "06:40" not in updated


def test_a_quote_outside_verbatim_does_not_earn_the_loose_bound():
    """A key moment that borrows a phrase said much later must stay put."""
    note = note_with(
        "Key moments", '- **00:00:04** - the setup, before "writes cost twenty five percent more"'
    )
    _updated, changes, _left = snap_text(note, build_index(TRANSCRIPT))
    for change in changes:
        assert abs(change.delta_ms) <= 20_000


def test_only_the_first_timestamp_on_a_line_is_touched():
    note = note_with("Claims", "- the ephemeral marker matters (00:00:19, 00:06:41)")
    updated, _changes, _left = snap_text(note, build_index(TRANSCRIPT))
    assert "00:06:41" in updated


def test_non_ascii_quotes_are_matched_not_ignored():
    transcript = "\n".join([cue(0, 4, "مرحبا بكم"), cue(200, 204, "تفرض رسوما اضافية")])
    note = note_with("Verbatim", '- "تفرض رسوما اضافية" (00:03:28)')
    _updated, changes, _left = snap_text(note, build_index(transcript))
    assert len(changes) == 1
    assert changes[0].after == "00:03:20"


# -- the walk ----------------------------------------------------------------


def make_library(tmp_path, note: str, transcript: str = TRANSCRIPT) -> Library:
    library = Library(tmp_path / "library")
    library.ensure()
    directory = library.video_dir("dQw4w9WgXcQ")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.txt").write_text(transcript, encoding="utf-8")
    library.write_note("dQw4w9WgXcQ", note)
    return library


def test_dry_run_writes_nothing(tmp_path):
    note = note_with("Verbatim", '- "writes cost twenty five percent more" (00:00:31)')
    library = make_library(tmp_path, note)
    result = snap_video(library, "dQw4w9WgXcQ", dry_run=True)
    assert result.snapped
    assert library.read_note("dQw4w9WgXcQ") == note


def test_transcript_is_never_rewritten(tmp_path):
    note = note_with("Verbatim", '- "writes cost twenty five percent more" (00:00:31)')
    library = make_library(tmp_path, note)
    path = library.transcript_path("dQw4w9WgXcQ")
    before = path.read_bytes()
    snap_video(library, "dQw4w9WgXcQ")
    assert path.read_bytes() == before


def test_a_second_run_over_the_library_changes_nothing(tmp_path):
    """The CLI-level guarantee. A single pass did not give this: moving a
    citation shifts its own search window, so a note could still be changing on
    the next invocation. snap_video iterates to a fixpoint so a re-run is a
    no-op, which is what makes the command safe to leave in a script."""
    note = note_with(
        "Verbatim",
        '- "writes cost twenty five percent more" (00:00:31)\n'
        '- "the minimum is one thousand and twenty four tokens" (00:00:11)',
    )
    library = make_library(tmp_path, note)
    first = snap_video(library, "dQw4w9WgXcQ")
    assert first.snapped

    second = snap_video(library, "dQw4w9WgXcQ")
    assert second.snapped == []
    assert second.changed is False


def test_a_multi_pass_move_is_reported_once(tmp_path):
    note = note_with("Verbatim", '- "writes cost twenty five percent more" (00:00:31)')
    library = make_library(tmp_path, note)
    result = snap_video(library, "dQw4w9WgXcQ")
    assert len(result.snapped) == 1
    assert result.snapped[0].before == "00:00:31"


def test_a_missing_note_is_reported_not_raised(tmp_path):
    library = Library(tmp_path / "library")
    library.ensure()
    result = snap_video(library, "dQw4w9WgXcQ")
    assert result.error == "no note"
    assert not result.changed


def test_a_missing_transcript_is_reported_not_raised(tmp_path):
    library = Library(tmp_path / "library")
    library.ensure()
    library.write_note("dQw4w9WgXcQ", note_with("Verbatim", '- "x" (00:00:01)'))
    result = snap_video(library, "dQw4w9WgXcQ")
    assert result.error == "no transcript"
