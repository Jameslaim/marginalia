"""Every query source follows the gaps, and an exhausted gap stops being chased.

Subjects are invented. The coverage is the measured shape: one part held whole,
one part with listed things missing.
"""

from __future__ import annotations

from dataclasses import replace

from marginalia.brain.coverage import BriefAnchor
from marginalia.brain.gaps import (
    KIND_GAP,
    KIND_HELD,
    KIND_THREAD,
    KIND_UNAIMED,
    credit_keeper,
    exhausted,
    gate,
    open_gaps,
    place,
    tally_queries,
    window,
)
from marginalia.brain.judge import brief_sha256
from marginalia.cli import EXIT_OK, _gaps_status, _reopen_gaps
from marginalia.config import DEFAULT_GAP_EXHAUSTED_AFTER
from marginalia.contracts import (
    ITEM_MISSING,
    ORIGIN_BRIEF,
    ORIGIN_PLAN,
    ORIGIN_PLANNER,
    ORIGIN_THREAD,
    VERDICT_YES,
    AimedQuery,
    CoverageItem,
    GapTally,
    MetadataVerdict,
    PlannedQuery,
    RoundPlan,
)
from marginalia.core.ledger import GAP_EXHAUSTED, GAP_REOPENED
from marginalia.flow.discover import STOP_GAPS_EXHAUSTED, STOP_REASONS
from test_discover import (  # noqa: F401 - fixtures are found by name
    kept_note,
    FakeJudge,
    FakePlanner,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)

TOPIC = "prompt caching"  # the fixtures' topic; the brief below is its approved text
BRIEF_TEXT = (
    "INTENT\nKeep a small flock of backyard hens.\n\n"
    "REQUIRED OF EVERY VIDEO\n- shows real hens\n\n"
    "PARTS TO COVER\n"
    "- Coop design: ventilation, roosting bars\n"
    "- Hen health: mites, egg binding\n\n"
    "EXCLUDED\n- adverts for coops\n"
)
COVERAGE = [
    CoverageItem("Coop design", True, "", held=("ventilation", "roosting bars")),
    CoverageItem("Hen health", False, "", missing=("mites", "egg binding")),
]
HELD_ONLY = [COVERAGE[0], replace(COVERAGE[1], satisfied=True, missing=(), held=("mites",))]
ANCHOR = BriefAnchor(BRIEF_TEXT)


def _query(text, origin, detail=""):
    return PlannedQuery(text=text, origin=origin, origin_detail=detail)


def _approve(ledger):
    ledger.approve_brief(TOPIC, BRIEF_TEXT, brief_sha256(BRIEF_TEXT))


def _with(config, **discover):
    return replace(config, discover=replace(config.discover, **discover))


# -- placing a query or a keeper ------------------------------------------------


def test_a_gap_is_its_brief_item_however_the_coverage_words_it() -> None:
    reworded = [COVERAGE[0], CoverageItem("Hen health", False, "", missing=("parasites on the skin",))]
    first, second = open_gaps(COVERAGE, ANCHOR), open_gaps(reworded, ANCHOR)
    assert [gap.key for gap in first] == [gap.key for gap in second] == ["item 1"]
    assert first[0].label == "Hen health"


def test_each_source_names_its_aim_and_is_placed_by_one_rule() -> None:
    gaps = open_gaps(COVERAGE, ANCHOR)
    assert place("Hen health — mites", COVERAGE, gaps).kind == KIND_GAP
    # A planner's free-text label naming most of a gap's words is on that gap.
    assert place("mites: how a keeper treats them", COVERAGE, gaps).kind == KIND_GAP
    assert place("Coop design", COVERAGE, gaps).kind == KIND_HELD
    assert place("thread: dust bathing", COVERAGE, gaps).kind == KIND_THREAD
    assert place("", COVERAGE, gaps).kind == KIND_UNAIMED


# -- point 1: every source follows the tiers while a gap is open -----------------


def test_no_source_spends_a_query_on_a_held_part_while_a_gap_is_open() -> None:
    queries = [
        _query("coop air flow", ORIGIN_BRIEF, "aim: Coop design"),
        _query("chicken mites treatment", ORIGIN_BRIEF, "aim: Hen health — mites"),
        _query("best roosting bars", ORIGIN_THREAD, "aim: Coop design — roosting bars"),
        _query("hen egg binding", ORIGIN_PLANNER, "gap — egg binding in a hen"),
        _query("coop build tour", ORIGIN_PLAN, "aim: Coop design — ventilation"),
        _query("something new", ORIGIN_THREAD, "dust bathing"),  # unaimed: kept
    ]
    kept, held, spent, threads = gate(queries, COVERAGE, [], DEFAULT_GAP_EXHAUSTED_AFTER, ANCHOR)

    assert [query.text for query in kept] == ["chicken mites treatment", "hen egg binding", "something new"]
    assert held == {ORIGIN_BRIEF: 1, ORIGIN_THREAD: 1, ORIGIN_PLAN: 1}, "counted per source"
    assert spent == {} and threads == {}


