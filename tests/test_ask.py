"""Answering from the library.

The model is scripted throughout. What is being tested is the machinery around
it — what gets loaded, when a transcript is opened, what reaches stdout — none
of which should depend on a real model having a good day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from marginalia.ask import (
    AskTooLarge,
    SelectionFailed,
    answer_question,
    check_grounding,
    deep_link,
    estimate_tokens,
    load_notes,
    mark_unverified,
    normalise_dashes,
    note_digest,
    parse_citations,
    parse_escalations,
    render_answer,
    render_links,
    scan_citations,
)
from marginalia.contracts import (
    SELECTION_ALL,
    SELECTION_SELECTED,
    Answer,
    AnswerCitation,
    LoadedNote,
)
from marginalia.core.library import Library
from marginalia.llm import LlmClient

from conftest import make_ask_config, make_config

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
    citations = [
        AnswerCitation(A, 571, f"[{A} 09:31]"),
        AnswerCitation(A, 571, f"[{A} 09:31]"),
    ]
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


# -- real output, not imagined output ----------------------------------------
#
# The strict parser passed every mock test in this file and still dropped 98%
# of a real answer, because the mocks returned the shape the test author had in
# mind. These fixtures are verbatim model output, Unicode dashes included.

FIXTURES = Path(__file__).parent / "fixtures"

LIBRARY_IDS = [
    "AMwrLF_TeWo", "GfNdXFS8O0w", "6cEQEba0i2A", "HDMqDV7mmGo", "-YTupW6YG1k",
    "iywyv-ZzfeA", "_0uiiJfsBPI", "tWJzCxV6Aqc", "dTh5fnB9CE8", "skXT6WFNQnc",
]


def shapes() -> str:
    return (FIXTURES / "answer_shapes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "video_id,seconds",
    [
        ("AMwrLF_TeWo", 42),          # HH:MM:SS range, en-dash -> start
        ("GfNdXFS8O0w", 37),          # first of two in one bracket, semicolon
        ("6cEQEba0i2A", 149),         # second of two in one bracket
        ("HDMqDV7mmGo", 159),         # MM:SS range
        ("-YTupW6YG1k", 60),          # ID beginning with a hyphen
        ("iywyv-ZzfeA", 62),          # U+2011 inside the ID
        ("_0uiiJfsBPI", 948),         # doubled dash before the ID
        ("dTh5fnB9CE8", 69),          # round brackets
        ("skXT6WFNQnc", 96),          # trailing milliseconds
    ],
)
def test_every_real_shape_is_recovered(video_id, seconds):
    found = parse_citations(shapes(), known=LIBRARY_IDS)
    assert (video_id, seconds) in [(c.video_id, c.seconds) for c in found]


def test_a_comma_list_inherits_the_video_from_the_entry_before_it():
    found = parse_citations(shapes(), known=LIBRARY_IDS)
    assert ("-YTupW6YG1k", 148) in [(c.video_id, c.seconds) for c in found]


def test_prose_inside_a_bracket_does_not_stop_the_citation():
    found = parse_citations(shapes(), known=LIBRARY_IDS)
    assert ("-YTupW6YG1k", 0) in [(c.video_id, c.seconds) for c in found]


def test_an_unknown_video_never_becomes_a_link():
    scan = scan_citations(shapes(), known=LIBRARY_IDS)
    assert all(c.video_id != "zzzzzzzzzzz" for c in scan.citations)
    assert scan.unparsed, "an unresolvable citation must be reported, not dropped"


def test_ordinary_parentheses_and_ratios_are_not_citations():
    scan = scan_citations("(see above) a 2:1 ratio, $4 per million", known=LIBRARY_IDS)
    assert scan.citations == [] and scan.unparsed == []


def test_the_real_answer_that_reported_two_citations_now_parses_many():
    """h2 reported 2 citations against dozens of inline references."""
    text = (FIXTURES / "answer_real_h2.txt").read_text(encoding="utf-8")
    ids = sorted({c.video_id for c in parse_citations(text, known=None)})
    scan = scan_citations(text, known=ids or None)
    assert len(scan.citations) > 2


def test_the_leaves_out_answer_parses_its_parenthesised_citations():
    text = (FIXTURES / "answer_real_q4.txt").read_text(encoding="utf-8")
    ids = sorted({c.video_id for c in parse_citations(text)})
    assert len(parse_citations(text, known=ids)) >= 30


def test_the_scan_reports_shaped_and_unparsed_counts():
    scan = scan_citations(shapes(), known=LIBRARY_IDS)
    assert scan.shaped == len(scan.citations) + len(scan.unparsed)
    assert scan.shaped > len(scan.citations)


def test_unicode_dashes_normalise_to_ascii():
    assert normalise_dashes("a\u2011b\u2013c\u2014d\u2212e") == "a-b-c-d-e"


# -- repairing a mis-cased video ID against the library ------------------------
#
# Seven citations were lost across three real answers because the model wrote
# `57AOoangAJA` for `57AOoAngaJA` and `UVejThz8DSo` for `UVEjThz8DSo`. The parser
# was right to refuse them — rule 5 forbids normalising an ID, because two
# YouTube IDs differing only in case are two different videos. Repairing against
# the IDs *this library holds* is a different operation with a different risk.


def test_a_mis_cased_id_is_repaired_to_the_library_s_spelling() -> None:
    known = ["57AOoAngaJA"]

    scan = scan_citations("as shown [57AOoangAJA 02:03]", known)

    assert [c.video_id for c in scan.citations] == ["57AOoAngaJA"]
    assert scan.unparsed == []


def test_a_repair_is_counted_rather_than_silent() -> None:
    """A repair is the model getting an ID wrong. Fixed silently, it stops being
    something anyone knows is happening."""
    scan = scan_citations("[UVejThz8DSo 18:11]", ["UVEjThz8DSo"])

    assert len(scan.repaired) == 1
    assert len(scan.citations) == 1


def test_an_exactly_matching_id_is_not_reported_as_repaired() -> None:
    scan = scan_citations("[UVEjThz8DSo 18:11]", ["UVEjThz8DSo"])

    assert scan.repaired == []
    assert len(scan.citations) == 1


def test_an_id_the_library_does_not_hold_is_still_refused() -> None:
    """The repair must not become an invention. A liberal parser that starts
    guessing is worse than a strict one that drops."""
    scan = scan_citations("[aaaaaaaaaaa 02:03]", ["57AOoAngaJA"])

    assert scan.citations == []
    assert len(scan.unparsed) == 1


def test_a_case_only_collision_in_the_library_refuses_rather_than_guesses() -> None:
    """Vanishingly unlikely and the reason the repair is safe. If two stored
    videos ever differ only in case, `len(matches) == 1` fails and the entry is
    refused exactly as it was before — never resolved to whichever came first."""
    scan = scan_citations("[dqw4w9wgxcq 02:03]", ["dQw4w9WgXcQ", "DQW4W9WGXCQ"])

    assert scan.citations == []
    assert len(scan.unparsed) == 1


def test_the_seven_lost_citations_recover_on_re_parse() -> None:
    """The measured case, both IDs, in the shapes the answers actually used."""
    known = ["57AOoAngaJA", "UVEjThz8DSo", "dQw4w9WgXcQ"]
    text = (
        "history is replayed [57AOoangAJA 00:02:03] and isolated by session id "
        "[57AOoangAJA 00:08:40], surviving restart [57AOoangAJA 00:12:53]. "
        "Memory defense can redact [UVejThz8DSo 18:11] or block [UVejThz8DSo 18:22], "
        "and the setting is per-preference [UVejThz8DSo 19:04]. "
        "Unrelated but correct: [dQw4w9WgXcQ 00:18]."
    )

    scan = scan_citations(text, known)

    assert len(scan.repaired) == 6
    assert len(scan.unparsed) == 0
    assert {c.video_id for c in scan.citations} == set(known)


# -- what the answer prompt must say -------------------------------------------


def test_the_answer_prompt_pins_the_answer_language() -> None:
    """An English question over 87 English notes came back in Mandarin. One
    occurrence in six, so intermittent — which is exactly the kind of fault a
    passing run does not disprove. The instruction is the only durable guard."""
    from marginalia.llm import load_prompt, prompts_dir

    text = load_prompt("answer", directory=prompts_dir()).text.lower()

    assert "language of the question" in text


def test_the_answer_prompt_says_off_topic_notes_are_to_be_ignored() -> None:
    """`/ask` loads every note in the library, and the library holds several
    unrelated subjects. The brief governs what enters the library, not what an
    answer draws on — so an agent-memory question was answered partly from
    prompt-caching videos that that topic's brief explicitly excluded."""
    from marginalia.llm import load_prompt, prompts_dir

    text = load_prompt("answer", directory=prompts_dir()).text.lower()

    assert "unrelated subjects" in text
    assert "ignore" in text


