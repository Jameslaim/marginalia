"""`marg run export` — one run, rendered as a folder.

What is being tested is the rendering and the derivation, not the ledger. Every
row here is written through the ledger's own API, so a schema change breaks
these the same way it breaks everything else rather than silently producing a
report about nothing.

The property worth defending hardest is that this is a **view**. Nothing is
written back to the ledger, and a second export replaces the first. An export
that accumulated would become a second source of truth, and two sources of truth
is one more than this project has.
"""

from __future__ import annotations

import pytest

from marginalia.contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    ORIGIN_BRIEF,
    ORIGIN_PLANNER,
    ORIGIN_TERRITORY,
    PlannedQuery,
    SearchFilters,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
)
from marginalia.core.ledger import STAGE_FULL, STAGE_METADATA, Ledger
from marginalia.core.library import Library
from marginalia.flow.export import export_run

from conftest import make_config

A = "dQw4w9WgXcQ"
B = "rGf9Db4tjUQ"
C = "GXh1KwzNIIM"
RUN = "abc123def456"

BRIEF = "INTENT\nHow connection pooling works.\n\nMUST CONTAIN\n- shows pgbouncer.ini"

TERRITORY = """{
  "topic": "postgres connection pooling",
  "version": "1",
  "concepts": ["transaction pooling", "pool sizing"],
  "vocabulary": ["PgBouncer", "PgCat"],
  "recency": {"months": null, "reason": "the mechanism is stable"},
  "queries": [{"text": "pgbouncer setup", "filters": {}, "reasons": {}, "variant_of": null}],
  "rejected": [
    {"query": "pgbouncer setup", "field": "publishedAfter",
     "value": "2026-01-01T00:00:00Z", "reason": "recency was not reasoned"}
  ],
  "parsed": true,
  "problem": "",
  "revision": 2,
  "revisions": [
    {"action": "add", "kind": "vocabulary", "term": "PgCat",
     "reason": "a kept video compares PgCat", "evidence": ""}
  ],
  "history": [
    {"revision": 1, "concepts": ["transaction pooling", "pool sizing"],
     "vocabulary": ["PgBouncer"]}
  ]
}"""


