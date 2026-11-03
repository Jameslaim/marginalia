"""An expansion query that comes back with no aim is given one from the coverage lines.

Measured: a round-one expansion reply named no aim for any of its 9 queries,
and the rule-43 gate kept all 9 unchecked. Each is now placed by the matcher
the aims use: one on a gap runs aimed at it, one on a held line while a gap
is open is dropped and counted, one that matches nothing stays unaimed and is
counted. Subjects are invented.
"""

from __future__ import annotations

from marginalia.brain.gaps import infer_aim
from marginalia.contracts import ORIGIN_BRIEF
from test_discover import (  # noqa: F401 - fixtures are found by name
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
from test_follow_gaps import COVERAGE, TOPIC, _approve


def test_an_unaimed_query_is_placed_on_the_line_it_names() -> None:
    assert infer_aim("treating mites on hens", COVERAGE) == "Hen health — mites"
    assert infer_aim("best roosting bars for a coop", COVERAGE) == "Coop design — roosting bars"
    assert infer_aim("winter garden planting", COVERAGE) == ""


def test_the_gate_checks_an_expansion_query_once_it_has_an_aim(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    judge = FakeJudge(queries=[["treating mites on hens", "best roosting bars for a coop",
                                "winter garden planting"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=1,
                  planner=FakePlanner(baseline=COVERAGE))

    first = outcome.rounds[0]
    assert first.queries == ["treating mites on hens", "winter garden planting"]
    assert first.held_while_gaps == {ORIGIN_BRIEF: 1}, "the held one, dropped and counted"
    assert (first.expansion_aims_inferred, first.expansion_unaimed) == (2, 1)
    details = {query.text: query.origin_detail for query in first.planned}
    assert details["treating mites on hens"] == "aim: Hen health — mites"