def test_the_answer_prompt_still_names_no_model_family() -> None:
    from marginalia.llm import load_prompt, prompts_dir

    text = load_prompt("answer", directory=prompts_dir()).text.lower()

    for family in ("claude", "gpt-", "gemini", "llama", "kimi", "deepseek", "qwen"):
        assert family not in text


# -- truncated ids, repaired on the same terms as case ------------------------


def test_a_truncated_id_resolves_to_its_unique_library_match() -> None:
    """`VIg_7sqS2D` for `VIg_7sqS2dE` — the model dropped the last character.
    The same answer wrote that one video's ID three ways: right, mis-cased, and
    cut short."""
    scan = scan_citations("as shown [VIg_7sqS2D 05:45]", ["VIg_7sqS2dE"])

    assert [c.video_id for c in scan.citations] == ["VIg_7sqS2dE"]
    assert len(scan.repaired) == 1


def test_a_prefix_matching_two_library_ids_is_refused() -> None:
    """Unique or nothing. A prefix shared by two stored videos names neither."""
    scan = scan_citations("[VIg_7sqS2 05:45]", ["VIg_7sqS2dE", "VIg_7sqS2xQ"])

    assert scan.citations == []
    assert len(scan.unparsed) == 1


def test_a_dropped_character_is_still_refused() -> None:
    """`aYfZN8tAQs` for `aYfZN8t6AQs`. Not a prefix and not a case difference,
    so it would need edit distance — and two YouTube IDs one character apart are
    two different videos, so that repair can point a claim at the wrong one."""
    scan = scan_citations("[aYfZN8tAQs 11:23]", ["aYfZN8t6AQs"])

    assert scan.citations == []
    assert len(scan.unparsed) == 1


