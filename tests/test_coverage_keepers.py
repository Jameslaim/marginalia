"""A topic's coverage rests only on videos that passed that topic's stage 2.

Measured: a baseline read every note in the library and cited a MAYBE note and
ten notes no stage 2 had judged for the topic as held evidence. Its evidence
now holds only notes whose video's latest stage-2 verdict for this topic is
YES; every other note is left out and counted by kind. Subjects are invented.
"""

from __future__ import annotations

from marginalia.cli import _baseline_lines
from marginalia.contracts import VERDICT_MAYBE, VERDICT_NO, VERDICT_YES
from marginalia.core.ledger import STAGE_FULL, topic_verdict
from marginalia.ask import keeper_notes, load_notes
from marginalia.llm import load_prompt
from test_discover import (  # noqa: F401 - fixtures are found by name
    TOPIC,
    FakeJudge,
    FakePlanner,
    client,
    config,
    kept_note,
    ledger,
    library,
    run,
    searches,
)
from test_gap_first import COVERAGE


def _verdict(ledger, video_id, verdict, topic=TOPIC):
    ledger.log_decision(video_id, topic, stage=STAGE_FULL, verdict=verdict, reason="judged")


def _library(library, ledger):
    """One note of each kind: this topic's YES, NO and MAYBE, another topic's
    YES, and one with no stage-2 verdict at all."""
    kept_note(library, ledger, TOPIC, "keeperyes01", "# Kept\n\nIt covers a held part.\n")
    library.write_note("declinedno1", "# Declined\n\nSomething else.\n")
    _verdict(ledger, "declinedno1", VERDICT_NO)
    library.write_note("uncertain01", "# Unsure\n\nPerhaps.\n")
    _verdict(ledger, "uncertain01", VERDICT_MAYBE)
    kept_note(library, ledger, "keeping backyard hens", "othertopic1", "# Hens\n\nCoops.\n")
    library.write_note("ingestedby1", "# Ingested\n\nBy hand.\n")


def test_only_this_topics_stage_two_yes_is_evidence(ledger, library) -> None:
    _library(library, ledger)

    kept, left_out = keeper_notes(load_notes(library, ledger), ledger, TOPIC)

    assert [note.video_id for note in kept] == ["keeperyes01"]
    assert left_out == {"no": 1, "maybe": 1, "unjudged": 2}


def test_the_latest_verdict_for_the_topic_decides(ledger, library) -> None:
    kept_note(library, ledger, TOPIC, "changedmind", "# A note\n\nIt covers a held part.\n")
    _verdict(ledger, "changedmind", VERDICT_NO)
    assert topic_verdict(ledger.decisions_for("changedmind"), TOPIC) == VERDICT_NO
    assert keeper_notes(load_notes(library, ledger), ledger, TOPIC)[1]["no"] == 1


def test_the_baseline_reads_keepers_and_counts_the_rest(
    config, ledger, library, client, searches
) -> None:
    _library(library, ledger)
    planner = FakePlanner(baseline=COVERAGE)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1, planner=planner)

    assert planner.assessed == 2
    assert "keeperyes01" in planner.notes_sent and "declinedno1" not in planner.notes_sent
    assert "othertopic1" not in planner.notes_sent and "ingestedby1" not in planner.notes_sent
    assert outcome.baseline.notes_read == 1
    assert outcome.baseline.notes_left_out == {"no": 1, "maybe": 1, "unjudged": 2}
    assert ("          notes left out (not this topic's stage-2 YES): 1 NO, 1 MAYBE, "
            "2 not judged for this topic") in _baseline_lines(outcome)


def test_a_fresh_topic_in_a_shared_library_sends_nothing(
    config, ledger, library, client, searches
) -> None:
    kept_note(library, ledger, "keeping backyard hens", "othertopic1", "# Hens\n\nCoops.\n")
    planner = FakePlanner(baseline=COVERAGE)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1, planner=planner)

    assert planner.assessed == 0, "no call"
    assert outcome.baseline.items == [] and outcome.baseline.notes_left_out == {
        "no": 0, "maybe": 0, "unjudged": 1}
    assert _baseline_lines(outcome)[0].startswith(
        "baseline: none — the library holds no note this topic's stage 2 kept")


def test_the_prompt_no_longer_asks_for_no_reasons() -> None:
    prompt = load_prompt("plan/baseline")
    assert prompt.version == "7"
    assert "A NO reason can still" not in prompt.text and "for another brief" not in prompt.text
    assert VERDICT_YES  # the evidence is this topic's YES alone
