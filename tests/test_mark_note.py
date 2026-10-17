"""The marking pass: a second call that lists a note's unmarked outside
statements, and code that wraps exactly those in `[note: ...]` markers.

Audited on one note written under write_note v9: every claim, quote and key
moment was faithful, and 14 statements of the writer's own knowledge,
arithmetic or extrapolation were unmarked. Ask reads a note as what the video
says (rule 26). The pass only lists; the code marks and changes nothing else.
Rule 30: a failed, cut or unreadable pass leaves the first note intact and
says so. Rule 14: what it did is counted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config, make_note_config

from marginalia.contracts import NOTED, STOP_REFUSED, NoteResult, NoteRun
from marginalia.enrich.note import apply_marks, parse_marks, write_notes
from marginalia.llm import LlmClient, LlmError

from test_note import (
    GOOD_NOTE,
    FakeResponse,
    FakeUsage,
    Http402,
    client,
    library_with,
    prompts,
)

MARK_PROMPT = (
    '---\nname: mark_note\nversion: "1"\n---\n'
    "Check {{ note }} against {{ transcript }}\n"
)
LEAVES_OUT_SPAN = "private detail about leaves out"


def marking_prompts(tmp_path: Path) -> Path:
    directory = prompts(tmp_path)
    (directory / "mark_note.md").write_text(MARK_PROMPT, encoding="utf-8")
    return directory


def marking(tmp_path: Path, *ids: str):
    config, library, ledger = library_with(tmp_path, *ids)
    from dataclasses import replace

    return replace(config, note=make_note_config(mark_pass=True)), library, ledger


# -- parsing the reply -----------------------------------------------------------


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("MARK: a dry chain wears twice as fast\nMARK: about two cents per hundred",
         ["a dry chain wears twice as fast", "about two cents per hundred"]),
        ("- MARK: “a dry chain wears twice as fast”", ["a dry chain wears twice as fast"]),
        ("NONE", []),
        ("```\nNONE\n```", []),
        ("I looked and everything seems fine.", None),
    ],
    ids=["two-spans", "bulleted-and-quoted", "none", "fenced-none", "unreadable"],
)
def test_a_marking_reply_is_read_strictly(reply, expected) -> None:
    assert parse_marks(reply) == expected


# -- applying marks: exactly once, outside protected ranges ---------------------

_BODY = (
    "## Claims\n- The chain wears steadily.\n"
    "## Leaves out\n- Never says how often to oil it, and a dry chain wears twice as fast.\n"
    "- Never names a brand. [note: the usual brands cost more]\n"
    "## Verbatim\n- \"a dry chain squeaks loudly\" (01:00)\n"
)


def test_a_listed_span_found_once_is_wrapped_and_nothing_else_changes() -> None:
    body, applied, rejected = apply_marks(_BODY, ["and a dry chain wears twice as fast"])

    assert (applied, rejected) == (1, 0)
    assert "[note: and a dry chain wears twice as fast]" in body
    assert body.replace("[note: and a dry chain wears twice as fast]",
                        "and a dry chain wears twice as fast") == _BODY


def test_a_span_too_short_to_match_safely_is_refused() -> None:
    body, applied, rejected = apply_marks(_BODY, ["the chain"])

    assert (applied, rejected) == (0, 1)
    assert body == _BODY


@pytest.mark.parametrize(
    "span, why",
    [
        ("a dry chain squeaks loudly", "inside Verbatim"),
        ("the usual brands cost more", "inside an existing marker"),
        ("nowhere in this note at all", "not in the note"),
        ("dry chain [wears]", "holds a bracket"),
    ],
)
def test_a_span_that_cannot_be_marked_safely_is_counted_not_guessed(span, why) -> None:
    body, applied, rejected = apply_marks(_BODY, [span])

    assert (applied, rejected) == (0, 1), why
    assert body == _BODY


def test_a_span_that_appears_twice_is_not_marked() -> None:
    doubled = _BODY + "## Key moments\n- The chain wears steadily.\n"

    body, applied, rejected = apply_marks(doubled, ["The chain wears steadily"])

    assert (applied, rejected) == (0, 1)
    assert body == doubled


# -- the pass in the drain --------------------------------------------------------


def test_the_second_pass_marks_what_it_lists_and_counts_it(tmp_path: Path) -> None:
    config, library, ledger = marking(tmp_path, "aaaaaaaaaaa")
    script = [GOOD_NOTE, f"MARK: {LEAVES_OUT_SPAN}\nMARK: not in the note at all here"]
    made = client(lambda n: script[n])

    run = write_notes(config, ledger, library, made, prompts_directory=marking_prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == NOTED
    assert (result.mark_ran, result.marked, result.mark_rejected, result.mark_problem) == (
        True, 1, 1, "",
    )
    assert "1 statement(s) marked by the second pass" in result.detail
    assert len(made.sent) == 2, "one note call, one marking call"
    assert f"[note: {LEAVES_OUT_SPAN}]" in library.read_note("aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").noted_at is not None
    # The marking call was fed the finished note and the transcript.
    assert "Check" in made.sent[1]["messages"][-1]["content"]


def _written_body(library) -> str:
    return library.read_note("aaaaaaaaaaa").split("\n", 1)[1]


@pytest.mark.parametrize(
    "second, problem",
    [
        ("cut", "cut off at the token ceiling"),
        ("I think this note is fine overall.", "unreadable reply"),
        (LlmError("boom"), "failed"),
    ],
    ids=["cut-off", "unreadable", "failed-call"],
)
def test_a_failed_second_pass_leaves_the_first_note_intact(tmp_path, second, problem) -> None:
    config, library, ledger = marking(tmp_path, "aaaaaaaaaaa")
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return FakeResponse(GOOD_NOTE, FakeUsage())
        if isinstance(second, Exception):
            raise second
        response = FakeResponse(f"MARK: {LEAVES_OUT_SPAN}", FakeUsage())
        if second == "cut":
            response.choices[0].finish_reason = "length"
        else:
            response.choices[0].message.content = second
        return response

    made = LlmClient(make_config(Path(".")).llm, transport=transport, sleep=lambda _: None)

    run = write_notes(config, ledger, library, made, prompts_directory=marking_prompts(tmp_path))

    result = run.results[0]
    assert result.outcome == NOTED
    assert result.mark_problem.startswith(problem)
    assert result.marked == 0
    assert "note kept as written" in result.detail
    assert "[note:" not in _written_body(library), "the first note stands, unmarked"
    assert ledger.get("aaaaaaaaaaa").noted_at is not None


def test_a_refusal_in_the_second_pass_stops_the_drain_and_keeps_the_note(tmp_path) -> None:
    config, library, ledger = marking(tmp_path, "aaaaaaaaaaa", "bbbbbbbbbbb")
    script = [GOOD_NOTE, Http402()]

    run = write_notes(
        config, ledger, library, client(lambda n: script[n]),
        prompts_directory=marking_prompts(tmp_path),
    )

    assert run.stopped_because == STOP_REFUSED
    assert library.has_note("aaaaaaaaaaa"), "the note this call paid for is kept"
    assert ledger.get("aaaaaaaaaaa").noted_at is not None
    assert not library.has_note("bbbbbbbbbbb"), "the drain stopped on the refusal"


def test_with_the_pass_off_there_is_one_call_per_note(tmp_path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    made = client(GOOD_NOTE)

    run = write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    assert len(made.sent) == 1
    assert run.results[0].mark_ran is False


# -- the report ---------------------------------------------------------------------


def test_the_note_report_counts_what_the_marking_pass_did(tmp_path, monkeypatch, capsys) -> None:
    from marginalia.cli import main

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
        NoteResult("aaaaaaaaaaa", NOTED, "x", version="10", mark_ran=True, marked=3,
                   mark_rejected=1),
        NoteResult("bbbbbbbbbbb", NOTED, "y", version="10", mark_ran=True,
                   mark_problem="cut off at the token ceiling"),
    ]
    monkeypatch.setattr("marginalia.cli.write_notes", lambda *a, **k: run)

    main(["note"])

    out = capsys.readouterr().out
    assert ("marking pass: 3 statement(s) marked in 2 note(s); 1 listed span(s) not applied; "
            "1 pass(es) failed, those notes kept as written") in out