def test_a_transposition_is_still_refused() -> None:
    """`o6vU4u_yEuE` for `o6v4Uu_yEuE`. Correct length, wrong video. A wrong
    link is worse than a missing one: the missing one shows up in the unparsed
    count, the wrong one reads as a working citation."""
    scan = scan_citations("[o6vU4u_yEuE 05:19]", ["o6v4Uu_yEuE"])

    assert scan.citations == []
    assert len(scan.unparsed) == 1


def test_a_short_fragment_never_becomes_a_prefix_match() -> None:
    """Below the floor a prefix stops identifying a video and starts matching
    whatever the library happens to hold."""
    scan = scan_citations("[VIg_ 05:45]", ["VIg_7sqS2dE"])

    assert scan.citations == []


def test_the_three_real_failures_end_with_one_repaired_and_two_refused() -> None:
    """The measured case from the last answer, exactly as it occurred."""
    known = ["VIg_7sqS2dE", "aYfZN8t6AQs", "o6v4Uu_yEuE"]
    text = ("files still cost context [VIg_7sqS2D 05:45]; the extractor sees recent "
            "messages [aYfZN8tAQs 11:23]; tiers differ [o6vU4u_yEuE 05:19].")

    scan = scan_citations(text, known)

    assert [c.video_id for c in scan.citations] == ["VIg_7sqS2dE"]
    assert len(scan.repaired) == 1
    assert len(scan.unparsed) == 2


# -- the context headroom guard ---------------------------------------------
#
# `ask` sends the whole library, which is correct while it fits. The other two
# triggers PLAN.md names — cost per answer climbing, an answer missing a video
# known to be in the library — are slopes you can watch. Running out of context
# is a cliff, and "wait for the symptom" is bad advice when the symptom is the
# failure. So the size is reported every time, and a configured ceiling turns
# the cliff into a refusal that names both numbers.


def _big(chars: int) -> str:
    return "x" * chars


def test_the_input_size_is_reported_without_any_ceiling(tmp_path, caplog):
    """No ceiling configured is the default state, and it must never block.

    There is no provider-neutral context limit for this project to fall back on,
    and inventing one would be a vendor's number chosen on the user's behalf —
    the same mistake rule 7 forbids for `llm.model`.
    """
    config, library = build(tmp_path, {"aaaaaaaaaaa": _big(4_000)})
    with caplog.at_level(logging.INFO, logger="marginalia.ask"):
        answer = answer_question(config, library, client(["An answer."]), "q")

    assert answer.text == "An answer."
    assert answer.estimated_input_tokens > 0
    assert answer.headroom is None
    assert "no ceiling" in caplog.text


def test_a_library_under_the_ceiling_answers(tmp_path):
    config, library = build(tmp_path, {"aaaaaaaaaaa": _big(4_000)})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    answer = answer_question(config, library, client(["An answer."]), "q")

    assert answer.text == "An answer."
    assert answer.headroom is not None and answer.headroom < 0.8


def test_a_library_near_the_ceiling_warns_and_still_answers(tmp_path, caplog):
    """80% is where the wall becomes visible while there is still room to decide
    what to do about it."""
    config, library = build(tmp_path, {"aaaaaaaaaaa": _big(360_000)})
    config = replace(config, ask=make_ask_config(max_input_tokens=100_000))

    with caplog.at_level(logging.WARNING, logger="marginalia.ask"):
        answer = answer_question(config, library, client(["An answer."]), "q")

    assert answer.text == "An answer.", "a warning, not a refusal"
    assert 0.8 <= answer.headroom < 1.0
    assert "ceiling" in caplog.text


def test_over_the_ceiling_refuses_before_sending_anything(tmp_path):
    """A clean error naming both numbers, not an opaque provider 400 several
    seconds later. Nothing is sent, so nothing is spent."""
    config, library = build(tmp_path, {"aaaaaaaaaaa": _big(600_000)})
    config = replace(config, ask=make_ask_config(max_input_tokens=100_000))
    spoken = client(["An answer."])

    with pytest.raises(AskTooLarge) as caught:
        answer_question(config, library, spoken, "q")

    message = str(caught.value)
    assert "100,000" in message, "the ceiling"
    assert "1 notes" in message or "notes" in message
    assert spoken.sent == [], "no request was made"


