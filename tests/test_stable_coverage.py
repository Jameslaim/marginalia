"""The same state gives the same coverage, and a fresh one resolves flips toward missing.

A baseline is stored under the digest of what it reads and reused, with no
call, while the approved brief, the notes, the prompt and the settings are
unchanged; anything changing makes it fresh. A fresh baseline makes
`baseline_calls` calls, and a disagreement between them resolves to the
less-held state and to missing, counted. Subjects are invented and general.
"""

from __future__ import annotations

import json
from dataclasses import replace

from marginalia.brain.coverage import (
    FLIP_HELD,
    FLIP_STATE,
    FLIP_UNLISTED,
    resolve_coverage,
)
from marginalia.brain.judge import brief_sha256
from marginalia.cli import _baseline_lines, _discover_json
from marginalia.contracts import BASELINE_FRESH, BASELINE_REUSED, CoverageItem, LibraryCoverage
from marginalia.flow.export import render_report
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
from test_search_plan import BRIEF_TEXT, TOPIC

PART1 = "Hot composting"
PART2 = "Worm bins"
ALSO = "costs of the equipment"


def cov(*items: CoverageItem) -> LibraryCoverage:
    return LibraryCoverage(items=list(items), notes_read=1)


def item(part, mark, held=(), missing=()):
    return CoverageItem(part, mark == "yes", "", mark == "partly", tuple(held), tuple(missing))


class Scripted(FakePlanner):
    """A planner whose baseline calls answer in turn, the last repeated."""

    def __init__(self, *answers: LibraryCoverage, version: str = "1") -> None:
        super().__init__()
        self.answers = list(answers)
        self.version = version

    def prompt(self, name):
        return type("P", (), {"version": self.version})()

    def assess_library(self, brief, notes, *, notes_read=0, max_tokens=None):
        self.assessed += 1
        answer = self.answers[min(self.assessed, len(self.answers)) - 1]
        return replace(answer, notes_read=notes_read)


HELD = cov(item(PART1, "yes", held=["carbon-to-nitrogen ratio", "turning schedule"]),
           item(PART2, "partly", held=["bedding"], missing=["harvesting castings"]))


def _approve(ledger, text=BRIEF_TEXT):
    return ledger.approve_brief(TOPIC, text, brief_sha256(text))


def _go(config, ledger, library, client, planner, **kwargs):
    return run(config, ledger, library, client, FakeJudge(), topic=TOPIC, max_rounds=1,
               planner=planner, **kwargs)


# -- resolving flips -----------------------------------------------------------------


def test_one_call_comes_back_as_it_was() -> None:
    resolved, flips = resolve_coverage([HELD], BRIEF_TEXT)
    assert resolved is HELD and flips == []


def test_an_item_takes_its_least_held_state_and_the_flip_is_counted() -> None:
    a = cov(item(PART1, "yes", held=["turning schedule"]))
    b = cov(item(PART1, "partly", held=["turning schedule"], missing=["pile temperature"]))
    resolved, flips = resolve_coverage([a, b], BRIEF_TEXT)
    (only,) = resolved.items
    assert only.mark == "partly" and only.missing == ("pile temperature",)
    assert [(f["kind"], f["states"], f["resolved"]) for f in flips if f["kind"] == FLIP_STATE] == [
        (FLIP_STATE, ["yes", "partly"], "partly")
    ]


def test_a_thing_held_in_one_call_and_missing_in_another_is_missing() -> None:
    # Two wordings of one listed thing, with evidence after one of them.
    a = cov(item(PART2, "partly", held=["harvesting castings — shown in a bin tour"], missing=["bedding"]))
    b = cov(item(PART2, "partly", held=["bedding"], missing=["how castings are harvested"]))
    resolved, flips = resolve_coverage([a, b], BRIEF_TEXT)
    (only,) = resolved.items
    assert len(only.missing) == 2 and only.held == ()
    held_flips = [f for f in flips if f["kind"] == FLIP_HELD]
    assert {f["thing"] for f in held_flips} == {"bedding", "harvesting castings"}
    assert all("tour" not in json.dumps(f) for f in flips), "a held line's evidence is never copied"


