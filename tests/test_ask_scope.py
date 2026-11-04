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
