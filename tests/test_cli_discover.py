"""The `marg discover` report, and `marg llm-check`.

The report is a contract: a skill reads it, and the brief line is the first
thing anyone looks at when a library comes out wrong. `llm-check` is the first
thing anyone runs after swapping providers, so it has to cost exactly one
request and say plainly whether parsing worked.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from marginalia.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from marginalia.contracts import (
    ORIGIN_BRIEF,
    ORIGIN_PLANNER,
    ORIGIN_TERRITORY,
    RECOMMEND_STOP,
    REVISE_ADD,
    REVISE_VOCABULARY,
    CoverageItem,
    RoundPlan,
    TerritoryRevision,
    Brief,
    DiscoverRun,
    FilterRejection,
    PlannedQuery,
    Recency,
    RoundReport,
    SearchFilters,
    TerritoryMap,
)
from marginalia.llm import LlmConfigError

BRIEF = Brief(
    topic="prompt caching",
    text=(
        "INTENT\nUnderstand how prompt caching cuts cost in practice.\n\n"
        "REQUIRED OF EVERY VIDEO\n- x\n\nPARTS TO COVER\n- x"
    ),
    version="1/1",
)


def fake_run(**overrides) -> DiscoverRun:
    run = DiscoverRun(
        run_id="abc123def456",
        topic="prompt caching",
        brief=BRIEF,
        model="test-model",
        stopped_because="no_new_videos",
        requests=24,
        input_tokens=8000,
        output_tokens=900,
        stored_ids=["aaaaaaaaaaa", "bbbbbbbbbbb"],
    )
    run.rounds = [
        RoundReport(
            number=1, queries=["a"] * 5, candidates=63, known=8,
            verdict_no=41, verdict_maybe=14, verdict_yes=8, stored=9,
        ),
        RoundReport(
            number=2, queries=["b"] * 4, candidates=22, known=19,
            verdict_no=2, verdict_maybe=1, verdict_yes=0, stored=1, from_threads=True,
        ),
    ]
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


@pytest.fixture()
def stubbed(configured: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_discover(config, ledger, library, client, topic, **kwargs):
        captured.update(kwargs)
        captured["topic"] = topic
        # A test may set `stubbed["run"]` beforehand to choose what the report
        # is rendering. Set after the kwargs, so it survives them.
        return captured.get("run") or fake_run()

    monkeypatch.setattr("marginalia.cli.discover", fake_discover)
    return captured


# -- the report -------------------------------------------------------------


def test_the_report_leads_with_the_brief(stubbed, capsys) -> None:
    """The first question when results look wrong is what it thought the topic
    meant. That has to be on screen without opening the database."""
    assert main(["discover", "prompt caching"]) == EXIT_OK

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "brief:    Understand how prompt caching cuts cost in practice."


def test_the_report_has_one_line_per_round_then_a_summary(stubbed, capsys) -> None:
    main(["discover", "prompt caching"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert len(lines) == 6
    assert lines[1].startswith("round 1:  5 queries, 63 candidates, 8 known, 41 no, 14 maybe")
    assert lines[1].endswith("9 stored")
    assert lines[4] == "judge-full: 10 judged, 10 parsed, 0 unparsed"
    # Printed at zero for the same reason "0 unparsed" is: the rate of unmarked
    # era judgement is only readable if the number is always there.
    assert lines[5] == (
        "            0 dated an API without the outside-knowledge marker"
    )
    assert lines[3] == (
        "run:      2 stored, 66 judged, 2 rounds, 24 requests, stopped: no_new_videos"
    )


def test_a_thread_driven_round_says_so(stubbed, capsys) -> None:
    """Round two coming from the loop's own findings is the interesting part."""
    main(["discover", "prompt caching"])
    assert "queries from threads" in capsys.readouterr().out.splitlines()[2]


def test_a_dry_run_says_would_store(configured, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.cli.discover",
        lambda *a, **k: fake_run(dry_run=True, stopped_because="dry_run", stored_ids=[]),
    )
    main(["discover", "prompt caching", "--dry-run"])

    summary = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("run:")
    )
    assert "would store" in summary
    assert "stopped: dry_run" in summary


def test_flags_reach_the_loop(stubbed, capsys) -> None:
    main([
        "discover", "prompt caching",
        "--max-rounds", "2", "--max-videos", "5", "--max-requests", "30", "--dry-run",
    ])

    assert stubbed["max_rounds"] == 2
    assert stubbed["max_videos"] == 5
    assert stubbed["max_requests"] == 30
    assert stubbed["dry_run"] is True


