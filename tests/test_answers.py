"""Answers land on disk.

An answer costs a model call over the whole library and carries citations,
links, costs and the list of notes it was written from. Until now all of that
existed until the terminal scrolled. These tests are about the record, not the
answering — the model is scripted throughout.

The rule that outranks everything here: the file may name notes and transcripts,
and must never contain them. It is written into the library, which is the one
place a note-sized leak would sit unnoticed forever.
"""

from __future__ import annotations

import json
import re

import pytest

from marginalia.ask import render_answer
from marginalia.contracts import Answer, AnswerCitation
from marginalia.core.library import Library, slugify

from conftest import make_config

A = "dQw4w9WgXcQ"
B = "rGf9Db4tjUQ"

NOTE_TEXT = "the speaker claims a thousand-token minimum and never shows the flag"
TRANSCRIPT_TEXT = "[00:00:00.000 --> 00:00:04.000] below that nothing is cached\n"


def answer(**overrides) -> Answer:
    values = dict(
        question="How does prompt caching work?",
        text=f"He sets a minimum [{A} 00:31].",
        version="2",
        model="test-model",
        notes_loaded=2,
        citations=[AnswerCitation(video_id=A, seconds=31, raw=f"[{A} 00:31]")],
        titles={A: "Caching deep dive", B: "Another video"},
        requests=1,
        input_tokens=90_000,
        output_tokens=1_200,
        cost_usd=0.288,
        elapsed_s=12.5,
    )
    values.update(overrides)
    made = Answer(question=values.pop("question"))
    for key, value in values.items():
        setattr(made, key, value)
    return made


# -- the slug ----------------------------------------------------------------


def test_a_question_becomes_a_recognisable_filename() -> None:
    assert slugify("How does prompt caching work?") == "how-does-prompt-caching-work"


def test_punctuation_and_script_cannot_reach_the_filename() -> None:
    """A question may contain quotes, slashes and colons — all of which are
    either path syntax or illegal on some filesystem this will run on."""
    assert "/" not in slugify('What does "pool_mode: transaction" do?')
    assert slugify("キャッシュとは？") == "question"
    assert slugify("///") == "question"


def test_a_long_question_is_cut_at_a_word() -> None:
    slug = slugify("what exactly happens when the cache expires and nobody notices at all")
    assert len(slug) <= 60
    assert not slug.endswith("-")
    assert slug.split("-")[-1] in {"and", "nobody", "notices", "at", "expires"}


# -- the file ----------------------------------------------------------------


def test_the_answer_file_carries_everything_needed_to_judge_it_later(tmp_path) -> None:
    rendered = render_answer(answer(), asked_at="2026-08-25T09:15:00+00:00")

    assert "# How does prompt caching work?" in rendered
    assert "He sets a minimum" in rendered          # the answer, verbatim
    assert "2026-08-25T09:15:00+00:00" in rendered  # when
    assert "notes loaded: 2" in rendered            # what it read
    assert "answer v2" in rendered                  # which prompt
    assert "$0.2880" in rendered                    # what it cost
    assert "12.5s" in rendered                      # how long it took
    assert "youtube.com/watch?v=dQw4w9WgXcQ&t=31s" in rendered  # the citations
    assert "Caching deep dive" in rendered


def test_an_escalation_is_recorded_with_the_reason_the_model_gave(tmp_path) -> None:
    """Which transcript was opened is half the record; *why* is the half that
    says whether the notes were insufficient or the model was being thorough."""
    rendered = render_answer(
        answer(opened=[(A, "the question asks for exact wording")]),
        asked_at="2026-08-25T09:15:00+00:00",
    )

    assert "## Transcripts opened" in rendered
    assert f"- {A} — the question asks for exact wording" in rendered


def test_no_escalation_says_so_rather_than_leaving_a_blank(tmp_path) -> None:
    rendered = render_answer(answer(), asked_at="2026-08-25T09:15:00+00:00")
    assert "(none — the notes were enough)" in rendered


def test_unreadable_citations_are_kept_in_the_file(tmp_path) -> None:
    """The counter is printed and then lost. The fragment is the next shape to
    teach the parser, and it exists nowhere else once the answer scrolls away."""
    rendered = render_answer(
        answer(unparsed=["01:00-01:09, 02:28"]), asked_at="2026-08-25T09:15:00+00:00"
    )
    assert "Citation-shaped but unreadable" in rendered
    assert "01:00-01:09, 02:28" in rendered