@pytest.fixture
def library(tmp_path) -> Library:
    made = Library(make_config(tmp_path).library_path)
    made.ensure()
    return made


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A ledger clock that advances a minute per write.

    Ledger timestamps are ISO-8601 to the second, and a whole run written inside
    one test takes microseconds — every row would share a timestamp, and the
    round attribution below would be reading a tie rather than an ordering. A
    real run puts minutes between rounds. This makes the test resemble that
    rather than resemble the speed of the test suite.
    """
    from marginalia.core import ledger as module

    state = {"minute": 0}

    def tick() -> str:
        state["minute"] += 1
        return f"2026-08-25T09:{state['minute']:02d}:00+00:00"

    monkeypatch.setattr(module, "_utcnow", tick)
    return state


@pytest.fixture
def ledger(library):
    with Ledger(library.ledger_path) as opened:
        yield opened


def a_run(ledger: Ledger, *, finished: bool = True) -> None:
    ledger.start_run(
        RUN, "postgres connection pooling", brief=BRIEF, brief_version="1", model="test-model"
    )
    ledger.save_territory(RUN, TERRITORY, "1/v2")
    ledger.log_query(
        RUN, round=1, query="pgbouncer setup", origin=ORIGIN_BRIEF,
        origin_detail="postgres pooling", results_count=10, provider="youtube_api",
        quota_units=101,
    )
    ledger.log_query(
        RUN, round=1, query="pgcat vs pgbouncer", origin=ORIGIN_TERRITORY,
        origin_detail="vocabulary: PgCat",
        filters_json=PlannedQuery(
            "pgcat vs pgbouncer", ORIGIN_TERRITORY,
            filters=SearchFilters(order="date"),
            reasons={"order": "newest first, the tools move"},
        ).filters_json(),
        results_count=4, provider="youtube_api", quota_units=101,
    )
    for video_id, stage, verdict, evidence, reason in (
        (A, STAGE_METADATA, VERDICT_MAYBE, None, "title is ambiguous"),
        (A, STAGE_FULL, VERDICT_YES, EVIDENCE_CLEAR, "walks through pgbouncer.ini at 04:10"),
        (B, STAGE_METADATA, VERDICT_NO, None, "vendor marketing\twith a stray tab"),
        (C, STAGE_FULL, VERDICT_YES, EVIDENCE_DEGRADED, "the captions are unreadable"),
    ):
        ledger.log_decision(
            video_id, "postgres connection pooling", stage=stage, verdict=verdict,
            reason=reason, evidence=evidence, prompt_version="1", run_id=RUN, model="test-model",
        )
    ledger.log_query(
        RUN, round=2, query="pgbouncer pool sizing", origin=ORIGIN_PLANNER,
        origin_detail="gap — sizing", results_count=3, provider="youtube_api",
        quota_units=101,
    )
    if finished:
        ledger.finish_run(
            RUN, rounds=2, candidates_seen=17, videos_stored=2, requests_made=9,
            input_tokens=100_000, output_tokens=5_000, stopped_because="max_rounds",
        )


def read(library: Library, name: str) -> str:
    return (library.export_dir(RUN) / name).read_text(encoding="utf-8")


# -- the folder --------------------------------------------------------------


def test_an_export_writes_the_five_files(config, ledger, library) -> None:
    a_run(ledger)

    exported = export_run(config, ledger, library, RUN)

    assert {path.name for path in exported.files} == {
        "report.md", "brief.md", "territory.md", "queries.tsv", "decisions.tsv"
    }
    assert exported.directory == library.export_dir(RUN)
    assert (exported.rounds, exported.queries, exported.decisions) == (2, 3, 4)


def test_an_unknown_run_is_a_lookup_error_not_an_empty_folder(config, ledger, library) -> None:
    """An export of a run that does not exist must not look like an export of a
    run that did nothing."""
    with pytest.raises(LookupError):
        export_run(config, ledger, library, "ffffffffffff")
    assert not library.exports_dir.exists()


def test_a_run_id_that_is_not_one_never_becomes_a_directory(config, ledger, library) -> None:
    """It arrives from the command line and is about to be a path component."""
    with pytest.raises(ValueError):
        library.export_dir("../../etc")


def test_exporting_twice_replaces_rather_than_accumulates(config, ledger, library) -> None:
    """A view, not a record. The ledger stays the single source of truth, which
    is only true while nothing here is the only copy of anything."""
    a_run(ledger)

    export_run(config, ledger, library, RUN)
    export_run(config, ledger, library, RUN)

    assert len(list(library.export_dir(RUN).iterdir())) == 5


# -- report.md ---------------------------------------------------------------


def test_the_report_says_what_the_run_did(config, ledger, library) -> None:
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    report = read(library, "report.md")

    assert "# postgres connection pooling" in report
    assert "max_rounds" in report
    assert "| candidates seen | 17 |" in report
    assert "| videos stored | 2 |" in report
    assert "| tokens | 100000 in, 5000 out |" in report
    assert "estimated cost" in report


def test_the_report_is_round_by_round(config, ledger, library) -> None:
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    report = read(library, "report.md")

    assert "### Round 1" in report and "### Round 2" in report
    assert "pgbouncer setup" in report
    assert "pgbouncer pool sizing" in report
    # The origin is the column the queries table exists for: "why did it search
    # for that?" is the first question anyone asks.
    assert "`territory`" in report and "`planner`" in report


def test_decisions_are_attributed_to_a_round_and_the_method_is_stated(
    config, ledger, library
) -> None:
    """The `decisions` table has no round column. Reconstructing one from
    timestamps is sound — the loop is sequential — but it is an inference, and a
    report that presents an inference as a recorded fact is one nobody can
    correct."""
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    report = read(library, "report.md")

    assert "attributed to a round by timestamp" in report
    first = report.split("### Round 1")[1].split("### Round 2")[0]
    assert "4 decision(s)" in first


def test_the_per_round_counts_sum_to_the_total(config, ledger, library) -> None:
    """A report whose parts do not sum to its whole is one nobody trusts twice.
    A decision made before the round's first query — a gate rejecting a video
    seen in an earlier run — still has to land somewhere."""
    ledger.start_run(RUN, "topic", brief=BRIEF, brief_version="1", model="m")
    ledger.log_decision(
        A, "topic", stage=STAGE_METADATA, verdict=VERDICT_NO, reason="early", run_id=RUN
    )
    ledger.log_query(
        RUN, round=1, query="a query", origin=ORIGIN_BRIEF,
        results_count=1, provider="p", quota_units=0,
    )
    ledger.finish_run(
        RUN, rounds=1, candidates_seen=1, videos_stored=0, requests_made=1,
        input_tokens=0, output_tokens=0, stopped_because="max_rounds",
    )

    export_run(config, ledger, library, RUN)

    assert "1 decision(s)" in read(library, "report.md")


def test_a_degraded_verdict_is_called_out_in_the_report(config, ledger, library) -> None:
    """The same fact the planner now gets. A degraded keeper is a video nobody
    could read, and it is the single most misleading row in the ledger if you
    are skimming for what a run found."""
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    report = read(library, "report.md")

    assert "rested on degraded evidence" in report
    assert C in report
    assert "satisfies no part of the brief" in report


def test_a_run_that_never_finished_says_so_before_any_number(config, ledger, library) -> None:
    a_run(ledger, finished=False)
    export_run(config, ledger, library, RUN)

    report = read(library, "report.md")

    assert "never — the run did not complete" in report
    assert "every number above is whatever had been reached" in report


# -- territory.md ------------------------------------------------------------


def test_the_territory_file_carries_the_map_the_revisions_and_the_diff(
    config, ledger, library
) -> None:
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    territory = read(library, "territory.md")

    assert "- transaction pooling" in territory
    assert "- PgCat" in territory
    assert "revision 2" in territory
    assert "a kept video compares PgCat" in territory
    assert "### revision 1 → 2" in territory
    assert "+ PgCat" in territory
    # A refused filter widened the search. "Why did this run see more than I
    # asked for?" should have an answer that is not a shrug.
    assert "publishedAfter" in territory and "recency was not reasoned" in territory


def test_a_run_with_no_map_says_so_rather_than_rendering_an_empty_one(
    config, ledger, library
) -> None:
    ledger.start_run(RUN, "topic", brief=BRIEF, brief_version="1", model="m")
    ledger.finish_run(
        RUN, rounds=0, candidates_seen=0, videos_stored=0, requests_made=0,
        input_tokens=0, output_tokens=0, stopped_because="max_rounds",
    )

    export_run(config, ledger, library, RUN)

    assert "No territory map was stored" in read(library, "territory.md")


def test_an_unreadable_map_does_not_fail_the_export(config, ledger, library) -> None:
    ledger.start_run(RUN, "topic", brief=BRIEF, brief_version="1", model="m")
    ledger.save_territory(RUN, "{not json", "1")

    export_run(config, ledger, library, RUN)

    assert "No territory map was stored" in read(library, "territory.md")


# -- the TSVs ----------------------------------------------------------------


def test_queries_tsv_has_a_header_and_one_row_per_query(config, ledger, library) -> None:
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    lines = read(library, "queries.tsv").strip().split("\n")

    assert lines[0].split("\t")[:3] == ["round", "query", "origin"]
    assert len(lines) == 4
    assert "order=date" in lines[2]


def test_decisions_tsv_carries_the_evidence_grade(config, ledger, library) -> None:
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    lines = read(library, "decisions.tsv").strip().split("\n")
    header = lines[0].split("\t")

    assert "evidence" in header
    degraded = [line for line in lines if line.startswith(C)][0]
    assert degraded.split("\t")[header.index("evidence")] == EVIDENCE_DEGRADED


def test_a_tab_inside_a_reason_cannot_shift_a_column(config, ledger, library) -> None:
    """It would still open, with every column after it silently wrong — which
    is the failure mode this project keeps finding."""
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    lines = read(library, "decisions.tsv").strip().split("\n")
    widths = {len(line.split("\t")) for line in lines}

    assert widths == {8}
    assert "vendor marketing with a stray tab" in read(library, "decisions.tsv")


def test_an_empty_cell_says_so(config, ledger, library) -> None:
    """A run of tabs tells you a column exists and nothing about whether anyone
    ever wrote to it. These files are read in a terminal as often as in a
    spreadsheet."""
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    metadata = [
        line for line in read(library, "decisions.tsv").split("\n") if "\tmetadata\t" in line
    ][0]
    assert "\t-\t" in metadata


# -- the brief ---------------------------------------------------------------


def test_the_brief_is_stored_whole(config, ledger, library) -> None:
    """When a library looks wrong the first question is what it thought you
    meant, and that is unanswerable from a topic string."""
    a_run(ledger)
    export_run(config, ledger, library, RUN)

    assert "MUST CONTAIN" in read(library, "brief.md")
    assert "shows pgbouncer.ini" in read(library, "brief.md")


# -- no content, here as everywhere ------------------------------------------


def test_no_transcript_or_note_text_reaches_an_export(config, ledger, library) -> None:
    """The export reads the ledger, and the ledger holds no transcript. Checked
    anyway: this writes five files into the library, and the one place a leak
    would sit unnoticed is a file nobody opens until something goes wrong."""
    a_run(ledger)
    library.write_note(A, "SECRET NOTE TEXT that must never travel")
    library.transcript_path(A).parent.mkdir(parents=True, exist_ok=True)
    library.transcript_path(A).write_text("SECRET TRANSCRIPT TEXT", encoding="utf-8")

    exported = export_run(config, ledger, library, RUN)

    for path in exported.files:
        body = path.read_text(encoding="utf-8")
        assert "SECRET NOTE TEXT" not in body
        assert "SECRET TRANSCRIPT TEXT" not in body


# -- the plan, in full, under the round that produced it ----------------------
#
# Before the `plans` table had a writer, a plan reached the export only as the
# one-line gap label attached to each query it produced. That answers *what* the
# planner asked for and never *why* — and "why did the planner decide that?" is
# exactly the question a stored run is supposed to be able to answer.

PLAN = """{
  "round": 1,
  "recommendation": "continue",
  "reason": "two brief items still have nothing against them",
  "coverage": [
    {"part": "reproduces the too-many-connections error", "satisfied": true,
     "detail": "both kept videos connect many clients directly"},
    {"part": "transaction vs session pooling with SET/LISTEN/NOTIFY pitfalls",
     "satisfied": false,
     "detail": "prepared statements are covered, but no kept video names SET or LISTEN/NOTIFY breakage"}
  ],
  "queries": [
    {"text": "pgbouncer listen notify transaction mode", "origin": "planner",
     "origin_detail": "gap - SET/LISTEN/NOTIFY pitfalls", "filters": {}}
  ],
  "revisions": [
    {"action": "add", "kind": "vocabulary", "term": "PgCat",
     "reason": "a kept video compares it", "evidence": ""}
  ],
  "refused_revisions": [
    {"term": "Odyssey", "action": "remove", "why": "no query naming it actually ran"}
  ],
  "rejected_filters": [
    {"query": "pgbouncer listen notify transaction mode", "field": "videoDuration",
     "value": "medium", "reason": "a planner query is a main query"}
  ],
  "parsed": true,
  "problem": "",
  "input_tokens": 2600,
  "output_tokens": 2500,
  "seconds": 24.1
}"""


def test_the_planner_s_assessment_is_rendered_under_its_round(
    config, ledger, library
) -> None:
    a_run(ledger)
    ledger.save_plan(RUN, round=1, plan_json=PLAN, prompt_version="1")

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "#### The plan after round 1" in report
    assert "**continue** — two brief items still have nothing against them" in report
    # The coverage assessment itself, both halves. A planner shown only its
    # gaps would be a planner that only ever learned to avoid.
    assert "reproduces the too-many-connections error" in report
    assert "both kept videos connect many clients directly" in report
    assert "no kept video names SET or LISTEN/NOTIFY breakage" in report
    assert "Planning prompt v1" in report


def test_a_satisfied_item_and_a_gap_are_told_apart(config, ledger, library) -> None:
    a_run(ledger)
    ledger.save_plan(RUN, round=1, plan_json=PLAN, prompt_version="1")

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    covered = next(line for line in report.splitlines() if "too-many-connections" in line)
    gap = next(line for line in report.splitlines() if "SET or LISTEN" in line)
    assert covered.startswith("| YES |")
    assert gap.startswith("| **NO** |")


def test_everything_the_code_refused_the_planner_is_in_the_report(
    config, ledger, library
) -> None:
    """A refusal nobody can see is a narrowing nobody can correct — the same
    discipline the validator's own rejections already follow."""
    a_run(ledger)
    ledger.save_plan(RUN, round=1, plan_json=PLAN, prompt_version="1")

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "no query naming it actually ran" in report
    assert "a planner query is a main query" in report
    assert "PgCat" in report
    assert "2600 in, 2500 out" in report


