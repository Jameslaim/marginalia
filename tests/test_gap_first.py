"""Stage 1 names the coverage item each candidate would serve, and the fetch
budget is spent on what the coverage is missing first.

Measured on run `71cc2c8f5c57`, a library assessed as 10 parts held and 3
partly held: of 25 on-budget keepers, 16 to 22 were on parts already held, and
166 stage-1 MAYBEs were never read. YouTube ranks general explainers first even
for a narrow gap query, stage 1 says YES most readily to familiar subjects, and
the fetch order took YES first — so each round's share filled with repeats.

The subjects below are invented and general; none is a test topic.
"""

from __future__ import annotations

from dataclasses import replace

from conftest import make_llm_config
from marginalia.brain.judge import (
    Judge,
    item_block,
    match_item,
    parse_batch,
    parse_batch_items,
    split_item,
)
from marginalia.contracts import (
    ITEM_HELD,
    ITEM_MISSING,
    ITEM_PARTLY,
    ITEM_UNTAGGED,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    Brief,
    CoverageItem,
    SearchResult,
)
from marginalia.llm import LlmClient

BRIEF = Brief(
    topic="t",
    text="INTENT\nx\nREQUIRED OF EVERY VIDEO\n- x\nPARTS TO COVER\n- x\nEXCLUDED\n- y",
    version="1/1",
)

# The measured shape: most parts held whole, a few held in part with named
# missing things.
COVERAGE = [
    CoverageItem("Dough fermentation", True, "three notes", held=("yeast", "proofing time")),
    CoverageItem("Oven temperature", True, "two notes", held=("preheating",)),
    CoverageItem(
        "Shaping and scoring", False, "", partly=True,
        held=("folding the dough",), missing=("scoring patterns", "tension when shaping"),
    ),
    CoverageItem(
        "Common failures", False, "", partly=True,
        held=("dense crumb",), missing=("gummy interior", "burnt base"),
    ),
]


class _Reply:
    def __init__(self, text: str) -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]
        self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        self.model = "m"


def _judge(text: str, sent: list | None = None) -> Judge:
    def transport(**kwargs):
        if sent is not None:
            sent.append(kwargs)
        return _Reply(text)

    return Judge(LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None))


def _candidates(n: int) -> list[SearchResult]:
    return [
        SearchResult(f"vid{index:08d}", f"title {index}", "chan", "desc", 600, None, None)
        for index in range(n)
    ]


# -- the tag is read apart from the verdict -----------------------------------


def test_the_tag_is_cut_from_the_reason_so_the_stored_reason_is_unchanged() -> None:
    plain = "1: YES - explains how a starter rises\n2: MAYBE - title is ambiguous"
    tagged = (
        "1: YES - explains how a starter rises | ITEM: Dough fermentation\n"
        "2: MAYBE - title is ambiguous | ITEM: NONE"
    )

    assert parse_batch(tagged, 2) == parse_batch(plain, 2)
    assert [tag for *_, tag in parse_batch_items(tagged, 2)] == ["Dough fermentation", ""]


def test_a_line_with_no_tag_is_untagged() -> None:
    assert split_item("explains how a starter rises") == ("explains how a starter rises", "")
    assert split_item("x | item: n/a") == ("x", "")


# -- matching: the aim matcher, resolved to the thing the tag names ------------


def test_a_missing_thing_of_a_partly_held_part_is_missing() -> None:
    assert match_item("Shaping and scoring — scoring patterns", COVERAGE) == (
        ITEM_MISSING, "Shaping and scoring — scoring patterns", False,
    )
    # Copied with its label, as the item block writes it.
    assert match_item("MISSING: Common failures — gummy interior", COVERAGE)[0] == ITEM_MISSING
    # Or the thing alone.
    assert match_item("burnt base", COVERAGE)[:2] == (ITEM_MISSING, "Common failures — burnt base")


def test_a_partly_held_part_named_alone_is_partly() -> None:
    assert match_item("Shaping and scoring", COVERAGE) == (ITEM_PARTLY, "Shaping and scoring", False)


