"""`marg note` with no ids notes keepers only; named, the owner's choice stands.

Measured: a no-ids run queued videos stage 2 had said NO to, against its own
docstring. A keeper is a video some topic's stage 2 kept (YES or MAYBE). One
stage 2 said NO to for every topic is not queued; one discovery found and
stage 2 has not read waits for it; one with no decision at all — a manual
ingest — is the owner's choice and is queued. Subjects are invented.
"""

from __future__ import annotations

from pathlib import Path

from marginalia.contracts import NOTED, VERDICT_MAYBE, VERDICT_NO, VERDICT_YES
from marginalia.core.ledger import (
    NOT_KEEPER_NO,
    NOT_KEEPER_UNREAD,
    STAGE_FULL,
    STAGE_METADATA,
    note_keeper,
)
from marginalia.enrich.note import write_notes
from test_note import GOOD_NOTE, client, library_with, prompts


def _decide(ledger, video_id, verdict, topic="keeping backyard hens", stage=STAGE_FULL):
    ledger.log_decision(video_id, topic, stage=stage, verdict=verdict, reason="r")


def test_who_is_a_keeper(tmp_path: Path) -> None:
    _, _, ledger = library_with(tmp_path, "keptyes0001", "keptmaybe01", "allno000001", "mixedtopic1",
                                "unread00001", "ingested001")
    _decide(ledger, "keptyes0001", VERDICT_YES)
    _decide(ledger, "keptmaybe01", VERDICT_MAYBE)
    _decide(ledger, "allno000001", VERDICT_NO)
    _decide(ledger, "allno000001", VERDICT_NO, topic="winter coops")
    _decide(ledger, "mixedtopic1", VERDICT_NO)
    _decide(ledger, "mixedtopic1", VERDICT_YES, topic="winter coops")
    _decide(ledger, "unread00001", VERDICT_MAYBE, stage=STAGE_METADATA)

    keeper = {video: note_keeper(ledger.decisions_for(video)) for video in (
        "keptyes0001", "keptmaybe01", "allno000001", "mixedtopic1", "unread00001", "ingested001")}

    assert keeper == {
        "keptyes0001": "", "keptmaybe01": "", "mixedtopic1": "", "ingested001": "",
        "allno000001": NOT_KEEPER_NO, "unread00001": NOT_KEEPER_UNREAD,
    }


def test_a_no_ids_run_notes_keepers_and_counts_the_rest(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "keptyes0001", "allno000001", "unread00001")
    _decide(ledger, "keptyes0001", VERDICT_YES)
    _decide(ledger, "allno000001", VERDICT_NO)
    _decide(ledger, "unread00001", VERDICT_MAYBE, stage=STAGE_METADATA)

    run = write_notes(config, ledger, library, client(GOOD_NOTE), prompts_directory=prompts(tmp_path))

    assert [r.video_id for r in run.results if r.outcome == NOTED] == ["keptyes0001"]
    assert run.not_keepers == {NOT_KEEPER_NO: 1, NOT_KEEPER_UNREAD: 1}
    counts = ledger.note_counts(version=run.version)
    assert counts["not_keepers"] == 2


def test_a_named_video_stage_2_said_no_to_is_noted_and_says_so(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "allno000001")
    _decide(ledger, "allno000001", VERDICT_NO)

    run = write_notes(config, ledger, library, client(GOOD_NOTE), video_ids=["allno000001"],
                      prompts_directory=prompts(tmp_path))

    (result,) = run.results
    assert result.outcome == NOTED
    assert result.detail.endswith(f"— {NOT_KEEPER_NO}; named, so noted")
    assert run.named_not_keepers == ["allno000001"]