def test_the_guard_never_quotes_a_note(tmp_path, caplog):
    """Hard rule 1 at one more surface. A message about *size* has no reason to
    carry content, and counts are safe precisely because they are counts — no
    titles, no excerpts, no "the largest note is X"."""
    secret = "the speaker contradicts himself about cache expiry four minutes apart"
    config, library = build(tmp_path, {"aaaaaaaaaaa": secret + _big(600_000)})
    config = replace(config, ask=make_ask_config(max_input_tokens=100_000))

    with caplog.at_level(logging.DEBUG, logger="marginalia.ask"):
        with pytest.raises(AskTooLarge) as caught:
            answer_question(config, library, client(["An answer."]), "q")

    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert "title for" not in str(caught.value), "not even a title"


def test_the_estimate_tracks_what_the_api_reported():
    """The ratio is measured, not folklore — four real answers over 3,466,968
    characters of notes reported 868,392–868,399 input tokens, which is 3.992
    characters per token. It happens to land within 0.2% of the usual chars/4,
    and that is recorded so nobody "corrects" a measured constant to the value
    it already equals."""
    notes = [LoadedNote(video_id="a" * 11, title="", text=_big(3_466_968))]

    estimate = estimate_tokens(notes)

    assert abs(estimate - 868_399) / 868_399 < 0.01, "within 1% of the measurement"


# -- M7: two stages, whole notes --------------------------------------------


def test_a_library_under_budget_loads_every_note(tmp_path):
    """The path that must not change. Selection is a response to the ceiling,
    not an improvement on small libraries — 182 notes beat 87, and giving that
    up as a side effect of building this would make the milestone a regression.
    """
    config, library = build(tmp_path, {A: "note a", B: "note b"})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))
    spoken = client(["An answer."])

    answer = answer_question(config, library, spoken, "q")

    assert answer.notes_loaded == 2
    assert answer.notes_considered == 2
    assert answer.selection.mode == SELECTION_ALL
    assert len(spoken.sent) == 1, "one call: no selector ran"
    sent = spoken.sent[0]["messages"][1]["content"]
    assert "note a" in sent and "note b" in sent


def test_a_library_over_budget_selects_and_reports_both_counts(tmp_path):
    config, library = build(
        tmp_path, {A: "aaa " * 4_000, B: "bbb " * 4_000, C: "ccc " * 4_000}
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=8_000))
    spoken = client([f"DROPPED: {B}, {C}\nWHY: only the first is on topic", "An answer."])

    answer = answer_question(config, library, spoken, "q")

    assert answer.selection.mode == SELECTION_SELECTED
    assert answer.notes_considered == 3
    assert answer.notes_loaded == 1
    assert answer.selection.reason == "only the first is on topic"
    assert answer.estimated_input_tokens <= config.ask.max_input_tokens
    answering = spoken.sent[1]["messages"][1]["content"]
    assert "aaa" in answering and "bbb" not in answering


def test_selected_notes_arrive_whole(tmp_path):
    """The constraint, asserted rather than assumed. What reaches stage 2 is the
    note as written — no chunk, no extract, no fragment."""
    whole = "## Claims\n\n- first claim\n\n## Leaves out\n\n- the hard part\n"
    config, library = build(tmp_path, {A: whole, B: "bbb " * 8_000})
    config = replace(config, ask=make_ask_config(max_input_tokens=8_000))
    spoken = client([f"DROPPED: {B}\nWHY: x", "An answer."])

    answer_question(config, library, spoken, "q")

    answering = spoken.sent[1]["messages"][1]["content"]
    assert whole.strip() in answering, "the note, unchanged and entire"


def test_a_failed_selector_refuses_rather_than_filling_arbitrarily(tmp_path):
    """**The correction a real run forced.** The original design filled the
    budget by position when selection failed, on rule 8's logic that more notes
    is safer. At 422 notes that produced *worse* answers than selection did: the
    two questions that fell through loaded 263 notes and cited 6 and 3 videos,
    while the two that selected loaded 104 and cited 29 and 8.

    Filling by sorted video id is silent exclusion wearing a different hat —
    159 notes go unread on an ordering unrelated to the question, and the answer
    reads as confident either way. Rule 8 is about exclusion nobody can see."""
    config, library = build(
        tmp_path, {A: "aaa " * 1_000, B: "bbb " * 1_000, C: "ccc " * 1_000}
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=5_334))
    spoken = client(["I think the first one looks good.", "still not a list.", "An answer."])

    with pytest.raises(SelectionFailed) as caught:
        answer_question(config, library, spoken, "q")

    assert len(spoken.sent) == 2, "one retry, then it stops"
    assert "failed twice" in str(caught.value)
    assert "unrelated to the question" in str(caught.value)


def test_a_retry_rescues_a_selector_that_stumbled_once(tmp_path):
    """The quota makes the runaway reply much less likely, so most of what used
    to fall through now succeeds on a second attempt."""
    config, library = build(
        tmp_path, {A: "aaa " * 1_000, B: "bbb " * 1_000, C: "ccc " * 1_000}
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=5_334))
    spoken = client(["not a list at all", f"DROPPED: {C}\nWHY: it ranked lowest", "An answer."])

    answer = answer_question(config, library, spoken, "q")

    assert answer.text == "An answer."
    assert answer.selection.mode == SELECTION_SELECTED
    assert C not in answer.selection.video_ids


