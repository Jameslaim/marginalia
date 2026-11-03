"""Exhaustion counts only queries whose candidates could be judged.

Measured: a gap was recorded exhausted on 6 queries run while every transcript
fetch was blocked — which says the fetches failed, not that the gap is empty.
A query whose candidates hit a transient fetch failure is left out of the gap
tallies, and counted; a round that stopped on a block counts none, and a run
that stopped `throttled` records no new exhaustion. Subjects are invented.
"""

from __future__ import annotations

from marginalia.contracts import STOP_THROTTLED, AimedQuery, RoundPlan
from marginalia.providers import TransientError
from test_discover import (  # noqa: F401 - fixtures are found by name
    kept_note,
    FakeJudge,
    FakePlanner,
    FakeTranscripts,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)
from test_follow_gaps import COVERAGE, TOPIC, _approve, _with

AIMS = ("Hen health — mites", "Hen health — egg binding", "Hen health")


def _rounds(searches, rounds, failing=()):
    """Three queries a round on the one gap, one candidate each; `failing`
    names the (round, query) whose candidate's fetch fails transiently."""
    per_round, failures = [], {}
    for number in range(1, rounds + 1):
        texts = [f"mites round {number} take {n}" for n in range(3)]
        for n, text in enumerate(texts):
            video = f"r{number}q{n}video".ljust(11, "x")[:11]
            searches[text] = [result(video)]
            if (number, n) in failing:
                failures[video] = TransientError("bot check")
        per_round.append([AimedQuery(text, aim) for text, aim in zip(texts, AIMS)])
    planner = FakePlanner(plans=[RoundPlan(round_number=n, coverage=COVERAGE) for n in range(1, rounds)],
                          baseline=COVERAGE)
    return FakeJudge(queries=per_round), FakeTranscripts(failures=failures), planner


def test_a_query_whose_candidate_failed_to_fetch_is_not_counted(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    judge, transcripts, planner = _rounds(searches, 2, failing={(1, 0), (2, 0)})

    outcome = run(_with(config, gap_exhausted_after=5), ledger, library, client, judge,
                  provider=transcripts, planner=planner, max_rounds=2, max_videos=20)

    assert [report.exhaustion_excluded for report in outcome.rounds] == [1, 1]
    assert outcome.gaps[0].queries == 4, "6 ran, 2 could not be judged"
    assert outcome.gaps[0].exhausted_in == 0, "counted all 6, the gap would be exhausted"


def test_a_round_that_stopped_on_a_block_counts_nothing_and_records_nothing(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    judge, transcripts, planner = _rounds(searches, 2, failing={(2, 0), (2, 1), (2, 2)})

    outcome = run(_with(config, gap_exhausted_after=3), ledger, library, client, judge,
                  provider=transcripts, planner=planner, max_rounds=2, max_videos=20)

    assert outcome.stopped_because == STOP_THROTTLED
    assert outcome.rounds[1].exhaustion_excluded == 3
    assert [r[1] for r in outcome.gaps[0].rounds if r[0] == 2] in ([], [0])
    assert outcome.gaps[0].exhausted_in == 0
    assert ledger.exhausted_gaps(TOPIC, 1) == []
