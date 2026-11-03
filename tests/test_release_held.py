"""The last round's release of held candidates is a setting, and it is off.

Measured on two real runs: the release took 9 and 14 held candidates in the
last round, and 13 and 8 of the runs' stored videos came from that round,
nearly all repeats of parts the library already held. Off, a run stores fewer
and says how many it held back. Subjects are invented.
"""

from __future__ import annotations

from dataclasses import replace

from marginalia.config import DEFAULT_RELEASE_HELD_LAST_ROUND
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakePlanner,
    client,
    config,
    ledger,
    library,
    run,
    searches,
)
from test_gap_first import COVERAGE, GAP, _shape


def test_the_release_is_off_by_default() -> None:
    assert DEFAULT_RELEASE_HELD_LAST_ROUND is False


def test_off_the_last_round_stores_fewer_and_counts_what_it_held_back(
    config, ledger, library, client, searches
) -> None:
    judge = _shape(searches, library, ledger, gaps_maybe=GAP[:1])

    outcome = run(config, ledger, library, client, judge,
                  max_rounds=1, max_videos=4, planner=FakePlanner(baseline=COVERAGE))

    only = outcome.rounds[0]
    assert (only.held_cap, only.stored, only.released) == (1, 2, 0)
    assert only.held_back_last == 3, "every held candidate past the cap, counted"


def test_on_the_last_round_releases_them_as_before(config, ledger, library, client, searches) -> None:
    judge = _shape(searches, library, ledger, gaps_maybe=GAP[:1])
    config = replace(config, discover=replace(config.discover, release_held_last_round=True))

    outcome = run(config, ledger, library, client, judge,
                  max_rounds=1, max_videos=4, planner=FakePlanner(baseline=COVERAGE))

    only = outcome.rounds[0]
    assert (only.stored, only.released, only.held_back_last) == (4, 2, 0)