def test_the_summary_lines_never_reach_the_answer_file_or_the_log(tmp_path, caplog):
    """Hard rule 1 at M7's new surface. The summaries are built *from* notes, so
    they are note text: they go to the model and nowhere else."""
    marker = "the speaker contradicts himself about cache expiry"
    config, library = build(
        tmp_path,
        {A: f"## Claims\n\n- {marker} (00:01:00)\n", B: "bbb " * 2_000},
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=4_000))
    spoken = client([f"DROPPED: {A}\nWHY: the other one is off topic", "An answer."])

    with caplog.at_level(logging.DEBUG, logger="marginalia.ask"):
        answer = answer_question(config, library, spoken, "q")

    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")
    assert marker not in rendered, "not in the answer file"
    assert marker not in caplog.text, "not in the log"
    assert "selection: selected" in rendered, "but the decision is recorded"


def test_the_selection_reason_is_stored(tmp_path):
    """Rule 23. "Did it not know, or did it not look?" is the first question
    asked of a thin answer, and it has to be answerable from disk."""
    config, library = build(tmp_path, {A: "aaa " * 4_000, B: "bbb " * 4_000})
    config = replace(config, ask=make_ask_config(max_input_tokens=6_000))
    spoken = client([f"DROPPED: {B}\nWHY: dropped the framework tutorials", "An answer."])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert "dropped the framework tutorials" in rendered
    assert "1 of 2 considered" in rendered


# -- what the answer file records about selection ---------------------------
#
# Rule 23 arriving one layer late. The selector stored its reasoning and its
# counts and not its output, so "was that note dropped, or loaded and never
# cited?" needed a second selector call — which is how it was established that
# 67 of 95 prompt-caching notes were being dropped by subject. Recording the
# surviving ids turns that question into a grep.


def test_a_selected_run_records_which_notes_it_read(tmp_path):
    config, library = build(tmp_path, {A: "aaa " * 4_000, B: "bbb " * 4_000})
    config = replace(config, ask=make_ask_config(max_input_tokens=6_000))
    spoken = client([f"DROPPED: {B}\nWHY: it ranked lowest", "An answer."])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert f"- notes read: {A}" in rendered
    assert B not in rendered.split("## Answer")[0], "the dropped note is not in the header"


def test_the_requested_and_allowed_drops_are_recorded(tmp_path):
    """409 requested against 159 allowed is the number that says a model is
    thresholding rather than ranking, and it reads exactly like 160 requested
    unless the request sits beside the allowance."""
    config, library = build(
        tmp_path, {A: "aaa " * 4_000, B: "bbb " * 4_000, C: "ccc " * 4_000}
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=8_000))
    # One drop is the quota; the reply names three.
    spoken = client([f"DROPPED: {A}, {B}, {C}\nWHY: everything looks off topic", "An answer."])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert answer.selection.requested_drops == 3
    assert answer.selection.refused_drops > 0
    assert "selector asked to drop: 3 of 3" in rendered
    assert "re-included" in rendered


def test_the_load_order_differs_between_two_runs_and_both_are_recorded(tmp_path):
    """Notes loaded in video-id order tie a note's chance of being read to where
    its id sorts: pooled over 17 answers the first third took 43.9% of citations
    against a 33.3% null, on both providers. Shuffling per question converts a
    systematic bias into noise — it does not remove primacy, and re-measuring
    primacy afterwards is measuring the wrong thing."""
    notes = {f"vid{n:07d}xyz"[:11]: f"note {n}" for n in range(40)}
    config, library = build(tmp_path, notes)
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    first = answer_question(config, library, client(["An answer."]), "q")
    second = answer_question(config, library, client(["An answer."]), "q")

    a = first.selection.video_ids
    b = second.selection.video_ids
    assert a and b, "both runs record the order they read"
    assert a != b, "and the order is not the same twice"
    assert sorted(a) == sorted(b) == sorted(notes), "same notes, only reordered"


def test_shuffling_changes_nothing_but_the_order(tmp_path):
    """The gate. Shuffling happens after selection and the budget fill, both of
    which walk the list in order — earlier would change *which* notes survive."""
    notes = {f"vid{n:07d}xyz"[:11]: f"note {n}" for n in range(40)}
    config, library = build(tmp_path, notes)
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    runs = [answer_question(config, library, client(["An answer."]), "q") for _ in range(3)]

    assert len({r.notes_loaded for r in runs}) == 1
    assert len({r.notes_considered for r in runs}) == 1
    assert len({tuple(sorted(r.note_digests)) for r in runs}) == 1, "same notes every time"
    assert len({tuple(r.selection.video_ids) for r in runs}) > 1, "in different orders"


