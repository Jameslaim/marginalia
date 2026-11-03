"""No placeholder ever runs as a query, and a refused expansion is not replaced by the topic.

Measured: an expansion reply of one output token, after 686.7 seconds, was the
word `query`; it ran as round one's only expansion search, recorded as aimed at
the whole topic. Placeholders are dropped where the reply is read (rule 30); a
reply with no usable query is refused and the round runs without it; a reply
with fewer than half the queries asked for is counted short. Subjects are invented.
"""

from __future__ import annotations

from marginalia.contracts import (
    ORIGIN_BRIEF,
    ORIGIN_TERRITORY,
    Brief,
    ExpandedQueries,
    PlannedQuery,
    TerritoryMap,
)
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeMapper,
    client,
    config,
    ledger,
    library,
    run,
    searches,
)
from test_judge_full import _recording_judge

BRIEF = Brief(topic="keeping backyard hens through winter, with all the detail a long topic has",
              text="INTENT\nKeep hens warm.\n\nPARTS TO COVER\n- Coop heating\n", version="1/1")


def test_the_logged_one_token_reply_is_refused_not_searched() -> None:
    judge, _ = _recording_judge("query")

    queries = judge.expand_queries(BRIEF, count=9)

    assert list(queries) == [], "neither the placeholder nor the topic runs"
    assert (queries.refused, queries.placeholders) == (True, 1)


def test_placeholder_lines_are_dropped_and_counted_and_the_rest_run() -> None:
    judge, _ = _recording_judge(
        "query | aim\n<query>\nheated waterer for hens | Coop heating\n...\n"
        "coop heater safety | Coop heating\nwinter hen care tips | Coop heating\n"
        "deep litter method winter | Coop heating\nhens in snow | Coop heating\n"
    )

    queries = judge.expand_queries(BRIEF, count=9)

    assert queries == ["heated waterer for hens", "coop heater safety", "winter hen care tips",
                       "deep litter method winter", "hens in snow"]
    assert (queries.placeholders, queries.refused, queries.short) == (3, False, False)


def test_a_reply_with_fewer_than_half_the_queries_asked_for_is_short() -> None:
    judge, _ = _recording_judge("heated waterer for hens | Coop heating\ncoop heater safety | Coop heating")
    queries = judge.expand_queries(BRIEF, count=9)
    assert queries.short and not queries.refused and len(queries) == 2


class _RefusingJudge(FakeJudge):
    def expand_queries(self, brief, *, count=9, concepts=(), coverage=()):
        super().expand_queries(brief, count=count, concepts=concepts, coverage=coverage)
        return ExpandedQueries(placeholders=1, refused=True)


def test_round_one_runs_the_maps_queries_when_the_expansion_is_refused(
    config, ledger, library, client, searches
) -> None:
    mapped = TerritoryMap(topic="t", version="1", queries=[
        PlannedQuery("coop heater safety", ORIGIN_TERRITORY, "term: heater"),
    ])

    outcome = run(config, ledger, library, client, _RefusingJudge(), max_rounds=1,
                  mapper=FakeMapper(mapped), search_source="map")

    first = outcome.rounds[0]
    assert first.queries == ["coop heater safety"]
    assert (first.expansion_refused, first.expansion_placeholders) == (1, 1)
    assert all(query.origin != ORIGIN_BRIEF for query in first.planned)