def test_a_held_part_or_a_held_thing_is_held() -> None:
    assert match_item("Oven temperature", COVERAGE)[0] == ITEM_HELD
    assert match_item("Dough fermentation — yeast", COVERAGE)[0] == ITEM_HELD
    assert match_item("Common failures — dense crumb", COVERAGE)[0] == ITEM_HELD


def test_a_tag_matching_no_line_is_untagged_and_counted_as_unmatched() -> None:
    """Rule 30: a reply that feeds the fetch order is checked before it is used."""
    assert match_item("knife sharpening", COVERAGE) == (ITEM_UNTAGGED, "", True)


def test_no_tag_and_no_coverage_are_untagged_and_not_unmatched() -> None:
    assert match_item("", COVERAGE) == (ITEM_UNTAGGED, "", False)
    assert match_item("Dough fermentation", []) == (ITEM_UNTAGGED, "", False)


def test_the_item_block_lists_each_missing_thing_on_its_own_line() -> None:
    block = item_block(COVERAGE)

    assert "- HELD: Dough fermentation — yeast; proofing time" in block
    assert "- MISSING: Shaping and scoring — scoring patterns" in block
    assert "- MISSING: Shaping and scoring — tension when shaping" in block
    assert "- HELD: Shaping and scoring — folding the dough" in block
    assert "nothing has been assessed" in item_block([])


def test_a_single_part_topic_tags_without_breaking() -> None:
    single = [CoverageItem("Knife sharpening", False, "", missing=("whetstone angle",))]

    assert match_item("Knife sharpening — whetstone angle", single)[0] == ITEM_MISSING
    assert match_item("Knife sharpening", single)[0] == ITEM_MISSING
    assert "- MISSING: Knife sharpening — whetstone angle" in item_block(single)


# -- stage 1 carries the tag; the verdict does not depend on it ----------------


def test_stage_one_tags_each_verdict_and_shows_the_round_its_coverage() -> None:
    sent: list = []
    judge = _judge(
        "1: YES - explains oven spring | ITEM: Oven temperature\n"
        "2: MAYBE - may show a scoring pattern | ITEM: Shaping and scoring — scoring patterns\n"
        "3: NO - a kitchen tour, off the brief | ITEM: NONE\n"
        "4: MAYBE - could cover a failure | ITEM: knife sharpening",
        sent,
    )

    verdicts = judge.judge_metadata(BRIEF, _candidates(4), batch_size=10, coverage=COVERAGE)

    assert [v.verdict for v in verdicts] == [VERDICT_YES, VERDICT_MAYBE, VERDICT_NO, VERDICT_MAYBE]
    assert [v.item_state for v in verdicts] == [ITEM_HELD, ITEM_MISSING, ITEM_UNTAGGED, ITEM_UNTAGGED]
    assert [v.unmatched for v in verdicts] == [False, False, False, True]
    assert verdicts[1].reason == "may show a scoring pattern", "the tag never enters the reason"
    prompt = sent[0]["messages"][-1]["content"]
    assert "- MISSING: Common failures — gummy interior" in prompt


def test_a_failed_batch_is_still_maybe_and_untagged() -> None:
    def transport(**_):
        raise ConnectionError("reset")

    judge = Judge(LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None))

    verdicts = judge.judge_metadata(BRIEF, _candidates(2), batch_size=10, coverage=COVERAGE)

    assert [(v.verdict, v.item_state) for v in verdicts] == [(VERDICT_MAYBE, ITEM_UNTAGGED)] * 2


# -- the fetch order and the held cap, through the discovery loop ---------------

import pytest  # noqa: E402