def test_the_recorded_order_is_the_order_the_model_was_shown(tmp_path):
    """`- notes read:` is what the positional measurement is computed from, so
    it has to follow the shuffle rather than the alphabet."""
    # Markers no one of which is a prefix of another, or `find` locates the
    # wrong one: "note 2" matches inside "note 23".
    ids = [f"vid{n:07d}xyz"[:11] for n in range(30)]
    config, library = build(tmp_path, {v: f"body<{v}>end" for v in ids})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    made = client(["An answer."])
    answer = answer_question(config, library, made, "q")
    shown = made.sent[0]["messages"][1]["content"]

    positions = {v: shown.find(f"body<{v}>end") for v in ids}
    assert all(p >= 0 for p in positions.values()), "every note reached the prompt"
    by_prompt = sorted(ids, key=lambda v: positions[v])
    assert by_prompt == answer.selection.video_ids


def test_a_whole_library_run_names_the_notes_it_read(tmp_path):
    """`all` used to stand in here, and it names nothing about a library that
    grows: 26 stored answers say `all` or nothing and cannot be re-scored,
    because the library they meant is not the library on disk."""
    config, library = build(tmp_path, {A: "note a", B: "note b"})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    answer = answer_question(config, library, client(["An answer."]), "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert "- notes read: all" not in rendered
    header = rendered.split("## Answer")[0]
    assert f"{A}:{note_digest('note a')}" in header
    assert f"{B}:{note_digest('note b')}" in header


def test_a_rewritten_note_changes_the_digest_the_answer_recorded(tmp_path):
    """The whole point. A digest lets a later re-score say "all unchanged, this
    score is valid" instead of scoring against text that did not exist when the
    answer was written — which is what 646 of 646 August notes did."""
    config, library = build(tmp_path, {A: "note a"})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))
    answer = answer_question(config, library, client(["An answer."]), "q")
    recorded = answer.note_digests[A]

    library.write_note(A, "note a, regenerated by a later prompt")

    assert note_digest(load_notes(library)[0].text) != recorded


def test_a_note_digest_is_not_the_note(tmp_path):
    """Hard rule 1 does not stop at the answer file."""
    secret = "the speaker contradicts himself about cache expiry"
    config, library = build(tmp_path, {A: f"## Claims\n\n- {secret}\n"})
    config = replace(config, ask=make_ask_config(max_input_tokens=1_000_000))

    answer = answer_question(config, library, client(["An answer."]), "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert len(answer.note_digests[A]) == 8
    assert secret not in rendered


def test_a_refused_run_writes_no_partial_list(tmp_path):
    """There is no answer to write, so there is nothing claiming to have been
    read. The refusal is the record."""
    config, library = build(
        tmp_path, {A: "aaa " * 1_000, B: "bbb " * 1_000, C: "ccc " * 1_000}
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=5_334))
    spoken = client(["not a list", "still not a list", "An answer."])

    with pytest.raises(SelectionFailed):
        answer_question(config, library, spoken, "q")


def test_no_note_text_reaches_a_stored_answer(tmp_path):
    """**The test this change specifically warrants.** It is the first time
    note-derived data is written into an answer file, and the thing standing
    between ids and a rule 1 breach is that nobody reached for the summary line
    while they were right there."""
    marker = "the speaker contradicts himself about cache expiry four minutes apart"
    config, library = build(
        tmp_path,
        {A: f"## Claims\n\n- {marker} (00:01:00)\n", B: "bbb " * 2_000},
    )
    config = replace(config, ask=make_ask_config(max_input_tokens=4_000))
    spoken = client([f"DROPPED: {A}\nWHY: it ranked lowest", "An answer."])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert marker not in rendered, "no note text"
    assert "title for" not in rendered.split("## Answer")[0], "and no titles either"
    assert f"- notes read: {B}" in rendered, "ids, which are safe"


# -- what the answer layer invents ------------------------------------------
#
# Every other check in this system reads shape: an HTTP status, five lines,
# citation syntax. A reply that terminates cleanly, parses, and is simply wrong
# passes all three — rule 24's semantic failure. An external check of one real
# answer found 5 of 23 checkable strings in neither transcript nor note, four
# of them inside quote marks, and two cited ids that do not exist, on a run
# that reported 23 parsed and 0 unparsed.


def test_a_quote_missing_from_the_cited_note_is_counted(tmp_path):
    config, library = build(tmp_path, {A: "## Claims\n\n- the speaker says caching is cheap\n"})
    text = f'The video claims "caching is free forever and always will be" [{A} 01:00].'
    notes = load_notes(library)

    ids, speech, phrases, unspoken, uncited = check_grounding(text, notes, [A])

    assert ids == []
    assert len(speech) == 1 and A in speech[0].video_ids
    assert (phrases, uncited) == ([], [])