def test_a_thing_one_call_did_not_name_is_missing_when_another_lists_it() -> None:
    a = cov(item(ALSO, "partly", missing=["prices of thermometers"]))
    b = cov(item(ALSO, "partly", missing=["prices of thermometers", "prices of bins"]))
    resolved, flips = resolve_coverage([a, b], BRIEF_TEXT)
    assert len(resolved.items[0].missing) == 2
    assert [(f["kind"], f["states"]) for f in flips] == [(FLIP_UNLISTED, ["unlisted", "missing"])]


def test_a_part_one_call_found_nothing_of_holds_nothing() -> None:
    a = cov(item(PART2, "partly", held=["bedding"], missing=["feeding"]))
    b = cov(item(PART2, "no", missing=["feeding"]))
    resolved, _ = resolve_coverage([a, b], BRIEF_TEXT)
    (only,) = resolved.items
    assert only.mark == "no" and only.held == () and set(only.missing) == {"feeding", "bedding"}


def test_agreeing_calls_resolve_nothing_and_keep_the_order() -> None:
    resolved, flips = resolve_coverage([HELD, HELD], BRIEF_TEXT)
    assert flips == [] and [i.part for i in resolved.items] == [PART1, PART2]
    assert [i.mark for i in resolved.items] == ["yes", "partly"]


# -- reuse on an unchanged state ------------------------------------------------------


