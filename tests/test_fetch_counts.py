"""A fetch is counted when its transcript arrives, so the rounds sum to the run.

Counted before the fetch, a video with no captions was a fetch on the items
line and a no-caption route on the line below it: two real runs' rounds summed
to 54 and 53 against 52 fetched each.
"""

from __future__ import annotations

from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeMapper,
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

def test_b4_the_rounds_fetched_sum_to_the_run_and_a_captionless_video_is_not_one(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("captions001"), result("nocaption01"), result("captions002")]
    transcripts = FakeTranscripts(missing={"nocaption01"})

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  provider=transcripts, max_rounds=2, max_videos=6)

    acquired = [video for video in transcripts.fetched if video != "nocaption01"]
    assert outcome.rounds[0].no_captions == 1
    assert sum(report.fetched for report in outcome.rounds) == outcome.fetched == len(acquired)
