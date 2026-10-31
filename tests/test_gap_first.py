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
