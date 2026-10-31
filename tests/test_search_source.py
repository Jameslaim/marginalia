"""Which source writes round one's planned queries: the search plan or the map.

The switch and its default, the fallback to the map when a plan cannot be
used, and every reader of the run's territory reading the plan when it ran
(rule 25): round one's queries and their origin, the judges' word list, the
planner and its revisions, the plan line, `--json`, the run row and the export.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from marginalia.brain.planner import apply_revisions, render_territory
from marginalia.cli import _discover_json, _territory_lines
from marginalia.config import ConfigError, load_config
from marginalia.contracts import (
    ORIGIN_PLAN,
    ORIGIN_TERRITORY,
    REVISE_ADD,
    REVISE_VOCABULARY,
    SOURCE_MAP,
    SOURCE_PLAN,
    DiscoverRun,
    PlanItem,
    PlanLanguage,
    PlannedQuery,
    Recency,
    SearchPlan,
    TerritoryMap,
    TerritoryRevision,
)
from marginalia.flow.discover import round_one_source
from marginalia.flow.export import render_map
from test_discover import (  # noqa: F401 - fixtures are found by name
    BRIEF,
    TOPIC,
    FakeJudge,
    FakeMapper,
    FakePlanner,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)


def a_plan(*texts: str, problem: str = "") -> TerritoryMap:
    items = (
        PlanItem(part=1, item="Part one", intent="A video that shows part one.",
                 titled=("my part one",), queries=texts[:1], languages=()),
        PlanItem(part=2, item="Part two: a thing", intent="A video on the thing.",
                 titled=("the thing explained",), queries=texts[1:], languages=("xx",)),
    )
    return TerritoryMap(
        topic=TOPIC,
        version="plan-1",
        vocabulary=["my part one", "the thing explained"],
        recency=Recency(),
        queries=[
            PlannedQuery(text=text, origin=ORIGIN_PLAN, origin_detail=f"aim: {items[0].item}")
            for text in texts
        ],
        problem=problem,
        source=SOURCE_PLAN,
        plan=SearchPlan(
            items=items if texts else (),
            languages=(PlanLanguage("Examplish", "xx", "romanised", "Latin letters"),),
            parts_without_query=() if texts else ("Part one", "Part two"),
        ),
    )


def mapped(*texts: str) -> TerritoryMap:
    return TerritoryMap(
        topic=TOPIC, version="7", concepts=["a concept"], vocabulary=["a term"],
        queries=[PlannedQuery(text=text, origin=ORIGIN_TERRITORY) for text in texts],
    )


class FakeSearchPlanner:
    def __init__(self, territory: TerritoryMap) -> None:
        self.territory = territory
        self.calls = 0
        self.context_seen = "unset"

    def plan(self, brief, *, count=8, max_tokens=None, recency_samples=3, context=None,
             coverage=(), coverage_source="fresh", coverage_note=""):
        self.calls += 1
        self.count_seen = count
        self.context_seen = context
        self.coverage_seen = (list(coverage), coverage_source, coverage_note)
        return self.territory


def _with_source(config, source):
    return replace(config, discover=replace(config.discover, search_source=source))


# -- the switch --------------------------------------------------------------------


def test_the_default_is_the_plan(config) -> None:
    """Rule 39: the plan is the default since the live comparison."""
    from marginalia.config import DEFAULT_SEARCH_SOURCE, DiscoverConfig

    assert DEFAULT_SEARCH_SOURCE == SOURCE_PLAN
    default = DiscoverConfig(max_rounds=1, max_videos=1, max_requests=1, quiet_rounds=1,
                             queries_per_round=1, results_per_query=1, max_cost_usd=1.0,
                             fetch_delay_s=0.0)
    assert default.search_source == SOURCE_PLAN
    mapper, planner = FakeMapper(mapped("map query")), FakeSearchPlanner(a_plan("plan query"))

    territory = round_one_source(config, mapper, planner, BRIEF, source=default.search_source)

    assert (mapper.calls, planner.calls) == (0, 1)
    assert territory.source == SOURCE_PLAN and territory.fallback == ""


def test_the_map_stays_reachable_by_the_setting(config) -> None:
    mapper, planner = FakeMapper(mapped("map query")), FakeSearchPlanner(a_plan("plan query"))

    territory = round_one_source(config, mapper, planner, BRIEF, source=SOURCE_MAP)

    assert (mapper.calls, planner.calls) == (1, 0)
    assert territory.source == SOURCE_MAP and territory.fallback == ""


def test_the_plan_writes_round_one_when_chosen(config) -> None:
    mapper, planner = FakeMapper(mapped("map query")), FakeSearchPlanner(a_plan("plan query"))

    territory = round_one_source(config, mapper, planner, BRIEF, source=SOURCE_PLAN)

    assert (mapper.calls, planner.calls) == (0, 1)
    assert territory.source == SOURCE_PLAN
    assert planner.count_seen == config.discover.territory_queries, "the same quota as the map"
    assert planner.context_seen is None, "step 4's slot, absent today"


def test_a_plan_with_no_query_falls_back_to_the_map(config) -> None:
    mapper = FakeMapper(mapped("map query"))
    planner = FakeSearchPlanner(a_plan(problem="reply hit the token ceiling before finishing"))

    territory = round_one_source(config, mapper, planner, BRIEF, source=SOURCE_PLAN)

    assert (mapper.calls, planner.calls) == (1, 1)
    assert territory.source == SOURCE_MAP
    assert [query.text for query in territory.queries] == ["map query"]
    assert "unusable: reply hit the token ceiling" in territory.fallback
    assert json.loads(territory.as_json())["fallback"] == territory.fallback


def test_a_run_with_the_plan_searches_its_queries_under_their_own_origin(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["brief query"]])
    planner = FakeSearchPlanner(a_plan("plan query one", "plan query two"))
    mapper = FakeMapper(mapped("map query"))

    outcome = run(config, ledger, library, client, judge, mapper=mapper,
                  search_planner=planner, search_source=SOURCE_PLAN, max_rounds=1)

    assert mapper.calls == 0
    rows = ledger.queries_for_run(outcome.run_id)
    assert {(row.query, row.origin) for row in rows} >= {
        ("plan query one", ORIGIN_PLAN), ("plan query two", ORIGIN_PLAN),
    }
    assert "map query" not in {row.query for row in rows}
    plan_rows = [row for row in rows if row.origin == ORIGIN_PLAN]
    assert {row.origin_detail for row in plan_rows} == {"aim: Part one"}
    # The judges' word list is the plan's.
    assert judge.territory_seen[0].source == SOURCE_PLAN
    assert judge.territory_seen[0].terms == ["my part one", "the thing explained"]
    # The run row records the source and its prompt version.
    row = ledger.get_run(outcome.run_id)
    assert row.territory_version == "plan-1"
    stored = json.loads(row.territory_map)
    assert stored["source"] == SOURCE_PLAN
    assert stored["plan"]["items"][1]["item"] == "Part two: a thing"


def test_the_config_switch_reaches_the_run(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakeSearchPlanner(a_plan("plan query"))
    mapper = FakeMapper(mapped("map query"))

    run(_with_source(config, SOURCE_PLAN), ledger, library, client, FakeJudge(),
        mapper=mapper, search_planner=planner, max_rounds=1)

    assert (mapper.calls, planner.calls) == (0, 1)


def test_the_command_line_overrides_the_config(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakeSearchPlanner(a_plan("plan query"))
    mapper = FakeMapper(mapped("map query"))

    run(_with_source(config, SOURCE_PLAN), ledger, library, client, FakeJudge(),
        mapper=mapper, search_planner=planner, search_source=SOURCE_MAP, max_rounds=1)

    assert (mapper.calls, planner.calls) == (1, 0)


def test_a_failed_plan_is_recorded_on_the_run_row(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakeSearchPlanner(a_plan(problem="no items; no queries"))

    outcome = run(config, ledger, library, client, FakeJudge(), mapper=FakeMapper(mapped("map query")),
                  search_planner=planner, search_source=SOURCE_PLAN, max_rounds=1)

    stored = json.loads(ledger.get_run(outcome.run_id).territory_map)
    assert stored["source"] == SOURCE_MAP
    assert stored["fallback"] == "search plan plan-1 unusable: no items; no queries"
    assert ("map query", ORIGIN_TERRITORY) in {
        (row.query, row.origin) for row in ledger.queries_for_run(outcome.run_id)
    }


# -- the planner revises the plan --------------------------------------------------


def test_a_revision_keeps_the_plan_and_its_source() -> None:
    plan = a_plan("plan query")
    revised = apply_revisions(plan, [
        TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "my own first attempt", "kept titles use it"),
    ])

    assert revised.source == SOURCE_PLAN and revised.plan == plan.plan
    assert revised.revision == 2 and "my own first attempt" in revised.vocabulary
    assert revised.queries == plan.queries
    assert json.loads(revised.as_json())["plan"]["items"][0]["item"] == "Part one"


def test_the_planner_is_shown_the_plans_items() -> None:
    shown = render_territory(a_plan("plan query"))
    assert "written as a search plan" in shown
    assert "- Part one: A video that shows part one." in shown
    assert "VOCABULARY: my part one, the thing explained" in shown
    assert "SEARCH PLAN" not in render_territory(mapped("map query"))


def test_the_planner_revises_the_plan_during_a_run(config, ledger, library, client, searches) -> None:
    from test_discover import planned

    searches["*"] = [result("aaaaaaaaaaa")]
    revision = TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "my second try", "kept titles use it")
    planner = FakePlanner([planned("next", revisions=[revision])])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, planner=planner, mapper=FakeMapper(mapped("map query")),
                  search_planner=FakeSearchPlanner(a_plan("plan query")), search_source=SOURCE_PLAN)

    assert outcome.territory.source == SOURCE_PLAN and outcome.territory.revision == 2
    assert "my second try" in outcome.territory.vocabulary
    row = ledger.get_run(outcome.run_id)
    assert row.territory_version == "plan-1/v2"
    stored = json.loads(row.territory_map)
    assert stored["source"] == SOURCE_PLAN and stored["plan"]["items"], "the plan survives a revision"


# -- the plan line, --json and the export -------------------------------------------


def _run_with(territory: TerritoryMap) -> DiscoverRun:
    outcome = DiscoverRun(run_id="r1", topic=TOPIC, brief=BRIEF, model="m")
    outcome.territory = territory
    return outcome


def test_the_plan_line_replaces_the_map_line() -> None:
    lines = _territory_lines(_run_with(a_plan("one", "two", "three")))
    text = "\n".join(lines)

    assert lines[0] == "plan:     2 items, 3 queries (per item 1/2), prompt plan-1"
    assert "map:" not in text
    assert "languages: Examplish (xx, romanised)" in text
    assert "no query: 0 part(s)" in text
    assert "context: absent" in text


def test_the_plan_line_names_parts_with_no_query_and_the_counters() -> None:
    plan = a_plan("one")
    plan = replace(plan, plan=replace(plan.plan, parts_without_query=("Part three",),
                                      unknown_items=("P9",), queries_dropped=2, script_mismatches=1))
    text = "\n".join(_territory_lines(_run_with(plan)))

    assert "no query: 1 part(s) — Part three" in text
    assert "1 item(s) naming no PART of the brief, not planned" in text
    assert "2 quer(ies) under those items, not run" in text
    assert "1 quer(ies) not in the script their language's titles use" in text


def test_a_fallback_is_printed_before_the_map_line() -> None:
    territory = replace(mapped("map query"), fallback="search plan plan-1 unusable: no queries")
    lines = _territory_lines(_run_with(territory))

    assert lines[0] == "plan:     not used — search plan plan-1 unusable: no queries; the map wrote round one"
    assert lines[1].startswith("map:      1 concepts")


def test_json_names_the_source_and_carries_the_plan() -> None:
    out = _discover_json(_run_with(a_plan("one")))

    assert out["search_source"] == SOURCE_PLAN and out["search_fallback"] == ""
    assert out["territory"]["plan"]["languages"][0]["script"] == "romanised"


def test_the_export_renders_the_plan() -> None:
    from marginalia.core.ledger import RunRow

    territory = a_plan("one", "two")
    row = RunRow(
        run_id="r1", topic=TOPIC, brief=BRIEF.text, brief_version="1/1", model="m",
        started_at="t", finished_at="t", rounds=1, candidates_seen=0, videos_stored=0,
        requests_made=0, input_tokens=0, output_tokens=0, stopped_because="done",
        territory_map=territory.as_json(), territory_version="plan-1",
    )
    text = render_map(row, json.loads(row.territory_map))

    assert "## Search plan" in text
    assert "### P2: Part two: a thing" in text
    assert "- titled: the thing explained" in text
    assert "## Queries the plan proposed" in text
    assert "- Examplish (xx, romanised): Latin letters" in text
    assert "- current context: absent" in text


def test_the_export_summary_names_the_source() -> None:
    from marginalia.core.ledger import RunRow
    from marginalia.flow.export import _round_one_source

    def row(territory):
        return RunRow(
            run_id="r1", topic=TOPIC, brief="b", brief_version="1", model="m", started_at="t",
            finished_at="t", rounds=1, candidates_seen=0, videos_stored=0, requests_made=0,
            input_tokens=0, output_tokens=0, stopped_because="done",
            territory_map=territory.as_json(),
        )

    assert _round_one_source(row(a_plan("one"))) == "search plan (plan-1)"
    assert _round_one_source(row(mapped("q"))) == "territory map"
    fallback = replace(mapped("q"), fallback="search plan plan-1 unusable: no queries")
    assert _round_one_source(row(fallback)) == "territory map — search plan plan-1 unusable: no queries"


# -- the setting ------------------------------------------------------------------


def _config(tmp_path: Path, monkeypatch, body: str):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[library]\npath = "{(tmp_path / "lib").as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "m"\n\n' + body,
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(path))
    return load_config()


def test_the_setting_is_read_and_checked(tmp_path, monkeypatch) -> None:
    assert _config(tmp_path, monkeypatch, "").discover.search_source == "plan"
    assert _config(tmp_path, monkeypatch, '[discover]\nsearch_source = "Map"\n').discover.search_source == "map"
    with pytest.raises(ConfigError, match="search_source must be one of plan, map"):
        _config(tmp_path, monkeypatch, '[discover]\nsearch_source = "both"\n')


def test_the_flag_reaches_discover(tmp_path, monkeypatch) -> None:
    from marginalia import cli

    _config(tmp_path, monkeypatch, "")
    seen = {}

    def fake_discover(*args, **kwargs):
        seen.update(kwargs)
        return _run_with(a_plan("one"))

    monkeypatch.setattr(cli, "discover", fake_discover)
    assert cli.main(["discover", "a topic", "--search-source", "plan", "--json"]) == cli.EXIT_OK
    assert seen["search_source"] == "plan"
    assert cli.main(["discover", "a topic", "--json"]) == cli.EXIT_OK
    assert seen["search_source"] is None, "the config decides"


# -- the baseline runs first, and the plan is made with its coverage -------------

from marginalia.contracts import CoverageItem, LibraryCoverage  # noqa: E402
from marginalia.flow.discover import plan_coverage  # noqa: E402


class OrderedPlanner(FakePlanner):
    def __init__(self, log, **kwargs) -> None:
        super().__init__(**kwargs)
        self.log = log

    def assess_library(self, brief, notes, **kwargs):
        self.log.append("baseline")
        return super().assess_library(brief, notes, **kwargs)


class OrderedSearchPlanner(FakeSearchPlanner):
    def __init__(self, log, territory) -> None:
        super().__init__(territory)
        self.log = log

    def plan(self, brief, **kwargs):
        self.log.append("plan")
        return super().plan(brief, **kwargs)


class OrderedMapper(FakeMapper):
    def __init__(self, log, territory) -> None:
        super().__init__(territory)
        self.log = log

    def map(self, brief, **kwargs):
        self.log.append("map")
        return super().map(brief, **kwargs)


HELD = [CoverageItem("Part one", True, held=("the part",)),
        CoverageItem("Part two", False, missing=("a thing",))]


def test_the_baseline_runs_before_the_plan_and_the_plan_sees_it(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# An old note\n\nCovers part one.\n")
    log: list[str] = []
    planner = OrderedSearchPlanner(log, a_plan("plan query"))

    run(config, ledger, library, client, FakeJudge(), max_rounds=1,
        planner=OrderedPlanner(log, baseline=HELD), search_planner=planner,
        search_source=SOURCE_PLAN)

    assert log == ["baseline", "plan"]
    coverage, source, note = planner.coverage_seen
    assert [item.part for item in coverage] == ["Part one", "Part two"]
    assert source == "baseline" and "1 of 2 item(s) held, from 1 note(s)" in note


def test_the_map_gets_the_same_ordering_and_no_coverage(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# An old note\n\nCovers part one.\n")
    log: list[str] = []

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1,
                  planner=OrderedPlanner(log, baseline=HELD),
                  mapper=OrderedMapper(log, mapped("map query")))

    assert log == ["baseline", "map"]
    assert [q.query for q in ledger.queries_for_run(outcome.run_id)
            if q.origin == ORIGIN_TERRITORY] == ["map query"]


def test_a_fresh_topic_tells_the_plan_nothing_is_held(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakeSearchPlanner(a_plan("plan query"))

    run(config, ledger, library, client, FakeJudge(), max_rounds=1,
        search_planner=planner, search_source=SOURCE_PLAN)

    assert planner.coverage_seen == ([], "fresh", "nothing is held for this topic")


def test_a_failed_baseline_plans_as_a_fresh_topic_and_says_so(
    config, ledger, library, client, searches
) -> None:
    class Failing(FakePlanner):
        def assess_library(self, brief, notes, **kwargs):
            return LibraryCoverage(notes_read=1, parsed=False, problem="reply hit the token ceiling")

    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# An old note\n\nCovers part one.\n")
    planner = FakeSearchPlanner(a_plan("plan query"))

    run(config, ledger, library, client, FakeJudge(), max_rounds=1, planner=Failing(),
        search_planner=planner, search_source=SOURCE_PLAN)

    coverage, source, note = planner.coverage_seen
    assert (coverage, source) == ([], "unusable")
    assert "failed (reply hit the token ceiling); planned as a fresh topic" in note


def test_a_degraded_baseline_plans_as_a_fresh_topic() -> None:
    degraded = LibraryCoverage(items=HELD, notes_read=600, evidence_mode="claims cut", notes_cut=600)
    coverage, source, note = plan_coverage(degraded)
    assert (coverage, source) == ([], "unusable")
    assert "degraded (claims cut, 600 notes cut); planned as a fresh topic" in note


def test_a_usable_baseline_is_the_plans_coverage() -> None:
    coverage, source, _ = plan_coverage(LibraryCoverage(items=HELD, notes_read=3, evidence_mode="full"))
    assert (coverage, source) == (HELD, "baseline")
    assert plan_coverage(None)[1] == plan_coverage(LibraryCoverage())[1] == "fresh"


def test_the_plan_line_shows_coverage_and_queries_by_state() -> None:
    plan = a_plan("one")
    plan = replace(plan, plan=replace(
        plan.plan, coverage="baseline", coverage_note="the library baseline: 1 of 2 item(s) held",
        queries_by_state={"missing": 1, "partly": 0, "untagged": 0, "held": 2},
        unserved_gaps=("Part two — a thing",), held_while_gaps=2, over_budget=1,
    ))
    text = "\n".join(_territory_lines(_run_with(plan)))

    assert "coverage: baseline — the library baseline: 1 of 2 item(s) held" in text
    assert "by state: missing 1, partly 0, untagged 0, held 2" in text
    assert "gaps with no query: 1 — Part two — a thing" in text
    assert "2 quer(ies) on held items while a gap had none" in text
    assert "1 quer(ies) over the budget, not run, held first" in text


def test_the_plan_line_shows_queries_by_language() -> None:
    plan = a_plan("one", "two")
    plan = replace(plan, plan=replace(
        plan.plan, main_language="en", language_items=(4, 5), broad_item=1,
        queries_by_language={"en": 1, "xx": 1}, language_misaimed=1,
    ))
    text = "\n".join(_territory_lines(_run_with(plan)))

    assert "by language: en 1, xx 1 (main en; other languages aim at P4, P5)" in text
    assert "1 quer(ies) in another language aimed at a narrow part" in text


def test_the_plan_line_shows_tiers_exclusions_and_duplicates() -> None:
    plan = a_plan("one", "two")
    plan = replace(plan, plan=replace(
        plan.plan,
        queries_by_tier={"missing part": 1, "partly part": 1, "also wanted": 0, "untagged": 0, "held": 0},
        distinct_gaps=3, duplicates_avoided=1,
        excluded_gaps=("Cost side — page acquisition | pitches for buying pages",),
    ))
    text = "\n".join(_territory_lines(_run_with(plan)))

    assert ("by tier: missing part 1, partly part 1, also wanted 0, untagged 0, held 0 "
            "(3 distinct gap(s), 1 duplicate(s) avoided, 0 second gap quer(ies))") in text
    assert "skipped as excluded: 1" in text
    assert "Cost side — page acquisition — pitches for buying pages" in text
