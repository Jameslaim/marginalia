"""Quoted strings outside Verbatim are counted against the transcript.

Only Verbatim quotes were checked, and a note quoted a word elsewhere with a
spelling the transcript does not have. Counted per note and section, and in the
report line; nothing is marked (measure only).
"""

from __future__ import annotations

from marginalia.contracts import NOTED, NoteResult, NoteRun
from marginalia.enrich.note import quotes_outside_verbatim

TRANSCRIPT = "[00:00:00.000 --> 00:00:04.000] oil the chain every two hundred kilometres\n"


def test_quotes_are_counted_per_section_outside_verbatim_and_markers() -> None:
    note = (
        "## Claims\n\n- the video says \"oil the chain every two hundred kilometres\"\n\n"
        "## Wrong or shaky\n\n- it spells it \"oyle the chaine every week\"\n"
        "- [note: the writer's own \"words in a marker\" are not counted]\n\n"
        "## Verbatim\n\n- \"a verbatim quote is checked elsewhere\" (00:00)\n"
    )

    counts = quotes_outside_verbatim(note, TRANSCRIPT)

    assert counts == {"Claims": (1, 0), "Wrong or shaky": (0, 1)}


def test_a_note_with_no_quotes_outside_verbatim_counts_nothing() -> None:
    assert quotes_outside_verbatim("## Claims\n\n- plain prose only\n", TRANSCRIPT) == {}


def test_the_note_report_counts_quotes_outside_verbatim(tmp_path, monkeypatch, capsys) -> None:
    from marginalia.cli import main

    root = tmp_path / "library"
    configured = tmp_path / "config.toml"
    configured.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(configured))
    run = NoteRun(version="11", model="m", requests=2)
    run.results = [
        NoteResult("aaaaaaaaaaa", NOTED, "x", version="11",
                   quotes_outside={"Claims": (3, 1), "Wrong or shaky": (0, 2)}),
        NoteResult("bbbbbbbbbbb", NOTED, "y", version="11", quotes_outside={"Claims": (4, 0)}),
    ]
    monkeypatch.setattr("marginalia.cli.write_notes", lambda *a, **k: run)

    main(["note"])

    out = capsys.readouterr().out
    assert ("quotes outside Verbatim: 3 of 10 not found in the transcript, in 1 note(s); "
            "counted, not marked") in out


# -- split by script (H) --------------------------------------------------------
#
# On a note of a video in another language, the counter mixed English renderings,
# which cannot be in the transcript, with real misquotes in the speaker's script.

GREEK = "[00:00:00.000 --> 00:00:04.000] λαδώστε την αλυσίδα κάθε διακόσια χιλιόμετρα\n"


def test_quotes_outside_verbatim_are_split_by_the_transcript_s_script() -> None:
    from marginalia.enrich.note import SCRIPT_OTHER, SCRIPT_SAME, quotes_outside_by_script

    note = (
        "## Claims\n\n- the speaker says \"λαδώστε την αλυσίδα κάθε διακόσια χιλιόμετρα\"\n"
        "- misquoted as \"λαδώστε την αλυσίδα κάθε εβδομάδα πάντα\"\n"
        "- rendered as \"oil the chain every two hundred kilometres\"\n"
        "- a figure, \"200 km\" each time\n"
    )

    split = quotes_outside_by_script(note, GREEK)

    assert split == {SCRIPT_SAME: (1, 1), SCRIPT_OTHER: (0, 1)}
    assert sum(f + m for f, m in quotes_outside_verbatim(note, GREEK).values()) == 3


def test_the_report_line_shows_the_split(tmp_path, monkeypatch, capsys) -> None:
    from marginalia.cli import main

    root = tmp_path / "library"
    configured = tmp_path / "config.toml"
    configured.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(configured))
    run = NoteRun(version="11", model="m", requests=2)
    run.results = [
        NoteResult("aaaaaaaaaaa", NOTED, "x", version="11",
                   quotes_outside={"Claims": (3, 4)},
                   quotes_outside_script={"same script": (3, 1), "other script": (0, 3)}),
    ]
    monkeypatch.setattr("marginalia.cli.write_notes", lambda *a, **k: run)

    main(["note"])

    out = capsys.readouterr().out
    assert ("quotes outside Verbatim: 4 of 7 not found in the transcript, in 1 note(s); "
            "counted, not marked (same script: 1 of 4 not found; "
            "other script: 3 of 3 not found)") in out
