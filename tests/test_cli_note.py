"""The `marg note` report.

The report is a contract a skill reads, so it is terse and one line per video.
The rule it exists to keep is the same one that makes marginalia a CLI: no
transcript and no note content ever reaches stdout. A note is ten times smaller
than a transcript and thirty-six of them still fill a context window, so a
skill that printed one back would rebuild exactly the cost curve this design
avoids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginalia.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from marginalia.contracts import FAILED, NOTED, SKIPPED, NoteResult, NoteRun

# The distinctive strings the leak tests look for.
SECRET_NOTE = "## Claims\nthe speaker calls quadratic growth exponential twice\n"
# Repeated to clear `[note] min_transcript_chars`: the floor is a real guard,
# and a fixture below it would skip the very note this test inspects.
SECRET_TRANSCRIPT = (
    "[00:00:00.000 --> 00:00:02.000] a phrase only the transcript contains\n" * 20
)


def fake_run(**overrides) -> NoteRun:
    run = NoteRun(version="1", model="test-model", requests=3, cost_usd=0.21, elapsed_s=134.0)
    run.results = [
        NoteResult("aaaaaaaaaaa", NOTED, "1,432 tokens", tokens=1432, version="1"),
        NoteResult("bbbbbbbbbbb", SKIPPED, "current note is v1", version="1"),
        NoteResult("ccccccccccc", FAILED, "transient: connection refused"),
    ]
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


@pytest.fixture()
def stubbed(configured: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_write_notes(config, ledger, library, client, **kwargs):
        captured.update(kwargs)
        return fake_run()

    monkeypatch.setattr("marginalia.cli.write_notes", fake_write_notes)
    return captured


# -- the report -------------------------------------------------------------


def test_one_line_per_video_then_a_summary(stubbed, capsys) -> None:
    assert main(["note"]) == EXIT_FAILED  # one failed video
    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "noted     aaaaaaaaaaa  1,432 tokens  v1"
    assert lines[1] == "skipped   bbbbbbbbbbb  current note is v1"
    assert lines[2] == "failed    ccccccccccc  transient: connection refused"


def test_the_summary_reports_cost_and_duration(stubbed, capsys) -> None:
    """Acceptance asks what a full run cost and how long it took. Both belong
    on the line the skill already reads, not in a log nobody opens."""
    main(["note"])
    summary = capsys.readouterr().out.strip().splitlines()[-1]

    assert summary == "run:      1 noted, 1 skipped, 1 failed, 3 requests, $0.21, 2m14s"


def test_a_clean_run_exits_zero(stubbed, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.cli.write_notes",
        lambda *a, **k: fake_run(results=[NoteResult("aaaaaaaaaaa", NOTED, "1,432 tokens", version="1")]),
    )
    assert main(["note"]) == EXIT_OK


def test_flags_reach_the_worker(stubbed) -> None:
    main(["note", "--limit", "3", "--regenerate", "--all"])
    assert stubbed["limit"] == 3
    assert stubbed["regenerate"] is True
    assert stubbed["every"] is True


def test_a_url_is_canonicalised_to_an_id(stubbed) -> None:
    main(["note", "https://youtu.be/dQw4w9WgXcQ?t=90"])
    assert stubbed["video_ids"] == ["dQw4w9WgXcQ"]


def test_a_bad_id_is_a_usage_error(configured, capsys) -> None:
    assert main(["note", "not a video"]) == EXIT_USAGE


def test_status_reports_current_and_stale(configured, capsys) -> None:
    assert main(["note", "--status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("notes:    0 with transcript, 0 current, 0 stale, 0 missing")


# -- the rule ----------------------------------------------------------------


def test_no_note_content_reaches_stdout(stubbed, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.cli.write_notes",
        lambda *a, **k: fake_run(
            results=[NoteResult("aaaaaaaaaaa", NOTED, "1,432 tokens", version="1")]
        ),
    )
    main(["note"])

    captured = capsys.readouterr()
    assert "quadratic" not in captured.out + captured.err


def test_a_real_run_prints_neither_the_transcript_nor_the_note(
    configured: Path, monkeypatch, capsys
) -> None:
    """End to end with only the model mocked: the worker reads a real
    transcript off disk and writes a real note, and neither appears."""
    from marginalia.core.ledger import Ledger
    from marginalia.core.library import Library
    from marginalia.enrich.note import REQUIRED_SECTIONS
    from marginalia.providers.base import TranscriptLine

    library = Library(configured)
    library.ensure()
    # Twenty lines, to clear `[note] min_transcript_chars`. The floor is a
    # real guard and a fixture under it would skip the note this inspects.
    library.write_transcript(
        "aaaaaaaaaaa",
        [
            TranscriptLine(i * 2000, (i + 1) * 2000,
                           "a phrase only the transcript contains")
            for i in range(20)
        ],
    )
    library.write_meta("aaaaaaaaaaa", {"title": "A video", "channel": "Chan"})
    with Ledger(library.ledger_path) as ledger:
        ledger.mark_pending("aaaaaaaaaaa")
        ledger.mark_has_transcript("aaaaaaaaaaa", source="captions", provider="ytdlp")

    body = "\n".join(f"## {section}\nquadratic not exponential\n" for section in REQUIRED_SECTIONS)

    class Response:
        choices = [type("C", (), {"message": type("M", (), {"content": body})()})()]
        usage = type("U", (), {"prompt_tokens": 4000, "completion_tokens": 1432})()
        model = "test-model"

    monkeypatch.setattr(
        "marginalia.llm.LlmClient._send", lambda self, messages, max_tokens, extra: Response()
    )

    assert main(["note"]) == EXIT_OK

    captured = capsys.readouterr()
    assert library.has_note("aaaaaaaaaaa")
    assert "quadratic" not in captured.out + captured.err
    assert "a phrase only the transcript contains" not in captured.out + captured.err
    assert captured.out.splitlines()[0].startswith("noted     aaaaaaaaaaa")


# -- a video id may begin with a hyphen ---------------------------------------
#
# `-Ueh5XBpcoY`, `-0czfBICp74`: roughly one id in thirty-two starts with one, and
# argparse reads such a token as an option. `marg note --regenerate -Ueh5XBpcoY`
# failed with "unrecognized arguments" — a documented command unusable for those
# videos. Same family as the citation repair: the id is right, the parser was not.


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["note", "--regenerate", "-Ueh5XBpcoY"], ["note", "--regenerate", "--", "-Ueh5XBpcoY"]),
        (["note", "-Ueh5XBpcoY", "dQw4w9WgXcQ"], ["note", "--", "-Ueh5XBpcoY", "dQw4w9WgXcQ"]),
        (["snap-timestamps", "-0czfBICp74"], ["snap-timestamps", "--", "-0czfBICp74"]),
    ],
)
def test_a_hyphen_leading_id_is_shielded(argv, expected) -> None:
    from marginalia.cli import _shield_leading_hyphen_ids

    assert list(_shield_leading_hyphen_ids(argv)) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["note", "--regenerate", "--all"],   # long options are untouched
        ["note", "-v"],                      # short options are untouched
        ["ask", "-v", "what is caching"],
        ["note", "--", "-Ueh5XBpcoY"],       # already ended; not doubled
        ["note", "-not-an-id"],              # wrong length
        ["discover", "-YTupW6YG1"],          # ten characters, not an id
    ],
)
def test_nothing_else_is_shielded(argv) -> None:
    """Narrow on purpose. Every real option here is two characters or long-form,
    so the eleven-character rule cannot collide with one."""
    from marginalia.cli import _shield_leading_hyphen_ids

    assert list(_shield_leading_hyphen_ids(argv)) == argv


def test_the_hyphen_id_reaches_the_command(stubbed) -> None:
    """End to end through `main`: the id argparse used to reject now arrives as
    a video rather than as an unrecognized option."""
    assert main(["note", "-Ueh5XBpcoY"]) in (EXIT_OK, EXIT_FAILED)

    assert stubbed["video_ids"] == ["-Ueh5XBpcoY"]


def test_snap_timestamps_prints_citations_and_never_the_notes_line(configured, capsys) -> None:
    """It printed seventy characters of the note's line beside every move, and
    on a `## Verbatim` line those characters are the video's own words."""
    from marginalia.core.library import Library
    from marginalia.providers.base import TranscriptLine

    library = Library(configured)
    library.ensure()
    spoken = "the minimum cacheable prefix is one thousand tokens"
    library.write_transcript(
        "aaaaaaaaaaa",
        [TranscriptLine(0, 3000, "welcome back"), TranscriptLine(3000, 6000, spoken)],
    )
    library.write_note("aaaaaaaaaaa", f'## Verbatim\n- "{spoken}" (00:00:05)\n')

    assert main(["snap-timestamps"]) == EXIT_OK
    out = capsys.readouterr().out

    assert "00:00:05 -> 00:00:03" in out
    assert "cacheable" not in out
