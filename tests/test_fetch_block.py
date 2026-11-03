"""A transcript-fetch block stops discovery as `throttled`.

Measured on one run: YouTube answered a fetch with 429 and then a bot check,
and every one of the next 170 fetches failed, while discovery ran two more
rounds — 28 searches and their stage-1 calls — for one transcript, and stopped
as `no_new_videos`, which hid the block. The rule is `refetch`'s: three
transient failures in a row stop the run. The replay fixture is that run's
fetch outcomes in order, as failure classes only. Subjects are invented.
"""

from __future__ import annotations

import json
from pathlib import Path

from marginalia.cli import _DISCOVER_FAILED, _discover_json, _discover_report
from marginalia.contracts import STOP_THROTTLED, STATUS_FAILED
from marginalia.core.ledger import FAIL_TRANSIENT
from marginalia.flow.acquire import FAILURE_STREAK
from marginalia.flow.discover import STOP_REASONS
from marginalia.flow.export import render_report
from marginalia.providers import TransientError
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeTranscripts,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)

SEQUENCE = json.loads(
    (Path(__file__).parent / "fixtures" / "fetch_block_sequence.json").read_text(encoding="utf-8")
)["outcomes"]


def _ids(n):
    return [f"vid{index:08d}" for index in range(n)]


def _scripted(outcomes):
    """A provider that answers each video with the outcome at its position."""
    ids = _ids(len(outcomes))
    failures = {video: TransientError(kind) for video, kind in zip(ids, outcomes) if kind != "acquired"}
    return ids, FakeTranscripts(failures=failures)


def test_the_replayed_block_stops_the_run_at_its_third_failure(
    config, ledger, library, client, searches
) -> None:
    ids, transcripts = _scripted(SEQUENCE[:40])
    searches["*"] = [result(video) for video in ids]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"], ["q3"]]),
                  provider=transcripts, max_rounds=3, max_videos=60)

    assert FAILURE_STREAK == 3 and STOP_THROTTLED in STOP_REASONS
    assert outcome.stopped_because == STOP_THROTTLED
    first_failure = SEQUENCE.index("bot check")
    assert transcripts.fetched == ids[:first_failure + 3], "nothing fetched past the block"
    assert len(outcome.rounds) == 1 and searches["calls"] == ["q1"], "no further search"
    assert outcome.fetch_block["first"] == ids[first_failure]
    assert outcome.fetch_block["classes"] == {"bot check": 3}
    assert [entry.video_id for entry in outcome.rounds[0].unread][:1] == [ids[first_failure + 3]]
    # Every failed video is retryable, which is what `marg refetch` reads first.
    row = ledger.get(ids[first_failure])
    assert (row.status, row.fail_kind) == (STATUS_FAILED, FAIL_TRANSIENT)


def test_the_block_is_on_the_line_in_json_and_in_the_export(
    config, ledger, library, client, searches
) -> None:
    ids, transcripts = _scripted(["acquired", "bot check", "dns failure", "bot check"])
    searches["*"] = [result(video) for video in ids]

    outcome = run(config, ledger, library, client, FakeJudge(), provider=transcripts, max_rounds=2)

    text = "\n".join(_discover_report(outcome))
    assert f"stopped: throttled — transcript fetches blocked from {ids[1]}" in text
    assert "(2 bot check, 1 dns failure)" in text
    assert _discover_json(outcome)["fetch_block"]["failures"] == 3
    assert STOP_THROTTLED in _DISCOVER_FAILED
    row = ledger.get_run(outcome.run_id)
    assert row.stop_detail == outcome.stop_detail
    assert f"| stopped because | throttled — transcript fetches blocked from {ids[1]}" in render_report(
        config, row, [], [])


def test_an_answered_fetch_breaks_the_streak(config, ledger, library, client, searches) -> None:
    ids, transcripts = _scripted(["bot check", "bot check", "acquired", "bot check", "bot check", "acquired"])
    searches["*"] = [result(video) for video in ids]

    outcome = run(config, ledger, library, client, FakeJudge(), provider=transcripts, max_rounds=1)

    assert outcome.stopped_because != STOP_THROTTLED
    assert transcripts.fetched == ids
