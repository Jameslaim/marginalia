"""Logs have a home, and nothing in them is content.

Logging worked before this; where it went was never specified, which meant it
went nowhere the moment a command was not run with `-v` in a terminal someone
was watching. It now goes to `<library>/logs/`, one file per day, at DEBUG —
because DEBUG is where the unparsed model output lands, and unparsed output is
what you go looking for when a counter says something was dropped.

The rule that governs this file: **a log is a surface like any other.** Hard rule
1 does not stop applying because the destination is a file nobody reads. The
handler is attached to `marginalia` rather than the root logger precisely so the
HTTP client's DEBUG records — which carry request bodies, and therefore every
transcript and note the model was ever shown — cannot reach it.
"""

from __future__ import annotations

import logging
import re

import pytest

from marginalia.core.library import Library

from conftest import make_config

A = "dQw4w9WgXcQ"
B = "rGf9Db4tjUQ"

# Distinctive enough that a substring check is meaningful, and shaped like the
# thing it stands in for.
NOTE_TEXT = (
    "## What it claims\n\nThe speaker asserts a thousand-token minimum and never "
    "shows the flag that sets it. [note: this contradicts the documented default]"
)
TRANSCRIPT_TEXT = (
    "[00:00:00.000 --> 00:00:04.000] the minimum is one thousand tokens\n"
    "[00:00:04.000 --> 00:00:08.000] below that nothing is cached at all\n"
)


@pytest.fixture(autouse=True)
def clean_handlers():
    """Each test starts with no file handler attached.

    `main` attaches one per process and skips if the same path is already
    there — correct in production, and in a suite it would leak one test's
    library into the next test's assertions.
    """
    marginalia = logging.getLogger("marginalia")
    before = list(marginalia.handlers)
    marginalia.handlers = []
    yield
    for handler in marginalia.handlers:
        handler.close()
    marginalia.handlers = before


def build(tmp_path, monkeypatch):
    from marginalia import cli
    from test_ask import client

    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    for video_id in (A, B):
        library.video_dir(video_id).mkdir(parents=True, exist_ok=True)
        library.write_note(video_id, NOTE_TEXT)
        library.write_meta(video_id, {"title": f"title for {video_id}"})
    library.transcript_path(A).write_text(TRANSCRIPT_TEXT, encoding="utf-8")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli,
        "LlmClient",
        lambda *_a, **_k: client(
            [
                f"ESCALATE: {A}: exact wording",
                # The second citation names a video the library does not hold,
                # so the parser drops it — which is what puts a DEBUG record in
                # the file for the tests below to look for.
                f"He sets a minimum [{A} 00:31], and also [zzzzzzzzzzz 02:28].",
            ]
        ),
    )
    return cli, library


def log_text(library: Library) -> str:
    files = sorted(library.logs_dir.glob("*.log"))
    assert files, "nothing was written to logs/"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


# -- where it goes -----------------------------------------------------------


def test_a_command_writes_a_log_file_under_the_library(tmp_path, monkeypatch) -> None:
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["ask", "what is the minimum?"])

    files = sorted(library.logs_dir.glob("*.log"))
    assert len(files) == 1
    assert re.fullmatch(r"marg-\d{4}-\d{2}-\d{2}\.log", files[0].name)


def test_one_file_per_day(tmp_path) -> None:
    library = Library(make_config(tmp_path).library_path)
    assert library.log_path("2026-08-25").name == "marg-2026-08-25.log"
    assert library.log_path("2026-08-26") != library.log_path("2026-08-25")


def test_a_malformed_date_never_becomes_a_log_name(tmp_path) -> None:
    library = Library(make_config(tmp_path).library_path)
    with pytest.raises(ValueError):
        library.log_path("../../etc/passwd")


def test_the_same_command_twice_does_not_attach_two_handlers(
    tmp_path, monkeypatch
) -> None:
    """Two handlers on one path means every line written twice, which looks
    exactly like the loop running twice."""
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["ask", "q"])
    cli.main(["ask", "q"])

    assert len(logging.getLogger("marginalia").handlers) == 1


