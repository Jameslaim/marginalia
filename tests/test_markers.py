"""A `[note: ...]` marker never cuts a quotation.

Measured on 37 stored notes: 53 marker boundaries sat inside a quoted unit, in
22 notes. Each one is now resolved — the unit kept inside the marker, or put
outside it when the transcript holds it at the line's own moment, or the marker
refused — and counted (rule 14). Weak evidence keeps the unit inside: slightly
discounting record is cheap, leaving the writer's words unmarked is not.
"""

from __future__ import annotations

from pathlib import Path

from marginalia.contracts import NOTED, NoteResult, NoteRun
from marginalia.enrich.markers import settle_markers

CUES = (
    "[00:00:10.000 --> 00:00:14.000] the cash is warmed every night\n"
    "[00:00:14.000 --> 00:00:18.000] and the kv case is thrown away\n"
    "[00:05:00.000 --> 00:05:04.000] much later something else entirely\n"
)


def words(text: str) -> str:
    return " ".join(text.replace("[note:", " ").replace("]", " ").split())


def test_a_caption_string_said_at_the_line_s_moment_is_put_outside() -> None:
    # The marker opens before the quote and closes inside it: the caption's own
    # words ended up in the writer's marker. The transcript holds them within
    # seconds of the line's timestamp, so they go back outside.
    line = '- (00:12) the captions say [note: a garble of "the cash is warmed] every night"\n'

    out, settle = settle_markers(line, CUES)

    assert (settle.straddles, settle.put_outside, settle.kept_inside) == (1, 1, 0)
    assert '"the cash is warmed every night"' in out
    assert words(out) == words(line), "only the brackets move"


def test_without_a_timestamp_on_the_line_the_unit_is_kept_inside() -> None:
    # "Somewhere in the transcript" is not evidence: a writer's correction can
    # occur elsewhere in its own transcript.
    line = '- the captions say [note: a garble of "the cash is warmed] every night"\n'

    out, settle = settle_markers(line, CUES)

    assert (settle.straddles, settle.kept_inside, settle.put_outside) == (1, 1, 0)
    assert out.index("[note:") < out.index('"the cash')
    assert out.index("night\"") < out.index("]")


def test_a_string_said_only_at_another_moment_is_kept_inside() -> None:
    line = '- (05:01) [note: the writer means "the cash is warmed] every night"\n'

    _out, settle = settle_markers(line, CUES)

    assert (settle.kept_inside, settle.put_outside) == (1, 0)


def test_a_single_short_word_is_weak_evidence_even_at_the_right_moment() -> None:
    line = '- (00:15) [note: written as "case]" for cache\n'

    _out, settle = settle_markers(line, CUES)

    assert (settle.kept_inside, settle.put_outside) == (1, 0)


def test_a_gloss_inside_a_quotation_grows_to_hold_the_whole_quotation() -> None:
    line = '- (00:15) "and the kv [note: cache] case is thrown away"\n'

    out, settle = settle_markers(line, CUES)

    assert (settle.straddles, settle.kept_inside) == (1, 1)
    assert out.index("[note:") < out.index('"and')
    assert words(out) == words(line)


def test_a_marker_holding_only_record_is_refused() -> None:
    # The closing boundary cuts the quote; once the quote is put outside, the
    # marker holds nothing of the writer's, so it is dropped and counted.
    line = '- (00:12) [note: "the cash is warmed] every night"\n'

    out, settle = settle_markers(line, CUES)

    assert (settle.straddles, settle.refused) == (1, 1)
    assert "[note:" not in out
    assert words(out) == words(line)


def test_a_marker_that_does_not_cut_a_quotation_is_untouched() -> None:
    line = '- (00:12) "the cash is warmed every night" [note: that is, the cache]\n'

    out, settle = settle_markers(line, CUES)

    assert out == line
    assert settle.straddles == 0
    assert settle.lines_checked == 1


def test_other_quotation_styles_are_units_too() -> None:
    line = '- (00:12) [note: rendered as «the cash is warmed] every night»\n'

    _out, settle = settle_markers(line, CUES)

    assert settle.straddles == 1


def test_straight_single_quotes_are_counted_as_unhandled_never_paired() -> None:
    line = "- it's said as 'the cash is warmed' here [note: a gloss]\n"

    out, settle = settle_markers(line, CUES)

    assert out == line
    assert settle.single_quoted_unhandled == 1


def test_the_note_worker_settles_the_writer_s_own_markers_before_writing(tmp_path: Path) -> None:
    from test_note import GOOD_NOTE, client, library_with, prompts
    from marginalia.enrich.note import write_notes

    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    note = GOOD_NOTE.replace(
        "## Wrong or shaky\nprivate detail about wrong or shaky — quadratic not exponential\n",
        '## Wrong or shaky\n- [note: a gloss on "the quoted] words"\n',
    )

    run = write_notes(config, ledger, library, client(note), prompts_directory=prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == NOTED
    assert (result.markers.straddles, result.markers.kept_inside) == (1, 1)
    assert "1 marker boundary(ies) cut a quotation" in result.detail
    stored = library.read_note("aaaaaaaaaaa")
    assert stored.index("[note:") < stored.index('"the quoted')


def test_the_note_report_counts_quotations_cut_by_markers(tmp_path, monkeypatch, capsys) -> None:
    from marginalia.cli import main
    from marginalia.contracts import MarkerSettle

    root = tmp_path / "library"
    configured = tmp_path / "config.toml"
    configured.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(configured))
    run = NoteRun(version="10", model="m", requests=2)
    run.results = [
        NoteResult("aaaaaaaaaaa", NOTED, "x", version="10",
                   markers=MarkerSettle(straddles=3, kept_inside=2, put_outside=1,
                                        lines_checked=5)),
        NoteResult("bbbbbbbbbbb", NOTED, "y", version="10",
                   markers=MarkerSettle(lines_checked=2, single_quoted_unhandled=1)),
    ]
    monkeypatch.setattr("marginalia.cli.write_notes", lambda *a, **k: run)

    main(["note"])

    out = capsys.readouterr().out
    assert ("quotations: 3 marker boundary(ies) cut one: 2 kept inside, 1 put outside, "
            "0 refused; 1 single-quoted unit(s) not handled; 7 line(s) held a marker and a "
            "quotation") in out
