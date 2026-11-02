"""A plan shorter than its budget while gaps are named is accepted and counted.

Measured: a reply wrote 5 queries for a budget of 8, one per gap, with 4 gaps
given none. Asking again costs a call; the planner takes the gaps from round
two. Subjects are invented.
"""

from __future__ import annotations

from marginalia.contracts import CoverageItem


def test_a_plan_shorter_than_its_budget_while_gaps_are_named_is_counted() -> None:
    from test_search_plan import GOOD_PLAN, _planned

    coverage = [CoverageItem("Hot composting", False, "", missing=("pile temperature",))]
    plan = _planned(GOOD_PLAN, count=9, coverage=coverage).plan
    assert plan.unfilled == 9 - sum(len(item.queries) for item in plan.items)
    assert plan.as_dict()["unfilled"] == plan.unfilled
    fresh = _planned(GOOD_PLAN, count=9).plan
    assert fresh.unfilled == 0, "no coverage, no gaps named"