def test_unset_flags_defer_to_config(stubbed, capsys) -> None:
    main(["discover", "prompt caching"])
    assert stubbed["max_rounds"] is None
    assert stubbed["max_videos"] is None


def test_no_transcript_text_can_reach_the_report(stubbed, capsys) -> None:
    """The report is built from counts and the brief. There is no field on
    DiscoverRun that carries caption text, and that is the point."""
    main(["discover", "prompt caching"])
    out = capsys.readouterr().out
    assert "transcript" not in out.lower()


# -- structured output ------------------------------------------------------


def test_json_is_valid_and_carries_the_run_id(stubbed, capsys) -> None:
    main(["discover", "prompt caching", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["run_id"] == "abc123def456"
    assert payload["model"] == "test-model"
    assert payload["brief_version"] == "1/1"
    assert payload["stopped_because"] == "no_new_videos"
    assert payload["requests"] == 24
    assert len(payload["rounds"]) == 2
    assert payload["rounds"][1]["from_threads"] is True


def test_json_carries_the_whole_brief_not_just_the_intent(stubbed, capsys) -> None:
    main(["discover", "prompt caching", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["brief"] == BRIEF.text
    assert payload["brief_intent"] in BRIEF.text


# -- llm-check --------------------------------------------------------------


def test_llm_check_reports_model_latency_and_parsing(configured, monkeypatch, capsys) -> None:
    from marginalia.llm import Reply, Usage

    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: Reply("ready", "test-model", Usage(1, 12, 2), 0.4),
    )
    assert main(["llm-check"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "test-model" in out
    assert "parse=ok" in out
    assert "in=12 out=2" in out


def test_llm_check_costs_exactly_one_request(configured, monkeypatch) -> None:
    from marginalia.llm import Reply, Usage

    calls: list[int] = []

    def once(self, system, user, **kwargs):
        calls.append(1)
        return Reply("ready", "test-model", Usage(1, 1, 1), 0.1)

    monkeypatch.setattr("marginalia.llm.LlmClient.complete", once)
    main(["llm-check"])
    assert len(calls) == 1


def test_llm_check_fails_loudly_when_parsing_breaks(configured, monkeypatch, capsys) -> None:
    """A provider that answers but not usably is the failure this catches —
    the one that would otherwise show up as an empty library."""
    from marginalia.llm import Reply, Usage

    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: Reply("I cannot comply.", "test-model", Usage(1, 1, 1), 0.1),
    )
    assert main(["llm-check"]) == EXIT_FAILED
    assert "parse=FAILED" in capsys.readouterr().out


def test_llm_check_reports_a_config_error_as_usage(configured, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: (_ for _ in ()).throw(LlmConfigError("bad key")),
    )
    assert main(["llm-check"]) == EXIT_USAGE
    assert "bad key" in capsys.readouterr().err


# -- the report cannot hide what a round actually did -----------------------


def test_fetch_failures_are_surfaced(configured, monkeypatch, capsys) -> None:
    """Observed in a real run: 13 of 13 fetches failed to YouTube throttling
    and the round line was indistinguishable from a round where the judge
    rejected everything. A degradation nobody sees is one nobody fixes."""
    run = fake_run()
    run.rounds[0].failed = 13
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: run)

    main(["discover", "prompt caching"])

    assert "(13 fetch failed)" in capsys.readouterr().out


def test_gate_2_drops_are_surfaced(configured, monkeypatch, capsys) -> None:
    run = fake_run()
    run.rounds[0].rejected_before = 3
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: run)

    main(["discover", "prompt caching"])

    assert "3 previously rejected" in capsys.readouterr().out


def test_guessed_nos_are_surfaced_in_the_report_and_the_json(configured, monkeypatch, capsys) -> None:
    """Rule 14: a stage-1 NO kept as MAYBE because its reason was a guess is
    its own outcome. Without its own line it is indistinguishable from a MAYBE
    the judge chose."""
    run = fake_run()
    run.rounds[0].guessed_no = 2
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: run)

    main(["discover", "prompt caching"])
    assert "2 stage-1 NO on a guess, kept as MAYBE" in capsys.readouterr().out

    main(["discover", "prompt caching", "--json"])
    assert json.loads(capsys.readouterr().out)["rounds"][0]["guessed_no"] == 2


def test_a_clean_round_stays_terse(configured, monkeypatch, capsys) -> None:
    """The extra clauses appear only when they have something to report."""
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: fake_run())
    main(["discover", "prompt caching"])

    line = capsys.readouterr().out.splitlines()[1]
    assert "fetch failed" not in line
    assert "previously rejected" not in line