def test_a_short_unverified_phrase_is_not_counted_as_speech(tmp_path):
    """Rule 14, and the reason for the split. `"an agent loop"` beside a
    citation is the model quoting its own vocabulary; a sentence beside one is
    a claim that somebody said it. Summing them over-reported fabrication by
    roughly 3x — four samples of one question gave 14, 11, 34 and 22 against
    sentence-length subsets of 4, 4, 5 and 9."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something else entirely\n"})
    text = f'It leans on "an agent loop" throughout [{A} 01:00].'

    ids, speech, phrases, unspoken, uncited = check_grounding(text, load_notes(library), [A])

    assert speech == [], "too short to be a claim about what somebody said"
    assert len(phrases) == 1 and A in phrases[0].video_ids
    assert (ids, uncited) == ([], [])


def test_the_two_unverified_counters_are_reported_apart(tmp_path):
    """Never summed, and both rendered: the reader's question is "did the model
    quote a video, or quote itself?" and neither number answers it alone."""
    config, library = build(tmp_path, {A: "## Claims\n\n- nothing that matches\n"})
    text = (
        f'It calls this "the read path" [{A} 01:00] and reports that '
        f'"the whole pipeline collapses under any real production load" [{A} 02:00].'
    )

    _ids, speech, phrases, _unspoken, _uncited = check_grounding(text, load_notes(library), [A])

    assert len(speech) == 1 and len(phrases) == 1
    rendered = render_answer(
        Answer(question="q", unverified_speech=speech, unverified_phrases=phrases),
        asked_at="2026-01-01T00:00:00+00:00",
    )
    assert "1 sentence(s) attributed to a video" in rendered
    assert "1 shorter quoted phrase(s) likewise" in rendered


def test_a_quote_present_in_verbatim_is_not_counted(tmp_path):
    """Real speech, correctly quoted — the only outcome that reports nothing."""
    config, library = build(tmp_path, {A: "## Verbatim\n\n- caching is cheap\n"})
    text = f'The video says "caching is cheap" [{A} 01:00].'

    assert check_grounding(text, load_notes(library), [A]) == ([], [], [], [], [])


def test_a_quote_found_only_in_note_prose_is_its_own_outcome(tmp_path):
    """The hidden class this pass exists for.

    `check_grounding` used to match against the whole note, so the note-writer's
    own sentence inside quotation marks verified silently — and note prose is
    roughly twelve times the size of `## Verbatim`, a median 11,400 characters
    against 916, all of it text nobody said.

    Its own outcome rather than folded into `unverified`: a legitimate quote can
    sit inline in `## Claims`, the note-writer quoting a phrase in passing, and a
    Verbatim-only test would call that a failure. Rule 14 — keeping the
    ambiguity visible beats miscounting it either way."""
    config, library = build(
        tmp_path,
        {
            A: "## Claims\n\n- the speaker says caching is cheap\n\n"
            "## Verbatim\n\n- nothing about cost here\n"
        },
    )
    text = f'The video says "the speaker says caching is cheap" [{A} 01:00].'

    ids, speech, phrases, unspoken, uncited = check_grounding(text, load_notes(library), [A])

    assert len(unspoken) == 1 and unspoken[0].video_ids == A
    assert (ids, speech, phrases, uncited) == ([], [], [], [])
    marked = mark_unverified(Answer(question="q", text=text, unspoken_quotes=unspoken))
    assert "[not spoken]" in marked


def test_a_quote_with_no_citation_is_its_own_outcome(tmp_path):
    """Rule 14. "Attributed to the wrong video" and "attributed to nothing" are
    different failures with different fixes."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something\n"})
    text = 'Somebody said "this is a completely unattributed claim" in one of them.'

    ids, speech, phrases, unspoken, uncited = check_grounding(text, load_notes(library), [A])

    assert (speech, phrases) == ([], [])
    assert len(uncited) == 1


def test_an_id_naming_no_loaded_video_is_counted(tmp_path):
    """The gap the citation counter cannot see: a fabricated id inside the
    model's own deliberation is not citation-shaped, so it was never counted as
    anything. `EXLCnmcyLcQ` and `VkLnUsKX0Cw` were reported as zero problems."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something\n"})
    text = f"Some claim [EXLCnmcyLcQ — I might be making this up] and [{A} 01:00]."

    ids, _, _, _, _ = check_grounding(text, load_notes(library), [A])

    assert ids == ["EXLCnmcyLcQ"]


def test_an_eleven_letter_word_in_brackets_is_not_an_id(tmp_path):
    """`consolidate`, `fabricating` and `contradicti` all matched the id shape
    inside the model's prose. A real id carries a digit or a case change."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something\n"})
    text = "[a note about the contradiction and the fabricating of claims]"

    ids, _, _, _, _ = check_grounding(text, load_notes(library), [A])

    assert ids == []


def test_a_hyphenated_compound_is_not_an_id(tmp_path):
    """`_ID_SHAPE_RE` slides, so `single-scalar` matched as `single-scal` and
    was reported as an id naming no loaded video. A hyphen is not evidence of an
    id — English compounds carry one, which is how this survived the old
    all-lowercase guard."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something\n"})
    text = f"[{A} results are single-scalar points with no memory-coupling shown]"

    ids, _, _, _, _ = check_grounding(text, load_notes(library), [A])

    assert ids == []