def test_a_round_with_no_plan_says_which_kind_of_none_it_is(
    config, ledger, library
) -> None:
    """Two different absences, and conflating them would mislead.

    A run with plans, missing one for a round, means that round genuinely was
    not planned — the last one never is. A run with no plans at all is more
    likely one that predates the table, whose planning was never recorded.
    """
    a_run(ledger)
    ledger.save_plan(RUN, round=1, plan_json=PLAN, prompt_version="1")

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "the last round is never planned" in report


def test_a_run_from_before_the_plans_table_says_its_planning_was_not_recorded(
    config, ledger, library
) -> None:
    a_run(ledger)

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "No plan was stored for this round" in report
    assert "gap label" in report


def test_an_unreadable_plan_does_not_fail_the_export(config, ledger, library) -> None:
    """Same posture as the unreadable territory map. An export is a diagnostic
    tool, and one that refuses to render because a run is malformed is useless
    at the moment it is most needed."""
    a_run(ledger)
    ledger.save_plan(RUN, round=1, plan_json="{not json", prompt_version="1")

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "could not be read" in report
    assert "### Round 2" in report


def test_a_single_round_run_is_not_called_older_than_the_plans_table(
    config, ledger, library
) -> None:
    """Found by the first broad-topic run, which the export libelled.

    `ai agent memory` stopped on `max_videos` inside round one, three hours
    after the `plans` table gained its writer — and the report said its planning
    predated the table. Zero plans has three causes and they are not the same
    claim: the last round is never planned, a ceiling can skip the call, and
    only a genuinely old run has planning that was never recorded.
    """
    ledger.start_run(RUN, "t", brief=BRIEF, brief_version="1", model="m")
    ledger.log_query(RUN, round=1, query="only round", origin=ORIGIN_BRIEF, results_count=1)
    ledger.finish_run(
        RUN, rounds=1, candidates_seen=1, videos_stored=1, requests_made=1,
        input_tokens=1, output_tokens=1, stopped_because="max_videos",
    )

    export_run(config, ledger, library, RUN)
    report = read(library, "report.md")

    assert "the last round is never planned" in report
    assert "predate" not in report and "gap label" not in report
