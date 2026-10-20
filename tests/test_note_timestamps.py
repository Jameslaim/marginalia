"""Timestamps a note writes that name no moment in its video.

Measured: one note wrote every moment as seconds and hundredths in a clock
shape, so all of its readings fell past the end of a short video, the snapper
placed none of them, and nothing said so — the note was recorded as current.
These tests hold the counter (rule 14), the redraw-once response (rule 30), and
the same check where answers and the snap command read timestamps (rule 25).
They also hold the other side: a reading a second or two past the listed
duration is a caption end time, not an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config

from marginalia.ask import citations_past_end, render_answer
from marginalia.contracts import FAILED, NOTED, Answer, AnswerCitation, NoteResult, NoteRun
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library
from marginalia.enrich.note import write_notes
from marginalia.enrich.timestamps import (
    END_TOLERANCE_MS,
    check_timestamps,
    transcript_end_ms,
    unusable,
)
from marginalia.providers.base import TranscriptLine

from test_note import GOOD_NOTE, client, library_with, prompts

ONE_HOUR_TWO = 3720  # `library_with`'s listed duration, in seconds


def note_citing(*readings: str) -> str:
    """A valid note whose Key moments cite ``readings`` and nothing else does."""
    moments = "\n".join(f"- {reading} — a moment" for reading in readings)
    return GOOD_NOTE.replace(
        "## Key moments\nprivate detail about key moments — quadratic not exponential\n",
        f"## Key moments\n{moments}\n",
    )


# -- the counter -------------------------------------------------------------------


def test_the_measured_shape_is_counted_as_unplaceable() -> None:
    # Seconds and hundredths written as a clock: read as minutes:seconds they
    # fall far past a 337-second video, and a field of 60 or more is no clock
    # reading at all. Minutes past 99 were invisible to the snapper's parser.
    text = "at 38:39, then 116:24, then 01:15:76, then 00:55:84"

    check = check_timestamps(text, duration_s=337, transcript_end_ms=335_500)

    assert (check.total, check.unplaceable, check.malformed, check.in_tolerance) == (4, 4, 2, 0)
    assert unusable(check)


def test_a_caption_end_time_just_past_the_listed_duration_is_not_an_error() -> None:
    # Listed 49s; captions end at 50.2s; the note cites 00:50 and 00:49.
    check = check_timestamps("from 00:49 to 00:50", duration_s=49, transcript_end_ms=50_200)

    assert (check.total, check.unplaceable, check.in_tolerance) == (2, 0, 1)
    assert check.placeable == 2


def test_the_tolerance_is_a_bound_not_a_guess() -> None:
    end_s = 100
    inside = f"01:{(end_s + END_TOLERANCE_MS // 1000) - 60:02d}"
    beyond = f"01:{(end_s + END_TOLERANCE_MS // 1000) - 60 + 1:02d}"

    assert check_timestamps(inside, duration_s=end_s).unplaceable == 0
    assert check_timestamps(beyond, duration_s=end_s).unplaceable == 1


@pytest.mark.parametrize("text", ["a 100:1 ratio", "a 16:9 frame", "costs 2:1"])
def test_a_ratio_is_not_a_clock_reading(text) -> None:
    assert check_timestamps(text, duration_s=60).total == 0


def test_with_no_end_to_check_against_nothing_is_called_unplaceable() -> None:
    check = check_timestamps("at 75:10", duration_s=None, transcript_end_ms=0)

    assert (check.total, check.unplaceable, check.checked) == (1, 0, False)


def test_the_transcript_end_is_the_last_caption_end_time() -> None:
    transcript = (
        "[00:00:00.000 --> 00:00:04.000] a\n"
        "[00:00:03.500 --> 00:00:50.200] b\n"
        "[00:00:46.700 --> 00:00:49.000] c\n"
    )

    assert transcript_end_ms(transcript) == 50_200


# -- the note worker: counted, redrawn once, never written unusable ---------------


def test_a_few_unplaceable_readings_are_written_and_counted(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    note = note_citing("00:10", "00:20", "00:30", "75:10")  # one past the end of 62 min

    run = write_notes(config, ledger, library, client(note), prompts_directory=prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == NOTED
    assert (result.timestamps.total, result.timestamps.unplaceable) == (4, 1)
    assert result.redrawn is False
    assert "1 of 4 timestamp(s) name no moment in the video" in result.detail
    assert library.has_note("aaaaaaaaaaa")


def test_a_mostly_unplaceable_note_is_drawn_once_more(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    broken = note_citing("75:10", "88:20", "01:15:76")
    good = note_citing("00:10", "00:20", "00:30")
    script = [broken, good]
    made = client(lambda n: script[n])

    run = write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == NOTED
    assert result.redrawn is True
    assert result.timestamps.unplaceable == 0
    assert len(made.sent) == 2, "the first draw and one more, never a third"
    assert "drawn twice" in result.detail
    assert ledger.get("aaaaaaaaaaa").noted_at is not None


def test_a_note_unusable_twice_is_not_written_and_stays_due(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    broken = note_citing("75:10", "88:20", "01:15:76")
    made = client(lambda n: broken)

    run = write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == FAILED
    assert result.detail.startswith("timestamps unusable: 3 of 3")
    assert result.redrawn is True
    assert not library.has_note("aaaaaaaaaaa"), "never written as if it were whole"
    row = ledger.get("aaaaaaaaaaa")
    assert row.noted_at is None and row.note_failures == 1
    assert len(made.sent) == 2


def test_the_note_report_counts_timestamps_that_name_no_moment(
    tmp_path, monkeypatch, capsys
) -> None:
    from marginalia.cli import main
    from marginalia.contracts import TimestampCheck

    root = tmp_path / "library"
    configured = tmp_path / "config.toml"
    configured.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(configured))
    run = NoteRun(version="10", model="m", requests=4)
    run.results = [
        NoteResult("aaaaaaaaaaa", NOTED, "x", version="10",
                   timestamps=TimestampCheck(total=40, unplaceable=0, in_tolerance=2)),
        NoteResult("bbbbbbbbbbb", NOTED, "y", version="10", redrawn=True,
                   timestamps=TimestampCheck(total=30, unplaceable=1)),
        NoteResult("ccccccccccc", FAILED, "z", redrawn=True,
                   timestamps=TimestampCheck(total=20, unplaceable=19, malformed=5)),
    ]
    monkeypatch.setattr("marginalia.cli.write_notes", lambda *a, **k: run)

    main(["note"])

    out = capsys.readouterr().out
    assert ("timestamps: 20 of 90 name no moment in the video, in 2 note(s); 2 within the "
            "caption tolerance past the listed end; 2 note(s) drawn twice, 1 of them not "
            "written") in out


# -- rule 25: the same question wherever a model's timestamps are read -------------


def test_snap_timestamps_reports_readings_that_name_no_moment(
    tmp_path, monkeypatch, capsys
) -> None:
    from marginalia.cli import main

    root = tmp_path / "library"
    configured = tmp_path / "config.toml"
    configured.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(configured))
    library = Library(root)
    library.ensure()
    library.write_transcript("aaaaaaaaaaa", [TranscriptLine(0, 50_200, "words said here")])
    library.write_meta("aaaaaaaaaaa", {"video_id": "aaaaaaaaaaa", "duration_s": 49})
    library.write_note("aaaaaaaaaaa", "# header\n" + note_citing("00:50", "38:39", "01:15:76"))

    code = main(["snap-timestamps", "--dry-run"])

    captured = capsys.readouterr()
    out = captured.out
    assert code == 0, captured.err
    assert "2 of 3 timestamp(s) name no moment in the video" in out
    assert ("placing:  2 of 3 timestamp(s) cannot be placed in their video, in 1 note(s); "
            "1 within the caption tolerance past the listed end") in out


def _library_for_citations(tmp_path: Path) -> Library:
    library = Library(make_config(tmp_path).library_path)
    library.ensure()
    library.write_transcript("aaaaaaaaaaa", [TranscriptLine(0, 50_200, "words said here")])
    library.write_meta("aaaaaaaaaaa", {"video_id": "aaaaaaaaaaa", "duration_s": 49})
    return library


def test_an_answer_citation_past_its_video_end_is_counted(tmp_path) -> None:
    library = _library_for_citations(tmp_path)
    cited = [
        AnswerCitation(video_id="aaaaaaaaaaa", seconds=10, raw="[aaaaaaaaaaa 00:10]"),
        AnswerCitation(video_id="aaaaaaaaaaa", seconds=50, raw="[aaaaaaaaaaa 00:50]"),
        AnswerCitation(video_id="aaaaaaaaaaa", seconds=2319, raw="[aaaaaaaaaaa 38:39]"),
    ]

    assert citations_past_end(library, cited) == ["[aaaaaaaaaaa 38:39]"]


def test_the_answer_record_lists_citations_past_the_end(tmp_path) -> None:
    answer = Answer(question="q", text="t", version="1", model="m",
                    past_end=["[aaaaaaaaaaa 38:39]"])

    rendered = render_answer(answer, asked_at="2026-10-20T00:00:00Z")

    assert "Later than the end of the video it cites:" in rendered
    assert "[aaaaaaaaaaa 38:39]" in rendered


def test_the_ledger_is_not_touched_by_counting(tmp_path) -> None:
    # Counting is pure: it reads text and numbers and writes nothing.
    library = _library_for_citations(tmp_path)
    ledger = Ledger(library.ledger_path)
    before = ledger.get("aaaaaaaaaaa")
    check_timestamps("38:39", duration_s=49, transcript_end_ms=50_200)
    assert ledger.get("aaaaaaaaaaa") == before