def test_a_thread_on_a_held_keepers_concept_is_held_back_and_one_aimed_at_a_gap_runs() -> None:
    queries = [
        _query("coop paint colours", ORIGIN_THREAD, "aim: thread: coop paint"),
        _query("does coop paint keep mites off", ORIGIN_THREAD, "aim: Hen health — mites"),
        _query("diatomaceous earth for hens", ORIGIN_THREAD, "aim: thread: diatomaceous earth"),
    ]
    kept, held, spent, threads = gate(queries, COVERAGE, [], 6, ANCHOR, held_concepts=["coop paint"])

    assert [query.text for query in kept] == ["does coop paint keep mites off",
                                              "diatomaceous earth for hens"]
    assert threads == {ORIGIN_THREAD: 1}


def test_with_no_gap_open_nothing_is_taken_out() -> None:
    queries = [_query("coop air flow", ORIGIN_BRIEF, "aim: Coop design")]
    kept, held, spent, threads = gate(queries, HELD_ONLY, [], 6, ANCHOR, held_concepts=["coop"])
    assert kept == queries and held == {} and spent == {} and threads == {}


def test_a_source_with_nothing_on_a_gap_runs_fewer_queries_not_held_ones(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    judge = FakeJudge(queries=[[AimedQuery("coop air flow", "Coop design"),
                                AimedQuery("coop tour", "Coop design — ventilation"),
                                AimedQuery("mites on hens", "Hen health — mites")]])

    outcome = run(config, ledger, library, client, judge, max_rounds=1,
                  planner=FakePlanner(baseline=COVERAGE))

    first = outcome.rounds[0]
    assert first.queries == ["mites on hens"]
    assert first.held_while_gaps == {ORIGIN_BRIEF: 2}
    assert searches["calls"] == ["mites on hens"]


class _TaggedJudge(FakeJudge):
    """Stage 1 tags each video with a line; stage 2 keeps it with concepts."""

    def __init__(self, tags, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tags = dict(tags)

    def judge_metadata(self, brief, candidates, *, batch_size, territory=None, coverage=()):
        out = []
        for verdict in super().judge_metadata(brief, candidates, batch_size=batch_size):
            state, line = self._tags.get(verdict.video_id, ("untagged", ""))
            out.append(MetadataVerdict(verdict.video_id, VERDICT_YES, "kept", serves=line,
                                       item=line, item_state=state))
        return out


def test_threads_come_from_keepers_on_a_gap_or_aim_at_one_while_a_gap_is_open(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    searches["mites on hens"] = [result("gapkeeper01"), result("heldkeep001")]
    judge = _TaggedJudge(
        {"gapkeeper01": (ITEM_MISSING, "Hen health — mites")},
        queries=[[AimedQuery("mites on hens", "Hen health — mites")],
                 [AimedQuery("coop paint colours", "thread: coop paint"),
                  AimedQuery("coop paint against mites", "Hen health — mites"),
                  AimedQuery("diatomaceous earth hens", "thread: diatomaceous earth")]],
        openings={"gapkeeper01": (VERDICT_YES, "covers mites", ["diatomaceous earth"]),
                  "heldkeep001": (VERDICT_YES, "covers a coop", ["coop paint"])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2, max_videos=6,
                  planner=FakePlanner(plans=[RoundPlan(round_number=1, coverage=COVERAGE)],
                                      baseline=COVERAGE))

    assert outcome.rounds[0].held_concepts == ["coop paint"]
    assert judge.query_calls[1] == (2, ("diatomaceous earth", "coop paint")), "gap keepers' first"
    second = outcome.rounds[1]
    assert second.queries == ["coop paint against mites", "diatomaceous earth hens"]
    assert second.threads_held_back == {ORIGIN_THREAD: 1}, "the query on the held concept itself"
    assert outcome.gaps[0].keepers == 1


# -- point 2: an exhausted gap stops being chased --------------------------------


def test_a_gap_is_exhausted_after_n_queries_over_two_rounds_with_no_keeper() -> None:
    tallies: list[GapTally] = []
    queries = [_query(f"mites {n}", ORIGIN_BRIEF, "aim: Hen health — mites") for n in range(3)]
    tally_queries(tallies, queries, COVERAGE, 1, ANCHOR)
    assert not exhausted(tallies[0], 6), "one round is never enough"
    tally_queries(tallies, queries, COVERAGE, 2, ANCHOR)
    assert window(tallies[0]) == (2, 6) and exhausted(tallies[0], 6)
    assert not exhausted(tallies[0], 7)


def test_a_keeper_resets_the_window() -> None:
    tallies: list[GapTally] = []
    queries = [_query(f"mites {n}", ORIGIN_BRIEF, "aim: Hen health — mites") for n in range(4)]
    tally_queries(tallies, queries, COVERAGE, 1, ANCHOR)
    tally_queries(tallies, queries, COVERAGE, 2, ANCHOR)
    assert credit_keeper(tallies, "Hen health — egg binding", COVERAGE, 2, ANCHOR) == 1
    tally_queries(tallies, queries, COVERAGE, 3, ANCHOR)
    assert window(tallies[0]) == (1, 4) and not exhausted(tallies[0], 6)


def _exhausting_run(config, ledger, library, client, searches, rounds=3):
    """A gap queried three times a round, finding only untagged keepers."""
    kept_note(library, ledger, TOPIC, "zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    per_round = []
    for number in range(1, rounds + 1):
        texts = [f"mites round {number} take {n}" for n in range(3)]
        for n, text in enumerate(texts):
            searches[text] = [result(f"r{number}t{n}video"[:11].ljust(11, "x"))]
        # Three aims on the one gap: the expansion keeps at most two per aim.
        aims = ("Hen health — mites", "Hen health — egg binding", "Hen health")
        per_round.append([AimedQuery(text, aim) for text, aim in zip(texts, aims)])
    judge = FakeJudge(queries=per_round)
    planner = FakePlanner(plans=[RoundPlan(round_number=n, coverage=COVERAGE) for n in (1, 2)],
                          baseline=COVERAGE)
    return run(_with(config, gap_exhausted_after=6), ledger, library, client, judge,
               max_rounds=rounds, max_videos=20, planner=planner)


def test_when_every_open_gap_is_exhausted_the_run_stops_and_names_it(
    config, ledger, library, client, searches
) -> None:
    _approve(ledger)
    outcome = _exhausting_run(config, ledger, library, client, searches)

    assert outcome.stopped_because == STOP_GAPS_EXHAUSTED
    assert STOP_GAPS_EXHAUSTED in STOP_REASONS
    assert len(outcome.rounds) == 2, "round three is not run"
    assert "Hen health (6 queries)" in outcome.stop_detail
    assert [gap["line"] for gap in outcome.rounds[1].exhausted] == ["Hen health"]
    rows = ledger.exhausted_gaps(TOPIC, 1)
    assert [(row.line, row.queries, row.state) for row in rows] == [("Hen health", 6, GAP_EXHAUSTED)]


def test_the_next_run_gives_an_exhausted_gap_one_probe_and_the_owner_reopens_it(
    config, ledger, library, client, searches, capsys
) -> None:
    _approve(ledger)
    _exhausting_run(config, ledger, library, client, searches)
    searches.clear()
    searches["calls"] = []

    again = _exhausting_run(config, ledger, library, client, searches, rounds=1)
    first = again.rounds[0]
    assert len(first.queries) == 1, "one probe"
    assert first.exhausted_skipped == {ORIGIN_BRIEF: 2}
    assert again.gaps[0].carried

    assert "1 exhausted, one probe query each per run — Hen health (6 queries" in _gaps_status(
        ledger, TOPIC, 1)
    assert _reopen_gaps(library, TOPIC) == EXIT_OK
    assert "1 re-opened — Hen health" in capsys.readouterr().out
    assert ledger.gap_states(TOPIC, 1)[0].state == GAP_REOPENED
    assert ledger.exhausted_gaps(TOPIC, 1) == []


def test_without_an_approved_brief_no_gap_is_recorded(
    config, ledger, library, client, searches
) -> None:
    outcome = _exhausting_run(config, ledger, library, client, searches)
    assert outcome.gaps, "still counted within the run"
    assert ledger.gaps_for_run(outcome.run_id) == []


def test_the_round_line_json_and_export_name_the_exhausted_gap(
    config, ledger, library, client, searches
) -> None:
    from marginalia.cli import _discover_json, _discover_report
    from marginalia.flow.export import render_report

    _approve(ledger)
    outcome = _exhausting_run(config, ledger, library, client, searches)

    text = "\n".join(_discover_report(outcome))
    assert "exhausted: Hen health — 6 quer(ies) over 2 round(s), no keeper" in text
    assert "stopped: gaps_exhausted — Hen health (6 queries)" in text
    body = _discover_json(outcome)
    assert body["rounds"][1]["exhausted"][0]["queries"] == 6
    assert body["gaps"][0]["line"] == "Hen health"
    report = render_report(config, ledger.get_run(outcome.run_id), [], [],
                           gaps=ledger.gaps_for_run(outcome.run_id))
    assert "| Hen health | exhausted | 6 | r1: 3, 0, r2: 3, 0 |" in report
