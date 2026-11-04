"""`marg ask --topic` reads only the notes the topic's stage 2 kept.

Measured: one question read all 178 notes in the library, about 613,000 tokens,
for $0.096. With `--topic`, only notes whose video's latest stage-2 verdict for
that approved topic is YES are read — the keeper rule the baseline uses — and
the rest are counted. Subjects are invented.
"""

from __future__ import annotations

import json

from marginalia.ask import answer_question, keeper_notes
from marginalia.brain.judge import brief_sha256
from marginalia.core.ledger import STAGE_FULL, Ledger
from test_ask import A, B, C, build, client

TOPIC = "keeping backyard hens"


def _judged(library):
    """A is a keeper for the topic, B was declined, C belongs to another topic."""
    ledger = Ledger(library.ledger_path)
    ledger.log_decision(A, TOPIC, stage=STAGE_FULL, verdict="yes", reason="kept")
    ledger.log_decision(B, TOPIC, stage=STAGE_FULL, verdict="no", reason="declined")
    ledger.log_decision(C, "winter gardening", stage=STAGE_FULL, verdict="yes", reason="kept")
    return ledger


def test_a_topic_reads_only_its_keepers_and_counts_the_rest(tmp_path) -> None:
    config, library = build(tmp_path, {A: "a" * 50, B: "b" * 50, C: "c" * 50})
    ledger = _judged(library)
    made = client(["Answered."])

    answer = answer_question(config, library, made, "q", ledger=ledger, topic=TOPIC)

    prompt = made.sent[0]["messages"][1]["content"]
    assert f"===== VIDEO {A} =====" in prompt
    assert f"===== VIDEO {B} =====" not in prompt and f"===== VIDEO {C} =====" not in prompt
    assert (answer.scope_topic, answer.notes_loaded) == (TOPIC, 1)
    assert answer.notes_left_out == {"no": 1, "maybe": 0, "unjudged": 1}
    assert set(answer.titles) == {A, B, C}, "a citation to any note still resolves"


def test_the_keeper_rule_is_the_baselines(tmp_path) -> None:
    from marginalia.ask import load_notes
    from marginalia.flow import discover

    config, library = build(tmp_path, {A: "a" * 50, B: "b" * 50})
    ledger = _judged(library)
    assert discover.keeper_notes is keeper_notes, "one rule, not two"
    kept, _ = keeper_notes(load_notes(library, ledger), ledger, TOPIC)
    assert [note.video_id for note in kept] == [A]


def test_a_topic_with_no_keepers_makes_no_call(tmp_path) -> None:
    config, library = build(tmp_path, {B: "b" * 50})
    ledger = _judged(library)
    made = client(["Answered."])

    answer = answer_question(config, library, made, "q", ledger=ledger, topic=TOPIC)

    assert made.sent == [] and answer.requests == 0
    assert "nothing to answer from" in answer.text


def test_the_cli_refuses_a_topic_with_no_approved_brief(tmp_path, capsys, monkeypatch) -> None:
    from marginalia import cli

    config, library = build(tmp_path, {A: "a" * 50})
    _judged(library)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "LlmClient", lambda *_a, **_k: client(["Answered."]))

    assert cli.main(["ask", "q", "--topic", TOPIC, "--no-save"]) == cli.EXIT_USAGE
    assert "no approved brief" in capsys.readouterr().err

    text = "INTENT\nKeep hens.\n\nPARTS TO COVER\n- Coops\n"
    Ledger(library.ledger_path).approve_brief(TOPIC, text, brief_sha256(text))
    assert cli.main(["ask", "q", "--topic", TOPIC, "--no-save", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["notes_loaded"] == 1


# -- part 2: with no --topic, the whole library, said on the header -----------------


def test_with_no_topic_the_whole_library_is_read_and_the_header_says_so(tmp_path) -> None:
    from marginalia.ask import render_answer
    from marginalia.cli import _ask_summary

    config, library = build(tmp_path, {A: "a" * 50, B: "b" * 50, C: "c" * 50})
    _judged(library)
    made = client(["Answered."])

    answer = answer_question(config, library, made, "q", ledger=Ledger(library.ledger_path))

    assert answer.notes_loaded == 3 and answer.scope_topic == "" and answer.notes_left_out == {}
    assert "- scope: the whole library (no --topic)" in render_answer(answer, asked_at="t")
    assert "; scope: the whole library (no --topic)" in _ask_summary(answer).splitlines()[0]


# -- part 3: the answer file and --json record the scope ------------------------------


def test_the_file_and_json_record_the_topic_and_what_it_left_out(tmp_path, capsys, monkeypatch) -> None:
    from marginalia import cli
    from marginalia.ask import render_answer

    config, library = build(tmp_path, {A: "a" * 50, B: "b" * 50, C: "c" * 50})
    ledger = _judged(library)
    text = "INTENT\nKeep hens.\n\nPARTS TO COVER\n- Coops\n"
    ledger.approve_brief(TOPIC, text, brief_sha256(text))

    answer = answer_question(config, library, client(["Answered."]), "q", ledger=ledger, topic=TOPIC)
    assert (f"- scope: topic {TOPIC!r} — 1 note(s) of videos its stage 2 kept loaded, 2 left out "
            "(1 NO, 0 MAYBE, 1 not judged for this topic)") in render_answer(answer, asked_at="t")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "LlmClient", lambda *_a, **_k: client(["Answered."]))
    assert cli.main(["ask", "q", "--topic", TOPIC, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["topic"] == TOPIC
    assert payload["scope"]["notes_loaded"] == 1
    assert payload["scope"]["notes_left_out"] == {"no": 1, "maybe": 0, "unjudged": 1}
    saved = payload["saved_to"]
    assert saved and f"- scope: topic {TOPIC!r}" in open(saved, encoding="utf-8").read()