def test_an_unchanged_state_reuses_the_baseline_with_no_call(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    planner = Scripted(HELD)

    first = _go(config, ledger, library, client, planner)
    assert first.baseline.reuse == BASELINE_FRESH and first.baseline.calls == 2
    assert planner.assessed == 2
    second = _go(config, ledger, library, client, planner)

    assert planner.assessed == 2, "no call on the second run"
    assert second.baseline.reuse == BASELINE_REUSED
    assert second.baseline.reused_from == first.run_id and second.baseline.calls == 0
    assert [i.as_dict() for i in second.baseline.items] == [i.as_dict() for i in first.baseline.items]
    assert _baseline_lines(second)[1].strip() == (
        f"reused (from run {first.run_id}): unchanged state, 0 calls"
    )
    body = _discover_json(second)["baseline"]
    assert body["reuse"] == "reused" and body["reused_from"] == first.run_id and body["calls"] == 0
    stored = json.loads(ledger.get_run(second.run_id).library_baseline)
    assert stored["reuse"] == "reused", "the run row says it was reused"


def test_a_fresh_baseline_says_so_with_its_calls_and_flips(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    other = cov(item(PART1, "partly", held=["turning schedule"], missing=["pile temperature"]),
                HELD.items[1])
    outcome = _go(config, ledger, library, client, Scripted(HELD, other))

    assert outcome.baseline.flips_resolved >= 1
    assert outcome.baseline.items[0].mark == "partly", "toward missing"
    line = _baseline_lines(outcome)[1].strip()
    assert line.startswith("fresh (no stored baseline for this topic): 2 call(s), ")
    assert "flip(s) resolved toward missing" in line
    report = render_report(config, ledger.get_run(outcome.run_id), [], [])
    assert "Fresh (no stored baseline for this topic): 2 call(s)" in report
    assert "| item state | Hot composting |" in report


def _two_runs(config, ledger, library, client, change, planner=None):
    """A run, `change`, then a second run: the second's baseline."""
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    planner = planner or Scripted(HELD)
    _go(config, ledger, library, client, planner)
    changed = change(planner)
    config = changed if hasattr(changed, "discover") else config
    return _go(config, ledger, library, client, planner).baseline


def test_a_note_added_makes_it_fresh(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    after = _two_runs(config, ledger, library, client,
                      lambda _: kept_note(library, ledger, TOPIC, "ccccccccccc", "# Another\n\nWorm bins.\n"))
    assert after.reuse == BASELINE_FRESH and after.calls == 2
    assert after.fresh_reason == "notes changed (1 added, 0 regenerated, 0 removed)"


def test_a_note_regenerated_makes_it_fresh(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    after = _two_runs(config, ledger, library, client,
                      lambda _: kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nRewritten.\n"))
    assert after.fresh_reason == "notes changed (0 added, 1 regenerated, 0 removed)"


def test_a_note_removed_makes_it_fresh(config, ledger, library, client, searches) -> None:
    searches["*"] = []

    def remove(_):
        kept_note(library, ledger, TOPIC, "ccccccccccc", "# Another\n\nWorm bins.\n")

    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    remove(None)
    planner = Scripted(HELD)
    _go(config, ledger, library, client, planner)
    library.note_path("ccccccccccc").unlink()
    after = _go(config, ledger, library, client, planner).baseline
    assert after.fresh_reason == "notes changed (0 added, 0 regenerated, 1 removed)"


def test_a_new_brief_version_makes_it_fresh(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    after = _two_runs(config, ledger, library, client,
                      lambda _: _approve(ledger, BRIEF_TEXT.replace("at home", "in a small yard")))
    assert after.reuse == BASELINE_FRESH and after.fresh_reason == "brief v1 -> v2"


def test_a_new_prompt_version_makes_it_fresh(config, ledger, library, client, searches) -> None:
    searches["*"] = []

    def bump(planner):
        planner.version = "2"

    after = _two_runs(config, ledger, library, client, bump)
    assert after.fresh_reason == "prompt v1 -> v2"


def test_a_setting_that_changes_the_call_makes_it_fresh(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = []
    after = _two_runs(
        config, ledger, library, client,
        lambda _: replace(config, discover=replace(config.discover, baseline_calls=1)),
    )
    assert after.fresh_reason == "settings changed" and after.calls == 1


def test_a_failed_call_is_not_stored_and_is_made_again(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = []
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    broken = LibraryCoverage(parsed=False, problem="reply hit the token ceiling before finishing")
    planner = Scripted(HELD, broken)
    first = _go(config, ledger, library, client, planner)
    assert "1 of 2 call(s) unusable and left out" in first.baseline.fresh_reason
    planner.answers = [HELD]
    second = _go(config, ledger, library, client, planner)
    assert second.baseline.reuse == BASELINE_FRESH, "nothing was stored to reuse"


def test_without_an_approved_brief_a_baseline_is_always_fresh(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = []
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    planner = Scripted(HELD)
    _go(config, ledger, library, client, planner)
    after = _go(config, ledger, library, client, planner).baseline
    assert planner.assessed == 4
    assert after.fresh_reason == "no approved brief, so a baseline is never reused"


def test_a_fresh_topic_makes_no_call_and_says_so(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    _approve(ledger)
    planner = Scripted(HELD)
    outcome = _go(config, ledger, library, client, planner)
    assert planner.assessed == 0 and outcome.baseline.reuse == ""
    assert _baseline_lines(outcome) == [
        "baseline: none — the library holds no note this topic's stage 2 kept, so this is planned "
        "as a fresh topic, with no call",
        "          notes left out (not this topic's stage-2 YES): 0 NO, 0 MAYBE, 0 not judged for "
        "this topic",
    ]


def test_one_call_is_the_old_behaviour(config, ledger, library, client, searches) -> None:
    searches["*"] = []
    _approve(ledger)
    kept_note(library, ledger, TOPIC, "bbbbbbbbbbb", "# A note\n\nIt shows a hot pile.\n")
    planner = Scripted(HELD)
    one = replace(config, discover=replace(config.discover, baseline_calls=1))
    outcome = _go(one, ledger, library, client, planner)
    assert planner.assessed == 1 and outcome.baseline.calls == 1 and outcome.baseline.flips == ()