def test_an_unwritable_log_never_fails_the_command(tmp_path, monkeypatch, capsys) -> None:
    """The command was about to work. A log that cannot be opened is a warning,
    not a reason to refuse."""
    cli, library = build(tmp_path, monkeypatch)
    monkeypatch.setattr(
        logging, "FileHandler", lambda *_a, **_k: (_ for _ in ()).throw(OSError("locked"))
    )

    assert cli.main(["ask", "what is the minimum?"]) == 0
    assert "could not open the log file" in capsys.readouterr().err


# -- what goes in it ---------------------------------------------------------


def test_debug_records_reach_the_file(tmp_path, monkeypatch) -> None:
    """The reason the file is at DEBUG at all. `ask` logs each citation-shaped
    string it could not read at DEBUG, and that fragment is the next shape to
    teach the parser — it exists nowhere else once the terminal scrolls."""
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["ask", "what is the minimum?"])

    assert "DEBUG" in log_text(library)
    assert "unreadable citation" in log_text(library)


def test_debug_records_do_not_reach_the_terminal(tmp_path, monkeypatch, capsys) -> None:
    """Two destinations, two levels. The handler's level is what separates them
    — a root handler left at level 0 prints every DEBUG record that any logger
    lets through, whatever the root logger's own level says."""
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["ask", "what is the minimum?"])

    captured = capsys.readouterr()
    assert "unreadable citation" in log_text(library)
    assert "unreadable citation" not in captured.err
    assert "unreadable citation" not in captured.out


def test_no_transcript_text_reaches_the_log(tmp_path, monkeypatch) -> None:
    """Hard rule 1, one more surface — and the worst one to get wrong, because
    a log file accumulates every run and nobody reads it until something breaks.

    This run escalates: a transcript is opened, read, and sent to the model
    while the file handler is attached at DEBUG.
    """
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["-v", "ask", "what is the minimum?"])

    body = log_text(library)
    assert "the minimum is one thousand tokens" not in body
    assert "below that nothing is cached" not in body
    # The video was named, and that is the point of the distinction: the log
    # says which transcript was opened, never what was in it.
    assert A in body


def test_no_note_text_reaches_the_log(tmp_path, monkeypatch) -> None:
    """Notes are held to the same rule as transcripts. A note is about a tenth
    of a transcript, and a log that accumulated one per question would rebuild
    the cost curve the note layer exists to flatten."""
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["-v", "ask", "what is the minimum?"])

    body = log_text(library)
    assert "The speaker asserts a thousand-token minimum" not in body
    assert "[note:" not in body


def test_the_note_command_leaks_nothing_either(tmp_path, monkeypatch) -> None:
    """The other command that holds a whole transcript in memory. It reads the
    transcript, sends it, and writes a note — three chances to log content."""
    from marginalia import cli
    from test_ask import client

    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    library.video_dir(A).mkdir(parents=True, exist_ok=True)
    library.transcript_path(A).write_text(TRANSCRIPT_TEXT, encoding="utf-8")
    library.write_meta(A, {"title": "A video"})

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "LlmClient", lambda *_a, **_k: client(["a reply"]))

    cli.main(["-v", "note", A])

    if library.logs_dir.is_dir():
        body = log_text(library)
        assert "the minimum is one thousand tokens" not in body
        assert "below that nothing is cached" not in body


def test_the_handler_is_on_marginalia_not_the_root(tmp_path, monkeypatch) -> None:
    """The structural half of the rule above, and the half that keeps holding
    when someone adds a new log line without thinking about it.

    At DEBUG the root logger collects the HTTP client's records, and those carry
    request bodies — which on this project means every transcript and every note
    the model was ever shown. Nothing outside this package can reach the file.
    """
    cli, library = build(tmp_path, monkeypatch)

    cli.main(["ask", "q"])

    assert logging.getLogger("marginalia").handlers, "no file handler was attached"
    for handler in logging.getLogger().handlers:
        assert not isinstance(handler, logging.FileHandler)

    logging.getLogger("some_http_client").debug("POST body: %s", TRANSCRIPT_TEXT)
    for handler in logging.getLogger("marginalia").handlers:
        handler.flush()
    assert "one thousand tokens" not in log_text(library)
