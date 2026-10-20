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
