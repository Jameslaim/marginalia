"""What the library baseline is shown, and what it says when it had to be cut.

One index line per note — a title and the first claim — scaled and could not
show coverage: a run's baseline marked 0 of 14 brief items held while about 25
stored keepers' own stage-2 reasons named five of the brief's parts. Each note
now brings its latest stage-2 reason and its Claims, within a fixed budget, and
a cut is a named, counted mode (rule 14).
"""

from __future__ import annotations

from marginalia.contracts import LibraryCoverage
from marginalia.core.ledger import STAGE_FULL, STAGE_METADATA
from marginalia.flow.discover import (
    EVIDENCE_CLAIMS_CUT,
    EVIDENCE_FULL,
    EVIDENCE_INDEX,
    NoteEvidence,
    _note_evidence,
    library_evidence,
)
from marginalia.contracts import LoadedNote

CLAIMS = [f"claim number {n} says something specific about the subject" for n in range(30)]


def _entries(count: int) -> list[NoteEvidence]:
    return [
        NoteEvidence(
            f"vid{n:08d}", "2025-01-01", f"title {n}",
            "stage 2 said YES for this topic: explains the drainage part", CLAIMS,
        )
        for n in range(count)
    ]


def test_a_small_library_is_sent_whole_and_says_so() -> None:
    text, size = library_evidence(_entries(3), budget_tokens=100_000)

    assert size.mode == EVIDENCE_FULL
    assert text.startswith("EVIDENCE: full")
    assert text.count("judge: stage 2 said YES") == 3
    assert "claim number 0 says" in text


def test_a_large_library_cuts_every_note_s_claims_to_one_share_and_counts_it() -> None:
    text, size = library_evidence(_entries(200), budget_tokens=20_000)

    assert size.mode == EVIDENCE_CLAIMS_CUT
    assert size.notes_cut == 200
    assert 200 <= size.claims_chars < 1_500
    assert text.startswith("EVIDENCE: DEGRADED (claims cut)")
    assert text.count("judge:") == 200, "no note is dropped"
    assert size.chars <= 20_000 * 3.992 + 200


def test_a_library_too_large_for_shares_falls_back_to_index_lines() -> None:
    text, size = library_evidence(_entries(2_000), budget_tokens=20_000)

    assert size.mode == EVIDENCE_INDEX
    assert text.startswith("EVIDENCE: DEGRADED (index lines)")
    assert text.count("judge:") == 2_000


def test_the_baseline_reports_whether_it_was_degraded() -> None:
    assert not LibraryCoverage(evidence_mode=EVIDENCE_FULL).degraded
    assert LibraryCoverage(evidence_mode=EVIDENCE_CLAIMS_CUT).degraded
    stored = LibraryCoverage(evidence_mode=EVIDENCE_INDEX, notes_cut=7).as_dict()
    assert (stored["evidence_mode"], stored["notes_cut"]) == (EVIDENCE_INDEX, 7)


class _Row:
    def __init__(self, topic, stage, verdict, reason, outcome="judged") -> None:
        self.topic, self.stage, self.verdict = topic, stage, verdict
        self.reason, self.outcome = reason, outcome


class _Ledger:
    def __init__(self, rows) -> None:
        self.rows = rows

    def decisions_for(self, video_id):
        return self.rows


NOTE = LoadedNote(
    "vid00000001", "A title",
    "# A title\n\n## Claims\n\n- Drainage depends on texture [note: the writer's guess].\n"
    "- Clay holds water.\n\n## Key moments\n\n- 0:10 something else\n",
)


def test_a_note_brings_its_claims_without_markers_and_its_judge_reason() -> None:
    ledger = _Ledger([
        _Row("other topic", STAGE_FULL, "no", "covers no PART of that brief"),
        _Row("this topic", STAGE_METADATA, "yes", "a title match"),
        _Row("this topic", STAGE_FULL, "yes", "explains drainage"),
        _Row("other topic", STAGE_FULL, "yes", "a later reason for another brief"),
    ])

    evidence = _note_evidence(NOTE, "2025-01-01", ledger, "this topic")

    assert evidence.judged == "stage 2 said YES for this topic: explains drainage"
    assert evidence.claims == ["Drainage depends on texture .", "Clay holds water."]


def test_another_brief_s_reason_is_shown_and_labelled_when_none_is_this_topic_s() -> None:
    ledger = _Ledger([
        _Row("other topic", STAGE_FULL, "yes", "explains drainage for a lawn brief"),
        _Row("other topic", STAGE_FULL, "maybe", "no verdict", outcome="call_failed"),
    ])

    evidence = _note_evidence(NOTE, "", ledger, "this topic")

    assert evidence.judged == (
        "stage 2 said YES for another brief: explains drainage for a lawn brief"
    )


def test_a_note_never_judged_says_so() -> None:
    evidence = _note_evidence(NOTE, "", _Ledger([]), "this topic")

    assert evidence.judged == "no stage-2 judgement on record"