def test_an_empty_answer_still_renders(tmp_path) -> None:
    """The failed answer is the one most worth having a record of: it carries
    the cost, the prompt version and the notes that were loaded."""
    rendered = render_answer(answer(text=""), asked_at="2026-08-25T09:15:00+00:00")
    assert "(the model returned nothing)" in rendered
    assert "notes loaded: 2" in rendered


# -- writing -----------------------------------------------------------------


def test_the_file_lands_under_answers_named_for_the_day_and_the_question(tmp_path) -> None:
    library = Library(make_config(tmp_path).library_path)
    library.ensure()

    path = library.write_answer("How does prompt caching work?", "body", day="2026-08-25")

    assert path.parent == library.answers_dir
    assert path.name == "2026-08-25-how-does-prompt-caching-work.md"
    assert path.read_text(encoding="utf-8") == "body\n"


def test_a_second_answer_to_the_same_question_never_overwrites_the_first(tmp_path) -> None:
    """The earlier answer is what you compare against when a prompt change was
    supposed to have improved something. A counter, not a clock: "which came
    second" is the useful distinction."""
    library = Library(make_config(tmp_path).library_path)
    library.ensure()

    first = library.write_answer("same question", "first", day="2026-08-25")
    second = library.write_answer("same question", "second", day="2026-08-25")
    third = library.write_answer("same question", "third", day="2026-08-25")

    assert first.name == "2026-08-25-same-question.md"
    assert second.name == "2026-08-25-same-question-2.md"
    assert third.name == "2026-08-25-same-question-3.md"
    assert first.read_text(encoding="utf-8") == "first\n"


def test_a_malformed_day_is_refused(tmp_path) -> None:
    library = Library(make_config(tmp_path).library_path)
    with pytest.raises(ValueError):
        library.write_answer("q", "body", day="25/08/2026")


# -- through the command -----------------------------------------------------


def build(tmp_path, monkeypatch, reply: str):
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
    monkeypatch.setattr(cli, "LlmClient", lambda *_a, **_k: client(reply))
    return cli, library


def saved_files(library: Library):
    return sorted(library.answers_dir.glob("*.md")) if library.answers_dir.is_dir() else []


def test_asking_saves_the_answer_by_default(tmp_path, monkeypatch, capsys) -> None:
    cli, library = build(tmp_path, monkeypatch, [f"He sets a minimum [{A} 00:31]."])

    assert cli.main(["ask", "what is the minimum?"]) == 0

    files = saved_files(library)
    assert len(files) == 1
    # The date is today's, so it is checked for shape rather than value.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-what-is-the-minimum\.md", files[0].name)
    body = files[0].read_text(encoding="utf-8")
    assert "He sets a minimum" in body
    assert "what is the minimum?" in body
    assert str(files[0]) in capsys.readouterr().out


def test_no_save_is_the_exception(tmp_path, monkeypatch) -> None:
    cli, library = build(tmp_path, monkeypatch, [f"Answered [{A} 00:31]."])

    assert cli.main(["ask", "q", "--no-save"]) == 0

    assert saved_files(library) == []


def test_the_json_output_names_the_file(tmp_path, monkeypatch, capsys) -> None:
    cli, library = build(tmp_path, monkeypatch, [f"Answered [{A} 00:31]."])

    cli.main(["ask", "q", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["saved_to"] == str(saved_files(library)[0])


def test_a_saved_answer_contains_no_note_or_transcript_text(
    tmp_path, monkeypatch
) -> None:
    """Hard rule 1 at a surface that did not exist before this change, and the
    worst one to get wrong: a file inside the library, written every time
    anybody asks anything, that nobody would think to check."""
    cli, library = build(
        tmp_path,
        monkeypatch,
        [f"ESCALATE: {A}: wording", f"He sets a minimum [{A} 00:31]."],
    )

    cli.main(["ask", "what is the minimum?"])

    body = saved_files(library)[0].read_text(encoding="utf-8")
    assert NOTE_TEXT not in body
    assert "below that nothing is cached" not in body
    # Named, though. Which notes were read and which transcript was opened is
    # the record; their contents are not.
    assert A in body and B in body
    assert "wording" in body


def test_a_save_that_fails_does_not_fail_the_answer(
    tmp_path, monkeypatch, capsys
) -> None:
    """The answer has already been produced and paid for. A full disk costs the
    record, never the thing the user asked for."""
    cli, library = build(tmp_path, monkeypatch, [f"Answered [{A} 00:31]."])
    monkeypatch.setattr(
        Library, "write_answer", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no space"))
    )

    assert cli.main(["ask", "q"]) == 0
    assert "Answered" in capsys.readouterr().out
