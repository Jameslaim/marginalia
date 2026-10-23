"""One rule decides whether a short transcript can be noted, for both layers.

Stage 2 kept two clips of 29 and 55 seconds as YES on clear evidence, each with
concrete figures, and the note worker refused both as under the 1,200-character
floor — a floor measured where short clips were thin. A stage-2 YES on clear
evidence, whose reason claims no damage, lowers the floor to `_VETTED_FLOOR`; a
transcript too thin to say anything is still refused, and counted.
"""

from __future__ import annotations

from conftest import make_note_config

from marginalia.core.ledger import DecisionRow
from marginalia.enrich.note import _VETTED_FLOOR, _length_refusal, note_refusal, vetted_by_stage_two


def _full(verdict="yes", evidence="CLEAR", reason="explains the payout with figures", outcome="judged"):
    return DecisionRow(
        video_id="aaaaaaaaaaa", topic="t", stage="full", verdict=verdict, reason=reason,
        decided_at="2026-10-24T00:00:00", evidence=evidence, outcome=outcome,
    )


def test_a_short_transcript_stage_two_kept_on_clear_evidence_is_noteable() -> None:
    config = make_note_config(min_transcript_chars=1_200)

    assert note_refusal(None, [_full()], 961, config) == ""
    assert note_refusal(None, [], 961, config).startswith("transcript is 961 chars, under the 1,200 floor")


def test_vetting_needs_a_judged_yes_on_clear_evidence_whose_reason_claims_no_damage() -> None:
    assert vetted_by_stage_two([_full()])
    assert not vetted_by_stage_two([_full(verdict="no")])
    assert not vetted_by_stage_two([_full(evidence="DEGRADED")])
    assert not vetted_by_stage_two([_full(outcome="call_failed")])
    assert not vetted_by_stage_two([_full(reason="Captions failed for most of this clip.")])
    metadata_yes = DecisionRow(
        video_id="aaaaaaaaaaa", topic="t", stage="metadata", verdict="yes",
        reason="a title", decided_at="2026-10-24T00:00:00",
    )
    assert not vetted_by_stage_two([metadata_yes]), "a title is not a reading"


def test_a_transcript_too_thin_to_say_anything_is_still_refused() -> None:
    config = make_note_config(min_transcript_chars=1_200)

    refusal = _length_refusal(_VETTED_FLOOR - 1, config, vetted=True)

    assert refusal is not None and refusal[0] == "floor"
    assert "vetted-video floor" in refusal[1]


def test_a_floor_set_lower_than_the_vetted_one_still_wins() -> None:
    config = make_note_config(min_transcript_chars=100)

    assert _length_refusal(150, config, vetted=True) is None