def test_a_fabricated_id_of_the_wrong_length_is_still_counted(tmp_path):
    """The reason the length is a window rather than a constant.

    `[2s0ixGRLo8QM — not in corpus]` is twelve characters, and pinning the scan
    to exactly eleven removed two false positives while losing this one true
    positive. That is backwards: an over-reporting counter is visible noise, an
    under-reporting one lets a fabricated citation pass silently."""
    config, library = build(tmp_path, {A: "## Claims\n\n- something\n"})
    text = "the orchestration choice [2s0ixGRLo8QM — not in corpus] and the rest"

    ids, _, _, _, _ = check_grounding(text, load_notes(library), [A])

    assert ids == ["2s0ixGRLo8QM"], "caught by rule, where before it was caught by truncation"


def test_a_mis_cased_id_is_a_citation_repair_not_a_missing_video(tmp_path):
    """Counting it here would report one event under two names."""
    config, library = build(tmp_path, {A: "## Claims\n\n- caching is cheap\n"})
    text = f'It says "caching is cheap" [{A.upper()} 01:00].'

    ids, speech, phrases, _unspoken, _uncited = check_grounding(text, load_notes(library), [A])

    assert ids == []
    assert (speech, phrases) == ([], []), "and the quote still resolves against the note"


def test_a_failing_quote_is_marked_where_it_stands(tmp_path):
    """A counter saying "1 unverified" tells a reader to distrust the answer.
    A marker on the sentence tells them to distrust the sentence."""
    config, library = build(tmp_path, {A: "## Verbatim\n\n- nothing alike here\n"})
    invented = "here is an invented quotation nobody in the video ever said"
    spoken = client([f'It claims "{invented}" [{A} 01:00]. The rest is fine.'])

    answer = answer_question(config, library, spoken, "q")
    marked = mark_unverified(answer)

    assert f'"{invented}" [unverified]' in marked
    assert "The rest is fine." in marked, "and nothing else is touched"


def test_a_loose_rendering_of_verbatim_is_marked_differently(tmp_path):
    """Of 42 measured failures, 5 were `## Verbatim` passages rendered
    imprecisely and 24 had no source at all. One marker would accuse the first
    group of what the second did."""
    config, library = build(
        tmp_path,
        {A: "## Verbatim\n\n- you delivered one hundred thousand tokens worth of value here\n"},
    )
    loose = "you delivered one hundred thousand tokens of value here"
    spoken = client([f'He says "{loose}" [{A} 01:00].'])

    answer = answer_question(config, library, spoken, "q")

    assert [q.loose for q in answer.unverified_speech] == [True]
    assert f'"{loose}" [loosely quoted]' in mark_unverified(answer)


def test_prose_outside_verbatim_is_not_a_loose_quote(tmp_path):
    """The 13 in the middle: the claim is in the note, but as the note-writer's
    account rather than as speech, and worded differently. Marking that
    `[loosely quoted]` would say a speaker nearly said it, which is the collapse
    the rule exists to stop — `loose` looks at `## Verbatim` and nowhere else.

    Note what this cannot catch, and the failing first draft of this test is
    what showed it: `check_grounding` matches against the *whole* note, so prose
    copied into quotation marks **exactly** verifies and is never counted. The
    13 are the paraphrased ones; an exact lift of note prose is invisible."""
    config, library = build(
        tmp_path,
        {A: "## Claims\n\n- the pipeline collapses under real production load\n\n"
            "## Verbatim\n\n- something entirely unrelated to that\n"},
    )
    paraphrase = "the whole pipeline falls over once real production traffic arrives"
    spoken = client([f'He says "{paraphrase}" [{A} 01:00].'])

    answer = answer_question(config, library, spoken, "q")

    assert [q.loose for q in answer.unverified_speech] == [False]
    assert "[unverified]" in mark_unverified(answer)


def test_the_marker_never_says_what_the_note_held(tmp_path):
    """Rule 1. Closeness may travel; content may not."""
    secret = "you delivered one hundred thousand tokens worth of value here"
    config, library = build(tmp_path, {A: f"## Verbatim\n\n- {secret}\n"})
    loose = "you delivered one hundred thousand tokens of value here"
    spoken = client([f'He says "{loose}" [{A} 01:00].'])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert "[loosely quoted]" in rendered
    assert secret not in rendered


def test_the_grounding_section_never_prints_the_note(tmp_path):
    """A failing quote may travel — it came from the answer. The note may not:
    not the passage searched, not the nearest match, not what stood there
    instead. Same trap as the size warning, in the same shape."""
    secret = "the speaker contradicts himself about cache expiry four minutes apart"
    config, library = build(tmp_path, {A: f"## Claims\n\n- {secret}\n"})
    invented = "here is an invented quotation nobody in the video ever said"
    spoken = client([f'It claims "{invented}" [{A} 01:00].'])

    answer = answer_question(config, library, spoken, "q")
    rendered = render_answer(answer, asked_at="2026-01-01T00:00:00+00:00")

    assert len(answer.unverified_speech) == 1
    assert invented in rendered, "the failing quote is shown"
    assert secret not in rendered, "the note is not"
