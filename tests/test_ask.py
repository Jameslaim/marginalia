"""Answering from the library.

The model is scripted throughout. What is being tested is the machinery around
it — what gets loaded, when a transcript is opened, what reaches stdout — none
of which should depend on a real model having a good day.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marginalia.ask import (
    Citation,
    answer_question,
    deep_link,
    load_notes,
    parse_citations,
    parse_escalations,
    render_links,
)
from marginalia.library import Library
from marginalia.llm import LlmClient

from conftest import make_config

A = "dQw4w9WgXcQ"
B = "rGf9Db4tjUQ"
C = "GXh1KwzNIIM"

TRANSCRIPT = (
    "[00:00:00.000 --> 00:00:04.000] the minimum is one thousand tokens\n"
    "[00:00:04.000 --> 00:00:08.000] below that nothing is cached\n"
)


class FakeUsage:
    def __init__(self, prompt_tokens: int = 90_000, completion_tokens: int = 1_200) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeResponse:
    def __init__(self, text: str) -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = FakeUsage()
        self.model = "test-model"


def client(replies):
    """Scripted transport. `replies` is a list, one per call, and every prompt
    sent is recorded so a test can assert what the model was actually shown."""
    sent: list[dict] = []
    script = list(replies)

    def transport(**kwargs):
        sent.append(kwargs)
        return FakeResponse(script[min(len(sent) - 1, len(script) - 1)])

    made = LlmClient(make_config(Path(".")).llm, transport=transport, sleep=lambda _: None)
    made.sent = sent  # type: ignore[attr-defined]
    return made


def build(tmp_path, notes: dict[str, str], transcripts: dict[str, str] | None = None):
    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    for video_id, text in notes.items():
        library.video_dir(video_id).mkdir(parents=True, exist_ok=True)
        library.write_note(video_id, text)
        library.write_meta(video_id, {"title": f"title for {video_id}"})
    for video_id, text in (transcripts or {}).items():
        library.video_dir(video_id).mkdir(parents=True, exist_ok=True)
        library.transcript_path(video_id).write_text(text, encoding="utf-8")
    return config, library


# -- parsing -----------------------------------------------------------------


def test_citations_are_parsed_in_both_shapes():
    found = parse_citations(f"first [{A} 09:31] then [{B} 01:02:03] done")
    assert [(c.video_id, c.seconds) for c in found] == [(A, 571), (B, 3723)]


def test_deep_link_seconds_match_the_cited_timestamp():
    citation = parse_citations(f"[{A} 09:31]")[0]
    assert deep_link(citation.video_id, citation.seconds) == (
        f"https://www.youtube.com/watch?v={A}&t=571s"
    )
    assert citation.seconds == 9 * 60 + 31


def test_rendered_links_are_deduplicated_and_titled():
    citations = [Citation(A, 571, f"[{A} 09:31]"), Citation(A, 571, f"[{A} 09:31]")]
    lines = render_links(citations, {A: "A title"})
    assert len(lines) == 1
    assert "A title" in lines[0] and f"t=571s" in lines[0]


def test_escalation_is_parsed_and_unknown_ids_dropped():
    reply = f"ESCALATE: {A}: need his exact words\nESCALATE: zzzzzzzzzzz: nope"
    assert parse_escalations(reply, known=[A], limit=3) == [(A, "need his exact words")]


def test_escalation_is_capped():
    reply = "\n".join(f"ESCALATE: {v}: why" for v in (A, B, C))
    assert len(parse_escalations(reply, known=[A, B, C], limit=2)) == 2


def test_a_marker_inside_a_real_answer_is_not_an_escalation():
    """Otherwise a finished answer gets thrown away and a transcript opened for
    nothing, because the answer happened to discuss escalation."""
    reply = (
        f"Caching starts at 1024 tokens [{A} 00:08]. " + "Further detail. " * 60
        + f"\nESCALATE: {A}: this line is discussed, not requested"
    )
    assert parse_escalations(reply, known=[A], limit=3) == []


# -- loading -----------------------------------------------------------------


def test_notes_load_with_titles(tmp_path):
    _config, library = build(tmp_path, {A: "note A", B: "note B"})
    loaded = load_notes(library)
    assert [n.video_id for n in loaded] == sorted([A, B])
    assert loaded[0].title.startswith("title for")


def test_a_missing_note_does_not_stop_the_others(tmp_path):
    _config, library = build(tmp_path, {A: "note A"})
    library.video_dir(B).mkdir(parents=True, exist_ok=True)  # no note.md
    library.write_note(C, "   ")  # empty
    loaded = load_notes(library)
    assert [n.video_id for n in loaded] == [A]


def test_an_empty_library_says_so_without_calling_the_model(tmp_path):
    config, library = build(tmp_path, {})
    made = client(["unused"])
    answer = answer_question(config, library, made, "anything?")
    assert "library is empty" in answer.text.lower()
    assert made.sent == []


# -- the flow ----------------------------------------------------------------


def test_a_general_question_answers_without_escalating(tmp_path):
    config, library = build(tmp_path, {A: "note A", B: "note B"}, {A: TRANSCRIPT})
    made = client([f"Caching starts at 1024 tokens [{A} 00:00]."])
    answer = answer_question(config, library, made, "how does caching work?")
    assert answer.escalated is False
    assert len(made.sent) == 1
    assert answer.citations[0].video_id == A


def test_a_wording_question_escalates_and_reports_which(tmp_path):
    config, library = build(tmp_path, {A: "note A", B: "note B"}, {A: TRANSCRIPT})
    made = client(
        [
            f"ESCALATE: {A}: need the exact phrasing",
            f'He says "below that nothing is cached" [{A} 00:04].',
        ]
    )
    answer = answer_question(config, library, made, "what were his exact words?")
    assert answer.escalated is True
    assert answer.opened == [(A, "need the exact phrasing")]
    assert len(made.sent) == 2
    # The transcript reached the model on the second call and not the first.
    assert "below that nothing is cached" not in made.sent[0]["messages"][1]["content"]
    assert "below that nothing is cached" in made.sent[1]["messages"][1]["content"]


def test_escalation_is_refused_on_the_second_call(tmp_path):
    config, library = build(tmp_path, {A: "note A"}, {A: TRANSCRIPT})
    made = client([f"ESCALATE: {A}: why", "Answered."])
    answer_question(config, library, made, "q")
    second = made.sent[1]["messages"][1]["content"]
    assert "not allowed" in second


def test_a_request_for_a_transcript_that_is_missing_still_answers(tmp_path):
    """The note exists, the transcript does not. The second call must still
    happen — the first reply was a request, and returning it would hand the
    user an ESCALATE line instead of prose."""
    config, library = build(tmp_path, {A: "note A"}, {})
    made = client([f"ESCALATE: {A}: need wording", "Answered from notes alone."])
    answer = answer_question(config, library, made, "q")
    assert answer.text == "Answered from notes alone."
    assert answer.opened == []
    assert len(made.sent) == 2


def test_deep_forces_a_transcript_open(tmp_path):
    config, library = build(tmp_path, {A: "note A"}, {A: TRANSCRIPT})
    made = client(["Answered."])
    answer = answer_question(config, library, made, "q", deep=[A])
    assert answer.opened == [(A, "opened by request")]
    assert len(made.sent) == 1
    assert "below that nothing is cached" in made.sent[0]["messages"][1]["content"]


def test_note_markers_survive_into_the_answer(tmp_path):
    config, library = build(tmp_path, {A: "note A"})
    marked = f"He claims X [{A} 00:03]. [note: this is my arithmetic, not his]"
    made = client([marked])
    answer = answer_question(config, library, made, "q")
    assert "[note:" in answer.text


def test_an_unanswerable_question_is_passed_through_not_embellished(tmp_path):
    config, library = build(tmp_path, {A: "note A"})
    refusal = "The library does not cover this. Nothing here discusses it."
    made = client([refusal])
    answer = answer_question(config, library, made, "what about beekeeping?")
    assert answer.text == refusal
    assert answer.citations == []


def test_a_failed_model_call_reports_rather_than_raising(tmp_path):
    config, library = build(tmp_path, {A: "note A"})

    def transport(**_kwargs):
        raise RuntimeError("provider is down")

    made = LlmClient(config.llm, transport=transport, sleep=lambda _: None, max_attempts=1)
    answer = answer_question(config, library, made, "q")
    assert "failed" in answer.text.lower()


def test_the_whole_library_is_sent_with_no_ranking_step(tmp_path):
    config, library = build(tmp_path, {A: "a" * 50, B: "b" * 50, C: "c" * 50})
    made = client(["Answered."])
    answer_question(config, library, made, "q")
    prompt = made.sent[0]["messages"][1]["content"]
    for video_id in (A, B, C):
        assert f"===== VIDEO {video_id} =====" in prompt


# -- the surface -------------------------------------------------------------


def test_no_transcript_text_reaches_stdout(tmp_path, capsys, monkeypatch):
    """Rule 1 at the last surface. The answer is printed; the transcript that
    produced it is not, even though it was opened during this question."""
    from marginalia import cli

    config, library = build(tmp_path, {A: "note A"}, {A: TRANSCRIPT})
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli,
        "LlmClient",
        lambda *_a, **_k: client(
            [f"ESCALATE: {A}: wording", f"He sets a minimum [{A} 00:00]."]
        ),
    )
    code = cli.main(["ask", "what is the minimum?"])
    out = capsys.readouterr().out
    assert code == 0
    assert "below that nothing is cached" not in out
    assert "the minimum is one thousand tokens" not in out
    assert "He sets a minimum" in out
    assert f"t=0s" in out  # the deep link


def test_json_output_carries_links_and_costs(tmp_path, capsys, monkeypatch):
    from marginalia import cli

    config, library = build(tmp_path, {A: "note A"})
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli, "LlmClient", lambda *_a, **_k: client([f"Answered [{A} 09:31]."])
    )
    cli.main(["ask", "q", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["citations"][0]["url"].endswith("t=571s")
    assert payload["notes_loaded"] == 1
    assert payload["cost_usd"] >= 0


def test_citations_in_parentheses_are_accepted():
    """The prompt asks for square brackets and the model often writes round
    ones. A correct citation that produces no link is the product failing on a
    delimiter, so both are parsed."""
    found = parse_citations(f"see ({A} 00:01:09) and [{B} 02:30]")
    assert [(c.video_id, c.seconds) for c in found] == [(A, 69), (B, 150)]


def test_trailing_milliseconds_are_tolerated():
    assert parse_citations(f"[{A} 01:36.600]")[0].seconds == 96