# -- B4: the map, the recency call, and the rejections in the report --------


def _mapped_run(**overrides) -> DiscoverRun:
    run = fake_run(**overrides)
    run.territory = TerritoryMap(
        topic="prompt caching",
        version="1",
        concepts=["prefix matching", "cache expiry"],
        vocabulary=["cache_control", "KV cache"],
        recency=Recency(months=6, reason="provider pricing moved three times this year"),
        queries=[PlannedQuery(text="kv cache reuse", origin=ORIGIN_TERRITORY)],
    )
    return run


def test_the_report_names_the_recency_call_and_its_reason(stubbed, capsys) -> None:
    """The one judgement in this phase that silently *excludes*. A twelve-month
    cutoff removes older material with no trace anywhere a reader would look, so
    the reason is printed rather than only stored."""
    stubbed["run"] = _mapped_run()

    assert main(["discover", "prompt caching"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "recency:  last 6 months" in out
    assert "provider pricing moved three times this year" in out


def test_the_report_sizes_the_map(stubbed, capsys) -> None:
    stubbed["run"] = _mapped_run()

    main(["discover", "prompt caching"])

    assert "2 concepts, 2 vocabulary terms, 1 queries" in capsys.readouterr().out


def test_rejected_filters_are_reported_even_at_zero(stubbed, capsys) -> None:
    """Same reason the unparsed count prints at zero: it is what makes the
    number trustworthy on the day it is not zero."""
    stubbed["run"] = _mapped_run()

    main(["discover", "prompt caching"])

    assert "filters:  0 rejected" in capsys.readouterr().out


def test_a_rejected_filter_is_named_with_its_reason(stubbed, capsys) -> None:
    run = _mapped_run()
    run.rejected_filters = [
        FilterRejection("kv cache reuse", "publishedAfter", "2027-13-45", "not a readable date")
    ]
    stubbed["run"] = run

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "filters:  1 rejected" in out
    assert "publishedAfter=2027-13-45" in out
    assert "not a readable date" in out


def test_the_report_says_what_only_the_map_surfaced(stubbed, capsys) -> None:
    run = _mapped_run()
    run.rounds[0].surfaced_by = {
        "aaaaaaaaaaa": {ORIGIN_BRIEF, ORIGIN_TERRITORY},
        "bbbbbbbbbbb": {ORIGIN_TERRITORY},
    }
    stubbed["run"] = run

    main(["discover", "prompt caching"])

    assert "map-only: 1 candidate(s)" in capsys.readouterr().out


def test_a_run_with_no_map_prints_no_map_lines(stubbed, capsys) -> None:
    """A mapping call that failed leaves discovery narrower, not broken, and the
    report must not grow a row of zeroes describing something that did not run."""
    stubbed["run"] = fake_run()

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "recency:" not in out
    assert "brief:" in out


def test_the_json_carries_the_whole_map(stubbed, capsys) -> None:
    stubbed["run"] = _mapped_run()

    main(["discover", "prompt caching", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["territory"]["vocabulary"] == ["cache_control", "KV cache"]
    assert payload["territory"]["recency"]["months"] == 6
    # `1/v1` since B5: the prompt that wrote the map, and how many times the
    # planner revised it. An unrevised map still says so rather than staying
    # silent about a number that now exists.
    assert payload["territory_version"] == "1/v1"
    assert payload["territory_revision"] == 1
    assert "seconds" in payload["territory_call"]


def test_the_json_keeps_the_plain_query_list(stubbed, capsys) -> None:
    """`queries` stays a list of strings. Every earlier baseline carries that
    shape, and a comparison against one should not need a new reader."""
    stubbed["run"] = _mapped_run()

    main(["discover", "prompt caching", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["rounds"][0]["queries"] == ["a"] * 5
    assert payload["rounds"][0]["planned"] == []


def test_the_json_carries_each_query_with_its_origin(stubbed, capsys) -> None:
    run = _mapped_run()
    run.rounds[0].planned = [
        PlannedQuery(
            text="kv cache reuse",
            origin=ORIGIN_TERRITORY,
            origin_detail="vocabulary: KV cache",
            filters=SearchFilters(order="date"),
        )
    ]
    stubbed["run"] = run

    main(["discover", "prompt caching", "--json"])

    planned = json.loads(capsys.readouterr().out)["rounds"][0]["planned"][0]
    assert planned["origin"] == ORIGIN_TERRITORY
    assert planned["origin_detail"] == "vocabulary: KV cache"
    assert planned["filters"] == {"order": "date"}


# -- B5: the plan in the report ---------------------------------------------


def _with_plan(**kwargs) -> DiscoverRun:
    run = fake_run()
    run.rounds[0].plan = RoundPlan(
        round_number=1,
        coverage=[
            CoverageItem("shows API configuration", True, "two kept videos walk through it"),
            CoverageItem("cache expiry and TTL", False, "nothing kept mentions expiry"),
            CoverageItem("provider comparison", False, "every kept video covers one"),
        ],
        queries=[
            PlannedQuery("prompt cache ttl expiry", ORIGIN_PLANNER, "gap - cache expiry")
        ],
        reason="two PARTS TO COVER still have nothing against them",
        **kwargs,
    )
    return run


def test_the_report_names_the_gap_and_the_recommendation(stubbed, capsys) -> None:
    """The interesting thing is the change: a reader should see the gap that
    produced round two's queries on the line above them."""
    stubbed["run"] = _with_plan()

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "plan:   1 of 3 brief items covered; gap: cache expiry and TTL (+1 more)" in out
    assert "→ continue: two PARTS TO COVER" in out
    assert "next:   prompt cache ttl expiry" in out


def test_the_report_ends_with_the_coverage_assessment(stubbed, capsys) -> None:
    """"Which part of the brief did this run fail to satisfy?" is the question a
    library is judged on, and it should not need the database to answer."""
    stubbed["run"] = _with_plan()

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "coverage: assessed at the end of round 1" in out
    assert "yes  shows API configuration" in out
    assert "NO   cache expiry and TTL — nothing kept mentions expiry" in out


def test_a_territory_revision_is_printed_with_its_reason(stubbed, capsys) -> None:
    stubbed["run"] = _with_plan(
        revisions=[TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "TTL", "kept videos use it")]
    )

    main(["discover", "prompt caching"])

    assert "map:    + vocabulary: TTL — kept videos use it" in capsys.readouterr().out


def test_a_refused_revision_is_never_silent(stubbed, capsys) -> None:
    """Same discipline as a rejected filter: a proposal the code declined and
    nobody counted is indistinguishable from one that was never made."""
    revision = TerritoryRevision("remove", REVISE_VOCABULARY, "semantic cache", "feels off")
    stubbed["run"] = _with_plan()
    stubbed["run"].rounds[0].plan = replace(
        stubbed["run"].rounds[0].plan,
        refused_revisions=[(revision, "a removal must name the queries")],
    )

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "refused - vocabulary: semantic cache" in out
    assert "must name the queries" in out


def test_a_run_with_no_plan_prints_no_plan_lines(stubbed, capsys) -> None:
    """A dry run, a single-round run, and a run the budget stopped first all
    made no plan — which is different from making an empty one."""
    stubbed["run"] = fake_run()

    main(["discover", "prompt caching"])

    out = capsys.readouterr().out
    assert "plan:" not in out
    assert "coverage:" not in out


def test_the_json_carries_the_whole_plan(stubbed, capsys) -> None:
    stubbed["run"] = _with_plan(recommendation=RECOMMEND_STOP)

    main(["discover", "prompt caching", "--json"])

    plan = json.loads(capsys.readouterr().out)["rounds"][0]["plan"]
    assert plan["recommendation"] == RECOMMEND_STOP
    assert len(plan["coverage"]) == 3
    assert plan["queries"][0]["origin"] == ORIGIN_PLANNER
    assert plan["coverage"][1] == {
        "part": "cache expiry and TTL",
        "satisfied": False,
        "detail": "nothing kept mentions expiry",
    }


def test_the_json_says_no_plan_rather_than_an_empty_one(stubbed, capsys) -> None:
    stubbed["run"] = fake_run()

    main(["discover", "prompt caching", "--json"])

    assert json.loads(capsys.readouterr().out)["rounds"][0]["plan"] is None


def test_the_run_summary_does_not_call_a_duplicate_query_a_rejected_filter() -> None:
    """The sibling of the round-line fix, and it survived one commit longer.

    A run whose only refusal was a duplicate query announced "1 filter rejected
    before executing" — the opposite of what happened. No filter was refused;
    one search did not run.
    """
    from marginalia.cli import _territory_lines
    from marginalia.contracts import (
        DiscoverRun,
        FILTER_FIELD_QUERY,
        FilterRejection,
        TerritoryMap,
    )

    run = DiscoverRun(
        run_id="r", topic="t", brief=BRIEF, model="m",
        territory=TerritoryMap(topic="t", version="1"),
        rejected_filters=[
            FilterRejection("a query", FILTER_FIELD_QUERY, "a query",
                            "identical query and filters already run this round"),
        ],
    )

    lines = "\n".join(_territory_lines(run))

    assert "filters:  0 rejected before executing" in lines
    assert "1 quer(ies) skipped as already run" in lines


def test_a_genuinely_refused_filter_is_still_named_in_the_run_summary() -> None:
    from marginalia.cli import _territory_lines
    from marginalia.contracts import (
        DiscoverRun,
        FILTER_PUBLISHED_AFTER,
        FilterRejection,
        TerritoryMap,
    )

    run = DiscoverRun(
        run_id="r", topic="t", brief=BRIEF, model="m",
        territory=TerritoryMap(topic="t", version="1"),
        rejected_filters=[
            FilterRejection("a query", FILTER_PUBLISHED_AFTER, "2027-01-01",
                            "recency was not reasoned"),
        ],
    )

    lines = "\n".join(_territory_lines(run))

    assert "filters:  1 rejected before executing" in lines
    assert "would have excluded" in lines
    assert "skipped as already run" not in lines


# -- how the brief was made ----------------------------------------------------


def test_the_report_says_when_a_draft_was_drawn_again_or_the_review_was_unusable(
    stubbed, capsys
) -> None:
    from dataclasses import replace

    stubbed["run"] = fake_run(brief=replace(
        BRIEF,
        draft_attempts=2,
        draft_problems=("cut off at the token ceiling (32768 output tokens, 0 characters)",),
        review_problem="no EXCLUDED section",
    ))

    assert main(["discover", "home plumbing"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "draft 1 unusable (cut off at the token ceiling" in out
    assert "drawn again" in out
    assert "review unusable (no EXCLUDED section); the unreviewed draft is the brief" in out


def test_an_ordinary_brief_adds_no_line_to_the_report(stubbed, capsys) -> None:
    assert main(["discover", "home plumbing"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "unusable" not in out


def test_the_json_carries_how_the_brief_was_made(stubbed, capsys) -> None:
    from dataclasses import replace

    stubbed["run"] = fake_run(brief=replace(BRIEF, draft_attempts=2, draft_problems=("empty",)))

    assert main(["discover", "home plumbing", "--json"]) == EXIT_OK

    body = json.loads(capsys.readouterr().out)
    assert body["brief_outcome"] == {
        "draft_attempts": 2, "draft_problems": ["empty"], "review_problem": "",
        "final_is": "review",
    }


def test_no_usable_draft_ends_the_command_with_one_sentence(
    configured, monkeypatch, capsys
) -> None:
    from marginalia.brain.judge import BriefUnusable

    def no_draft(*args, **kwargs):
        raise BriefUnusable(["cut off at the token ceiling", "cut off at the token ceiling"])

    monkeypatch.setattr("marginalia.cli.discover", no_draft)

    assert main(["discover", "home plumbing"]) != EXIT_OK

    err = capsys.readouterr().err
    assert "no usable brief draft after 2 attempt(s)" in err
    assert "Nothing was searched or recorded" in err
    assert "Traceback" not in err and "BriefUnusable:" not in err


def test_the_report_says_what_was_left_unread_and_the_round_s_share(stubbed, capsys) -> None:
    run = fake_run()
    run.rounds[0].allowance = 9
    run.rounds[0].unread_yes = 2
    run.rounds[0].unread_maybe = 5
    run.rounds[0].unshown_no = 3
    stubbed["run"] = run

    assert main(["discover", "home plumbing"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "→ 9 stored of 9 allowed" in out
    assert "2 stage-1 YES not yet read, carried forward" in out
    assert "5 stage-1 MAYBE not yet read, carried forward" in out
    assert "3 stage-1 NO on missing evidence, kept as MAYBE" in out