from marginalia.contracts import MetadataVerdict  # noqa: E402
from marginalia.flow.discover import fetch_order, held_cap  # noqa: E402
from test_discover import (  # noqa: E402,F401 - fixtures are found by name
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

HELD = [f"held{index:07d}" for index in range(4)]
GAP = [f"gaps{index:07d}" for index in range(3)]


class TaggingJudge(FakeJudge):
    """Stage 1 as scripted, each verdict carrying the item state it was given."""

    def __init__(self, tags, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tags = dict(tags)
        self.coverage_given: list[list] = []

    def judge_metadata(self, brief, candidates, *, batch_size, territory=None, coverage=()):
        self.coverage_given.append(list(coverage))
        out = []
        for verdict in super().judge_metadata(brief, candidates, batch_size=batch_size):
            state, unmatched = self._tags.get(verdict.video_id, (ITEM_UNTAGGED, False))
            out.append(
                MetadataVerdict(
                    verdict.video_id, verdict.verdict, verdict.reason,
                    serves="" if state == ITEM_UNTAGGED and not unmatched else "x",
                    item_state=state, unmatched=unmatched,
                )
            )
        return out


def _shape(searches, library, held_yes=HELD, gaps_maybe=GAP):
    """Held YES ranked first by search, gap MAYBEs after: the measured shape."""
    searches["*"] = [result(video_id) for video_id in [*held_yes, *gaps_maybe]]
    library.write_note("zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    tags = {video_id: (ITEM_HELD, False) for video_id in held_yes}
    tags.update({video_id: (ITEM_MISSING, False) for video_id in gaps_maybe})
    metadata = {video_id: (VERDICT_MAYBE, "may cover a gap") for video_id in gaps_maybe}
    return TaggingJudge(tags, metadata=metadata)


def test_gap_maybes_are_read_before_held_yes(config, ledger, library, client, searches) -> None:
    judge = _shape(searches, library)
    transcripts = FakeTranscripts()

    outcome = run(config, ledger, library, client, judge, provider=transcripts,
                  max_rounds=3, max_videos=9, planner=FakePlanner(baseline=COVERAGE))

    first = outcome.rounds[0]
    assert first.allowance == 3
    assert transcripts.fetched[:3] == GAP, "every gap candidate before any held YES"
    assert first.fetched_by_state == {ITEM_MISSING: 3}
    assert first.tagged == {ITEM_HELD: 4, ITEM_MISSING: 3}
    assert judge.coverage_given[0] == COVERAGE, "round one tags against the baseline"


def test_held_candidates_take_a_capped_share_and_wait(config, ledger, library, client, searches) -> None:
    judge = _shape(searches, library, gaps_maybe=GAP[:1])

    outcome = run(config, ledger, library, client, judge,
                  max_rounds=3, max_videos=9, planner=FakePlanner(baseline=COVERAGE))

    first = outcome.rounds[0]
    assert (first.held_cap, first.stored) == (1, 2), "one gap, one held, one slot kept"
    assert first.fetched_by_state == {ITEM_MISSING: 1, ITEM_HELD: 1}
    assert first.held_capped == 3
    assert [entry.video_id for entry in first.unread] == HELD[1:], "waiting, not dropped"
    assert first.released == 0


def test_the_safeguard_releases_held_slots_when_no_later_round_can_use_them(
    config, ledger, library, client, searches
) -> None:
    judge = _shape(searches, library, gaps_maybe=GAP[:1])

    config = replace(config, discover=replace(config.discover, release_held_last_round=True))
    outcome = run(config, ledger, library, client, judge,
                  max_rounds=1, max_videos=4, planner=FakePlanner(baseline=COVERAGE))

    only = outcome.rounds[0]
    assert (only.held_cap, only.stored, only.released) == (1, 4, 2)
    assert only.fetched_by_state == {ITEM_MISSING: 1, ITEM_HELD: 3}, "the gap first"


def test_with_nothing_missing_held_candidates_never_pass_the_cap(
    config, ledger, library, client, searches
) -> None:
    """Spending the rest on repeats is the cost this cap exists to cut; whether
    to stop is the planner's call (`coverage_satisfied`)."""
    judge = _shape(searches, library, gaps_maybe=[])
    held_only = [item for item in COVERAGE if item.satisfied]

    outcome = run(config, ledger, library, client, judge,
                  max_rounds=1, max_videos=4, planner=FakePlanner(baseline=held_only))

    only = outcome.rounds[0]
    assert (only.held_cap, only.stored, only.released, only.held_capped) == (1, 1, 0, 3)


def test_a_fresh_topic_reads_as_it_always_did(config, ledger, library, client, searches) -> None:
    """Nothing held, so nothing to tag against: no cap, YES before MAYBE."""
    ids = ["yesyesyes01", "maybemaybe1", "yesyesyes02"]
    searches["*"] = [result(video_id) for video_id in ids]
    judge = TaggingJudge({}, metadata={"maybemaybe1": (VERDICT_MAYBE, "unclear")})
    transcripts = FakeTranscripts()

    outcome = run(config, ledger, library, client, judge, provider=transcripts,
                  max_rounds=1, max_videos=3)

    only = outcome.rounds[0]
    assert transcripts.fetched == ["yesyesyes01", "yesyesyes02", "maybemaybe1"]
    assert (only.held_cap, only.held_capped, only.stored) == (0, 0, 3)
    assert only.fetched_by_state == {ITEM_UNTAGGED: 3}
    assert judge.coverage_given == [[]]


def test_a_single_part_topic_spends_its_budget(config, ledger, library, client, searches) -> None:
    single = [CoverageItem("Knife sharpening", False, "", missing=("whetstone angle",))]
    ids = ["onegap00001", "onegap00002", "untagged001"]
    searches["*"] = [result(video_id) for video_id in ids]
    library.write_note("zzzzzzzzzzz", "# An old note\n\nAbout something else.\n")
    judge = TaggingJudge({"onegap00001": (ITEM_MISSING, False), "onegap00002": (ITEM_MISSING, False)})

    outcome = run(config, ledger, library, client, judge,
                  max_rounds=1, max_videos=3, planner=FakePlanner(baseline=single))

    only = outcome.rounds[0]
    assert only.stored == 3
    assert only.fetched_by_state == {ITEM_MISSING: 2, ITEM_UNTAGGED: 1}


def test_an_unmatched_tag_is_counted_and_read_as_untagged(
    config, ledger, library, client, searches
) -> None:
    ids = ["heldheld001", "unmatched01", "partpart001"]
    searches["*"] = [result(video_id) for video_id in ids]
    library.write_note("zzzzzzzzzzz", "# An old note\n\nIt covers a held part.\n")
    judge = TaggingJudge({
        "heldheld001": (ITEM_HELD, False),
        "unmatched01": (ITEM_UNTAGGED, True),
        "partpart001": (ITEM_PARTLY, False),
    })
    transcripts = FakeTranscripts()

    outcome = run(config, ledger, library, client, judge, provider=transcripts,
                  max_rounds=1, max_videos=3, planner=FakePlanner(baseline=COVERAGE))

    only = outcome.rounds[0]
    assert only.tags_unmatched == 1
    assert only.tagged == {ITEM_HELD: 1, ITEM_UNTAGGED: 1, ITEM_PARTLY: 1}
    # The held one is past the cap of 1, and the last round no longer releases it.
    assert transcripts.fetched == ["partpart001", "unmatched01"]
    assert only.held_back_last == 1


@pytest.mark.parametrize("allowance, cap", [(0, 0), (1, 1), (3, 1), (9, 3), (25, 8)])
def test_the_held_cap_is_a_third_and_at_least_one(allowance, cap) -> None:
    assert held_cap(allowance) == cap


def test_fetch_order_is_by_item_then_yes_before_maybe() -> None:
    from marginalia.contracts import UnreadCandidate

    def entry(video_id, verdict, state):
        return UnreadCandidate(
            result(video_id), MetadataVerdict(video_id, verdict, "r"), "q", 1,
            origin="brief", item_state=state,
        )

    entries = [
        entry("heldyes0001", VERDICT_YES, ITEM_HELD),
        entry("untagyes001", VERDICT_YES, ITEM_UNTAGGED),
        entry("partmaybe01", VERDICT_MAYBE, ITEM_PARTLY),
        entry("missmaybe01", VERDICT_MAYBE, ITEM_MISSING),
        entry("missyes0001", VERDICT_YES, ITEM_MISSING),
    ]

    assert [e.video_id for e in fetch_order(entries, {})] == [
        "missyes0001", "missmaybe01", "partmaybe01", "untagyes001", "heldyes0001",
    ]


def test_a_waiting_candidate_is_retagged_against_the_round_it_waits_in() -> None:
    """A gap an earlier round filled is held now; its candidates wait behind
    the gaps still open."""
    from marginalia.contracts import UnreadCandidate
    from marginalia.flow.discover import _retagged

    verdict = MetadataVerdict(
        "waiting0001", VERDICT_MAYBE, "r", serves="Common failures — burnt base",
        item_state=ITEM_MISSING,
    )
    waiting = UnreadCandidate(result("waiting0001"), verdict, "q", 1, item_state=ITEM_MISSING)
    filled = [
        CoverageItem("Common failures", True, "", held=("dense crumb", "burnt base")),
    ]

    assert _retagged(waiting, filled).item_state == ITEM_HELD
    assert _retagged(waiting, [CoverageItem("Something reworded", True, "")]) == waiting


# -- visible and counted: the round line, --json, the planner, the ledger ------


def _capped_run(config, ledger, library, client, searches):
    judge = _shape(searches, library, gaps_maybe=GAP[:1])
    return run(config, ledger, library, client, judge,
               max_rounds=3, max_videos=9, planner=FakePlanner(baseline=COVERAGE))


def test_the_round_line_and_json_carry_the_gap_first_counters(
    config, ledger, library, client, searches
) -> None:
    from marginalia.cli import _discover_json, _discover_report

    outcome = _capped_run(config, ledger, library, client, searches)

    report = "\n".join(_discover_report(outcome))
    assert (
        "items: stage 1 tagged 1 missing, 0 partly, 4 held, 0 untagged (0 unmatched); "
        "fetched 1 missing, 0 partly, 1 held, 0 untagged; held cap 1, 3 left by it, "
        "0 released past it"
    ) in report
    first = _discover_json(outcome)["rounds"][0]
    assert first["tagged"] == {ITEM_MISSING: 1, ITEM_PARTLY: 0, ITEM_HELD: 4, ITEM_UNTAGGED: 0}
    assert first["fetched_by_state"][ITEM_HELD] == 1
    assert (first["held_cap"], first["held_capped"], first["released"]) == (1, 3, 0)
    assert first["unread_by_item"] == {ITEM_HELD: 3}


def test_a_fresh_topic_prints_no_items_line(config, ledger, library, client, searches) -> None:
    from marginalia.cli import _discover_report

    searches["*"] = [result("yesyesyes01")]
    outcome = run(config, ledger, library, client, TaggingJudge({}), max_rounds=1)

    assert "items:" not in "\n".join(_discover_report(outcome))


def test_the_planner_sees_the_queue_grouped_by_item() -> None:
    from marginalia.brain.planner import render_round
    from marginalia.contracts import RoundReport, UnreadCandidate

    def waiting(video_id, state, item):
        return UnreadCandidate(
            result(video_id), MetadataVerdict(video_id, VERDICT_MAYBE, "r"), "a query", 1,
            item=item, item_state=state,
        )

    report = RoundReport(number=1, queries=["a query"], held_cap=1, held_capped=2)
    report.unread = [
        waiting("missing0001", ITEM_MISSING, "Common failures — burnt base"),
        waiting("held0000001", ITEM_HELD, "Oven temperature"),
        waiting("held0000002", ITEM_HELD, "Oven temperature"),
    ]

    text = render_round(report)

    assert "- MISSING: Common failures — burnt base — 1 waiting (0 stage-1 YES)" in text
    assert "- HELD: Oven temperature — 2 waiting (0 stage-1 YES)" in text
    assert "  - a query — 3 waiting (0 stage-1 YES)" in text
    assert "at most 1 keepers on held or no item, 2 left waiting by that cap" in text


def test_the_decision_row_stores_the_tag_and_its_state(
    config, ledger, library, client, searches
) -> None:
    from marginalia.core.ledger import STAGE_METADATA

    _capped_run(config, ledger, library, client, searches)

    rows = {
        row.video_id: row.item
        for video_id in (HELD[0], GAP[0])
        for row in ledger.decisions_for(video_id)
        if row.stage == STAGE_METADATA
    }
    assert rows == {HELD[0]: "held: x", GAP[0]: "missing: x"}
