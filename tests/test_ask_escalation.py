"""The answering path's honesty about transcripts, budgets and selection.

Each test reproduces a reviewer probe (R2) on synthetic notes and a scripted
transport: an `ESCALATE` line with a Unicode dash printed as the answer; a
second call that the size guard never saw; a transcript cut mid-line without a
word to the model or the record; a record saying "the notes were enough" when a
transcript had been asked for; `--deep` switching off the model's own
escalation; a `DROPPED` list read one line deep; selection overflow trimmed in
video-id order.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_ask_config, make_config
from marginalia.ask import answer_question, choose_notes, render_answer
from marginalia.brain.selector import parse_selection
from marginalia.contracts import LoadedNote, Selection, SELECTION_SELECTED
from marginalia.core.library import Library
from marginalia.llm import LlmClient

A, B = "aaaaaaaaaaa", "bbbbbbbbbbb"


class _Usage:
    prompt_tokens, completion_tokens = 100, 10


class _Response:
    def __init__(self, text: str) -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]
        self.usage = _Usage()
        self.model = "test-model"


def _client(replies):
    sent: list[dict] = []

    def transport(**kwargs):
        sent.append(kwargs)
        return _Response(replies[min(len(sent) - 1, len(replies) - 1)])

    made = LlmClient(make_config(Path(".")).llm, transport=transport, sleep=lambda _: None)
    made.sent = sent  # type: ignore[attr-defined]
    return made


def _build(tmp_path, notes, transcripts=None, **ask):
    config = make_config(tmp_path, ask=make_ask_config(**ask))
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


def _lines(count: int) -> str:
    return "".join(
        f"[00:{n // 60:02d}:{n % 60:02d}.000 --> 00:{n // 60:02d}:{n % 60:02d}.900] line number {n}\n"
        for n in range(count)
    )


def _prompt(made, n: int) -> str:
    return made.sent[n]["messages"][1]["content"]


def test_an_escalation_with_a_unicode_dash_is_an_escalation_not_the_answer(tmp_path) -> None:
    """R2 F3: printed and saved as the library's answer, exit 0."""
    dashed = "-YTupW6YG1k"
    config, library = _build(tmp_path, {dashed: "note"}, {dashed: _lines(5)})
    made = _client([f"ESCALATE: ‑YTupW6YG1k: need the exact wording", "The answer."])

    answer = answer_question(config, library, made, "q")

    assert answer.text == "The answer." and not answer.failed
    assert answer.opened == [(dashed, "need the exact wording")]


def test_a_mis_cased_escalation_id_is_repaired_as_a_citation_is(tmp_path) -> None:
    config, library = _build(tmp_path, {"dQw4w9WgXcQ": "note"}, {"dQw4w9WgXcQ": _lines(5)})
    made = _client(["ESCALATE: dqw4w9wgxcq: wording", "The answer."])

    answer = answer_question(config, library, made, "q")

    assert answer.opened == [("dQw4w9WgXcQ", "wording")]


def test_a_cut_transcript_is_cut_at_a_line_and_disclosed_to_model_and_record(tmp_path) -> None:
    """R2 F10: 2,073 of 25,090 chars sent, ending mid-cue, with no word of it."""
    config, library = _build(
        tmp_path, {A: "note"}, {A: _lines(400)}, max_transcript_chars=3_000
    )
    made = _client([f"ESCALATE: {A}: wording", "The answer."])

    answer = answer_question(config, library, made, "q")

    sent = _prompt(made, 1)
    assert "PARTIAL: the first" in sent and "NOT given to you" in sent
    body = sent.split(f"===== TRANSCRIPT {A}")[1].split("=====\n", 1)[1].split("\n\n")[0]
    last = body.rstrip("\n").split("\n")[-1]
    assert re.fullmatch(r"\[[^\]]+\] line number \d+", last), "cut at a line boundary"
    ((video_id, given, total),) = answer.transcripts_cut
    assert video_id == A and given <= 3_000 and total > given
    assert "cut: the first" in render_answer(answer, asked_at="2026-10-11T00:00:00")


def test_the_second_call_is_kept_under_the_input_ceiling(tmp_path) -> None:
    """R2 F9: the guard saw the notes only; the second call reached 120% of the
    ceiling after the first had been paid for."""
    note = "a note line\n" * 200  # ~600 tokens
    config, library = _build(
        tmp_path, {A: note}, {A: _lines(3_000)},
        max_input_tokens=8_000, max_transcript_chars=1_000_000,
    )
    made = _client([f"ESCALATE: {A}: wording", "The answer."])

    answer = answer_question(config, library, made, "q")

    assert answer.estimated_input_tokens + answer.transcript_tokens <= 8_000
    assert answer.transcripts_cut, "cut to the room left, and said so"


def test_a_refused_escalation_is_recorded_not_reported_as_unneeded(tmp_path) -> None:
    """R2 F12: the record said "(none — the notes were enough)"."""
    config, library = _build(tmp_path, {A: "note"}, {})
    made = _client([f"ESCALATE: {A}: wording", "Answered from the notes."])

    answer = answer_question(config, library, made, "q")
    record = render_answer(answer, asked_at="2026-10-11T00:00:00")

    assert answer.escalations_refused == [(A, "no transcript on disk")]
    assert "the notes were enough" not in record
    assert "asked for and not opened: no transcript on disk" in record


