"""Recency belongs to the approved brief: decided once per version, or set by the owner.

The first run on an approved version decides it, as the widest of the samples it
makes, and stores it with the version. Every later run on that version uses it
with one sample, for the plan, the map and the path with no context alike, and
says so. The owner can set it, checked like a model's RECENCY line. Without an
approved brief a run samples its own, as before. Subjects are invented.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from marginalia.brain.judge import brief_sha256
from marginalia.brain.search_plan import SearchPlanner
from marginalia.brain.territory import Mapper
from marginalia.cli import EXIT_FAILED, EXIT_OK, _territory_lines, main
from marginalia.contracts import (
    RECENCY_DECIDED,
    RECENCY_OWNER,
    RECENCY_SAMPLED,
    SOURCE_MAP,
    SOURCE_PLAN,
    Recency,
)
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library
from marginalia.flow.brief import set_owner_recency
from marginalia.flow.discover import decide_recency
from marginalia.flow.export import render_map
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeMapper,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)
from test_search_plan import BRIEF, BRIEF_TEXT, GOOD_PLAN, TOPIC, FakeClient, FakeReply
from test_search_source import a_plan, mapped


class VotingPlanner:
    """A search planner whose samples voted ``months``; records what it was given."""

    def __init__(self, months=24, samples=(None, 24, 24)) -> None:
        self.months, self.samples = months, samples
        self.given: list[object] = []

    def plan(self, brief, *, recency=None, **kwargs):
        self.given.append(recency)
        territory = a_plan("plan query")
        window = recency or Recency(months=self.months, reason="sampled", samples=self.samples)
        return replace(territory, recency=window)


class VotingMapper(FakeMapper):
    def __init__(self) -> None:
        super().__init__(mapped("map query"))
        self.given: list[object] = []

    def map(self, brief, *, count=8, max_tokens=None, recency=None):
        self.given.append(recency)
        self.calls += 1
        return replace(self.territory, recency=recency or Recency(months=12, samples=(12, 12, 12)))


def _approve(ledger, text=BRIEF_TEXT):
    return ledger.approve_brief(TOPIC, text, brief_sha256(text))


def _plan_config(config):
    return replace(config, discover=replace(config.discover, search_source=SOURCE_PLAN))


def _go(config, ledger, library, client, planner, **kwargs):
    return run(_plan_config(config), ledger, library, client, FakeJudge(), topic=TOPIC,
               max_rounds=1, search_planner=planner, **kwargs)


# -- decided once, reused --------------------------------------------------------------


def test_the_first_run_decides_it_and_later_runs_reuse_it(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    _approve(ledger)
    planner = VotingPlanner(months=None, samples=(None, None, 24))

    first = _go(config, ledger, library, client, planner)
    assert planner.given == [None], "the first run samples"
    assert first.territory.recency.source == RECENCY_DECIDED
    row = ledger.brief_recency(TOPIC, 1)
    assert row.months is None and row.source == RECENCY_DECIDED and row.run_id == first.run_id
    assert row.samples_json == "[null, null, 24]"

    second = _go(config, ledger, library, client, VotingPlanner(months=6, samples=(6, 6, 6)))
    assert second.territory.recency.months is None, "the stored window, not this run's vote"
    assert second.territory.recency.source == RECENCY_DECIDED
    assert second.territory.recency.stored_from == first.run_id
    text = "\n".join(_territory_lines(second))
    assert f"[the brief version's, decided by run {first.run_id} from samples any time/any time/24; stored]" in text


def test_the_map_reuses_it_too(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    _approve(ledger)
    map_config = replace(config, discover=replace(config.discover, search_source=SOURCE_MAP))
    mapper = VotingMapper()
    run(map_config, ledger, library, client, FakeJudge(), topic=TOPIC, max_rounds=1, mapper=mapper)
    run(map_config, ledger, library, client, FakeJudge(), topic=TOPIC, max_rounds=1, mapper=mapper)
    assert mapper.given[0] is None and mapper.given[1].months == 12
    assert mapper.given[1].source == RECENCY_DECIDED


def test_a_new_brief_version_decides_again(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    _approve(ledger)
    _go(config, ledger, library, client, VotingPlanner(months=24, samples=(24, 24, 24)))
    _approve(ledger, BRIEF_TEXT.replace("at home", "in a small yard"))
    planner = VotingPlanner(months=None, samples=(None, 24, 24))
    outcome = _go(config, ledger, library, client, planner)
    assert planner.given == [None]
    assert ledger.brief_recency(TOPIC, 1).months == 24 and ledger.brief_recency(TOPIC, 2).months is None
    assert outcome.territory.recency.months is None


def test_without_an_approved_brief_a_run_samples_its_own_and_stores_nothing(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = []
    planner = VotingPlanner()
    outcome = _go(config, ledger, library, client, planner)
    assert planner.given == [None] and outcome.territory.recency.source == RECENCY_SAMPLED
    assert ledger.brief_recency(TOPIC, 1) is None


def test_a_window_no_sample_voted_for_is_not_stored(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    _approve(ledger)
    outcome = _go(config, ledger, library, client, VotingPlanner(months=None, samples=()))
    assert outcome.territory.recency.source == RECENCY_SAMPLED
    assert ledger.brief_recency(TOPIC, 1) is None, "the next run decides"


def test_the_recency_and_its_source_reach_the_export(config, ledger, library, client, searches) -> None:
    import json

    searches["*"] = []
    _approve(ledger)
    outcome = _go(config, ledger, library, client, VotingPlanner())
    territory = json.loads(ledger.get_run(outcome.run_id).territory_map)
    assert territory["recency"]["source"] == RECENCY_DECIDED
    text = render_map(ledger.get_run(outcome.run_id), territory)
    assert "the brief version's, decided by run" in text


# -- the plan and the map take a given window with one sample ---------------------------


def test_the_plan_with_a_given_window_makes_one_call_and_keeps_it() -> None:
    fake = FakeClient(FakeReply(GOOD_PLAN))  # its own RECENCY line says ANY
    window = Recency(months=18, reason="the brief's", source=RECENCY_OWNER)
    territory = SearchPlanner(fake).plan(BRIEF, recency=window)
    assert len(fake.tasks) == 1 and territory.recency == window
    assert all("publishedAfter" in q.filters.as_dict() or q.waiver for q in territory.queries)


def test_a_context_cannot_move_a_given_window_and_its_vote_is_counted() -> None:
    fake = FakeClient(FakeReply(GOOD_PLAN))
    window = Recency(months=18, reason="the brief's", source=RECENCY_DECIDED)
    territory = SearchPlanner(fake).plan(
        BRIEF, recency=window, context=["[G1] (example.org) a pile that will not heat"]
    )
    assert len(fake.tasks) == 1, "no extra context-free sample: recency is given"
    assert territory.recency.months == 18 and territory.plan.recency_overridden == 1


def test_the_map_with_a_given_window_makes_one_call() -> None:
    reply = "CONCEPT: hot piles\nVOCAB: hot compost\nRECENCY: ANY — stable\nQUERY: hot compost pile\n"
    fake = FakeClient(FakeReply(reply))
    territory = Mapper(fake).map(BRIEF, recency=Recency(months=24, source=RECENCY_DECIDED))
    assert len(fake.tasks) == 1 and territory.recency.months == 24


# -- the owner sets it ------------------------------------------------------------------


@pytest.mark.parametrize("value, months", [
    ("ANY", None), ("any time", None), ("24 months", 24), ("2 years", 24), ("last 18 months", 18),
])
def test_the_owner_sets_it_checked(ledger, value, months) -> None:
    _approve(ledger)
    row, problem = set_owner_recency(ledger, TOPIC, value)
    assert problem == "" and row.months == months and row.source == RECENCY_OWNER
    assert ledger.brief_recency(TOPIC, 1).source == RECENCY_OWNER


@pytest.mark.parametrize("value", ["soon", "0 months", "900 months", "3 weeks", ""])
def test_an_unusable_owner_value_is_refused_and_nothing_stored(ledger, value) -> None:
    _approve(ledger)
    row, problem = set_owner_recency(ledger, TOPIC, value)
    assert row is None and "give ANY, N months or N years" in problem
    assert ledger.brief_recency(TOPIC, 1) is None


def test_the_owner_needs_an_approved_brief(ledger) -> None:
    row, problem = set_owner_recency(ledger, TOPIC, "ANY")
    assert row is None and "no approved brief" in problem


def test_an_owner_set_window_wins_over_a_decided_one(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    _approve(ledger)
    _go(config, ledger, library, client, VotingPlanner(months=24, samples=(24, 24, 24)))
    set_owner_recency(ledger, TOPIC, "ANY")
    planner = VotingPlanner()
    outcome = _go(config, ledger, library, client, planner)
    assert planner.given[0].source == RECENCY_OWNER and outcome.territory.recency.months is None
    assert "[owner-set for this brief version; reused]" in "\n".join(_territory_lines(outcome))


def test_the_cli_sets_and_shows_it(tmp_path, monkeypatch, capsys) -> None:
    from test_search_source import _config

    library = Library(_config(tmp_path, monkeypatch, "").library_path)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _approve(ledger)
    assert main(["brief", TOPIC, "--status"]) == EXIT_OK
    assert "recency:  v1 not decided yet — the next run on it decides it" in capsys.readouterr().out
    assert main(["brief", TOPIC, "--recency", "24 months"]) == EXIT_OK
    assert "recency:  v1 last 24 months — owner-set at " in capsys.readouterr().out
    assert main(["brief", TOPIC, "--status"]) == EXIT_OK
    assert "recency:  v1 last 24 months — owner-set at " in capsys.readouterr().out
    assert main(["brief", TOPIC, "--recency", "soon"]) == EXIT_FAILED
    assert "recency not set: recency line unreadable" in capsys.readouterr().err


# -- deciding without a run ---------------------------------------------------------------


def test_it_can_be_decided_without_a_run(config, ledger, library) -> None:
    from marginalia.flow.brief import run_brief

    _approve(ledger)
    brief = run_brief(config, ledger, FakeJudge(), TOPIC)
    planner = VotingPlanner(months=None, samples=(24, None, 24))
    recency, _ = decide_recency(_plan_config(config), ledger, library, None, brief,
                                mapper=FakeMapper(), search_planner=planner)
    row = ledger.brief_recency(TOPIC, 1)
    assert recency.source == RECENCY_DECIDED and row.months is None and row.run_id is None
    assert row.samples_json == "[24, null, 24]"
