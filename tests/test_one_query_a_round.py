"""One query text runs once a round, whatever its source or filters.

Measured: the planner's query and a thread query with the same words ran as two
searches in one round, differing only in a language filter. A different
`order` is still a different search: it returns another slice of the results.
Subjects are invented.
"""

from __future__ import annotations

from marginalia.contracts import (
    ORIGIN_BRIEF,
    ORIGIN_PLANNER,
    ORIGIN_THREAD,
    PlannedQuery,
    RoundPlan,
    SearchFilters,
)
from marginalia.flow.discover import one_per_text
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeMapper,
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

def test_b1_one_query_text_runs_once_a_round_whatever_its_source_or_filters(
    config, ledger, library, client, searches
) -> None:
    spanish = SearchFilters(relevance_language="es")
    plan = RoundPlan(round_number=1, queries=[
        PlannedQuery("cuánto cuesta un gallinero", ORIGIN_PLANNER, "gap — costs", filters=spanish),
    ])
    judge = FakeJudge(queries=[["q1"], ["Cuánto cuesta  un gallinero", "q2"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=2,
                  mapper=FakeMapper(), planner=FakePlanner(plans=[plan]))

    texts = [row.query.lower() for row in ledger.queries_for_run(outcome.run_id) if row.round == 2]
    assert texts.count("cuánto cuesta un gallinero") == 1
    assert outcome.rounds[1].duplicates_skipped == 1


def test_b1_a_different_order_is_still_a_different_search() -> None:
    by_date = PlannedQuery("hen mites", ORIGIN_BRIEF, filters=SearchFilters(order="date"))
    kept, skipped = one_per_text([PlannedQuery("hen mites", ORIGIN_PLANNER), by_date,
                                  PlannedQuery("Hen  Mites", ORIGIN_THREAD)])
    assert [q.origin for q in kept] == [ORIGIN_PLANNER, ORIGIN_BRIEF] and skipped == 1