def test_deep_leaves_room_for_the_models_own_escalation(tmp_path) -> None:
    """R2 F11: `--deep` told the model it had "the transcripts you asked for"."""
    config, library = _build(tmp_path, {A: "note A", B: "note B"}, {A: _lines(5), B: _lines(5)})
    made = _client([f"ESCALATE: {B}: the wording is in this one", "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert "you may ask for up to" in _prompt(made, 0)
    assert [video_id for video_id, _ in answer.opened] == [A, B]


def test_a_dropped_list_written_one_id_per_line_is_read_whole() -> None:
    """R2 F13: only the first id was dropped, and the reply parsed clean."""
    known = [f"aaaaaaaaa{n:02d}" for n in range(10)]
    reply = "DROPPED:\n" + "\n".join(known[:3]) + "\nWHY: off topic"

    selection = parse_selection(reply, known=known, most=3)

    assert set(known) - set(selection.video_ids) == set(known[:3])


def test_overflow_after_selection_does_not_always_trim_the_last_ids(tmp_path) -> None:
    """REVIEW C8: the notes trimmed were always the ones whose ids sort last."""
    notes = [LoadedNote(f"{chr(97 + n) * 11}", "t", "x" * 4_000) for n in range(12)]

    class KeepsEverything:
        def select(self, question, summaries, keep):
            return Selection(
                video_ids=[s.video_id for s in summaries], mode=SELECTION_SELECTED, parsed=True
            )

    config = make_config(Path("."), ask=make_ask_config(max_input_tokens=12_000))
    config = replace(config, ask=replace(config.ask, select_budget_share=0.75))
    trimmed: set[str] = set()
    per_run = 0
    for _ in range(25):
        packed, selection = choose_notes(config, KeepsEverything(), "q", notes)
        per_run = len(notes) - len(packed)
        trimmed |= {n.video_id for n in notes} - {n.video_id for n in packed}
    assert 0 < per_run < len(notes), "the budget forces a trim and leaves something"
    last_ids = {n.video_id for n in notes[-per_run:]}
    assert trimmed - last_ids, "the trim reaches ids other than the ones that sort last"


def test_a_transcript_already_opened_is_not_opened_twice(tmp_path) -> None:
    """Independent review F3: --deep opened A (cut), the model asked for A again,
    and A went to the second call twice."""
    config, library = _build(tmp_path, {A: "note"}, {A: _lines(400)}, max_transcript_chars=3_000)
    made = _client([f"ESCALATE: {A}: I need the rest of it", "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert answer.opened == [(A, "opened by request")]
    assert (A, "already opened") in answer.escalations_refused
    assert _prompt(made, 1).count(f"===== TRANSCRIPT {A}") == 1


def test_an_id_mentioned_in_prose_after_the_dropped_list_is_not_dropped() -> None:
    """Independent review F10: every id up to WHY counted as dropped, so
    "I kept X because…" dropped X."""
    known = [f"aaaaaaaaa{n:02d}" for n in range(10)]
    reply = (
        "DROPPED:\n" + "\n".join(known[:2])
        + f"\nI kept {known[5]} because it covers the question directly.\nWHY: off topic"
    )

    selection = parse_selection(reply, known=known, most=3)

    assert set(known) - set(selection.video_ids) == set(known[:2])


# -- duplicate requests and capacity (second independent review, finding D) ------
#
# With `max_transcripts=2` and A opened by `--deep`, one slot is left. A request
# for A again used to take that slot before it was recognised as a duplicate,
# so B was refused as over the limit and only A was opened.

C = "ccccccccccc"


def _three(tmp_path, **ask):
    return _build(
        tmp_path,
        {A: "note A", B: "note B", C: "note C"},
        {A: _lines(5), B: _lines(5), C: _lines(5)},
        **ask,
    )


@pytest.mark.parametrize(
    "reply",
    [
        f"ESCALATE: {A}: again\nESCALATE: {B}: new",  # duplicate first
        f"ESCALATE: {B}: new\nESCALATE: {A}: again",  # duplicate later
        # repeated ids, both kinds
        f"ESCALATE: {A}: again\nESCALATE: {B}: new\nESCALATE: {A}: and again\nESCALATE: {B}: twice",
    ],
)
def test_a_duplicate_request_does_not_take_the_slot_of_a_new_one(tmp_path, reply: str) -> None:
    config, library = _three(tmp_path, max_transcripts=2)
    made = _client([reply, "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert [video_id for video_id, _ in answer.opened] == [A, B]
    assert answer.escalations_refused == [(A, "already opened")]
    assert _prompt(made, 1).count(f"===== TRANSCRIPT {A}") == 1
    assert _prompt(made, 1).count(f"===== TRANSCRIPT {B}") == 1


def test_the_limit_still_applies_to_new_requests_after_a_duplicate(tmp_path) -> None:
    config, library = _three(tmp_path, max_transcripts=2)
    made = _client([f"ESCALATE: {A}: again\nESCALATE: {B}: new\nESCALATE: {C}: also new", "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert [video_id for video_id, _ in answer.opened] == [A, B]
    assert answer.escalations_refused == [
        (A, "already opened"),
        (C, "over the [ask] max_transcripts limit"),
    ]


def test_with_no_capacity_left_a_duplicate_is_still_named_as_one(tmp_path) -> None:
    """Exhausted capacity: the duplicate is refused for what it is, and only
    the new request is over the limit."""
    config, library = _three(tmp_path, max_transcripts=1)
    made = _client([f"ESCALATE: {B}: new\nESCALATE: {A}: again", "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert [video_id for video_id, _ in answer.opened] == [A]
    assert answer.escalations_refused == [
        (B, "over the [ask] max_transcripts limit"),
        (A, "already opened"),
    ]


def test_a_single_new_request_with_one_slot_left_is_opened(tmp_path) -> None:
    """The control the review compared against."""
    config, library = _three(tmp_path, max_transcripts=2)
    made = _client([f"ESCALATE: {B}: new", "The answer."])

    answer = answer_question(config, library, made, "q", deep=[A])

    assert [video_id for video_id, _ in answer.opened] == [A, B]
    assert answer.escalations_refused == []
